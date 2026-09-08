"""Executable reproducer for F-87 generated Acceptance Basis inputs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.fusesoc.core_projection import reconcile_projected_cores
from booley.harness import developer
from booley.harness.models import TicketContext
from booley.harness.setup.workspace import _validate_materialized_acceptance_basis
from booley.runtime.project_dir import reset_cache
from booley.ticket_board.io import TicketFileSpec, TicketIO


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _ticket_context(root: Path) -> TicketContext:
    project_dir = root / ".booley_project"
    (project_dir / "tickets/board/drafts").mkdir(parents=True)
    (project_dir / "cores").mkdir()
    (root / ".gitignore").write_text("/.booley-projected-*.core\n", encoding="utf-8")
    (project_dir / ".gitignore").write_text(
        "/worktrees/\n/.runtime/\n/tmp/\n",
        encoding="utf-8",
    )
    (project_dir / "booley.toml").write_text(
        "[flows]\n[stealth]\nenabled = true\nignore_native_cores = false\n",
        encoding="utf-8",
    )
    (project_dir / "cores/demo.core").write_text(
        "CAPI=2:\nname: booley::demo:0\ntargets: {}\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("redacted fixture\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")

    tickets = TicketIO(project_dir / "tickets", project_root=root)
    created = tickets.create_ticket_file(
        "generated-input",
        TicketFileSpec(
            summary="Accept generated input",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert created is not None
    assert tickets.enqueue_ticket("generated-input") is True
    workspace = project_dir / "worktrees/generated-input"
    return TicketContext(
        slug="generated-input",
        ticket_path=project_dir / "tickets/board/queue/generated-input.md",
        ticket_type="feature",
        branch="main",
        summary="Accept generated input",
        project_root=root,
        acceptance_basis=tickets.load_basis("generated-input"),
        worktree_path=workspace,
    )


def test_generated_projection_is_stable_and_drift_is_blocked_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real setup and handoff callers must agree across repeated runs."""
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    block = MagicMock()
    monkeypatch.setattr(developer, "block_ticket", block)

    for run_index in (1, 2):
        reset_cache()
        root = tmp_path / f"run-{run_index}"
        root.mkdir()
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "user.email", "test@example.invalid")
        ctx = _ticket_context(root)
        reconcile_projected_cores(ctx.work_dir)

        assert _validate_materialized_acceptance_basis(ctx, ctx.work_dir) is None
        assert developer._block_changed_acceptance_basis(ctx, run_index) is False

        projection = ctx.work_dir / ".booley-projected-demo.core"
        projection.write_text(
            projection.read_text(encoding="utf-8") + "# drift\n",
            encoding="utf-8",
        )
        setup_result = _validate_materialized_acceptance_basis(ctx, ctx.work_dir)
        assert setup_result is not None
        assert setup_result.block_reason.count("acceptance-input-change-required") == 1
        assert developer._block_changed_acceptance_basis(ctx, run_index) is True

    assert block.call_count == 2
    assert all(
        call.args[1].count("acceptance-input-change-required") == 1
        for call in block.call_args_list
    )
