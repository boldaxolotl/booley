"""Crash-safe Sandbox invalidation tests."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest
from tests.lifecycle_lock_support import observe_lifecycle_contention

from booley.eda.provisioning import authority
from booley.eda.provisioning.licensing import flexnet_docker
from booley.harness import eda_grants
from booley.runtime import (
    issuance_invalidation,
    session_issuance,
    session_refresh,
    session_runtime,
)


@pytest.fixture(autouse=True)
def private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_pending_revoke_invalidates_stamp_then_cleans_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_identity = str((tmp_path / "project").resolve())
    events = []
    issuance_invalidation.prepare(project_identity)
    monkeypatch.setattr(
        session_issuance, "invalidate_project", lambda root: events.append(("stamp", root))
    )
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda root: (events.append(("resources", root)), ())[1],
    )

    assert issuance_invalidation.recover_project_locked(project_identity)
    assert events == [("stamp", project_identity), ("resources", project_identity)]
    assert not issuance_invalidation._path(project_identity).exists()


def test_legacy_journal_keys_still_recover_with_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_identity = str((tmp_path / "project").resolve())
    events = []
    store = issuance_invalidation._store()
    store.ensure_directory()
    store.atomic_write_text(
        issuance_invalidation._filename(project_identity),
        json.dumps(
            {
                "schema_version": 1,
                "project_root": project_identity,
                "cleanup_resources": False,
            }
        ),
    )
    monkeypatch.setattr(
        session_issuance, "invalidate_project", lambda root: events.append(("stamp", root))
    )
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda root: (events.append(("resources", root)), ())[1],
    )

    assert session_refresh.shared_recovery_blocks_command(read_only=False)

    assert events == [("stamp", project_identity), ("resources", project_identity)]
    assert issuance_invalidation.pending_invalidations() == ()


def test_interrupted_cleanup_remains_pending_until_a_later_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_identity = str((tmp_path / "project").resolve())
    issuance_invalidation.prepare(project_identity)
    monkeypatch.setattr(session_issuance, "invalidate_project", lambda _root: None)
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: ("container:stale",),
    )

    with pytest.raises(issuance_invalidation.InvalidationError, match="residual objects"):
        issuance_invalidation.recover_project_locked(project_identity)

    assert issuance_invalidation.pending_invalidations() == (project_identity,)
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: (),
    )
    assert issuance_invalidation.recover_all_locked() == (project_identity,)
    assert issuance_invalidation.pending_invalidations() == ()


def test_tampered_invalidation_journal_fails_closed(tmp_path: Path) -> None:
    project_identity = str((tmp_path / "project").resolve())
    different_identity = str((tmp_path / "different-project").resolve())
    issuance_invalidation.prepare(project_identity)
    journal = issuance_invalidation._path(project_identity)
    journal.write_text(json.dumps({"project_root": different_identity}), encoding="utf-8")

    with pytest.raises(issuance_invalidation.InvalidationError, match="identity mismatch"):
        issuance_invalidation.pending_invalidations()
    with pytest.raises(issuance_invalidation.InvalidationError, match="identity mismatch"):
        issuance_invalidation.recover_project_locked(project_identity)
    assert journal.exists()


@pytest.mark.parametrize("identity_kind", ["relative", "parent-segment"])
def test_invalidation_journal_rejects_noncanonical_project_identity(
    tmp_path: Path,
    identity_kind: str,
) -> None:
    identity = (
        "relative/project"
        if identity_kind == "relative"
        else str(tmp_path / "project" / ".." / "other")
    )
    with pytest.raises(issuance_invalidation.InvalidationError, match="not canonical"):
        issuance_invalidation._decode({"project_root": identity})


def test_read_only_runtime_validation_blocks_pending_invalidation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    issuance_invalidation.prepare(str(project.resolve()))

    assert session_runtime.status(project) == "recovery-pending"
    with pytest.raises(session_runtime.SessionError, match="recovery is pending"):
        session_runtime.validate(project)


def _make_invalidation_store_unsafe(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "unsafe-project"
    project.mkdir()
    issuance_invalidation.prepare(str(project.resolve()))
    issuance_invalidation.cancel(str(project.resolve()))
    booley_config = tmp_path / "config" / "booley"
    booley_config.chmod(0o755)
    return project, booley_config


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
@pytest.mark.parametrize("operation", ["up", "status", "validate"])
def test_public_session_runtime_translates_unsafe_invalidation_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    project, booley_config = _make_invalidation_store_unsafe(tmp_path)
    monkeypatch.setattr(session_refresh, "has_pending_refresh", lambda _root: False)
    monkeypatch.setattr(
        session_runtime,
        "_up_unlocked",
        lambda *_a, **_kw: pytest.fail("unsafe recovery must precede Sandbox mutation"),
    )

    with pytest.raises(session_runtime.SessionError) as raised:
        getattr(session_runtime, operation)(project)

    message = str(raised.value)
    assert isinstance(raised.value.__cause__, issuance_invalidation.InvalidationError)
    assert str(booley_config) in message
    assert "0755" in message
    assert f"chmod 700 {booley_config}" in message


def test_shared_recovery_completes_invalidation_and_refresh_journals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    monkeypatch.setattr(
        issuance_invalidation,
        "recover_all_locked",
        lambda: events.append("invalidation") or ("/project",),
    )
    monkeypatch.setattr(
        session_refresh,
        "recover_all_locked",
        lambda: events.append("refresh") or (object(),),
    )

    assert session_refresh.shared_recovery_blocks_command(read_only=False)
    assert events == ["invalidation", "refresh"]


def _waiting_runtime_command(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_command: str,
    events: list[str],
) -> Callable[[], object]:
    if runtime_command == "up":
        monkeypatch.setattr(session_runtime, "_recover_before_lifecycle", lambda *_args: None)
        monkeypatch.setattr(
            session_runtime,
            "_up_unlocked",
            lambda *_args, **_kwargs: events.append("up"),
        )

        def run() -> object:
            return session_runtime.up(project)

        return run
    monkeypatch.setattr(
        session_refresh,
        "_refresh_unlocked",
        lambda *_args, **_kwargs: events.append("refresh"),
    )

    def run() -> object:
        return session_refresh.refresh(project, object())

    return run


def _run_runtime_command(
    operation: Callable[[], object],
    errors: list[RuntimeError],
    done: threading.Event,
) -> None:
    try:
        operation()
    except RuntimeError as exc:  # pragma: no cover - asserted by the caller
        errors.append(exc)
    finally:
        done.set()


def _start_grant_mutation(project, mutation, monkeypatch, done, errors) -> threading.Thread:
    monkeypatch.setattr(authority, "revoke_project_identity", lambda _project, _kind: str(project))
    monkeypatch.setattr(
        authority, "_prepare_revoke_grant", lambda identity, _kind: mutation(identity)
    )
    monkeypatch.setattr(
        flexnet_docker, "cleanup_project_resources_for_identity", lambda _identity: ()
    )

    def run_grant() -> None:
        try:
            eda_grants.COORDINATOR.revoke(project, "vivado")
        except RuntimeError as exc:  # pragma: no cover - asserted by the caller
            errors.append(exc)
        finally:
            done.set()

    grant = threading.Thread(target=run_grant)
    grant.start()
    return grant


@pytest.mark.parametrize("runtime_command", ["up", "refresh"])
def test_grant_mutation_excludes_concurrent_runtime_start_or_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_command: str,
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    commit_entered = threading.Event()
    release_commit = threading.Event()
    grant_done = threading.Event()
    runtime_done = threading.Event()
    events: list[str] = []
    errors: list[RuntimeError] = []

    @contextmanager
    def mutation(_identity: str):
        def commit() -> str:
            commit_entered.set()
            if not release_commit.wait(2):
                raise RuntimeError("test grant gate timed out")
            events.append("grant-commit")
            return "grant"

        yield commit

    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kwargs: False)
    monkeypatch.setattr(session_issuance, "invalidate_project", lambda _identity: None)

    grant = _start_grant_mutation(project, mutation, monkeypatch, grant_done, errors)
    assert commit_entered.wait(2)

    runtime_waiting = observe_lifecycle_contention(monkeypatch)
    operation = _waiting_runtime_command(project, monkeypatch, runtime_command, events)
    runtime = threading.Thread(
        target=_run_runtime_command,
        args=(operation, errors, runtime_done),
    )
    runtime.start()
    assert runtime_waiting.wait(2)
    assert events == []
    assert not runtime_done.is_set()

    release_commit.set()
    grant.join(2)
    runtime.join(2)
    assert grant_done.is_set()
    assert runtime_done.is_set()
    assert errors == []
    assert events == ["grant-commit", runtime_command]
    assert issuance_invalidation.pending_invalidations() == ()


def test_cancel_survives_transient_unlink_sharing_error(tmp_path: Path, monkeypatch) -> None:
    from tests.sharing_error_support import hold_unlinks

    identity = str((tmp_path / "project").resolve())
    issuance_invalidation.prepare(identity)
    journal = issuance_invalidation._path(identity)
    attempts = hold_unlinks(monkeypatch, {journal: 1})
    issuance_invalidation.cancel(identity)
    assert attempts[journal] == 2
    assert not journal.exists()


def test_cancel_retries_before_raising_lasting_sharing_error(tmp_path: Path, monkeypatch) -> None:
    from tests.sharing_error_support import hold_unlinks

    identity = str((tmp_path / "project").resolve())
    issuance_invalidation.prepare(identity)
    journal = issuance_invalidation._path(identity)
    attempts = hold_unlinks(monkeypatch, {journal: 99})
    with pytest.raises(PermissionError):
        issuance_invalidation.cancel(identity)
    assert attempts[journal] > 1
