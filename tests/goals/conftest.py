"""Goal Mode test support: a real Git repository laid out like a Booley Project.

The control Project directory is ``<main>/.booley_project`` (excluded from
Git, as ``init`` does); the linked worktree lives under its ``worktrees/``
and carries a copied ``.booley_project`` snapshot. Only the Target catalog is
faked, so entry never needs FuseSoC.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.core.project_dir import reset_cache
from booley.goals import entry as entry_module
from booley.goals.binding import GoalRunBinding, bind_run
from booley.goals.store import GoalStore
from booley.targets.domain import UnknownTargetError

GIT_TIMEOUT_S = 30
KNOWN_TARGETS = frozenset({"top", "base"})
DATE = "20261006"


def git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity and return its stripped output."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Goal Test",
            "-c",
            "user.email=goal@test.invalid",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
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
    git(main, "config", "core.autocrlf", "false")
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
    git(project_repo, "config", "core.autocrlf", "false")
    (project_repo / "booley.toml").write_text("[project]\n", encoding="utf-8")
    git(project_repo, "add", "-A")
    git(project_repo, "commit", "-q", "-m", "project")
    snapshot = layout.worktree / ".booley_project"
    for path in sorted(snapshot.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    snapshot.rmdir()
    git(project_repo, "worktree", "add", "-q", "--detach", str(snapshot))
    return snapshot


# ---------------------------------------------------------------------------
# An active Goal Mode (Phase 3 evidence tests)
# ---------------------------------------------------------------------------

TOP_CORE = (
    "CAPI=2:\n"
    "name: ::top:0\n"
    "filesets:\n"
    "  rtl: {files: [rtl.v], file_type: verilogSource}\n"
    "targets:\n"
    "  top: {filesets: [rtl], toplevel: top}\n"
)
#: The Goals the active Goal Mode fixture enters with.
GOAL_ARGS = (
    {"family": "lint", "target": "top"},
    {"family": "sim", "target": "top"},
)
LINT_KEY = "lint_clean_top"
SIM_KEY = "sim_pass_top"


def enter_goals(layout: SimpleNamespace, goals: Sequence[Mapping[str, Any]] = GOAL_ARGS) -> Any:
    """Commit a ``top`` core and a ``tests.toml`` in the worktree, then enter Goal Mode."""
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args

    # Byte writes keep every digested file identical on every platform.
    (layout.worktree / "top.core").write_bytes(TOP_CORE.encode())
    (layout.worktree / "rtl.v").write_bytes(b"module top; endmodule\n")
    git(layout.worktree, "add", "top.core", "rtl.v")
    git(layout.worktree, "commit", "-q", "-m", "top core")
    (layout.worktree / ".booley_project" / "tests.toml").write_bytes(b'[top]\ntests = ["smoke"]\n')
    request = EntryRequest(
        work_dir=layout.worktree, slug="evidence", goals=parse_goal_args(list(goals))
    )
    return enter_goal_mode(request, EntryEnvironment(project_dir=layout.control)).record


@pytest.fixture
def goal_mode(layout: SimpleNamespace) -> SimpleNamespace:
    """*layout* with an active Goal Mode on its worktree (lint and sim Goals on ``top``)."""
    record = enter_goals(layout)
    return SimpleNamespace(**vars(layout), record=record)


CAMPAIGN_ID = "12345678-1234-4234-9234-123456789abc"


def campaign_facts() -> dict[str, Any]:
    """Simulation acceptance facts the ledger accepts (content is not interpreted)."""
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": f"sha256:{'a' * 64}",
        "origin": {"execution_id": "b" * 32, "invocation_id": 7},
        "target": {"identity": "::top:0#top", "selector": "top"},
        "required_suite": {"names": ["smoke"], "default_invocation": False},
        "prerequisites": [],
        "consumed_results": [{"finished_at": "2026-10-07T10:00:00Z"}],
        "observations": [],
        "coverage_reference": None,
    }


def bind(layout: SimpleNamespace, invocation_id: str = "run-1") -> GoalRunBinding:
    """Bind a run in *layout*'s worktree to its active Goal Mode."""
    return bind_run(GoalStore(layout.control), layout.worktree, invocation_id)


def update_record(layout: SimpleNamespace, **changes: Any) -> None:
    """Save the worktree's occupying record with *changes*."""
    store = GoalStore(layout.control)
    record = store.active_for_worktree(layout.worktree)
    assert record is not None
    with store.record_lock(record.id) as lock:
        store.save(lock, replace(record, **changes))


def bump_spec(layout: SimpleNamespace, key: str) -> None:
    """Save the record with Goal *key* changed, as an applied Goal Change would."""
    store = GoalStore(layout.control)
    record = store.active_for_worktree(layout.worktree)
    assert record is not None
    goals = tuple(
        replace(goal, spec_revision=record.revision + 1) if goal.spec.key == key else goal
        for goal in record.goals
    )
    with store.record_lock(record.id) as lock:
        store.save(lock, replace(record, goals=goals))


def edit_protected(layout: SimpleNamespace, text: str = "[project]\nname = 'edited'\n") -> str:
    """Rewrite the session Project's ``booley.toml`` (a Protected Input); return what it held."""
    path = layout.control / "booley.toml"
    before = path.read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return before


def tree_digest(root: Path) -> dict[str, str]:
    """Every file beneath *root* by content, lock files excepted (they hold no evidence)."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in {"lock", ".sequence.lock"}
    }
