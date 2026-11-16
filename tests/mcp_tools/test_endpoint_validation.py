"""Project endpoint admission validates source text without importing custom code."""

from __future__ import annotations

import builtins
import logging

import pytest

from booley.mcp.endpoint_validation import (
    EndpointValidationError,
    validate_custom_endpoints_and_criteria,
)


@pytest.fixture
def project(tmp_path):
    data = tmp_path / ".booley"
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
        assert validate_custom_endpoints_and_criteria(root) == []
    assert "validation complete" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_doctor_ignores_project_endpoint_satisfies(project, caplog):
    root, data = project
    for satisfies in ("[]", "['missing_gate']"):
        _endpoint(data, satisfies=satisfies)
        assert validate_custom_endpoints_and_criteria(root) == []
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


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


_NO_SUBCLASS_REASON = (
    "no McpTool, BooleyFlow or Specialist subclass with plain string literal name and description"
)
_NO_SUBCLASS_FIX = (
    "make `name` and `description` plain string literals on an McpTool, "
    "BooleyFlow or Specialist subclass"
)


@pytest.mark.parametrize(
    "source,reason,fix",
    [
        (
            "x = 1\nclass Invalid(:\n",
            "Python syntax error on line 2: invalid syntax",
            "fix the syntax error",
        ),
        (
            "class Ordinary:\n    name = 'ordinary'\n",
            _NO_SUBCLASS_REASON,
            _NO_SUBCLASS_FIX,
        ),
        (
            "class Custom(McpTool):\n    name = 'missing_description'\n",
            _NO_SUBCLASS_REASON,
            _NO_SUBCLASS_FIX,
        ),
        (
            None,
            "name 'lint' collides with the built-in endpoint 'lint'",
            "rename the class's `name` attribute",
        ),
    ],
    ids=["syntax-error", "no-subclass", "missing-description", "builtin-collision"],
)
def test_skipped_endpoint_is_returned_with_reason_and_fix(project, source, reason, fix):
    root, data = project
    if source is None:
        _endpoint(data, name="lint", satisfies="['missing_gate']")
    else:
        (data / "mcp_tools/custom.py").write_text(source, encoding="utf-8")
    skipped = validate_custom_endpoints_and_criteria(root)
    assert [(s.file, s.reason, s.fix) for s in skipped] == [("custom.py", reason, fix)]


def test_builtin_name_collision_is_skipped_before_satisfies_validation(project, caplog):
    root, data = project
    _endpoint(data, name="lint", satisfies="['missing_gate']")
    validate_custom_endpoints_and_criteria(root)
    assert "missing_gate" not in caplog.text


def test_disabled_flow_and_private_files_are_not_validated(project, caplog):
    root, data = project
    _endpoint(data, base="BooleyFlow", name="lint", satisfies="['missing_gate']")
    (data / "booley.toml").write_text("[flows.lint]\nenabled = false\n")
    (data / "mcp_tools/_ignored.py").write_text("invalid syntax !")
    assert validate_custom_endpoints_and_criteria(root) == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


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


def test_doctor_source_checkout_validation_is_failure_not_traceback():
    from pathlib import Path

    from booley.harness import doctor

    outcomes = []
    doctor._check_custom_endpoints_and_criteria(
        Path(doctor.__file__).resolve().parents[3],
        lambda _: pytest.fail("Source Checkout was accepted as a Project"),
        lambda *_a: pytest.fail("Source Checkout produced a warning"),
        lambda message, fix="": outcomes.append(message),
    )
    assert len(outcomes) == 1
    assert "Source Checkout cannot be initialized or used as a Project" in outcomes[0]


def test_doctor_warns_once_per_skipped_file_without_pass_line(project):
    from booley.harness import doctor

    root, data = project
    (data / "mcp_tools/broken.py").write_text("x = 1\nclass Invalid(:\n", encoding="utf-8")
    (data / "mcp_tools/ordinary.py").write_text(
        "class Ordinary:\n    name = 'ordinary'\n", encoding="utf-8"
    )
    outcomes = []
    doctor._check_custom_endpoints_and_criteria(
        root,
        lambda message: outcomes.append(("pass", message)),
        lambda finding, fix="": outcomes.append(
            ("warn", finding.check_id, finding.subject, str(finding), fix)
        ),
        lambda message, fix="": outcomes.append(("fail", message)),
    )
    assert outcomes == [
        (
            "warn",
            "project.custom-endpoint-skipped",
            "broken.py",
            "custom endpoint broken.py skipped: Python syntax error on line 2: invalid syntax",
            "fix the syntax error",
        ),
        (
            "warn",
            "project.custom-endpoint-skipped",
            "ordinary.py",
            f"custom endpoint ordinary.py skipped: {_NO_SUBCLASS_REASON}",
            _NO_SUBCLASS_FIX,
        ),
    ]


@pytest.mark.parametrize(
    "validator,error",
    [
        (validate_custom_endpoints_and_criteria, EndpointValidationError),
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


@pytest.mark.parametrize("conflict", [False, True])
def test_external_booley_directory_has_doctor_preflight_parity(tmp_path, monkeypatch, conflict):
    from booley.harness import doctor
    from booley.runtime.booley_dir import reset_cache

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
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    reset_cache()
    outcomes = []
    doctor._check_custom_endpoints_and_criteria(
        root,
        lambda message: outcomes.append(("pass", message)),
        lambda message, fix="": outcomes.append(("warn", message)),
        lambda message, fix="": outcomes.append(("fail", message)),
    )
    if conflict:
        with pytest.raises(EndpointValidationError, match="CRITERIA CONFLICT"):
            validate_custom_endpoints_and_criteria(root)
        assert outcomes == [("fail", "custom endpoint/Criteria validation failed")]
    else:
        validate_custom_endpoints_and_criteria(root)
        assert outcomes == [("pass", "custom endpoints and Criteria validated")]
    assert not (root / ".booley").exists()
