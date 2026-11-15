"""Session registry rows and handle keys written under a former Sandbox mount keep attributing.

The former mount always comes from a temporary mount history; no mount point is spelled here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.mcp.session_registry import (
    Attribution,
    SessionRegistry,
    registered_worktree_presence,
    resolve_attribution,
)
from booley.runtime.handles import HandleMap, HandleStore, register_job
from booley.runtime.mount_history import MountHistoryError
from booley.runtime.timefmt import rfc3339_from_epoch
from tests.conftest import symlink_or_skip
from tests.mount_change_support import simulate_mount_change
from tests.subprocess_budget import run_bounded_child

SCOPE = "pid:[17]"


def _path_attribution(work_dir: str) -> Attribution:
    return Attribution(
        f"worktree:path:{work_dir}", "worktree", work_dir, f"path:{work_dir}", namespace=SCOPE
    )


def _mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, symlink: bool) -> tuple[Path, str]:
    """The Booley Directory (resolved), and the former mount its rows were written under."""
    real = tmp_path / "real"
    real.mkdir()
    current = real
    if symlink:
        current = tmp_path / "mount"
        symlink_or_skip(current, real, target_is_directory=True)
    former = simulate_mount_change(monkeypatch, current, tmp_path / "former-mount")
    (real / "worktrees" / "demo").mkdir(parents=True)
    return real, former


@pytest.mark.parametrize("symlink", [False, True], ids=["directory", "symlink"])
def test_former_mount_fallback_row_is_migrated_and_keeps_its_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    # Bug caught: a session that moved with the mount getting a second row and a new handle,
    # or none of its calls and start time, because its former-spelled row and key were not found.
    booley_dir, former = _mount(tmp_path, monkeypatch, symlink=symlink)
    registry = SessionRegistry(booley_dir, quiet_after=60, proc_root=tmp_path / "proc")
    old = _path_attribution(f"{former}/worktrees/demo")
    registry.upsert(old, "first", "completed", now=100)
    assert HandleStore(booley_dir).load().entries == {old.key: "S1"}

    current = resolve_attribution(
        booley_dir / "worktrees" / "demo", metadata={}, client_name="", proc_root=tmp_path / "proc"
    )
    assert HandleStore(booley_dir).snapshot()[0][current.key] == "S1"

    registry.upsert(current, "second", "completed", now=200)

    rows = registry.snapshot().rows
    assert len(rows) == 1
    assert rows[0].started_at == rfc3339_from_epoch(100)
    assert [call.tool for call in rows[0].calls] == ["first", "second"]
    assert not registry.path(old.key).exists()
    handles = HandleStore(booley_dir).load()
    assert handles.labels()[current.key] == "S1"
    assert "S2" not in handles.labels().values()


def test_former_mount_rows_read_and_prune_against_the_current_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: former-mount paths reported to readers, and a live checkout pruned as missing.
    booley_dir, former = _mount(tmp_path, monkeypatch, symlink=False)
    checkout = booley_dir / "worktrees" / "demo"
    run_bounded_child(["git", "init", "-q", str(checkout)], check=True)
    registry = SessionRegistry(booley_dir, quiet_after=60, proc_root=tmp_path / "proc")
    old = _path_attribution(f"{former}/worktrees/demo")
    registry.upsert(old, "tool", "completed", now=100)
    thread = Attribution(
        "codex:one",
        "thread",
        f"{former}/worktrees/demo",
        "repo:demo",
        namespace=SCOPE,
        default_work_dir=f"{former}/worktrees/demo/sub",
    )
    registry.upsert(thread, "tool", "completed", now=100)
    elsewhere = Attribution("codex:else", "thread", str(tmp_path / "elsewhere"), "repo:else")
    registry.upsert(elsewhere, "tool", "completed", now=100)

    by_key = {row.attribution.key: row.attribution for row in registry.snapshot().rows}

    path_row = by_key[f"worktree:path:{checkout}"]
    assert (path_row.work_dir, path_row.worktree_key) == (str(checkout), f"path:{checkout}")
    assert by_key["codex:one"].work_dir == str(checkout)
    assert by_key["codex:one"].default_work_dir == str(checkout / "sub")
    assert by_key["codex:else"].work_dir == str(tmp_path / "elsewhere")

    registry.prune(
        now=110, namespace=SCOPE, worktree_exists=registered_worktree_presence(checkout)
    )

    assert registry.path(old.key).exists()


def test_job_reservation_reuses_a_former_mount_session_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: a Job registered after a mount change allocating a second handle for a Session.
    booley_dir, former = _mount(tmp_path, monkeypatch, symlink=False)
    store = HandleStore(booley_dir)
    store.save(HandleMap({f"worktree:path:{former}/worktrees/demo": "S1"}, next_number=2))
    work_dir = booley_dir / "worktrees" / "demo"
    key = f"worktree:path:{work_dir}"

    register_job(booley_dir, "", "sim", key, work_dir=str(work_dir))

    assert store.load().labels()[key] == "S1"
    assert store.load().next_number == 2


def test_unreadable_mount_history_reports_the_history_file_not_an_absent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: a row read as former-spelled, or overwritten by a second row, while the
    # history that explains it cannot be read.
    booley_dir, former = _mount(tmp_path, monkeypatch, symlink=False)
    registry = SessionRegistry(booley_dir, quiet_after=60, proc_root=tmp_path / "proc")
    old = _path_attribution(f"{former}/worktrees/demo")
    registry.upsert(old, "tool", "completed", now=100)
    before = registry.path(old.key).read_bytes()
    (booley_dir / ".runtime" / "sandbox-mounts.json").write_bytes(b"{")

    snapshot = registry.snapshot()

    assert snapshot.unavailable and not snapshot.rows
    assert "sandbox-mounts.json" in snapshot.diagnostics[0]
    current = resolve_attribution(
        booley_dir / "worktrees" / "demo", metadata={}, client_name="", proc_root=tmp_path / "proc"
    )
    with pytest.raises(MountHistoryError, match=r"sandbox-mounts\.json"):
        registry.upsert(current, "tool", "completed", now=200)
    assert [path.name for path in registry.root.glob("*.json")] == [registry.path(old.key).name]
    assert registry.path(old.key).read_bytes() == before
