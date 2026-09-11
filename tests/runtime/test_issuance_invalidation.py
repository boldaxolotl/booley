"""Crash-safe Session Runtime invalidation tests."""

from __future__ import annotations

import json
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    pending = issuance_invalidation.prepare("/project", cleanup_resources=True)
    monkeypatch.setattr(
        session_issuance, "invalidate_project", lambda root: events.append(("stamp", root))
    )
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda root: (events.append(("resources", root)), ())[1],
    )

    assert issuance_invalidation.recover_project_locked(
        "/project",
        cleanup_resources=flexnet_docker.cleanup_project_resources_for_identity,
    )
    assert events == [("stamp", "/project"), ("resources", "/project")]
    assert not issuance_invalidation._path(pending.project_root).exists()


def test_interrupted_cleanup_remains_pending_until_a_later_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuance_invalidation.prepare("/project", cleanup_resources=True)
    monkeypatch.setattr(session_issuance, "invalidate_project", lambda _root: None)
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: ("container:stale",),
    )

    with pytest.raises(issuance_invalidation.InvalidationError, match="residual objects"):
        issuance_invalidation.recover_project_locked(
            "/project",
            cleanup_resources=flexnet_docker.cleanup_project_resources_for_identity,
        )

    assert [item.project_root for item in issuance_invalidation.pending_invalidations()] == [
        "/project"
    ]
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: (),
    )
    assert issuance_invalidation.recover_all_locked(
        cleanup_resources=flexnet_docker.cleanup_project_resources_for_identity,
    ) == ("/project",)
    assert issuance_invalidation.pending_invalidations() == ()


def test_tampered_invalidation_journal_fails_closed() -> None:
    pending = issuance_invalidation.prepare("/project", cleanup_resources=False)
    issuance_invalidation._path(pending.project_root).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_root": "/different-project",
                "cleanup_resources": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(issuance_invalidation.InvalidationError, match="identity mismatch"):
        issuance_invalidation.pending_invalidations()


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
    monkeypatch: pytest.MonkeyPatch,
    invalidate_before: bool,
    cleanup_after: bool,
    expected: list[str],
) -> None:
    from booley.runtime import lifecycle_lock

    events = []

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
    monkeypatch.setattr(
        issuance_invalidation,
        "prepare",
        lambda *_args, **_kwargs: events.append("prepare") or object(),
    )
    monkeypatch.setattr(
        session_issuance,
        "invalidate_project",
        lambda _identity: events.append("stamp"),
    )
    monkeypatch.setattr(
        issuance_invalidation,
        "recover_project_locked",
        lambda *_args, **_kwargs: events.append("recover") or True,
    )

    result = issuance_invalidation.coordinate_mutation(
        operation="grant change",
        resolve_project_identity=lambda: events.append("resolve") or "/stored/project",
        mutation=lambda _identity: nullcontext(lambda: events.append("mutation") or "result"),
        cleanup_resources=lambda _identity: (),
        invalidate_before_mutation=invalidate_before,
        cleanup_after_mutation=cleanup_after,
    )

    assert result == "result"
    assert events == [*expected, "lock-exit"]


def test_failed_mutation_keeps_durable_invalidation_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import lifecycle_lock

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
            resolve_project_identity=lambda: "/project",
            mutation=lambda _identity: nullcontext(
                lambda: (_ for _ in ()).throw(RuntimeError("ambiguous write failure"))
            ),
            cleanup_resources=lambda _identity: (),
            invalidate_before_mutation=True,
            cleanup_after_mutation=False,
        )

    assert issuance_invalidation.pending_invalidations()[0].project_root == "/project"


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
