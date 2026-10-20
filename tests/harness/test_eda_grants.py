"""Cross-context EDA grant mutation composition tests."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from booley.eda.provisioning import authority
from booley.eda.provisioning.licensing import flexnet_docker
from booley.harness import eda_grants
from booley.runtime import issuance_invalidation, lifecycle_lock, session_issuance, session_refresh
from tests import eda_grant_support


@pytest.fixture(autouse=True)
def private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _patch_revoke(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    identity: str,
    commit: Callable[[], authority.ProjectGrant],
) -> None:
    """Record the revoke sequence around a stubbed authority mutation."""

    @contextmanager
    def locked(_operation: str):
        events.append("lock-enter")
        yield
        events.append("lock-exit")

    @contextmanager
    def prepared(_identity: str, _kind: str):
        events.append("prepare")
        yield commit

    real_prepare = issuance_invalidation.prepare
    monkeypatch.setattr(lifecycle_lock, "host_lifecycle_lock", locked)
    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kwargs: events.append("recover-old") or False,
    )
    monkeypatch.setattr(authority, "revoke_project_identity", lambda *_args: identity)
    monkeypatch.setattr(authority, "_prepare_revoke_grant", prepared)
    monkeypatch.setattr(
        issuance_invalidation,
        "prepare",
        lambda root: (events.append("journal"), real_prepare(root))[1],
    )
    monkeypatch.setattr(
        session_issuance, "invalidate_project", lambda _root: events.append("stamp")
    )
    monkeypatch.setattr(
        flexnet_docker,
        "cleanup_project_resources_for_identity",
        lambda _root: (events.append("cleanup"), ())[1],
    )


def test_revoke_runs_recovery_prepare_journal_commit_then_cleanup_under_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = str((tmp_path / "stored-project").resolve())
    grant = authority.ProjectGrant(identity, "vivado", "registered", "site")
    events: list[str] = []
    _patch_revoke(monkeypatch, events, identity, lambda: events.append("commit") or grant)

    assert eda_grants.GrantCoordinator().revoke(tmp_path, "vivado") == grant
    assert events == [
        "lock-enter",
        "recover-old",
        "prepare",
        "journal",
        "commit",
        "stamp",
        "cleanup",
        "lock-exit",
    ]
    assert issuance_invalidation.pending_invalidations() == ()


def test_revoke_commit_failure_keeps_journal_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = str((tmp_path / "stored-project").resolve())
    events: list[str] = []

    def failing_commit() -> authority.ProjectGrant:
        raise RuntimeError("ambiguous write failure")

    _patch_revoke(monkeypatch, events, identity, failing_commit)

    with pytest.raises(RuntimeError, match="ambiguous write failure"):
        eda_grants.GrantCoordinator().revoke(tmp_path, "vivado")

    assert "stamp" not in events
    assert issuance_invalidation.pending_invalidations() == (identity,)


@pytest.mark.parametrize("failure", ["validation", "invalidation"])
def test_failed_add_commits_nothing_and_writes_no_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    profile = eda_grant_support.register_license("site")
    order: list[str] = []
    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kw: order.append("recover") or False,
    )

    def withdraw(root: str) -> None:
        order.append(f"stamp:{root}")
        with (
            pytest.raises(lifecycle_lock.LifecycleLockError),
            lifecycle_lock.host_lifecycle_lock("probe", wait_timeout_s=0),
        ):
            pass
        if failure == "invalidation":
            raise OSError("stamp is read-only")

    monkeypatch.setattr(session_issuance, "invalidate_project", withdraw)
    # A rejected add must stop before the stamp is touched.
    licence_arg = profile.name if failure == "invalidation" else None
    message = "cannot invalidate" if failure == "invalidation" else "requires an installation"

    with pytest.raises(authority.AuthorityError, match=message):
        eda_grants.GrantCoordinator().add(
            project, "vivado", installation=None, license_profile=licence_arg
        )

    # Shared recovery runs first; the stamp is only touched while the lock is held (probe above).
    assert order == ["recover"] + (
        [f"stamp:{project.resolve()}"] if failure == "invalidation" else []
    )
    assert authority.load_state().grants == ()
    assert issuance_invalidation.pending_invalidations() == ()


def test_raw_grant_mutations_are_not_public() -> None:
    assert not hasattr(authority, "add_grant")
    assert not hasattr(authority, "revoke_grant")


def test_pending_recovery_probe_translates_runtime_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        session_refresh,
        "shared_recovery_blocks_command",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("corrupt journal")),
    )

    with pytest.raises(authority.AuthorityError, match="corrupt journal"):
        eda_grants.GrantCoordinator().recovery_pending()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_pending_recovery_probe_preserves_private_store_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    project = tmp_path / "project"
    project.mkdir()
    issuance_invalidation.prepare(str(project.resolve()))
    issuance_invalidation.cancel(str(project.resolve()))
    booley_config = config_root / "booley"
    booley_config.chmod(0o755)
    monkeypatch.setattr(session_refresh, "pending_refresh_projects", lambda: ())

    with pytest.raises(authority.AuthorityError) as raised:
        eda_grants.GrantCoordinator().recovery_pending()

    message = str(raised.value)
    assert str(booley_config) in message
    assert "0755" in message
    assert f"chmod 700 {booley_config}" in message
