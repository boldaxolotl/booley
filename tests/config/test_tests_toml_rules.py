"""The Test / Test Variant name rule and the environment-table rule (ADR 0072)."""

from __future__ import annotations

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.config.tests_toml_rules import (
    TEST_NAME_RULES,
    EnvTableError,
    TestRunNameError,
    validate_env_table,
    validate_test_or_variant_name,
)

VALID_NAMES = [
    "reset",
    "test_reset",
    "slow-first",
    "a.b",
    "tb/smoke:1",
    "x1",
    "Default",
    "DEFAULT",
    "defaults",
    "a-",
    "é",
]

# (name, the specific reason the message must give)
INVALID_NAMES = [
    ("", "it is empty"),
    ("default", "'default' is reserved for naming a Test's default Test Variant"),
    ("-foo", "it starts with '-'"),
    ("--trace", "it starts with '-'"),
    ("-", "it starts with '-'"),
    ("a b", "it contains whitespace"),
    ("a\tb", "it contains whitespace"),
    ("a\nb", "it contains whitespace"),
    ("a\u00a0b", "it contains whitespace"),
    (" lead", "it contains whitespace"),
    ("a+b", "it contains '+' (it separates a Test from its Test Variant)"),
    ("a@b", "it contains '@' (it separates a Test Run from its seed)"),
    ("<default>", "it contains '<'"),
    ("a<b", "it contains '<'"),
    ("a>b", "it contains '>'"),
    ("a'b", "it contains a single quote"),
    ('a"b', "it contains a double quote"),
    ("a\\b", "it contains a backslash"),
    ("a\x00b", "it contains the control character U+0000"),
    ("a\x1bb", "it contains the control character U+001B"),
    ("a\x7fb", "it contains the control character U+007F"),
    ("a\u200bb", "it contains the control character U+200B"),
]


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert validate_test_or_variant_name(name) == name


@pytest.mark.parametrize(("name", "reason"), INVALID_NAMES)
@pytest.mark.parametrize("kind", ["Test", "Test Variant"])
def test_invalid_names_give_the_reason_and_the_whole_rule(
    name: str, reason: str, kind: str
) -> None:
    with pytest.raises(TestRunNameError) as excinfo:
        validate_test_or_variant_name(name, kind=kind)
    assert str(excinfo.value) == (
        f"{kind} name {name!r} is invalid: {reason}. Rule: {TEST_NAME_RULES}"
    )


@pytest.mark.parametrize("name", [7, None, 1.5, ["a"]])
def test_non_string_names_are_refused(name: object) -> None:
    with pytest.raises(TestRunNameError, match="it is not a string"):
        validate_test_or_variant_name(name)


def test_rule_sentence_mentions_every_refusal() -> None:
    # The sentence is quoted to users; a rule added to the check must be added here.
    for fragment in (
        "not empty",
        "'default'",
        "start with '-'",
        "whitespace",
        "control character",
        "'+'",
        "'@'",
        "'<'",
        "'>'",
        "quote",
        "backslash",
    ):
        assert fragment in TEST_NAME_RULES


@pytest.mark.parametrize("name", [*VALID_NAMES, *(name for name, _ in INVALID_NAMES)])
@pytest.mark.parametrize("key", ["tests", "skip"])
def test_loader_refuses_exactly_the_names_the_rule_refuses(name: str, key: str) -> None:
    try:
        validate_test_or_variant_name(name)
    except TestRunNameError as exc:
        expected: str | None = str(exc)
    else:
        expected = None
    raw = {"sim": {key: [name]}}
    if expected is None:
        assert normalize_tests_toml(raw) == raw
        return
    with pytest.raises(ValueError) as excinfo:
        normalize_tests_toml(raw)
    assert str(excinfo.value) == f"tests.toml [sim].{key}: {expected}"


# ---------------------------------------------------------------------------
# Environment tables
# ---------------------------------------------------------------------------


def test_env_table_accepts_string_pairs() -> None:
    raw = {"FLAVOR": "x y", "_A1": "", "lower_case": "1"}
    assert validate_env_table(raw, where="t.env") == raw


@pytest.mark.parametrize(
    "name",
    ["X\n", "\nX", "X ", "MY-VAR", "1X", "", "A=B", "É"],
)
def test_env_table_refuses_a_non_identifier_name(name: str) -> None:
    with pytest.raises(EnvTableError) as excinfo:
        validate_env_table({name: "1"}, where="tests.toml [sim].env")
    assert str(excinfo.value) == (
        f"tests.toml [sim].env key {name!r} is not a valid environment variable "
        "name ([A-Za-z_][A-Za-z0-9_]*)"
    )


@pytest.mark.parametrize("value", [1, 1.5, True, None, ["a"]])
def test_env_table_refuses_a_non_string_value(value: object) -> None:
    with pytest.raises(EnvTableError, match=r"^t\.env\.N must be a string — quote the value"):
        validate_env_table({"N": value}, where="t.env")


@pytest.mark.parametrize("raw", [["X=1"], "X=1", None, 3])
def test_env_table_refuses_a_non_table(raw: object) -> None:
    with pytest.raises(EnvTableError, match=r'^t\.env must be a table of NAME = "value" pairs$'):
        validate_env_table(raw, where="t.env")


def test_env_table_error_is_a_value_error() -> None:
    assert issubclass(EnvTableError, ValueError)


def test_names_longer_than_512_utf8_bytes_are_refused() -> None:
    assert validate_test_or_variant_name("t" * 512) == "t" * 512
    with pytest.raises(TestRunNameError, match="longer than 512 UTF-8 bytes"):
        validate_test_or_variant_name("é" * 257)
