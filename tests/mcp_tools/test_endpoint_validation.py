"""Project endpoint admission validates source text without importing custom code."""

from __future__ import annotations

import logging

import pytest

from booley.mcp.endpoint_validation import validate_custom_endpoints


@pytest.fixture
def project(tmp_path):
    data = tmp_path / ".booley"
    (data / "mcp_tools").mkdir(parents=True)
    return tmp_path, data


def _endpoint(data, *, name="custom_check", base="McpTool", extra=""):
    source = (
        "raise AssertionError('custom endpoint must not be imported')\n"
        f"class Custom({base}):\n"
        f"    name = {name!r}\n"
        "    description = 'Check the design'\n" + extra
    )
    (data / "mcp_tools/custom.py").write_text(source, encoding="utf-8")


def test_doctor_ignores_project_endpoint_satisfies(project, caplog):
    root, data = project
    for satisfies in ("[]", "['missing_gate']"):
        _endpoint(data, extra=f"    satisfies = {satisfies}\n")
        assert validate_custom_endpoints(root) == []
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
    validate_custom_endpoints(root)
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
        ("class Ordinary:\n    name = 'ordinary'\n", _NO_SUBCLASS_REASON, _NO_SUBCLASS_FIX),
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
        _endpoint(data, name="lint")
    else:
        (data / "mcp_tools/custom.py").write_text(source, encoding="utf-8")
    skipped = validate_custom_endpoints(root)
    assert [(s.file, s.reason, s.fix) for s in skipped] == [("custom.py", reason, fix)]


def test_disabled_flow_and_private_files_are_not_validated(project, caplog):
    root, data = project
    _endpoint(data, base="BooleyFlow", name="lint")
    (data / "booley.toml").write_text("[flows.lint]\nenabled = false\n")
    (data / "mcp_tools/_ignored.py").write_text("invalid syntax !")
    assert validate_custom_endpoints(root) == []
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
        validate_custom_endpoints(root)


def test_project_without_custom_endpoint_directory_is_valid(project):
    root, data = project
    (data / "mcp_tools").rmdir()
    validate_custom_endpoints(root)


def test_doctor_source_checkout_validation_is_failure_not_traceback():
    from pathlib import Path

    from booley.harness import doctor

    outcomes = []
    doctor._check_custom_endpoints(
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
    doctor._check_custom_endpoints(
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


def test_external_booley_directory_has_doctor_preflight_parity(tmp_path, monkeypatch):
    from booley.harness import doctor
    from booley.runtime.booley_dir import reset_cache

    root = tmp_path / "design"
    root.mkdir()
    external = tmp_path / "external-project-data"
    (external / "mcp_tools").mkdir(parents=True)
    (external / "criteria.toml").write_bytes(b'[lint_clean]\ndescription = "Left over"\n')
    _endpoint(external)
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    reset_cache()
    outcomes = []
    doctor._check_custom_endpoints(
        root,
        lambda message: outcomes.append(("pass", message)),
        lambda message, fix="": outcomes.append(("warn", message)),
        lambda message, fix="": outcomes.append(("fail", message)),
    )
    assert validate_custom_endpoints(root) == []
    assert outcomes == [("pass", "custom endpoints validated")]
    assert not (root / ".booley").exists()
