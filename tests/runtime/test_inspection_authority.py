"""Complete Runtime inspection with real specs/stamps and one fake command transport."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from tests.runtime.test_session_issuance import _install_trusted_validator

from booley import __version__
from booley.audit.diagnostic_results import Severity
from booley.eda.config import EdaConfig
from booley.eda.provisioning import authority
from booley.runtime import devcontainer as dc
from booley.runtime import inspection, runtime_context, session_issuance
from booley.runtime.project_dir import reset_cache


@pytest.fixture(params=[False, True], ids=["no-active-eda", "active-image-eda"])
def issued_environment(request, tmp_path, monkeypatch):
    active = request.param
    project = tmp_path / "project"
    data = project / ".booley_project"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(
        f"[flows.fpga]\nenabled = {str(active).lower()}\n[eda.vivado]\nprovisioning = 'image'\n"
    )
    reset_cache()
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = _install_trusted_validator(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    calls = []
    observations = {"image": "sha256:" + "a" * 64, "version": __version__, "listing_error": False}

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 1, "", "not a git repository")
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, observations["image"], "")
        if argv[1:3] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(argv, int(observations["listing_error"]), "", "")
        if argv[1:3] == ["run", "--rm"]:
            if isinstance(observations["version"], Exception):
                raise observations["version"]
            return subprocess.CompletedProcess(argv, 0, observations["version"], "")
        pytest.fail(f"unexpected command: {argv}")

    monkeypatch.setattr(subprocess, "run", run)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    session_issuance.pin_image(spec)
    session_issuance.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    session_issuance.issue(project, spec, path)
    calls.clear()
    inspection_request = inspection.RuntimeInspectionRequest(
        project,
        observations["image"],
        "docker",
        eda={"vivado": EdaConfig("vivado")},
        fpga_enabled=active,
    )
    return inspection_request, observations, calls


def test_inspection_preserves_stamp_and_conditional_authority_effects(issued_environment):
    request, _, calls = issued_environment
    stamp = session_issuance.stamp_path(request.project_root)
    before = stamp.read_bytes()
    # No registered grants: removing this test-only empty store lets inspection
    # demonstrate its conditional directory/lock effects from a clean state.
    if authority.state_dir().exists():
        shutil.rmtree(authority.state_dir())
    report = inspection.inspect_runtime(request).issuance
    assert all(f.severity is Severity.PASS for f in report.findings)
    assert stamp.read_bytes() == before
    assert authority.state_dir().exists() is request.fpga_enabled
    assert not authority.state_path().exists()
    if request.fpga_enabled:
        assert list(authority.state_dir().glob("*.lock"))
        # The authority owner can immediately reacquire its lock after inspection.
        with authority.resolve_for_issuance(request.project_root, False):
            pass
    version_call = next((argv, kw) for argv, kw in calls if argv[1:3] == ["run", "--rm"])
    assert version_call[1]["timeout"] == 30
    assert "--pull=never" in version_call[0]
    assert version_call[0][version_call[0].index("--network") + 1] == "none"


def test_owner_detects_issuance_drift_without_doctor_wiring_changes(issued_environment):
    request, _, calls = issued_environment
    path = dc.devcontainer_path(request.project_root)
    spec = json.loads(path.read_text())
    spec["runArgs"].append("--privileged")
    path.write_text(json.dumps(spec))
    report = inspection.inspect_runtime(request).issuance
    assert any(
        f.severity is Severity.FAIL and "host issuance is invalid" in f.message
        for f in report.findings
    )
    assert not any(argv[1:3] == ["run", "--rm"] for argv, _ in calls)


def test_version_probe_timeout_is_a_finding_and_retains_the_command_deadline(issued_environment):
    request, observations, _ = issued_environment
    observations["version"] = subprocess.TimeoutExpired("docker", 30)
    report = inspection.inspect_runtime(request).issuance
    assert any(
        f.severity is Severity.FAIL and "could not read" in f.message for f in report.findings
    )
    assert any("no stale live" in f.message for f in report.findings)


def test_failed_live_enumeration_is_not_an_empty_success(issued_environment):
    request, observations, _ = issued_environment
    observations["listing_error"] = True
    report = inspection.inspect_runtime(request).issuance
    assert any(
        f.severity is Severity.FAIL and "could not list" in f.message for f in report.findings
    )
    assert not any("no stale live" in f.message for f in report.findings)


@pytest.mark.parametrize("raw", ["[]", "null", "42", "{broken"])
def test_malformed_spec_returns_independent_configuration_and_issuance_failures(
    tmp_path, monkeypatch, raw
):
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    path = dc.devcontainer_path(tmp_path)
    path.parent.mkdir()
    path.write_text(raw)
    result = inspection.inspect_runtime(
        inspection.RuntimeInspectionRequest(tmp_path, "image", None)
    )
    assert any(f.severity is Severity.FAIL for f in result.configuration.findings)
    assert any(f.severity is Severity.FAIL for f in result.issuance.findings)
