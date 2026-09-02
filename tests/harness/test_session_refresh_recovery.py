"""Process-restart recovery tests for Session refresh."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.eda.provisioning import runtime_spec
from booley.eda.provisioning.runtime_spec import Issuance
from booley.harness import bootstrap_cli, init_cmd, session_refresh
from booley.harness import session_runtime as sr
from booley.harness.init_cmd import SessionSpecSnapshot


def _issuance(project: Path, image_id: str = "sha256:prior") -> Issuance:
    identity = hashlib.sha256(str(project).encode()).hexdigest()
    return Issuance(
        version=4,
        project_root=str(project),
        spec_sha256="a" * 64,
        image=image_id,
        image_id=image_id,
        keeper_image=f"booley-issued-{identity}:session",
        policy_revision=1,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="b" * 64,
        file_sha256="c" * 64,
        project_data_source=str(project / ".booley_project"),
    )


def _write_restore_journal(config: Path, project: Path) -> Path:
    root = config / "booley" / "eda" / "session-refresh"
    root.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        for directory in (config / "booley", config / "booley" / "eda", root):
            directory.chmod(0o700)
    identity = hashlib.sha256(str(project).encode()).hexdigest()
    path = root / f"{identity}.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": "txn-1",
                "project_root": str(project),
                "phase": "parked",
                "direction": "restore_eligible",
                "snapshot": {
                    "spec_present": True,
                    "spec_content": base64.b64encode(b'{"image":"sha256:prior"}\n').decode(),
                    "spec_mode": 0o644,
                    "stamp_present": True,
                    "stamp_content": base64.b64encode(b'{"version":4}\n').decode(),
                    "stamp_mode": 0o600,
                    "image_id": "sha256:prior",
                },
                "prior_issuance": asdict(_issuance(project)),
                "prior_runtime": {
                    "name": sr.session_container_name(project),
                    "backup": f"{sr.session_container_name(project)}-pre-refresh",
                    "was_running": True,
                    "project_id": identity,
                    "reconnect_egress": True,
                    "container_id": "container-prior",
                    "image_id": "sha256:prior",
                    "egress_network_id": "network-egress",
                },
                "target_image_id": None,
                "target_payload_fingerprint": None,
                "replacement_issuance": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _commit_forward(path: Path, project: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["direction"] = "committed_forward"
    document["phase"] = "verified"
    document["target_image_id"] = "sha256:fresh"
    document["target_payload_fingerprint"] = "payload-fresh"
    document["replacement_issuance"] = asdict(_issuance(project, "sha256:fresh"))
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _record_replacement_before_commit(path: Path, project: Path) -> Issuance:
    replacement = _issuance(project, "sha256:fresh")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["phase"] = "issued"
    document["target_image_id"] = "sha256:fresh"
    document["target_payload_fingerprint"] = "payload-fresh"
    document["replacement_issuance"] = asdict(replacement)
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return replacement


_INTERRUPTION_MATRIX = (
    *((f"{side} stop", "prepared") for side in ("before", "after")),
    *((f"{side} rename", "prepared") for side in ("before", "after")),
    *((f"{side} egress detach", "prepared") for side in ("before", "after")),
    *((f"{side} spec write", "publishing") for side in ("before", "after")),
    *((f"{side} keeper retag", "publishing") for side in ("before", "after")),
    *((f"{side} stamp publication", "publishing") for side in ("before", "after")),
    *((f"{side} replacement create", "issued") for side in ("before", "after")),
    *((f"{side} replacement start", "issued") for side in ("before", "after")),
    *((f"{side} payload verification", "issued") for side in ("before", "after")),
    *((f"{side} predecessor deletion", "committed") for side in ("before", "after")),
    ("before journal deletion", "committed"),
    ("after journal deletion", "deleted"),
)


def _configure_interrupted_journal(path: Path, project: Path, state: str) -> Issuance | None:
    if state == "deleted":
        path.unlink()
        return None
    if state == "committed":
        _commit_forward(path, project)
        return _issuance(project, "sha256:fresh")
    if state == "issued":
        spec = project / ".devcontainer" / "devcontainer.json"
        spec.parent.mkdir()
        spec.write_text('{"image":"sha256:fresh"}\n', encoding="utf-8")
        return _record_replacement_before_commit(path, project)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["phase"] = "prepared" if state == "prepared" else "image_selected"
    if state == "publishing":
        document["target_image_id"] = "sha256:fresh"
        document["target_payload_fingerprint"] = "payload-fresh"
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return None


@pytest.mark.parametrize(("boundary", "journal_state"), _INTERRUPTION_MATRIX)
def test_fresh_orchestration_recovers_each_abrupt_mutation_boundary(
    tmp_path: Path, monkeypatch, boundary: str, journal_state: str
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    replacement = _configure_interrupted_journal(path, project, journal_state)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    with (
        patch.object(session_refresh, "restore_session_spec") as restore_spec,
        patch.object(sr, "restore_refresh_session") as restore_runtime,
        patch.object(session_refresh, "_verify_restored_journal"),
        patch.object(runtime_spec, "validate", return_value=replacement),
        patch.object(sr, "_up_unlocked") as resume,
        patch.object(sr, "discard_refresh_session"),
    ):
        result = session_refresh.recover_project_locked(project)

    expected = {
        "prepared": session_refresh.RecoveryOutcome.RESTORED,
        "publishing": session_refresh.RecoveryOutcome.RESTORED,
        "issued": session_refresh.RecoveryOutcome.RESUMED,
        "committed": session_refresh.RecoveryOutcome.RESUMED,
        "deleted": session_refresh.RecoveryOutcome.NONE,
    }[journal_state]
    assert boundary and result.outcome is expected
    if expected is session_refresh.RecoveryOutcome.RESTORED:
        restore_spec.assert_called_once()
        restore_runtime.assert_called_once()
        resume.assert_not_called()
    elif expected is session_refresh.RecoveryOutcome.RESUMED:
        restore_spec.assert_not_called()
        resume.assert_called_once()


def test_fresh_recovery_restores_interrupted_park(tmp_path: Path, monkeypatch) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    journal_path = _write_restore_journal(config, project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with (
        patch.object(session_refresh, "restore_session_spec") as restore_spec,
        patch.object(sr, "restore_refresh_session") as restore_runtime,
        patch.object(session_refresh, "_verify_restored_journal"),
    ):
        result = session_refresh.recover_project_locked(project)

    assert result.outcome is session_refresh.RecoveryOutcome.RESTORED
    snapshot = restore_spec.call_args.args[1]
    assert snapshot.spec_content == b'{"image":"sha256:prior"}\n'
    assert snapshot.stamp_content == b'{"version":4}\n'
    parked = restore_runtime.call_args.args[0]
    assert parked.container_id == "container-prior"
    assert parked.egress_network_id == "network-egress"
    assert not journal_path.exists()


def test_refresh_persists_recovery_identity_before_parking(tmp_path: Path, monkeypatch) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    issuance = _issuance(project)
    parked = sr.ParkedSession(
        sr.session_container_name(project),
        f"{sr.session_container_name(project)}-pre-refresh",
        True,
        project_id=hashlib.sha256(str(project).encode()).hexdigest(),
        reconnect_egress=True,
        container_id="container-prior",
        image_id="sha256:prior",
        egress_network_id="network-egress",
    )
    snapshot = SessionSpecSnapshot(
        project / ".devcontainer" / "devcontainer.json",
        b'{"image":"sha256:prior"}\n',
        0o644,
        config / "booley" / "eda" / "session-specs" / "stamp.json",
        b'{"version":4}\n',
        0o600,
        "sha256:prior",
    )

    def fail_after_journal(*_args) -> None:
        identity = hashlib.sha256(str(project).encode()).hexdigest()
        journal = config / "booley" / "eda" / "session-refresh" / f"{identity}.json"
        assert journal.is_file()
        raise RuntimeError("parking failed")

    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "inspect_refreshable_session_image"),
        patch.object(session_refresh, "capture_session_spec", return_value=snapshot),
        patch.object(session_refresh, "_load_recovery_issuance", return_value=issuance),
        patch.object(sr, "plan_session_refresh", return_value=parked, create=True),
        patch.object(sr, "park_planned_session", side_effect=fail_after_journal, create=True),
        patch.object(
            sr,
            "park_session_for_refresh",
            side_effect=AssertionError("parking was not planned before mutation"),
        ),
        patch.object(session_refresh, "restore_session_spec"),
        patch.object(sr, "restore_refresh_session"),
        patch.object(session_refresh, "_verify_restored_journal"),
        pytest.raises(RuntimeError, match="parking failed"),
    ):
        session_refresh.refresh(project)


def test_committed_recovery_only_finishes_replacement_cleanup(tmp_path: Path, monkeypatch) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    _commit_forward(path, project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with (
        patch.object(sr, "_up_unlocked") as up,
        patch.object(sr, "discard_refresh_session") as discard,
        patch.object(session_refresh, "restore_session_spec") as restore,
    ):
        result = session_refresh.recover_project_locked(project)

    assert result.outcome is session_refresh.RecoveryOutcome.RESUMED
    up.assert_called_once_with(
        project,
        expected_image_id="sha256:fresh",
        expected_payload_fingerprint="payload-fresh",
    )
    discard.assert_called_once()
    restore.assert_not_called()
    assert not path.exists()


def test_committed_recovery_never_rolls_back_on_transient_failure(
    tmp_path: Path, monkeypatch
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    _commit_forward(path, project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with (
        patch.object(sr, "_up_unlocked", side_effect=sr.SessionError("Docker unavailable")),
        patch.object(session_refresh, "restore_session_spec") as restore,
        pytest.raises(sr.SessionError, match="Docker unavailable"),
    ):
        session_refresh.recover_project_locked(project)

    restore.assert_not_called()
    assert path.is_file()


def test_coherent_replacement_resumes_and_commits_forward(tmp_path: Path, monkeypatch) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    spec_path = project / ".devcontainer" / "devcontainer.json"
    spec_path.parent.mkdir()
    spec_path.write_text('{"image":"sha256:fresh"}\n', encoding="utf-8")
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    replacement = _record_replacement_before_commit(path, project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with (
        patch.object(runtime_spec, "validate", return_value=replacement),
        patch.object(sr, "_up_unlocked") as up,
        patch.object(sr, "discard_refresh_session") as discard,
        patch.object(session_refresh, "restore_session_spec") as restore,
    ):
        result = session_refresh.recover_project_locked(project)

    assert result.outcome is session_refresh.RecoveryOutcome.RESUMED
    up.assert_called_once()
    discard.assert_called_once()
    restore.assert_not_called()
    assert not path.exists()


def test_snapshot_restore_refuses_symlink_substitution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    spec = project / ".devcontainer" / "devcontainer.json"
    spec.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    spec.symlink_to(outside)
    snapshot = SessionSpecSnapshot(
        spec,
        b'{"image":"sha256:prior"}\n',
        0o644,
        tmp_path / "stamp.json",
        None,
        0o600,
        None,
    )

    with pytest.raises(RuntimeError, match="symlink"):
        session_refresh.restore_session_spec(project, snapshot)

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_host_scan_rejects_journal_filename_identity_mismatch(tmp_path: Path, monkeypatch) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    wrong = path.with_name(f"{'d' * 64}.json")
    path.rename(wrong)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with pytest.raises(sr.SessionError, match=r"filename.*Project identity"):
        session_refresh.recover_all_locked()

    assert wrong.is_file()


def test_refresh_stops_after_recovering_shared_host_state(tmp_path: Path) -> None:
    project = tmp_path.resolve()
    recovered = session_refresh.RecoveryResult(project, session_refresh.RecoveryOutcome.RESTORED)
    with (
        patch.object(session_refresh, "recover_all_locked", return_value=(recovered,)),
        patch.object(session_refresh, "_refresh_unlocked") as refresh_unlocked,
        pytest.raises(sr.SessionError, match=r"recovered.*run.*again"),
    ):
        session_refresh.refresh(project)

    refresh_unlocked.assert_not_called()


def test_session_up_stops_after_project_recovery(tmp_path: Path) -> None:
    recovered = session_refresh.RecoveryResult(tmp_path, session_refresh.RecoveryOutcome.RESTORED)
    with (
        patch.object(
            session_refresh,
            "recover_project_locked",
            return_value=recovered,
        ),
        patch.object(sr, "_up_unlocked") as up_unlocked,
        pytest.raises(sr.SessionError, match=r"recovered.*run.*again"),
    ):
        sr.up(tmp_path)

    up_unlocked.assert_not_called()


def test_shared_mutators_stop_after_host_wide_recovery(tmp_path: Path) -> None:
    recovered = session_refresh.RecoveryResult(tmp_path, session_refresh.RecoveryOutcome.RESTORED)
    args = SimpleNamespace(check_only=False, force=False, verbose=False)
    with (
        patch.object(session_refresh, "recover_all_locked", return_value=(recovered,)),
        patch.object(bootstrap_cli, "reconcile_bootstrap") as reconcile,
    ):
        assert bootstrap_cli.run_bootstrap(args) == 2
    reconcile.assert_not_called()

    with (
        patch.object(session_refresh, "recover_all_locked", return_value=(recovered,)),
        patch.object(init_cmd, "_run_init_unlocked") as run_init,
    ):
        assert init_cmd.run_init(args, tmp_path) == 2
    run_init.assert_not_called()


def test_recovery_snapshot_rejects_keeper_on_different_image(tmp_path: Path) -> None:
    project = tmp_path.resolve()
    issuance = _issuance(project)
    spec = {"image": "sha256:prior"}
    spec_path = tmp_path / "devcontainer.json"
    spec_path.write_text("{}\n", encoding="utf-8")
    with (
        patch.object(runtime_spec, "_load_stamp", return_value=issuance),
        patch.object(runtime_spec, "_file_sha256", return_value=issuance.file_sha256),
        patch.object(runtime_spec, "_spec_digest", return_value=issuance.spec_sha256),
        patch.object(runtime_spec, "_resolve_image_id", return_value="sha256:different"),
        pytest.raises(runtime_spec.RuntimeSpecError, match=r"keeper.*different"),
    ):
        runtime_spec.load_recovery_snapshot(project, spec, spec_path)


def test_journal_rejects_unknown_schema_fields(tmp_path: Path, monkeypatch) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["unreviewed_extension"] = True
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with pytest.raises(sr.SessionError, match=r"unexpected.*fields"):
        session_refresh.recover_project_locked(project)

    assert path.is_file()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("version", True),
        ("project_root", "relative/project"),
        ("spec_sha256", "bad-digest"),
        ("image", ""),
        ("image_id", 7),
        ("keeper_image", "unowned:tag"),
        ("policy_revision", False),
        ("installation", []),
        ("license_profile", 4),
        ("wrapper_sha256", "bad-digest"),
        ("relay_image_id", "mutable:tag"),
        ("validator_sha256", "bad-digest"),
        ("file_sha256", None),
        ("project_data_source", None),
    ],
)
def test_journal_rejects_every_invalid_issuance_field_before_recovery(
    tmp_path: Path, monkeypatch, field: str, invalid: object
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["prior_issuance"][field] = invalid
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with pytest.raises(sr.SessionError, match="prior issuance is invalid"):
        session_refresh.recover_project_locked(project)

    assert path.is_file()


def test_restore_keeps_journal_when_final_issuance_verification_fails(
    tmp_path: Path, monkeypatch
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    config = tmp_path / "config"
    path = _write_restore_journal(config, project)
    spec = project / ".devcontainer" / "devcontainer.json"
    spec.parent.mkdir()
    spec.write_text('{"image":"sha256:prior"}\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    with (
        patch.object(session_refresh, "restore_session_spec"),
        patch.object(sr, "restore_refresh_session"),
        patch.object(
            runtime_spec,
            "load_recovery_snapshot",
            side_effect=runtime_spec.RuntimeSpecError("keeper drift"),
        ),
        pytest.raises(sr.SessionError, match="keeper drift"),
    ):
        session_refresh.recover_project_locked(project)

    assert path.is_file()


def test_read_only_commands_report_pending_recovery(tmp_path: Path) -> None:
    args = SimpleNamespace(check_only=True, force=False, verbose=False)
    with (
        patch.object(session_refresh, "has_pending_refresh", return_value=True, create=True),
        patch.object(sr.idk, "container_exists") as container_exists,
    ):
        assert sr.status(tmp_path) == "recovery-pending"
    container_exists.assert_not_called()

    with (
        patch.object(
            session_refresh,
            "pending_refresh_projects",
            return_value=(tmp_path,),
            create=True,
        ),
        patch.object(bootstrap_cli, "reconcile_bootstrap") as reconcile,
    ):
        assert bootstrap_cli.run_bootstrap(args) == 2
    reconcile.assert_not_called()

    with (
        patch.object(
            session_refresh,
            "pending_refresh_projects",
            return_value=(tmp_path,),
            create=True,
        ),
        patch.object(init_cmd, "_run_init_unlocked") as run_init,
    ):
        assert init_cmd.run_init(args, tmp_path) == 2
    run_init.assert_not_called()
