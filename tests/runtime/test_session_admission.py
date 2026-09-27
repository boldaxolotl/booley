"""Host-wide Sandbox admission policy."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.runtime import session_admission


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def test_new_sandbox_at_cap_is_refused_with_live_project_and_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    other = tmp_path / "other project"
    other.mkdir()
    started = "2026-09-27T08:00:00Z"
    inspection = {
        "Id": "abc",
        "Name": "/existing",
        "Created": started,
        "Config": {"Labels": {"booley.role": "interactive"}},
        "State": {"Running": True, "StartedAt": started},
        "Mounts": [{"Type": "bind", "Source": str(other), "Destination": "/work"}],
    }

    def run(argv: list[str], *, timeout: int = 30):
        del timeout
        if argv[1] == "ps":
            return _completed(stdout="abc\texisting\n")
        return _completed(stdout=json.dumps(inspection))

    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda: session_admission.InteractiveHostPolicy(max_sessions=1),
    )
    with pytest.raises(session_admission.AdmissionError) as caught:
        session_admission.admit_start(
            current,
            target_name="new",
            run=run,
            now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        )

    message = str(caught.value)
    assert "max_sessions=1" in message
    assert f"Project {other}" in message
    assert "age 2h" in message
    assert "booley session down --project-root" in message
    assert "existing" in message


def test_pending_editor_claim_takes_the_last_slot_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda: session_admission.InteractiveHostPolicy(max_sessions=1),
    )

    def run(_argv: list[str], **_kwargs):
        return _completed()

    assert session_admission.claim_vscode_start(first, run=run) is True
    assert session_admission.claim_vscode_start(first, run=run) is False
    with pytest.raises(session_admission.AdmissionError, match="pending editor start"):
        session_admission.claim_vscode_start(second, run=run)
    with pytest.raises(session_admission.AdmissionError, match="pending editor start"):
        session_admission.admit_start(first, target_name="headless", run=run)
    assert session_admission.clear_vscode_claim(first) is True
    assert session_admission.claim_vscode_start(second, run=run) is True


def test_inventory_failure_refuses_instead_of_assuming_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def run(_argv: list[str], **_kwargs):
        return _completed(1, stderr="daemon unavailable")

    with pytest.raises(session_admission.AdmissionError, match="daemon unavailable"):
        session_admission.admit_start(project, target_name="headless", run=run)


def test_running_exact_target_is_idempotent_at_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    started = "2026-09-27T08:00:00Z"
    inspection = {
        "Id": "abc",
        "Name": "/headless",
        "Created": started,
        "Config": {"Labels": {"booley.role": "interactive"}},
        "State": {"Running": True, "StartedAt": started},
        "Mounts": [{"Type": "bind", "Source": str(project), "Destination": "/work"}],
    }

    def run(argv: list[str], **_kwargs):
        if argv[1] == "ps":
            return _completed(stdout="abc\theadless\n")
        return _completed(stdout=json.dumps(inspection))

    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda: session_admission.InteractiveHostPolicy(max_sessions=1),
    )
    session_admission.admit_start(project, target_name="headless", run=run)
