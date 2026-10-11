"""Value rules shared by everything ``tests.toml`` declares (ADR 0072).

Two rules live here, each with exactly one definition:

* the rule for one **Test** or **Test Variant** name. A Test Run is written
  ``test[+variant][@seed]``, so a name cannot contain the grammar's separators,
  cannot look like a command-line option or like the ``<default>`` display of
  the unnamed invocation, and must be the same single token on every shell so
  a printed rerun command works as written;
* the rule for an environment table (``NAME = "value"`` pairs), used by a
  Target's ``env`` table and by a Test Variant's.

They sit in the configuration layer because the ``tests.toml`` loader applies
them at load and configuration must not import Flows;
``booley.flows.test_runs`` builds the Test Run selector grammar on top.
"""

from __future__ import annotations

import re
import unicodedata

from booley.core.boundary import as_dict

#: Separates a Test from its Test Variant in a Test Run name.
VARIANT_SEPARATOR = "+"
#: Separates a Test Run from its seed.
SEED_SEPARATOR = "@"
#: The key that names a Test's default Test Variant in its Variant table. A
#: Test or Test Variant with this name would be ambiguous there, so no Test or
#: Test Variant may be called this.
DEFAULT_VARIANT_KEY = "default"

#: The complete name rule as one sentence. Error messages and Doctor's fix
#: hint both quote it, so the wording users see cannot drift from the check.
TEST_NAME_RULES = (
    "a Test or Test Variant name is not empty, is not 'default', does not "
    "start with '-', and contains no whitespace, control character, '+', '@', "
    "'<', '>', quote or backslash"
)

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
