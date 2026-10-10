"""Host-wide Sandbox admission policy."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.runtime import devcontainer as dc
from booley.runtime import session_admission, session_issuance


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _vscode_inspection(
    project: Path,
    *,
    container_id: str = "editor-id",
    running: bool = False,
    started: str = "0001-01-01T00:00:00Z",
    project_id: str | None = None,
) -> dict:
    return {
        "Id": container_id,
        "Name": "/editor",
        "Created": "2026-09-27T08:00:00.123456Z",
        "Config": {
            "Labels": {
                "booley.role": "interactive",
                "devcontainer.local_folder": str(project),
                "booley.project-id": project_id or dc.canonical_project_id(project),
            }
        },
        "State": {"Running": running, "StartedAt": started},
        "Mounts": [],
    }


class _Docker:
    def __init__(self, documents: dict[str, dict]):
        self.documents = documents
        self.all_queries = 0

    def __call__(self, argv: list[str], **_kwargs):
        if argv[1] == "ps":
            include_stopped = "-a" in argv
            if include_stopped:
                self.all_queries += 1
            rows = []
            for container_id, document in self.documents.items():
                if not include_stopped and not document["State"]["Running"]:
                    continue
                name = "editor"
                if include_stopped:
                    folder = document["Config"]["Labels"].get("devcontainer.local_folder", "")
                    rows.append(f"{container_id}\t{name}\t{folder}")
                else:
                    rows.append(f"{container_id}\t{name}")
            return _completed(stdout="\n".join(rows) + ("\n" if rows else ""))
        return _completed(stdout=json.dumps(self.documents[argv[3]]))


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
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=1),
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
    assert session_admission._command(["booley", "session", "down", "--project"]) in message
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
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=1),
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


def test_headless_start_does_not_consume_own_editor_claim_below_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )
    docker = _Docker({})

    assert session_admission.claim_vscode_start(project, run=docker)
    with pytest.raises(session_admission.AdmissionError, match="pending editor start"):
        session_admission.admit_start(project, target_name="headless", run=docker)


def test_live_editor_makes_claim_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )
    docker = _Docker(
        {"editor-id": _vscode_inspection(project, running=True, started="2026-09-27T08:00:00Z")}
    )

    assert not session_admission.claim_vscode_start(project, run=docker)


def test_invalid_host_policy_is_an_admission_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def invalid_policy(**_kwargs: object) -> session_admission.SandboxHostPolicy:
        raise session_admission.HostConfigError(
            tmp_path / "host.toml", "sandbox.max_sessions", "invalid host policy"
        )

    monkeypatch.setattr(session_admission, "load_host_policy", invalid_policy)

    with pytest.raises(session_admission.AdmissionError, match="invalid host policy"):
        session_admission.admit_start(project, target_name="headless", run=_Docker({}))


def test_malformed_inventory_row_fails_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def malformed(_argv: list[str], **_kwargs):
        return _completed(stdout="missing-tab\n")

    with pytest.raises(session_admission.AdmissionError, match="incomplete Sandbox listing"):
        session_admission.admit_start(project, target_name="headless", run=malformed)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_completed(1, stderr="daemon unavailable"), "daemon unavailable"),
        (_completed(stdout="too\tfew\n"), "incomplete VS Code Sandbox listing"),
    ],
)
def test_vscode_inventory_failure_fails_closed(
    tmp_path: Path, result: subprocess.CompletedProcess[str], message: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(session_admission.AdmissionError, match=message):
        session_admission.vscode_sandboxes(project, run=lambda _argv, **_kwargs: result)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("role", "other", "cannot prove Sandbox ownership"),
        ("name", "/renamed", "Sandbox identity changed"),
    ],
)
def test_inspection_revalidates_role_and_name(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    document = _vscode_inspection(project, running=True, started="2026-09-27T08:00:00Z")
    if field == "role":
        document["Config"]["Labels"]["booley.role"] = value
    else:
        document["Name"] = value

    with pytest.raises(session_admission.AdmissionError, match=message):
        session_admission.vscode_sandboxes(project, run=_Docker({"editor-id": document}))


def test_inventory_failure_refuses_instead_of_assuming_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def run(_argv: list[str], **_kwargs):
        return _completed(1, stderr="daemon unavailable")

    with pytest.raises(session_admission.AdmissionError, match="daemon unavailable"):
        session_admission.admit_start(project, target_name="headless", run=run)


@pytest.mark.parametrize(
    ("field", "value"),
    [("Name", 7), ("StartedAt", "0001-01-01T00:00:00Z")],
)
def test_malformed_live_inspection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    document = _vscode_inspection(
        project,
        running=True,
        started="2026-09-27T08:00:00Z",
    )
    if field == "StartedAt":
        document["State"][field] = value
    else:
        document[field] = value
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )

    with pytest.raises(session_admission.AdmissionError, match="incomplete inspection"):
        session_admission.admit_start(
            project,
            target_name="headless",
            run=_Docker({"editor-id": document}),
        )


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
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=1),
    )
    session_admission.admit_start(project, target_name="headless", run=run)


def test_claim_reconciliation_uses_observed_container_state_not_cross_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    document = _vscode_inspection(project)
    docker = _Docker({"editor-id": document})
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )

    assert session_admission.claim_vscode_start(
        project,
        run=docker,
        now=datetime(2026, 9, 27, 8, 0, 0, 900000, tzinfo=UTC),
    )
    assert session_admission.has_pending_claim(project, run=docker)

    document["State"]["StartedAt"] = "2026-09-27T08:00:01.000001Z"
    assert not session_admission.has_pending_claim(project, run=docker)


def test_claim_for_moved_project_can_be_reconciled_and_cleared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_id = dc.canonical_project_id(project)
    docker = _Docker({})
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )
    assert session_admission.claim_vscode_start(project, run=docker)
    project.rmdir()

    docker.documents["editor-id"] = _vscode_inspection(project, project_id=project_id)
    assert not session_admission.has_pending_claim(project, run=docker)

    project.mkdir()
    assert session_admission.claim_vscode_start(project, run=_Docker({}))
    project.rmdir()
    assert session_admission.clear_vscode_claim(project)


def test_vscode_cleanup_rejects_mismatched_sealed_project_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    docker = _Docker(
        {
            "editor-id": _vscode_inspection(
                project,
                running=True,
                started="2026-09-27T08:00:00Z",
                project_id="wrong",
            )
        }
    )

    with pytest.raises(session_admission.AdmissionError, match="identity disagrees"):
        session_admission.vscode_sandboxes(project, run=docker)


def test_unrelated_stopped_sandbox_is_not_in_admission_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    malformed = _vscode_inspection(other)
    malformed["Name"] = 7
    docker = _Docker({"old": malformed})
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=1),
    )

    session_admission.admit_start(project, target_name="headless", run=docker)
    assert docker.all_queries == 0


def test_corrupt_claim_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )
    assert session_admission.claim_vscode_start(project, run=_Docker({}))
    claim_path = next(session_admission._store().root.glob("*.json"))
    claim_path.write_text("{}", encoding="utf-8")

    with pytest.raises(session_admission.AdmissionError, match="invalid shape"):
        session_admission.admit_start(project, target_name="headless", run=_Docker({}))


def test_unreadable_claim_json_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )
    assert session_admission.claim_vscode_start(project, run=_Docker({}))
    claim_path = next(session_admission._store().root.glob("*.json"))
    claim_path.write_text("not json", encoding="utf-8")

    with pytest.raises(session_admission.AdmissionError, match="cannot read"):
        session_admission.admit_start(project, target_name="headless", run=_Docker({}))


def test_age_includes_minutes_and_seconds() -> None:
    assert (
        session_admission._age(
            datetime(2026, 9, 27, 8, 0, tzinfo=UTC),
            datetime(2026, 9, 27, 8, 2, 3, tzinfo=UTC),
        )
        == "2m 3s"
    )


def test_claim_candidate_disappearing_during_inspect_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=2),
    )
    assert session_admission.claim_vscode_start(project, run=_Docker({}))

    def disappearing(argv: list[str], **_kwargs):
        if argv[1] == "ps" and "-a" not in argv:
            return _completed()
        if argv[1] == "ps":
            return _completed(stdout=f"gone\teditor\t{project}\n")
        return _completed(1, stderr="no such container")

    with pytest.raises(session_admission.AdmissionError, match="cannot inspect"):
        session_admission.admit_start(project, target_name="headless", run=disappearing)


def test_recovery_reports_when_it_will_restore_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    docker = _Docker(
        {"editor-id": _vscode_inspection(project, running=True, started="2026-09-27T08:00:00Z")}
    )
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **_kwargs: session_admission.SandboxHostPolicy(max_sessions=1),
    )

    session_admission.admit_start(project, target_name="recovery", recovery=True, run=docker)

    assert "recovery is restoring work at or above" in caplog.text


@pytest.mark.parametrize("canonical", [False, True])
def test_runtime_reports_legacy_host_policy_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    canonical: bool,
) -> None:
    from booley.config.host_config import host_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = host_config_path()
    path.parent.mkdir()
    original = '[interactive]\nmax_sessions = 1\nidle_timeout_seconds = 600\negress_allowlist = ["foo.test"]\n'
    if canonical:
        original += "[sandbox]\nmax_sessions = 2\n"
    path.write_text(original)
    policy = session_admission._policy()
    assert "[interactive] is deprecated" in caplog.text
    if canonical:
        assert "ignored" in caplog.text
    else:
        assert "idle_timeout_seconds = 600" in caplog.text
        assert 'egress_allowlist = ["foo.test"]' in caplog.text
    refusal = session_admission._refusal(policy, (), (), datetime(2026, 10, 1, tzinfo=UTC))
    assert "rename it to [sandbox] first" in refusal
    assert "preserving all existing settings" in refusal
    assert "instead of adding a second policy table" in refusal
    assert path.read_text() == original


def _issued_labels(project: Path) -> dict[str, str]:
    issuance = session_issuance.Issuance(
        version=1,
        project_root=str(project.resolve()),
        spec_sha256="a" * 64,
        image="image",
        image_id="sha256:" + "b" * 64,
        keeper_image="keeper",
        policy_revision=1,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="c" * 64,
    )
    return dict(label.split("=", 1) for label in session_issuance.labels(issuance))


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("running", [False, True])
def test_admission_accepts_real_issuance_labels(monkeypatch, tmp_path, foreign, running):
    project = tmp_path / "project"
    project.mkdir()
    requester = tmp_path / "requester" if foreign else project
    requester.mkdir(exist_ok=True)
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **kw: session_admission.SandboxHostPolicy(max_sessions=4),
    )
    docker = _Docker({})
    assert session_admission.claim_vscode_start(project, run=docker)
    document = _vscode_inspection(
        project,
        running=running,
        started="2026-09-27T08:00:00Z" if running else "0001-01-01T00:00:00Z",
    )
    document["Config"]["Labels"].update(_issued_labels(project))
    docker.documents["editor-id"] = document
    session_admission.admit_start(requester, target_name="headless", run=docker)
    assert not session_admission.has_pending_claim(project, run=docker)


@pytest.mark.parametrize("entry", ["claim", "inventory"])
def test_editor_boundaries_accept_real_issuance_labels(tmp_path, monkeypatch, entry):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **kw: session_admission.SandboxHostPolicy(max_sessions=4),
    )
    document = _vscode_inspection(project, running=True, started="2026-09-27T08:00:00Z")
    labels = _issued_labels(project)
    assert (
        labels["booley.project-id"] == hashlib.sha256(str(project.resolve()).encode()).hexdigest()
    )
    document["Config"]["Labels"].update(labels)
    docker = _Docker({"editor-id": document})
    if entry == "claim":
        assert not session_admission.claim_vscode_start(project, run=docker)
    else:
        assert (
            session_admission.vscode_sandboxes(project, run=docker)[0].container_id == "editor-id"
        )


@pytest.mark.parametrize("cap", [2, 3])
@pytest.mark.parametrize("running", [False, True])
def test_foreign_mismatch_retains_reservation_and_reports_recovery(
    tmp_path, monkeypatch, caplog, cap, running
):
    project = tmp_path / "project space"
    project.mkdir()
    requester = tmp_path / "requester"
    requester.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **kw: session_admission.SandboxHostPolicy(max_sessions=cap),
    )
    docker = _Docker({})
    assert session_admission.claim_vscode_start(project, run=docker)
    docker.documents["editor-id"] = _vscode_inspection(
        project,
        project_id="wrong",
        running=running,
        started="2026-09-27T08:00:00Z" if running else "0001-01-01T00:00:00Z",
    )
    if cap == 2 and running:
        with pytest.raises(session_admission.AdmissionError) as caught:
            session_admission.admit_start(requester, target_name="headless", run=docker)
        assert session_admission._command(["docker", "rm", "editor-id"]) in str(caught.value)
    else:
        session_admission.admit_start(requester, target_name="headless", run=docker)
    assert session_admission._command(["docker", "stop", "editor-id"]) in caplog.text
    assert session_admission._command(["docker", "rm", "editor-id"]) in caplog.text
    assert session_admission._command(["booley", "session", "down", "--project"]) in caplog.text
    assert (
        session_admission._store().root / session_admission._claim_filename(str(project))
    ).exists()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("baseline", [False, True])
def test_matching_container_cannot_hide_own_mismatch(tmp_path, monkeypatch, reverse, baseline):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        session_admission,
        "load_host_policy",
        lambda **kw: session_admission.SandboxHostPolicy(max_sessions=5),
    )
    bad = _vscode_inspection(project, project_id="wrong")
    docker = _Docker({"bad": bad} if baseline else {})
    if baseline:
        # A historical valid baseline later drifts to another Project identity.
        bad["Config"]["Labels"].update(_issued_labels(project))
    assert session_admission.claim_vscode_start(project, run=docker)
    bad["Config"]["Labels"]["booley.project-id"] = "wrong"
    good = _vscode_inspection(project)
    good["Config"]["Labels"].update(_issued_labels(project))
    pairs = [("good", good), ("bad", bad)]
    docker.documents = dict(reversed(pairs) if reverse else pairs)
    with pytest.raises(session_admission.AdmissionError, match="identity disagrees"):
        session_admission.admit_start(project, target_name="headless", run=docker)
    assert (
        session_admission._store().root / session_admission._claim_filename(str(project))
    ).exists()


def test_deleted_root_observed_spelling_is_verified_before_label_acceptance(tmp_path, monkeypatch):
    project = tmp_path / "Project"
    project.mkdir()
    observed = str(project).upper()
    monkeypatch.setattr(session_admission.os.path, "normcase", str.lower)
    canonical_text = session_admission._canonical_text
    monkeypatch.setattr(
        session_admission, "_canonical_text", lambda value: canonical_text(value).lower()
    )
    document = _vscode_inspection(project)
    document["Config"]["Labels"]["devcontainer.local_folder"] = observed
    document["Config"]["Labels"]["booley.project-id"] = hashlib.sha256(
        observed.encode()
    ).hexdigest()
    docker = _Docker({"editor-id": document})
    project.rmdir()
    assert session_admission.vscode_sandboxes(project, run=docker)
    document["Config"]["Labels"]["booley.project-id"] = hashlib.sha256(b"/unrelated").hexdigest()
    with pytest.raises(session_admission.AdmissionError, match="identity disagrees"):
        session_admission.vscode_sandboxes(project, run=docker)


def test_windows_recovery_commands_quote_powershell_metacharacters(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(session_admission, "os", SimpleNamespace(name="nt"))
    assert session_admission._command(
        ["booley", "session", "down", "--project", "C:\\Projects\\A&B's $work"]
    ) == ("& 'booley' 'session' 'down' '--project' 'C:\\Projects\\A&B''s $work'")
