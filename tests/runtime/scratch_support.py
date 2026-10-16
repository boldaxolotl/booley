"""Bounded readiness and reaping for synthetic scratch subprocess tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from tests.execution_test_support import wait_for


def wait(predicate, seconds=5):
    wait_for(predicate, failure="scratch child did not settle", timeout_s=seconds)


def reap_guardian(pid: int) -> None:
    wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid)


def stop_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is None:
        worker.kill()
    worker.wait(timeout=10)


def assert_native_workspace_lifetimes(roots: Iterable[Path]) -> None:
    """Require proven Linux cleanup or marked retention without native ownership proof."""
    for root in roots:
        if scratch_process_ownership_supported():
            assert not root.exists(), f"proven native workspace was retained: {root}"
        else:
            assert root.is_dir(), f"unproven native workspace was deleted: {root}"
            owner_root = next(
                (
                    candidate
                    for candidate in (root, *root.parents)
                    if (candidate / ".booley-lease.json").is_file()
                ),
                None,
            )
            assert owner_root is not None, f"retained workspace is unmarked: {root}"
            data = json.loads((owner_root / ".booley-lease.json").read_text())
            assert data["owner"] is None
            assert data["launches"] == {}
            info = owner_root.stat()
            assert data["root"] == [info.st_dev, info.st_ino]


def worktree_registrations(repo: Path) -> dict[Path, tuple[str, ...]]:
    """Read registration records without display padding or pathname quoting."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
        timeout=30,
    )
    records = {}
    for block in result.stdout.split(b"\0\0"):
        fields = block.split(b"\0")
        if fields[0].startswith(b"worktree "):
            records[Path(os.fsdecode(fields[0][9:]))] = tuple(
                os.fsdecode(field) for field in fields[1:] if field
            )
    return records


def scratch_process_ownership_supported() -> bool:
    """Expose the test host's descendant-proof capability, including restricted Linux."""
    from booley.core.temporary_workspace import process_identity

    return sys.platform == "linux" and process_identity(os.getpid()) is not None
