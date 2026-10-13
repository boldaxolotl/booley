"""The Project Protected Input boundary is strict and shared with Doctor."""

import pytest

from booley.audit.project_schema import audit_project_table
from booley.config.protected_inputs import parse_protected_inputs


@pytest.mark.parametrize("project", [{}, {"protected_inputs": []}])
def test_omitted_or_empty_adds_nothing(project):
    assert parse_protected_inputs({"project": project}) == ()


def test_supported_declarations():
    declarations = ["Makefile", "firmware/", "firmware/**/*.c", "vectors/test?.hex", ".fixture"]
    assert parse_protected_inputs({"project": {"protected_inputs": declarations}}) == tuple(
        declarations
    )


@pytest.mark.parametrize("value", [None, "Makefile", 2, [2], [""], ["   "]])
def test_bad_types_and_empty_items_are_refused(value):
    with pytest.raises(ValueError, match="protected_inputs"):
        parse_protected_inputs({"project": {"protected_inputs": value}})


@pytest.mark.parametrize(
    "path",
    [
        "/Makefile",
        "C:/Makefile",
        "C:Makefile",
        "../Makefile",
        "a/../b",
        "a\\b",
        ".git/config",
        "foo/.git/config",
        "x[ab].c",
        "{a,b}.c",
        "a\0b",
        "foo**/bar",
        "./Makefile",
        "a//b",
    ],
)
def test_bad_paths_are_named(path):
    with pytest.raises(ValueError) as error:
        parse_protected_inputs({"project": {"protected_inputs": [path]}})
    assert repr(path) in str(error.value)


def test_doctor_validates_the_same_key():
    audit = audit_project_table({"project": {"name": "demo", "protected_inputs": ["../recipe"]}})
    assert any("protected_inputs" in finding.message for finding in audit.findings)
    assert not audit.is_valid
