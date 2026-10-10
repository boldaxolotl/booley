"""Project endpoint admission validates source text without importing custom code."""

from __future__ import annotations

import builtins
import logging

import pytest

from booley.harness.ticket_preflight import (
    TicketPreflightError,
    _validate_custom_endpoints_and_criteria,
)
from booley.mcp.endpoint_validation import (
    EndpointValidationError,
    validate_custom_endpoints_and_criteria,
)


@pytest.fixture
def project(tmp_path):
    data = tmp_path / ".booley_project"
    (data / "mcp_tools").mkdir(parents=True)
    return tmp_path, data


def _endpoint(data, *, name="custom_check", base="McpTool", satisfies="['lint_clean']", extra=""):
    source = (
        "raise AssertionError('custom endpoint must not be imported')\n"
        f"class Custom({base}):\n"
        f"    name = {name!r}\n"
        "    description = 'Check the design'\n"
        f"    satisfies = {satisfies}\n" + extra
    )
    (data / "mcp_tools/custom.py").write_text(source, encoding="utf-8")


def test_project_criteria_extend_known_satisfies_without_importing_code(project, caplog):
    root, data = project
    (data / "criteria.toml").write_text('[custom_gate]\ndescription = "Project gate"\n')
    _endpoint(data, satisfies="['lint_clean', 'custom_gate']")
    with caplog.at_level(logging.DEBUG, logger="booley.mcp.endpoint_validation"):
        validate_custom_endpoints_and_criteria(root)
    assert "validation complete" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.parametrize(
    "validator,error",
    [
        (validate_custom_endpoints_and_criteria, EndpointValidationError),
        (_validate_custom_endpoints_and_criteria, TicketPreflightError),
    ],
)
def test_base_criterion_conflict_refuses_endpoint_scan(project, validator, error):
    root, data = project
    (data / "criteria.toml").write_text('[lint_clean]\ndescription = "Override"\n')
    (data / "mcp_tools/broken.py").write_text("invalid syntax !")
    with pytest.raises(error, match="CRITERIA CONFLICT") as caught:
        validator(root)
    assert caught.value.failures == [
        "CRITERIA CONFLICT: Project criterion 'lint_clean' conflicts with base criterion"
    ]


@pytest.mark.parametrize(
    "satisfies,warning",
    [
        ("[]", "satisfies=[]"),
        ("['missing_gate']", "unknown criterion 'missing_gate'"),
    ],
)
def test_missing_satisfies_warns_without_failing_project(project, caplog, satisfies, warning):
    root, data = project
    _endpoint(data, satisfies=satisfies)
    validate_custom_endpoints_and_criteria(root)
    assert warning in caplog.text
    assert "custom.py" in caplog.text


@pytest.mark.parametrize(
    "declaration,warns",
    [
        ("    sandbox = 'host'\n", True),
        ("    sandbox: str = 'host'\n", True),
        ("    sandbox = 42\n", False),
        ("    sandbox: str\n", False),
        ("    sandbox = alias = 'host'\n", False),
        ("    unrelated: str = 'host'\n", False),
    ],
)
def test_sandbox_metadata_warning_only_for_a_literal_string(project, caplog, declaration, warns):
    root, data = project
    _endpoint(data, extra=declaration)
    validate_custom_endpoints_and_criteria(root)
    assert ("delete the retired sandbox metadata" in caplog.text) is warns
    if warns:
        assert "endpoints run in the Sandbox" in caplog.text


@pytest.mark.parametrize(
    "source,warning",
    [
        ("class Invalid(:\n", "Python syntax error"),
        ("class Ordinary:\n    name = 'ordinary'\n", "no McpTool/BooleyFlow/Specialist"),
        ("class Custom(McpTool):\n    name = 'missing_description'\n", "missing name/description"),
    ],
)
def test_bad_endpoint_is_skipped_with_a_filename(project, caplog, source, warning):
    root, data = project
    (data / "mcp_tools/custom.py").write_text(source)
    validate_custom_endpoints_and_criteria(root)
    assert warning in caplog.text
    assert "custom.py" in caplog.text


def test_builtin_name_collision_is_skipped_before_satisfies_validation(project, caplog):
    root, data = project
    _endpoint(data, name="lint", satisfies="['missing_gate']")
    validate_custom_endpoints_and_criteria(root)
    assert "conflicts with a built-in endpoint" in caplog.text
    assert "missing_gate" not in caplog.text


def test_disabled_flow_and_private_files_are_not_validated(project, caplog):
    root, data = project
    _endpoint(data, base="BooleyFlow", satisfies="['missing_gate']")
    (data / "booley.toml").write_text("[flows.custom_check]\nenabled = false\n")
    (data / "mcp_tools/_ignored.py").write_text("invalid syntax !")
    validate_custom_endpoints_and_criteria(root)
    assert not [
        record
        for record in caplog.records
        if record.name == "booley.mcp.endpoint_validation" and record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    "config,message",
    [
        ("[broken", "Cannot read"),
        ("[mcp_tools.custom_check]\nenabled = true\n", "is retired"),
        ("[specialists]\nreviewer = false\n", "specialists.reviewer"),
    ],
)
def test_invalid_endpoint_configuration_fails_closed(project, config, message):
    root, data = project
    (data / "booley.toml").write_text(config)
    with pytest.raises(ValueError, match=message):
        validate_custom_endpoints_and_criteria(root)


def test_project_without_custom_endpoint_directory_is_valid(project):
    root, data = project
    (data / "mcp_tools").rmdir()
    validate_custom_endpoints_and_criteria(root)


def test_unavailable_validator_dependency_skips_validation(project, monkeypatch, caplog):
    real_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "booley.criteria.templates":
            raise ImportError("unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    with caplog.at_level(logging.DEBUG, logger="booley.mcp.endpoint_validation"):
        validate_custom_endpoints_and_criteria(project[0])
    assert "validation skipped (imports unavailable)" in caplog.text


@pytest.mark.parametrize("conflict", [False, True])
def test_external_project_directory_has_doctor_preflight_parity(tmp_path, monkeypatch, conflict):
    from booley.harness import doctor
    from booley.runtime.project_dir import reset_cache

    root = tmp_path / "design"
    root.mkdir()
    external = tmp_path / "external-project-data"
    (external / "mcp_tools").mkdir(parents=True)
    (external / "criteria.toml").write_text(
        '[lint_clean]\ndescription = "Conflict"\n'
        if conflict
        else '[custom_gate]\ndescription = "Custom"\n'
    )
    _endpoint(external, satisfies="['custom_gate']")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(external))
    reset_cache()
    outcomes = []
    doctor._check_custom_endpoints_and_criteria(
        root,
        lambda message: outcomes.append(("pass", message)),
        lambda message, fix="": outcomes.append(("fail", message)),
    )
    if conflict:
        with pytest.raises(TicketPreflightError, match="CRITERIA CONFLICT"):
            _validate_custom_endpoints_and_criteria(root)
        assert outcomes == [("fail", "custom endpoint/Criteria validation failed")]
    else:
        _validate_custom_endpoints_and_criteria(root)
        assert outcomes == [("pass", "custom endpoints and Criteria validated")]
    assert not (root / ".booley_project").exists()


def test_doctor_source_checkout_validation_is_failure_not_traceback():
    from pathlib import Path

    from booley.harness import doctor

    outcomes = []
    doctor._check_custom_endpoints_and_criteria(
        Path(doctor.__file__).resolve().parents[3],
        lambda _: pytest.fail("Source Checkout was accepted as a Project"),
        lambda message, fix="": outcomes.append(message),
    )
    assert len(outcomes) == 1
    assert "Source Checkout cannot be initialized or used as a Project" in outcomes[0]
