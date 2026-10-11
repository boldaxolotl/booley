"""Rules for one Test or Test Variant name (ADR 0072).

A Test Run is written ``test[+variant][@seed]``, so a Test or Variant name
cannot contain the grammar's separators, and it must be the same single token
on every shell so a printed rerun command works as written.

The rules live in the configuration layer because the ``tests.toml`` loader
applies them at load; ``booley.flows.test_runs`` builds the selector grammar on
top of them and re-exports them.
"""

from __future__ import annotations

import re
import unicodedata

#: Separates a Test from its Variant in a Test Run name.
VARIANT_SEPARATOR = "+"
#: Separates a Test Run from its seed.
SEED_SEPARATOR = "@"
#: The word a Variant table uses to name its default Variant. A Test or Variant
#: with this name would be ambiguous there, so the name is reserved.
RESERVED_NAME = "default"

#: Environment variable names a POSIX shell can export. Shared by the
#: per-Target ``env`` table and Test Variant environment tables.
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Characters with a meaning in the selector grammar or in a shell. A name
# without them is the same single token on every shell.
_FORBIDDEN_NAME_CHARACTERS = {
    VARIANT_SEPARATOR: "'+' (it separates a Test from its Variant)",
    SEED_SEPARATOR: "'@' (it separates a Test Run from its seed)",
    "'": "a single quote",
    '"': "a double quote",
    "\\": "a backslash",
}
# Unicode categories refused in names: control and invisible format characters.
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf"})


class TestRunNameError(ValueError):
    """A Test, Variant, selector or seed is not written in the Test Run grammar."""

    # "Test" is the domain noun; pytest must not collect this when a test imports it.
    __test__ = False


def _character_problem(character: str) -> str:
    """Why *character* may not appear in a name; empty when it may."""
    if character in _FORBIDDEN_NAME_CHARACTERS:
        return f"must not contain {_FORBIDDEN_NAME_CHARACTERS[character]}"
    if character.isspace():
        return "must not contain whitespace"
    if unicodedata.category(character) in _CONTROL_CATEGORIES:
        return f"must not contain the control character U+{ord(character):04X}"
    return ""


def _name_problem(name: object) -> str:
    """Why *name* is not a valid Test or Variant name; empty when it is."""
    if not isinstance(name, str):
        return f"must be a string, got {type(name).__name__}"
    if not name:
        return "must not be empty"
    if name == RESERVED_NAME:
        return f"{RESERVED_NAME!r} is reserved for naming a Test's default Variant"
    return next((problem for problem in map(_character_problem, name) if problem), "")


def validate_test_run_name_part(name: object, *, kind: str = "Test") -> str:
    """Return *name* when it is a valid Test or Variant name.

    *kind* (``"Test"`` or ``"Variant"``) only words the error. Raises
    :class:`TestRunNameError` naming the broken rule otherwise.
    """
    problem = _name_problem(name)
    if problem:
        raise TestRunNameError(f"{kind} name {name!r} is invalid: {problem}")
    assert isinstance(name, str)
    return name
