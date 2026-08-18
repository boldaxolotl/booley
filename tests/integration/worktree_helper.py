"""Lightweight worktree creation for E2E tests.

Creates real git worktrees (for real git operations in the developer)
but without DPI builds or full submodule copies -- just stub files that
pass the worktree readiness checks.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def create_lightweight_worktree(
    project_root: Path,
    slug: str,
    base_branch: str = "main",
) -> Path:
    """Create a lightweight worktree with stub files.

    Returns the worktree path.
    """
    wt_dir = project_root / ".booley" / "worktrees"
    wt_dir.mkdir(parents=True, exist_ok=True)
    wt_path = wt_dir / slug

    # Remove stale worktree if it exists
    if wt_path.exists():
        cleanup_worktree(project_root, slug)

    # Create detached worktree from base_branch
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_path), base_branch],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )

    # Create feature branch
    subprocess.run(
        ["git", "checkout", "-B", slug],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )

    # Create stub files that pass worktree readiness checks
    _create_stubs(wt_path)

    # Initial commit so worktree isn't dirty
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(wt_path),
        capture_output=True,
        check=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "commit", "-m", f"e2e: init worktree for {slug}", "--allow-empty"],
        cwd=str(wt_path),
        capture_output=True,
        check=True,
        timeout=10,
    )

    logger.info("Created lightweight worktree: %s", wt_path)
    return wt_path


def _create_stubs(wt_path: Path) -> None:
    """Create minimal stubs to pass workspace-setup readiness checks."""
    # lib/ stub
    lib_dir = wt_path / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "stub.txt").write_text("stub", encoding="utf-8")

    # .booley/src stub (for sim/synthesis path checks)
    scripts_dir = wt_path / ".booley" / "src"
    scripts_dir.mkdir(parents=True, exist_ok=True)


def cleanup_worktree(project_root: Path, slug: str) -> None:
    """Remove a worktree and its branch."""
    wt_path = project_root / ".booley" / "worktrees" / slug

    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
            check=False,
        )

    # Delete the branch (ignore errors if it doesn't exist)
    subprocess.run(
        ["git", "branch", "-D", slug],
        cwd=str(project_root),
        capture_output=True,
        timeout=10,
        check=False,
    )

    # Prune stale worktree entries
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(project_root),
        capture_output=True,
        timeout=10,
        check=False,
    )
