"""Goal Mode test support: a real Git repository laid out like a Booley Project.

The control Project directory is ``<main>/.booley_project`` (excluded from
Git, as ``init`` does); the linked worktree lives under its ``worktrees/``
and carries a copied ``.booley_project`` snapshot. Only the Target catalog is
faked, so entry never needs FuseSoC.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.project_dir import reset_cache
from booley.goals import entry as entry_module
from booley.targets.domain import UnknownTargetError

GIT_TIMEOUT_S = 30
KNOWN_TARGETS = frozenset({"top", "base"})
DATE = "20261006"


def git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity and return its stripped output."""
    result = subprocess.run(
        ["git", "-c", "user.name=Goal Test", "-c", "user.email=goal@test.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_TIMEOUT_S,
    )
    return result.stdout.strip()


class FakeCatalog:
    """Knows :data:`KNOWN_TARGETS`; any other name is unknown."""

    @classmethod
    def build(cls, _root: Path) -> SimpleNamespace:
        def select(name: str) -> SimpleNamespace:
            if name not in KNOWN_TARGETS:
                raise UnknownTargetError(name)
            return SimpleNamespace(selector=name)

        return SimpleNamespace(select=select)


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A Project with a control Project directory and one linked worktree."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "booley.toml").write_text("# root config\n", encoding="utf-8")
    (main / "rtl.v").write_text("module top; endmodule\n", encoding="utf-8")
    (main / "docs").mkdir()
    (main / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "base")
    (main / ".git" / "info" / "exclude").write_text("/.booley_project\n", encoding="utf-8")
    control = main / ".booley_project"
    write_project_files(control)
    worktree = control / "worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "work", str(worktree))
    write_project_files(worktree / ".booley_project")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(control))
    monkeypatch.setattr(entry_module, "TargetCatalog", FakeCatalog)
    stamps = iter(f"{DATE}T12{minute:02d}00Z" for minute in range(60))
    monkeypatch.setattr(entry_module, "compact_utc_now", lambda: next(stamps))
    reset_cache()
    yield SimpleNamespace(main=main, control=control, worktree=worktree)
    reset_cache()


def write_project_files(project_dir: Path) -> None:
    """A minimal Project directory: configuration and one custom MCP tool."""
    (project_dir / "mcp_tools").mkdir(parents=True)
    (project_dir / "booley.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project_dir / "mcp_tools" / "tool.py").write_text("# custom tool\n", encoding="utf-8")


def install_paired_project(layout: SimpleNamespace, tmp_path: Path) -> Path:
    """Replace the worktree's snapshot with a paired Project repository checkout.

    The paired branch has no upstream, like a Goal worktree's would.
    """
    project_repo = tmp_path / "project-repo"
    project_repo.mkdir()
    git(project_repo, "init", "-q", "-b", "main")
    (project_repo / "booley.toml").write_text("[project]\n", encoding="utf-8")
    git(project_repo, "add", "-A")
    git(project_repo, "commit", "-q", "-m", "project")
    snapshot = layout.worktree / ".booley_project"
    for path in sorted(snapshot.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    snapshot.rmdir()
    git(project_repo, "worktree", "add", "-q", "--detach", str(snapshot))
    return snapshot
