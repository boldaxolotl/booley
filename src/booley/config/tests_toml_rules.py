"""Value rules shared by everything ``tests.toml`` declares (ADR 0072).

These rules live here, each with exactly one definition:

* the rule for one **Test** or **Test Variant** name. A Test Run is written
  ``test[+variant][@seed]``, so a name cannot contain the grammar's separators,
  cannot look like a command-line option or like the ``<default>`` display of
  the unnamed invocation, and must be the same single token on every shell so
  a printed rerun command works as written;
* the rule for an environment table (``NAME = "value"`` pairs), used by a
  Target's ``env`` table and by a Test Variant's, and the names such a table
  may not set because Booley or the simulator integration owns them;
* the shape of one Test's Variant table, of the ``seed`` argument template and
  of a per-Target ``default_seed``;
* the seed range every supported seed mechanism accepts.

They sit in the configuration layer because the ``tests.toml`` loader applies
them at load and configuration must not import Flows;
``booley.flows.test_runs`` builds the Test Run selector grammar on top.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from typing import cast

from booley.core.boundary import BoundaryError, as_dict, is_str_list, require_int

#: Separates a Test from its Test Variant in a Test Run name.
VARIANT_SEPARATOR = "+"
#: Separates a Test Run from its seed.
SEED_SEPARATOR = "@"
#: The key that names a Test's default Test Variant in its Variant table. A
#: Test or Test Variant with this name would be ambiguous there, so no Test or
#: Test Variant may be called this.
DEFAULT_VARIANT_KEY = "default"

#: Longest Test or Test Variant name, in UTF-8 bytes.
MAX_TEST_NAME_BYTES = 512

#: The complete name rule as one sentence. Error messages and Doctor's fix
#: hint both quote it, so the wording users see cannot drift from the check.
TEST_NAME_RULES = (
    f"a Test or Test Variant name is not empty, is at most {MAX_TEST_NAME_BYTES} "
    "UTF-8 bytes, is not 'default', does not start with '-', and contains no "
    "whitespace, control character, '+', '@', '<', '>', quote or backslash"
)

#: The seed a Test Run uses when none is named. Never ``0``: Verilator reads
#: ``+verilator+seed+0`` as "pick a seed at startup".
DEFAULT_SEED = 1
#: Largest seed Booley accepts or draws. Verilator 5.052 accepts
#: ``+verilator+seed+`` values ``0..INT_MAX`` (``verilated.cpp``, bounded by
#: ``std::numeric_limits<int>::max()``); cocotb parses its seed variable with
#: Python ``int`` and has no bound. So ``1..2**31-1`` is the common range.
MAX_SEED = 2**31 - 1
#: The option Verilator's runtime reads its seed from. Booley renders it for
#: every seeded Verilator Test Run, so no declared argument may use it.
VERILATOR_SEED_OPTION = "+verilator+seed+"

# Characters with a meaning in the selector grammar, in a shell, or in the
# ``<default>`` display of the unnamed invocation.
_FORBIDDEN_NAME_CHARACTERS = {
    VARIANT_SEPARATOR: "'+' (it separates a Test from its Test Variant)",
    SEED_SEPARATOR: "'@' (it separates a Test Run from its seed)",
    "<": "'<'",
    ">": "'>'",
    "'": "a single quote",
    '"': "a double quote",
    "\\": "a backslash",
}
# Unicode categories refused in names: control and invisible format characters.
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf"})
# A leading dash would make the name read as a command-line option.
_OPTION_PREFIX = "-"

# Names a POSIX shell can export. Applied with ``fullmatch``: ``$`` would also
# accept a trailing newline.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class TestRunNameError(ValueError):
    """A Test, Test Variant, selector or seed is not written in the Test Run grammar."""

    # "Test" is the domain noun; pytest must not collect this when a test imports it.
    __test__ = False


class EnvTableError(ValueError):
    """An environment table is not a table of ``NAME = "value"`` string pairs."""


class TestVariantError(ValueError):
    """One Test's Variant table in ``tests.toml`` is malformed."""

    __test__ = False


class UnknownVariantError(ValueError):
    """A name refers to a Variant that the Test does not declare."""


class SeedConfigurationError(ValueError):
    """A ``seed`` template or ``default_seed`` in ``tests.toml`` is malformed."""


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

    def variant(self, name: str) -> TestVariant | None:
        """The declared Variant called *name*, or ``None``."""
        return next((item for item in self.variants if item.name == name), None)


def _character_problem(character: str) -> str:
    """Why *character* may not appear in a name; empty when it may."""
    if character in _FORBIDDEN_NAME_CHARACTERS:
        return f"it contains {_FORBIDDEN_NAME_CHARACTERS[character]}"
    if character.isspace():
        return "it contains whitespace"
    if unicodedata.category(character) in _CONTROL_CATEGORIES:
        return f"it contains the control character U+{ord(character):04X}"
    return ""


def _name_problem(name: object) -> str:
    """Why *name* is not a valid Test or Test Variant name; empty when it is."""
    if not isinstance(name, str):
        return f"it is not a string but {type(name).__name__}"
    if not name:
        return "it is empty"
    if len(name.encode("utf-8")) > MAX_TEST_NAME_BYTES:
        return f"it is longer than {MAX_TEST_NAME_BYTES} UTF-8 bytes"
    if name == DEFAULT_VARIANT_KEY:
        return f"{DEFAULT_VARIANT_KEY!r} is reserved for naming a Test's default Test Variant"
    if name.startswith(_OPTION_PREFIX):
        return "it starts with '-'"
    return next((problem for problem in map(_character_problem, name) if problem), "")


def validate_test_or_variant_name(name: object, *, kind: str = "Test") -> str:
    """Return *name* when it is a valid Test or Test Variant name.

    *kind* (``"Test"`` or ``"Test Variant"``) only words the error. Raises
    :class:`TestRunNameError` naming the broken rule, followed by the whole
    rule (:data:`TEST_NAME_RULES`).
    """
    problem = _name_problem(name)
    if problem:
        raise TestRunNameError(
            f"{kind} name {name!r} is invalid: {problem}. Rule: {TEST_NAME_RULES}"
        )
    assert isinstance(name, str)
    return name


def validate_env_table(raw: object, *, where: str) -> dict[str, str]:
    """Return *raw* as a validated environment table.

    Names must be shell-exportable identifiers. Values must be strings: a TOML
    integer would invite ``FLAVOR = 1`` to mean something different from
    ``"1"`` on the shell side. *where* locates the table in every message
    (for example ``tests.toml [sim].env``). Raises :class:`EnvTableError`.
    """
    table = as_dict(raw)
    if table is None:
        raise EnvTableError(f'{where} must be a table of NAME = "value" pairs')
    env: dict[str, str] = {}
    for name, value in table.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise EnvTableError(
                f"{where} key {name!r} is not a valid environment variable name "
                "([A-Za-z_][A-Za-z0-9_]*)"
            )
        if not isinstance(value, str):
            raise EnvTableError(
                f"{where}.{name} must be a string — quote the value (got {type(value).__name__})"
            )
        env[name] = value
    return env


# Environment variables Booley or the simulator integration owns. A declared
# environment table (a Target's or a Test Variant's) may set none of them:
# Booley would overwrite the value, or the value would redirect the simulator
# integration behind Booley's back.
_RESERVED_ENV_NAMES = frozenset(
    {
        "MODULE",
        "TESTCASE",
        "TOPLEVEL",
        "TOPLEVEL_LANG",
        "RANDOM_SEED",
        "LIBPYTHON_LOC",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
    }
)
_RESERVED_ENV_PREFIXES = ("BOOLEY_", "COCOTB_", "PYGPI_", "GPI_")


def reserved_env_name_problem(name: str) -> str:
    """Why a declared environment table may not set *name*; empty when it may."""
    if name in _RESERVED_ENV_NAMES:
        return f"{name} is owned by Booley or the simulator integration"
    for prefix in _RESERVED_ENV_PREFIXES:
        if name.startswith(prefix):
            return f"{prefix}* names are owned by Booley or the simulator integration"
    return ""


def validate_declared_env_names(env: dict[str, str], *, where: str) -> dict[str, str]:
    """Return *env* when it sets no reserved name; raise :class:`EnvTableError`."""
    for name in env:
        problem = reserved_env_name_problem(name)
        if problem:
            raise EnvTableError(f"{where} may not set {name}: {problem}")
    return env


_VARIANT_TABLE_KEYS = ("args", "env")


_ARGUMENT_PREFIXES = ("+", "-")


def option_key(token: str) -> str:
    """The option an argument token sets, without its value: the one canonical key.

    ``+iters=5``, ``+iters`` and the adapter form ``iters=5`` (a plusarg
    without its ``+``, see :func:`adapter_argument`) all give ``+iters``;
    ``--mem=a,b`` gives ``--mem``. A ``+verilator+seed+<n>`` token gives the
    seed option itself, whatever the number. Two tokens with one key set the
    same option. Validation and launch rendering both compare this key, so a
    token cannot pass one and mean something else at the other.
    """
    if token.startswith("-"):
        return token.partition("=")[0]
    plusarg = token if token.startswith("+") else f"+{token}"
    if plusarg.startswith(VERILATOR_SEED_OPTION):
        return VERILATOR_SEED_OPTION
    return plusarg.partition("=")[0]


def adapter_argument(token: str) -> str:
    """*token* as simulator adapters take it: a plusarg without its ``+``.

    Adapters add the ``+`` back to every token that starts with neither ``+``
    nor ``-``. :func:`argument_token_problem` refuses the tokens this would
    change the meaning of, so ``option_key`` is the same before and after.
    """
    return token.removeprefix("+")


def argument_token_problem(token: str) -> str:
    """Why *token* cannot be one declared simulator argument; empty when it can.

    A token is a plusarg (``+name[=value]``) or a getopt argument (``-…``),
    one word. ``++name`` and ``+-name`` are refused: the simulator would read
    them as the plusarg ``+name`` or the getopt argument ``-name``, so they
    would set an option under another name than the one checked here.
    """
    if not token.startswith(_ARGUMENT_PREFIXES):
        return "must start with '+' or '-'"
    if "\0" in token or any(character.isspace() for character in token):
        return (
            "must be one token without whitespace or NUL; declare each "
            "argument as its own list entry"
        )
    if token.startswith(("++", "+-")):
        return (
            f"must not start with {token[:2]!r}, which the simulator reads as another "
            "option; write a plusarg with one '+' or a getopt argument with '-'"
        )
    return ""


def _parse_variant_args(raw: object, where: str) -> tuple[str, ...]:
    """Validate a Variant's argument list: one option token per entry."""
    if not is_str_list(raw):
        raise TestVariantError(f"{where} arguments must be a list of strings")
    arguments = cast(list[str], raw)
    for argument in arguments:
        problem = argument_token_problem(argument)
        if problem:
            raise TestVariantError(f"{where} argument {argument!r} {problem}")
    return tuple(arguments)


def _parse_variant_env(raw: object, where: str) -> tuple[tuple[str, str], ...]:
    """Validate a Test Variant's environment table of ``NAME = "value"`` pairs."""
    try:
        env = validate_env_table(raw, where=f"{where}.env")
        validate_declared_env_names(env, where=f"{where}.env")
    except EnvTableError as exc:
        raise TestVariantError(str(exc)) from exc
    return tuple(sorted(env.items()))


def _parse_variant(name: str, raw: object, where: str) -> TestVariant:
    """Parse one Variant value: an argument list, or an ``args``/``env`` table."""
    table = as_dict(raw)
    if table is not None:
        unknown = sorted(str(key) for key in table if key not in _VARIANT_TABLE_KEYS)
        if unknown:
            raise TestVariantError(
                f"{where} has unknown key {unknown[0]!r}; a Test Variant table holds only args and env"
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
        raise TestVariantError(f"{where} must be a table of Test Variants")
    if DEFAULT_VARIANT_KEY not in table:
        raise TestVariantError(
            f'{where} must name its default Test Variant: add default = "<variant>"'
        )
    default = table[DEFAULT_VARIANT_KEY]
    if not isinstance(default, str):
        raise TestVariantError(f"{where}.default must be the name of a Test Variant (a string)")
    variants: list[TestVariant] = []
    for key, value in table.items():
        if key == DEFAULT_VARIANT_KEY:
            continue
        try:
            name = validate_test_or_variant_name(key, kind="Test Variant")
        except TestRunNameError as exc:
            raise TestRunNameError(f"{where}: {exc}") from exc
        variants.append(_parse_variant(name, value, f"{where}.{name}"))
    declared = [variant.name for variant in variants]
    if default not in declared:
        raise UnknownVariantError(
            f"{where}.default names {default!r}, which is not a declared Test Variant "
            f"(declared: {', '.join(declared) or 'none'})"
        )
    return TestVariantTable(test=test, default=default, variants=tuple(variants))


def template_fields(template: str) -> tuple[tuple[str, str, str | None], ...]:
    """The replacement fields of a format *template*: (name, spec, conversion).

    Parsed, never guessed from the text: ``{{seed}}`` is an escaped literal
    and has no field. Raises :class:`ValueError` for a malformed template.
    """
    return tuple(
        (name, spec or "", conversion)
        for _literal, name, spec, conversion in string.Formatter().parse(template)
        if name is not None
    )


def validate_seed_template(template: object, *, where: str) -> str:
    """Return a valid ``seed`` argument template.

    Like ``select``, it is exactly one option token (a plusarg ``+…`` or a
    getopt argument ``-…``) without whitespace. It has exactly one replacement
    field, the plain ``{seed}``, so the rendered argument always holds the
    seed as a decimal number. Raises :class:`SeedConfigurationError`.
    """
    if not isinstance(template, str) or not template.strip():
        raise SeedConfigurationError(f"{where} must be a non-empty string")
    try:
        fields = template_fields(template)
    except ValueError as exc:
        raise SeedConfigurationError(
            f"{where} template {template!r} is not a valid template: {exc}"
        ) from exc
    if fields != (("seed", "", None),):
        raise SeedConfigurationError(
            f"{where} template {template!r} must contain the plain {{seed}} field "
            "exactly once and no other field, conversion or format"
        )
    problem = argument_token_problem(template.format(seed=DEFAULT_SEED))
    if problem:
        raise SeedConfigurationError(f"{where} template {template!r} {problem}")
    probe = template.format(seed=DEFAULT_SEED)
    if option_key(probe) == VERILATOR_SEED_OPTION:
        raise SeedConfigurationError(
            f"{where} may not be {VERILATOR_SEED_OPTION}: Booley passes the Verilator "
            "seed option itself"
        )
    return template


def validate_seed(seed: object, *, where: str) -> int:
    """Return *seed* when it is a strict integer in ``1..MAX_SEED``.

    Raises :class:`SeedConfigurationError`; a bool is not an integer here.
    """
    try:
        value = require_int(seed, field=where)
    except BoundaryError as exc:
        raise SeedConfigurationError(str(exc)) from exc
    if not 1 <= value <= MAX_SEED:
        raise SeedConfigurationError(f"{where} {value} is out of range 1..{MAX_SEED}")
    return value
