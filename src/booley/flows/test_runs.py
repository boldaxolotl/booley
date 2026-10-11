"""Test Run identity: names, selectors, Test Variants, seeds (ADR 0072).

A **Test Run** is one Test Variant simulated with one seed, written
``test[+variant][@seed]``. This module owns the grammar of those names, the
rules a Test or Variant name must obey, the shape of one Test's Variant table
in ``tests.toml``, the fingerprint of a Variant's declared inputs, random seed
draws, and the command that reruns one Test Run.

It is pure domain policy: it reads no files, starts no process, and imports
nothing from the CLI, the Sandbox or the Simulation Flow. The rules for one
Test or Variant name live in ``booley.config.test_name_rules`` because the
``tests.toml`` loader applies them and configuration must not import Flows;
they are re-exported here so callers have one place to look.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import shlex
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import cast

from booley.config.test_name_rules import (
    ENV_NAME_PATTERN,
    RESERVED_NAME,
    SEED_SEPARATOR,
    VARIANT_SEPARATOR,
    TestRunNameError,
    validate_test_run_name_part,
)
from booley.core.boundary import as_dict, is_str_list

__all__ = [
    "DEFAULT_SEED",
    "ENV_NAME_PATTERN",
    "MAX_MULTI_SEED_COUNT",
    "MAX_SEED",
    "RESERVED_NAME",
    "UNNAMED_TEST_DISPLAY",
    "Count",
    "Pinned",
    "SeedForm",
    "TestRun",
    "TestRunLimitError",
    "TestRunNameError",
    "TestSelector",
    "TestVariant",
    "TestVariantError",
    "TestVariantTable",
    "UnknownVariantError",
    "Unpinned",
    "UnseededTargetError",
    "build_test_run",
    "declared_inputs_fingerprint",
    "draw_seeds",
    "parse_test_selector",
    "parse_test_variants",
    "rerun_command",
    "validate_test_run_name_part",
]

#: The seed a Test Run uses when none is named. Never ``0``: Verilator reads
#: ``+verilator+seed+0`` as "pick a seed at startup".
DEFAULT_SEED = 1
#: Largest seed Booley accepts or draws (a positive signed 32-bit value).
MAX_SEED = 2**31 - 1
#: Largest N of a multi-seed selector ``test@xN`` and of one seed draw.
MAX_MULTI_SEED_COUNT = 1000

#: How the invocation of a Target without registered Tests is displayed.
UNNAMED_TEST_DISPLAY = "<default>"

_COUNT_PREFIX = "x"
_DECIMAL = re.compile(r"[0-9]+")
# ``MAX_SEED`` has ten digits; anything longer is out of range without ``int``.
_MAX_NUMBER_DIGITS = 10
# A draw collides with an earlier or excluded seed about once in two million
# tries, so this bound is reached only by a broken random source.
_DRAW_ATTEMPTS_PER_SEED = 8
_DRAW_ATTEMPTS_FLOOR = 64

_VARIANT_TABLE_KEYS = ("args", "env")
_ARGUMENT_PREFIXES = ("+", "-")

# Every ``Test…`` class below sets ``__test__ = False``: "Test" is the domain
# noun here, and pytest must not try to collect these when a test imports them.


class TestVariantError(ValueError):
    """One Test's Variant table in ``tests.toml`` is malformed."""

    __test__ = False


class UnknownVariantError(ValueError):
    """A name refers to a Variant that the Test does not declare."""


class UnseededTargetError(ValueError):
    """A seed was requested for a Target that cannot apply one."""


class TestRunLimitError(ValueError):
    """A request asks for more Test Runs or seeds than Booley allows."""

    __test__ = False


@dataclass(frozen=True)
class Unpinned:
    """A selector that names no seed: the Test Run uses the Default Seed."""


@dataclass(frozen=True)
class Pinned:
    """A selector written ``…@seed``: exactly that seed."""

    seed: int


@dataclass(frozen=True)
class Count:
    """A selector written ``…@xN``: N Test Runs, each with a different seed."""

    count: int


SeedForm = Unpinned | Pinned | Count


@dataclass(frozen=True)
class TestSelector:
    """What a user wrote to name Test Runs: Test, optional Variant, seed form."""

    __test__ = False

    test: str
    variant: str | None
    seed_form: SeedForm


@dataclass(frozen=True)
class TestVariant:
    """One declared Variant: its run-time arguments and environment."""

    __test__ = False

    name: str
    args: tuple[str, ...] = ()
    #: ``(name, value)`` pairs sorted by name.
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TestVariantTable:
    """The Variants one Test declares, in declaration order, and its default."""

    __test__ = False

    test: str
    default: str
    variants: tuple[TestVariant, ...]


@dataclass(frozen=True)
class TestRun:
    """One Test Variant simulated with one seed.

    ``test`` is ``None`` for the unnamed invocation of a Target without
    registered Tests; such a run has no Variant. ``seed`` is ``None`` on a
    Target that cannot apply seeds. ``env`` holds ``(name, value)`` pairs
    sorted by name. Build instances with :func:`build_test_run`, which
    validates the fields and computes ``fingerprint``.
    """

    __test__ = False

    test: str | None
    variant: str | None
    seed: int | None
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    fingerprint: str

    @property
    def name(self) -> str:
        """The resolved name ``test[+variant][@seed]``."""
        text = UNNAMED_TEST_DISPLAY if self.test is None else self.test
        if self.variant is not None:
            text += f"{VARIANT_SEPARATOR}{self.variant}"
        if self.seed is not None:
            text += f"{SEED_SEPARATOR}{self.seed}"
        return text


def _parse_bounded_number(text: str, *, what: str, selector: str) -> int:
    """Parse a canonical positive decimal: digits only, no sign, no leading zero."""
    if not _DECIMAL.fullmatch(text):
        raise TestRunNameError(
            f"Test Run selector {selector!r}: {what} {text!r} must be a decimal "
            "number without a sign"
        )
    if len(text) > 1 and text.startswith("0"):
        raise TestRunNameError(
            f"Test Run selector {selector!r}: {what} {text!r} must not have leading zeros"
        )
    # Longer than any accepted value: report the range without a huge ``int``.
    return MAX_SEED + 1 if len(text) > _MAX_NUMBER_DIGITS else int(text)


def _parse_seed_form(text: str, selector: str) -> SeedForm:
    """Parse the part after ``@``: a seed, or ``xN`` for N seeds."""
    if not text:
        raise TestRunNameError(
            f"Test Run selector {selector!r}: '@' must be followed by a seed or xN"
        )
    if not text.startswith(_COUNT_PREFIX):
        seed = _parse_bounded_number(text, what="seed", selector=selector)
        if not 1 <= seed <= MAX_SEED:
            raise TestRunNameError(
                f"Test Run selector {selector!r}: seed {text} is out of range 1..{MAX_SEED}"
            )
        return Pinned(seed)
    digits = text[len(_COUNT_PREFIX) :]
    count = _parse_bounded_number(digits, what="seed count", selector=selector)
    if count < 1:
        raise TestRunNameError(f"Test Run selector {selector!r}: seed count must be at least 1")
    if count > MAX_MULTI_SEED_COUNT:
        raise TestRunLimitError(
            f"Test Run selector {selector!r}: seed count {digits} exceeds the "
            f"limit of {MAX_MULTI_SEED_COUNT}"
        )
    return Count(count)


def parse_test_selector(text: str) -> TestSelector:
    """Parse ``test``, ``test+variant``, and either form with ``@seed`` or ``@xN``.

    Raises :class:`TestRunNameError` for a malformed selector and
    :class:`TestRunLimitError` for an N above :data:`MAX_MULTI_SEED_COUNT`.
    """
    head, *seed_parts = text.split(SEED_SEPARATOR)
    if len(seed_parts) > 1:
        raise TestRunNameError(
            f"Test Run selector {text!r} has more than one '@'; write one seed "
            "(test@seed) or one seed count (test@xN)"
        )
    test, *variant_parts = head.split(VARIANT_SEPARATOR)
    if len(variant_parts) > 1:
        raise TestRunNameError(
            f"Test Run selector {text!r} has more than one '+'; a Test Run names "
            "one Variant (test+variant)"
        )
    try:
        validate_test_run_name_part(test, kind="Test")
        variant = (
            validate_test_run_name_part(variant_parts[0], kind="Variant")
            if variant_parts
            else None
        )
    except TestRunNameError as exc:
        raise TestRunNameError(f"Test Run selector {text!r}: {exc}") from exc
    seed_form = _parse_seed_form(seed_parts[0], text) if seed_parts else Unpinned()
    return TestSelector(test=test, variant=variant, seed_form=seed_form)


def declared_inputs_fingerprint(args: tuple[str, ...], env: Mapping[str, str]) -> str:
    """Canonical SHA-256 of a Variant's declared arguments and environment.

    Argument order is significant (it is the simulator's argument order);
    environment key order is not. The encoding is the canonical JSON the
    Simulation Campaign uses (``canonical_sha256``), reproduced here because
    importing that module loads the whole Simulation Flow and this one stays
    free of it; a test pins that both produce the same digest.
    """
    document = {"args": list(args), "env": dict(env)}
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_test_run(
    test: str | None,
    *,
    variant: str | None = None,
    seed: int | None = None,
    args: tuple[str, ...] = (),
    env: Mapping[str, str] | None = None,
) -> TestRun:
    """Build a :class:`TestRun` and fingerprint its declared inputs.

    Raises :class:`TestRunNameError` for an invalid name, a Variant on the
    unnamed invocation, or a seed outside ``1..MAX_SEED``.
    """
    if test is None and variant is not None:
        raise TestRunNameError(f"the unnamed invocation has no Variants, got Variant {variant!r}")
    if test is not None:
        validate_test_run_name_part(test, kind="Test")
    if variant is not None:
        validate_test_run_name_part(variant, kind="Variant")
    if seed is not None and (isinstance(seed, bool) or not 1 <= seed <= MAX_SEED):
        raise TestRunNameError(f"seed {seed!r} is out of range 1..{MAX_SEED}")
    environment = dict(env or {})
    return TestRun(
        test=test,
        variant=variant,
        seed=seed,
        args=tuple(args),
        env=tuple(sorted(environment.items())),
        fingerprint=declared_inputs_fingerprint(tuple(args), environment),
    )


def _parse_variant_args(raw: object, where: str) -> tuple[str, ...]:
    """Validate a Variant's argument list: one option token per entry."""
    if not is_str_list(raw):
        raise TestVariantError(f"{where} arguments must be a list of strings")
    arguments = cast(list[str], raw)
    for argument in arguments:
        if not argument.startswith(_ARGUMENT_PREFIXES):
            raise TestVariantError(f"{where} argument {argument!r} must start with '+' or '-'")
        if "\0" in argument or any(character.isspace() for character in argument):
            raise TestVariantError(
                f"{where} argument {argument!r} must be one token without "
                "whitespace or NUL; declare each argument as its own list entry"
            )
    return tuple(arguments)


def _parse_variant_env(raw: object, where: str) -> tuple[tuple[str, str], ...]:
    """Validate a Variant's environment table of ``NAME = "value"`` pairs."""
    table = as_dict(raw)
    if table is None:
        raise TestVariantError(f'{where}.env must be a table of NAME = "value" pairs')
    env: dict[str, str] = {}
    for name, value in table.items():
        if not isinstance(name, str) or not ENV_NAME_PATTERN.match(name):
            raise TestVariantError(
                f"{where}.env key {name!r} is not a valid environment variable "
                "name ([A-Za-z_][A-Za-z0-9_]*)"
            )
        if not isinstance(value, str):
            raise TestVariantError(
                f"{where}.env.{name} must be a string, got {type(value).__name__}; quote the value"
            )
        env[name] = value
    return tuple(sorted(env.items()))


def _parse_variant(name: str, raw: object, where: str) -> TestVariant:
    """Parse one Variant value: an argument list, or an ``args``/``env`` table."""
    table = as_dict(raw)
    if table is not None:
        unknown = sorted(str(key) for key in table if key not in _VARIANT_TABLE_KEYS)
        if unknown:
            raise TestVariantError(
                f"{where} has unknown key {unknown[0]!r}; a Variant table holds only args and env"
            )
        return TestVariant(
            name=name,
            args=_parse_variant_args(table.get("args", []), where),
            env=_parse_variant_env(table.get("env", {}), where),
        )
    if isinstance(raw, list):
        return TestVariant(name=name, args=_parse_variant_args(cast(list[object], raw), where))
    raise TestVariantError(
        f"{where} must be a list of arguments or a table with args and env, "
        f"got {type(raw).__name__}"
    )


def parse_test_variants(test: str, raw: object, *, where: str | None = None) -> TestVariantTable:
    """Parse and validate the Variant table of one Test.

    *raw* is the TOML table: ``default = "<variant>"`` plus one entry per
    Variant. *where* prefixes every message with the table's location (the
    loader passes ``tests.toml [<target>].variants.<test>``).

    Raises :class:`TestVariantError` for a malformed table,
    :class:`TestRunNameError` for an invalid Variant name, and
    :class:`UnknownVariantError` when ``default`` names no declared Variant.
    """
    where = where or f"variants.{test}"
    table = as_dict(raw)
    if table is None:
        raise TestVariantError(f"{where} must be a table of Variants")
    if RESERVED_NAME not in table:
        raise TestVariantError(f'{where} must name its default Variant: add default = "<variant>"')
    default = table[RESERVED_NAME]
    if not isinstance(default, str):
        raise TestVariantError(f"{where}.default must be the name of a Variant (a string)")
    variants: list[TestVariant] = []
    for key, value in table.items():
        if key == RESERVED_NAME:
            continue
        try:
            name = validate_test_run_name_part(key, kind="Variant")
        except TestRunNameError as exc:
            raise TestRunNameError(f"{where}: {exc}") from exc
        variants.append(_parse_variant(name, value, f"{where}.{name}"))
    declared = [variant.name for variant in variants]
    if default not in declared:
        raise UnknownVariantError(
            f"{where}.default names {default!r}, which is not a declared Variant "
            f"(declared: {', '.join(declared) or 'none'})"
        )
    return TestVariantTable(test=test, default=default, variants=tuple(variants))


def draw_seeds(
    count: int,
    *,
    exclude: Collection[int] = (),
    rng: random.Random | None = None,
) -> tuple[int, ...]:
    """Draw *count* distinct seeds in ``1..MAX_SEED``, none of them in *exclude*.

    The default source is the operating system's (``secrets.SystemRandom``),
    so an agent cannot predict a draw; tests inject a seeded ``random.Random``.
    Raises :class:`TestRunLimitError` when *count* is negative or above
    :data:`MAX_MULTI_SEED_COUNT`, or when the source keeps repeating itself.
    """
    if count < 0 or count > MAX_MULTI_SEED_COUNT:
        raise TestRunLimitError(
            f"cannot draw {count} seeds: the count must be 0..{MAX_MULTI_SEED_COUNT}"
        )
    source = rng if rng is not None else secrets.SystemRandom()
    taken = set(exclude)
    drawn: list[int] = []
    attempts = count * _DRAW_ATTEMPTS_PER_SEED + _DRAW_ATTEMPTS_FLOOR
    for _ in range(attempts):
        if len(drawn) == count:
            break
        seed = source.randint(1, MAX_SEED)
        if seed not in taken:
            taken.add(seed)
            drawn.append(seed)
    if len(drawn) != count:
        raise TestRunLimitError(
            f"could not draw {count} distinct seeds in {attempts} attempts; "
            "the random source keeps returning seeds that are already taken"
        )
    return tuple(drawn)


def rerun_command(target: str, run: TestRun) -> str:
    """The shell command that runs *run* again on *target* with the same seed.

    A named run is selected by its resolved name. The unnamed invocation
    cannot be named by ``--test``, so its seed goes through ``--seed``; an
    unseeded run carries neither ``@seed`` nor ``--seed``.
    """
    argv = ["booley", "flow", "sim", "--target", target]
    if run.test is not None:
        argv += ["--test", run.name]
    elif run.seed is not None:
        argv += ["--seed", str(run.seed)]
    return shlex.join(argv)
