"""Crash-safe Session Runtime invalidation tests."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from booley.eda.provisioning.licensing import flexnet_docker
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
    pending = issuance_invalidation.prepare(project_identity, cleanup_resources=True)
    monkeypatch.setattr(
        session_issuance, "invalidate_project", lambda root: events.append(("stamp", root))
    )
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda root: (events.append(("resources", root)), ())[1],
    )

    assert issuance_invalidation.recover_project_locked(
        project_identity,
        cleanup_resources=flexnet_docker.cleanup_project_resources_for_identity,
    )
    assert events == [("stamp", project_identity), ("resources", project_identity)]
    assert not issuance_invalidation._path(pending.project_root).exists()


def test_interrupted_cleanup_remains_pending_until_a_later_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_identity = str((tmp_path / "project").resolve())
    issuance_invalidation.prepare(project_identity, cleanup_resources=True)
    monkeypatch.setattr(session_issuance, "invalidate_project", lambda _root: None)
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: ("container:stale",),
    )

    with pytest.raises(issuance_invalidation.InvalidationError, match="residual objects"):
        issuance_invalidation.recover_project_locked(
            project_identity,
            cleanup_resources=flexnet_docker.cleanup_project_resources_for_identity,
        )

    assert [item.project_root for item in issuance_invalidation.pending_invalidations()] == [
        project_identity
    ]
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: (),
    )
    assert issuance_invalidation.recover_all_locked(
        cleanup_resources=flexnet_docker.cleanup_project_resources_for_identity,
    ) == (project_identity,)
    assert issuance_invalidation.pending_invalidations() == ()


def test_tampered_invalidation_journal_fails_closed(tmp_path: Path) -> None:
    project_identity = str((tmp_path / "project").resolve())
    different_identity = str((tmp_path / "different-project").resolve())
    pending = issuance_invalidation.prepare(project_identity, cleanup_resources=False)
    issuance_invalidation._path(pending.project_root).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_root": different_identity,
                "cleanup_resources": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(issuance_invalidation.InvalidationError, match="identity mismatch"):
        issuance_invalidation.pending_invalidations()


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
        issuance_invalidation._decode(
            {
                "schema_version": 1,
                "project_root": identity,
                "cleanup_resources": False,
            }
        )


def test_direct_invalidation_load_rejects_mismatched_persisted_identity(
    tmp_path: Path,
) -> None:
    project_identity = str((tmp_path / "project").resolve())
    different_identity = str((tmp_path / "different-project").resolve())
    pending = issuance_invalidation.prepare(project_identity, cleanup_resources=False)
    issuance_invalidation._path(pending.project_root).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_root": different_identity,
                "cleanup_resources": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(issuance_invalidation.InvalidationError, match="identity mismatch"):
        issuance_invalidation.recover_project_locked(
            project_identity,
            cleanup_resources=lambda _: (),
        )


def test_read_only_runtime_validation_blocks_pending_invalidation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    issuance_invalidation.prepare(str(project.resolve()), cleanup_resources=False)

    assert session_runtime.status(project) == "recovery-pending"
    with pytest.raises(session_runtime.SessionError, match="recovery is pending"):
        session_runtime.validate(project)


def test_shared_recovery_completes_invalidation_and_refresh_journals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    monkeypatch.setattr(
        issuance_invalidation,
        "recover_all_locked",
        lambda **_kwargs: events.append("invalidation") or ("/project",),
    )
    monkeypatch.setattr(
        session_refresh,
        "recover_all_locked",
        lambda: events.append("refresh") or (object(),),
    )

    assert session_refresh.shared_recovery_blocks_command(read_only=False)
    assert events == ["invalidation", "refresh"]


@pytest.mark.parametrize(
    ("invalidate_before", "cleanup_after", "expected"),
    [
        (
            True,
            False,
            ["lock-enter", "recover-old", "resolve", "prepare", "stamp", "mutation", "recover"],
        ),
        (
            False,
            True,
            ["lock-enter", "recover-old", "resolve", "prepare", "mutation", "recover"],
        ),
    ],
)
def test_runtime_coordinates_grant_mutation_order_inside_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalidate_before: bool,
    cleanup_after: bool,
    expected: list[str],
) -> None:
    from booley.runtime import lifecycle_lock

    project_identity = str((tmp_path / "stored-project").resolve())
    events = []
    identities = []

    @contextmanager
    def locked(_operation: str):
        events.append("lock-enter")
        yield
        events.append("lock-exit")

    monkeypatch.setattr(lifecycle_lock, "host_lifecycle_lock", locked)
    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kwargs: events.append("recover-old") or False,
    )

    def prepare(identity: str, **_kwargs):
        identities.append(identity)
        events.append("prepare")
        return object()

    def invalidate(identity: str) -> None:
        identities.append(identity)
        events.append("stamp")

    def recover(identity: str, **_kwargs) -> bool:
        identities.append(identity)
        events.append("recover")
        return True

    monkeypatch.setattr(issuance_invalidation, "prepare", prepare)
    monkeypatch.setattr(session_issuance, "invalidate_project", invalidate)
    monkeypatch.setattr(issuance_invalidation, "recover_project_locked", recover)

    @contextmanager
    def mutation(identity: str):
        identities.append(identity)
        yield lambda: events.append("mutation") or "result"

    result = issuance_invalidation.coordinate_mutation(
        operation="grant change",
        resolve_project_identity=lambda: events.append("resolve") or project_identity,
        mutation=mutation,
        cleanup_resources=lambda _identity: (),
        invalidate_before_mutation=invalidate_before,
        cleanup_after_mutation=cleanup_after,
    )

    assert result == "result"
    assert events == [*expected, "lock-exit"]
    assert identities and set(identities) == {project_identity}


def _assert_runtime_start_is_blocked(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_command: str,
) -> None:
    from booley.runtime import lifecycle_lock

    invoked = []
    if runtime_command == "up":
        monkeypatch.setattr(session_runtime, "_recover_before_lifecycle", lambda *_args: None)
        monkeypatch.setattr(
            session_runtime,
            "_up_unlocked",
            lambda *_args, **_kwargs: invoked.append("up"),
        )
        with pytest.raises(lifecycle_lock.LifecycleLockError, match="lifecycle is busy"):
            session_runtime.up(project)
    else:
        monkeypatch.setattr(
            session_refresh,
            "_refresh_unlocked",
            lambda *_args, **_kwargs: invoked.append("refresh"),
        )
        with pytest.raises(lifecycle_lock.LifecycleLockError, match="lifecycle is busy"):
            session_refresh.refresh(project, object())
    assert invoked == []


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
    errors: list[RuntimeError] = []

    @contextmanager
    def mutation(_identity: str):
        def commit() -> str:
            commit_entered.set()
            if not release_commit.wait(2):
                raise RuntimeError("test grant gate timed out")
            return "grant"

        yield commit

    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kwargs: False)
    monkeypatch.setattr(session_issuance, "invalidate_project", lambda _identity: None)

    def run_grant() -> None:
        try:
            issuance_invalidation.coordinate_mutation(
                operation="grant revoke",
                resolve_project_identity=lambda: str(project),
                mutation=mutation,
                cleanup_resources=lambda _identity: (),
                invalidate_before_mutation=True,
                cleanup_after_mutation=False,
            )
        except RuntimeError as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            grant_done.set()

    grant = threading.Thread(target=run_grant)
    grant.start()
    assert commit_entered.wait(2)
    _assert_runtime_start_is_blocked(project, monkeypatch, runtime_command)

    release_commit.set()
    grant.join(2)
    assert grant_done.is_set()
    assert errors == []
    assert issuance_invalidation.pending_invalidations() == ()


def test_failed_mutation_keeps_durable_invalidation_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import lifecycle_lock

    project_identity = str((tmp_path / "project").resolve())
    monkeypatch.setattr(
        lifecycle_lock,
        "host_lifecycle_lock",
        lambda _operation: nullcontext(),
    )
    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(session_issuance, "invalidate_project", lambda _identity: None)

    with pytest.raises(RuntimeError, match="ambiguous write failure"):
        issuance_invalidation.coordinate_mutation(
            operation="grant add",
            resolve_project_identity=lambda: project_identity,
            mutation=lambda _identity: nullcontext(
                lambda: (_ for _ in ()).throw(RuntimeError("ambiguous write failure"))
            ),
            cleanup_resources=lambda _identity: (),
            invalidate_before_mutation=True,
            cleanup_after_mutation=False,
        )

    assert issuance_invalidation.pending_invalidations()[0].project_root == project_identity


def test_expected_mutation_validation_failure_does_not_invalidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import lifecycle_lock

    events = []

    @contextmanager
    def rejected_mutation(_identity: str):
        events.append("validate")
        raise ValueError("duplicate grant")
        yield lambda: None  # pragma: no cover

    monkeypatch.setattr(
        lifecycle_lock,
        "host_lifecycle_lock",
        lambda _operation: nullcontext(),
    )
    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        issuance_invalidation,
        "prepare",
        lambda *_args, **_kwargs: events.append("prepare"),
    )
    monkeypatch.setattr(
        session_issuance,
        "invalidate_project",
        lambda _identity: events.append("stamp"),
    )

    with pytest.raises(ValueError, match="duplicate grant"):
        issuance_invalidation.coordinate_mutation(
            operation="grant add",
            resolve_project_identity=lambda: "/project",
            mutation=rejected_mutation,
            cleanup_resources=lambda _identity: (),
            invalidate_before_mutation=True,
            cleanup_after_mutation=False,
        )

    assert events == ["validate"]
