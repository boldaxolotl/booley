"""Test Run identity: names, selectors, Test Variants, seeds (ADR 0072).

A **Test Run** is one Test Variant simulated with one seed, written
``test[+variant][@seed]``. This module owns the grammar of those names, the
shape of one Test's Variant table in ``tests.toml``, the fingerprint of a Test
Variant's declared inputs, random seed draws, and the command that reruns one
Test Run.

It is pure domain policy: it reads no files, starts no process, and imports
nothing from the CLI, the Sandbox or the Simulation Flow. The rule for one
Test or Test Variant name, its error type (``TestRunNameError``) and the
environment-table rule are defined in ``booley.config.tests_toml_rules``,
because the ``tests.toml`` loader applies them and configuration must not
import Flows; import them from there.
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
from typing import Literal, cast

from booley.config.tests_toml_rules import (
    DEFAULT_SEED,
    MAX_SEED,
    SEED_SEPARATOR,
    VARIANT_SEPARATOR,
    EnvTableError,
    SeedConfigurationError,
    TestRunNameError,
    TestVariant,
    TestVariantError,
    TestVariantTable,
    UnknownVariantError,
    parse_test_variants,
    validate_declared_env_names,
    validate_env_table,
    validate_seed,
    validate_test_or_variant_name,
)
from booley.core.boundary import BoundaryError, as_dict, is_str_list, require_str_value

__all__ = [
    "DEFAULT_SEED",
    "MAX_MULTI_SEED_COUNT",
    "MAX_SEED",
    "SEED_REQUEST_RANDOM",
    "SEED_WITH_SEEDED_SELECTOR",
    "UNNAMED_TEST_DISPLAY",
    "PinnedSeed",
    "SeedCount",
    "SeedForm",
    "SeedRequest",
    "TestRun",
    "TestRunLimitError",
    "TestSelector",
    "TestVariant",
    "TestVariantError",
    "TestVariantTable",
    "UnknownVariantError",
    "UnpinnedSeed",
    "UnseededTargetError",
    "build_test_run",
    "declared_inputs_fingerprint",
    "discovered_test_run",
    "draw_seeds",
    "parse_seed_request",
    "parse_test_selector",
    "parse_test_variants",
    "rerun_command",
    "test_run_document",
    "test_run_from_document",
]

#: Largest N of a multi-seed selector ``test@xN`` and of one seed draw.
MAX_MULTI_SEED_COUNT = 1000

#: The ``--seed`` value that asks Booley to draw a seed for each Test Run.
#: (Not cocotb's ``RANDOM_SEED`` variable; see ``backends.cocotb``.)
SEED_REQUEST_RANDOM: Literal["random"] = "random"
#: What a request says about seeds: nothing, one seed, or "draw them".
SeedRequest = int | Literal["random"] | None
#: Why ``--seed`` and a seeded ``--test`` selector cannot be combined.
SEED_WITH_SEEDED_SELECTOR = (
    "--seed cannot be combined with a --test that names a seed "
    "(test@seed or test@xN); name the seed in one place"
)

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

# Every ``Test…`` class below sets ``__test__ = False``: "Test" is the domain
# noun here, and pytest must not try to collect these when a test imports them.


class UnseededTargetError(ValueError):
    """A seed was requested for a Target that cannot apply one."""


class TestRunLimitError(ValueError):
    """A request asks for more Test Runs or seeds than Booley allows."""

    __test__ = False


@dataclass(frozen=True)
class UnpinnedSeed:
    """A selector that names no seed: the Test Run uses the Default Seed."""


@dataclass(frozen=True)
class PinnedSeed:
    """A selector written ``…@seed``: exactly that seed."""

    seed: int


@dataclass(frozen=True)
class SeedCount:
    """A selector written ``…@xN``: N Test Runs, each with a different seed."""

    count: int


SeedForm = UnpinnedSeed | PinnedSeed | SeedCount


@dataclass(frozen=True)
class TestSelector:
    """What a user wrote to name Test Runs: Test, optional Variant, seed form."""

    __test__ = False

    test: str
    variant: str | None
    seed_form: SeedForm


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
        return PinnedSeed(seed)
    digits = text[len(_COUNT_PREFIX) :]
    count = _parse_bounded_number(digits, what="seed count", selector=selector)
    if count < 1:
        raise TestRunNameError(f"Test Run selector {selector!r}: seed count must be at least 1")
    if count > MAX_MULTI_SEED_COUNT:
        raise TestRunLimitError(
            f"Test Run selector {selector!r}: seed count {digits} exceeds the "
            f"limit of {MAX_MULTI_SEED_COUNT}"
        )
    return SeedCount(count)


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
            "one Test Variant (test+variant)"
        )
    try:
        validate_test_or_variant_name(test, kind="Test")
        variant = (
            validate_test_or_variant_name(variant_parts[0], kind="Test Variant")
            if variant_parts
            else None
        )
    except TestRunNameError as exc:
        raise TestRunNameError(f"Test Run selector {text!r}: {exc}") from exc
    seed_form = _parse_seed_form(seed_parts[0], text) if seed_parts else UnpinnedSeed()
    return TestSelector(test=test, variant=variant, seed_form=seed_form)


def parse_seed_request(value: object) -> int | Literal["random"]:
    """Parse a ``--seed`` value: a seed in ``1..MAX_SEED`` or ``random``.

    Accepts the integer the MCP tool sends and the text the CLI sends; text
    obeys the selector's seed spelling (decimal, no sign, no leading zeros).
    Raises :class:`TestRunNameError`.
    """
    if value == SEED_REQUEST_RANDOM:
        return SEED_REQUEST_RANDOM
    if isinstance(value, str):
        seed = _parse_bounded_number(value, what="seed", selector=f"--seed {value}")
        if not 1 <= seed <= MAX_SEED:
            raise TestRunNameError(f"--seed {value} is out of range 1..{MAX_SEED}")
        return seed
    try:
        return validate_seed(value, where="--seed")
    except SeedConfigurationError as exc:
        raise TestRunNameError(f"{exc}; write a seed in 1..{MAX_SEED} or 'random'") from exc


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


def _require_seed(seed: object) -> int:
    """Return *seed* when it is a strict integer in ``1..MAX_SEED``."""
    try:
        return validate_seed(seed, where="seed")
    except SeedConfigurationError as exc:
        raise TestRunNameError(str(exc)) from exc


def _declared_environment(env: Mapping[str, str]) -> dict[str, str]:
    """A Test Variant's environment, under the rules ``tests.toml`` applies to it.

    Every Test Run is built here, also one decoded from a persisted record,
    so a record cannot carry a variable name the loader would have refused:
    not a shell-exportable name, or one Booley or the simulator integration
    owns. Raises :class:`TestRunNameError`.
    """
    where = "Test Run environment"
    try:
        return validate_declared_env_names(validate_env_table(env, where=where), where=where)
    except EnvTableError as exc:
        raise TestRunNameError(str(exc)) from exc


def build_test_run(
    test: str | None,
    *,
    variant: str | None = None,
    seed: int | None = None,
    args: tuple[str, ...] = (),
    env: Mapping[str, str] | None = None,
    fingerprint: str | None = None,
) -> TestRun:
    """Build a :class:`TestRun` and fingerprint its declared inputs.

    A *fingerprint* that a trusted record already holds is kept as given.

    Raises :class:`TestRunNameError` for an invalid name, a Test Variant on the
    unnamed invocation, a seed that is not an integer in ``1..MAX_SEED``, or
    an environment variable a Test Variant may not set.
    """
    if test is None and variant is not None:
        raise TestRunNameError(
            f"the unnamed invocation has no Test Variants, got Test Variant {variant!r}"
        )
    if test is not None:
        validate_test_or_variant_name(test, kind="Test")
    if variant is not None:
        validate_test_or_variant_name(variant, kind="Test Variant")
    if seed is not None:
        _require_seed(seed)
    environment = _declared_environment(env or {})
    return TestRun(
        test=test,
        variant=variant,
        seed=seed,
        args=tuple(args),
        env=tuple(sorted(environment.items())),
        fingerprint=(
            declared_inputs_fingerprint(tuple(args), environment)
            if fingerprint is None
            else fingerprint
        ),
    )


def discovered_test_run(test: str, unnamed: TestRun) -> TestRun:
    """The Test Run of a Test that an unfiltered batch discovered under *unnamed*.

    A Target without registered Tests runs as its unnamed invocation, and a
    Cocotb batch then reports the Tests its module holds. Each of them ran at
    that invocation's seed and environment, so that is its Test Run. A
    discovered name outside the Test name rules has no Test Run name of its
    own and stays under the unnamed invocation. This is the one rule; results,
    acceptance facts and reports all name a discovered Test through it.
    """
    try:
        return build_test_run(test, seed=unnamed.seed, env=dict(unnamed.env))
    except TestRunNameError:
        return unnamed


_TEST_RUN_DOCUMENT_FIELDS = frozenset(
    {"name", "test", "variant", "seed", "args", "env", "fingerprint"}
)


def test_run_document(run: TestRun) -> dict[str, object]:
    """The JSON form of *run* that persisted records carry."""
    return {
        "name": run.name,
        "test": run.test,
        "variant": run.variant,
        "seed": run.seed,
        "args": list(run.args),
        "env": dict(run.env),
        "fingerprint": run.fingerprint,
    }


# Not a pytest test, although its name starts with ``test_``.
test_run_document.__test__ = False  # type: ignore[attr-defined]


def _optional_name(value: object, field: str) -> str | None:
    """A Test Run record's Test or Test Variant: a string or ``None``."""
    if value is None:
        return None
    try:
        return require_str_value(value, field=field, allow_empty=True)
    except BoundaryError as exc:
        raise TestRunNameError(f"Test Run record {field} must be a string or null") from exc


def _record_args(value: object) -> tuple[str, ...]:
    """A Test Run record's argument list (a tuple once the record is frozen)."""
    items = list(cast("tuple[object, ...]", value)) if isinstance(value, tuple) else value
    if not is_str_list(items):
        raise TestRunNameError("Test Run record args must be a list of strings")
    return tuple(cast("list[str]", items))


def _record_fingerprint(value: object) -> str:
    """A trusted record's stored fingerprint: a ``sha256:`` string."""
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise TestRunNameError("a Test Run record's fingerprint must be a sha256 digest")
    return value


def _record_env(value: object) -> dict[str, str]:
    """A Test Run record's environment table."""
    table = as_dict(value)
    if table is None or not is_str_list([*table.keys(), *table.values()]):
        raise TestRunNameError("Test Run record env must map names to strings")
    return cast("dict[str, str]", table)


def test_run_from_document(document: object, *, trust_fingerprint: bool = False) -> TestRun:
    """Rebuild a :class:`TestRun` from its persisted form, strictly.

    The document must hold exactly the fields :func:`test_run_document`
    writes, and its ``name`` and ``fingerprint`` must be the ones the other
    fields produce: a record cannot name one Test Run and describe another.
    With *trust_fingerprint* the stored fingerprint is kept, not recomputed.
    Raises :class:`TestRunNameError`.
    """
    record = as_dict(document)
    if record is None:
        raise TestRunNameError("a Test Run record must be an object")
    if frozenset(record) != _TEST_RUN_DOCUMENT_FIELDS:
        raise TestRunNameError(
            "a Test Run record must hold exactly: " + ", ".join(sorted(_TEST_RUN_DOCUMENT_FIELDS))
        )
    seed = record["seed"]
    run = build_test_run(
        _optional_name(record["test"], "test"),
        variant=_optional_name(record["variant"], "variant"),
        seed=None if seed is None else _require_seed(seed),
        args=_record_args(record["args"]),
        env=_record_env(record["env"]),
        fingerprint=_record_fingerprint(record["fingerprint"]) if trust_fingerprint else None,
    )
    if record["name"] != run.name or record["fingerprint"] != run.fingerprint:
        raise TestRunNameError(
            f"Test Run record for {run.name!r} disagrees with its own name or fingerprint"
        )
    return run


test_run_from_document.__test__ = False  # type: ignore[attr-defined]


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
        # One token: the option and its value cannot be separated or reordered.
        argv.append(f"--test={run.name}")
    elif run.seed is not None:
        argv += ["--seed", str(run.seed)]
    return shlex.join(argv)
