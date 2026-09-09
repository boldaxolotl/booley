"""Regression coverage for live Acceptance Basis validation."""

from __future__ import annotations

import argparse
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from booley.flows.base import BooleyFlow, SubprocessResult
from booley.fusesoc.core_projection import (
    isolated_registry_root,
    reconcile_isolated_registry,
    reconcile_projected_cores,
)
from booley.harness import developer
from booley.harness.models import TicketContext
from booley.mcp.base import McpToolResult
from booley.runtime import runtime_context
from booley.runtime.project_dir import reset_cache
from booley.ticket_board import acceptance_basis as acceptance_basis_module
from booley.ticket_board import acceptance_validation
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
)
from booley.ticket_board.acceptance_validation import (
    assert_ticket_worktree_inputs_unchanged,
)
from booley.ticket_board.io import TicketFileSpec, TicketIO


class _AcceptanceFlow(BooleyFlow):
    name = "sim"
    description = "Acceptance guard test Flow"

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        pass

    def _build_command(self) -> list[str]:
        return []

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        return McpToolResult()


@pytest.fixture(autouse=True)
def _clear_project_dir_cache(
    monkeypatch: pytest.MonkeyPatch,
    _set_project_dir: None,
) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _project_with_projection(
    tmp_path: Path,
    *,
    ignore_native_cores: bool = False,
    tracked_projection: bool = False,
) -> tuple[Path, Path, TicketIO]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project_dir = root / ".booley_project"
    (project_dir / "tickets/board/drafts").mkdir(parents=True)
    (project_dir / "cores").mkdir()
    (root / ".gitignore").write_text("/.booley-projected-*.core\n", encoding="utf-8")
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n/tmp/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text(
        "[flows]\n[stealth]\nenabled = true\n"
        f"ignore_native_cores = {str(ignore_native_cores).lower()}\n",
        encoding="utf-8",
    )
    (project_dir / "cores/demo.core").write_text(
        "CAPI=2:\nname: booley::demo:0\ntargets: {}\n", encoding="utf-8"
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    if tracked_projection:
        (root / ".booley-projected-demo.core").write_text(
            "CAPI=2:\n"
            "# Booley stealth core projection: .booley_project/cores/demo.core\n"
            "name: booley::stale:0\n"
            "targets: {}\n",
            encoding="utf-8",
        )
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    if tracked_projection:
        _git(root, "add", "-f", ".booley-projected-demo.core")
    _git(root, "commit", "-m", "initial")
    return root, project_dir, TicketIO(project_dir / "tickets", project_root=root)


def _enqueued_projection_ticket(
    tmp_path: Path,
    *,
    ignore_native_cores: bool = False,
    tracked_projection: bool = False,
) -> tuple[Path, Path, AcceptanceBasis]:
    root, project_dir, tio = _project_with_projection(
        tmp_path,
        ignore_native_cores=ignore_native_cores,
        tracked_projection=tracked_projection,
    )
    ticket = tio.create_ticket_file(
        "generated-input",
        TicketFileSpec(
            summary="Accept generated input",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("generated-input") is True
    return root, project_dir / "worktrees/generated-input", tio.load_basis("generated-input")


def _paired_projection_ticket(tmp_path: Path) -> tuple[Path, Path, AcceptanceBasis]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text(
        "/.booley_project\n/.booley-projected-*.core\n", encoding="utf-8"
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial outer")

    project_dir = root / ".booley_project"
    (project_dir / "tickets/board/drafts").mkdir(parents=True)
    (project_dir / "cores").mkdir()
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n/tmp/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text(
        "[flows]\n[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )
    (project_dir / "cores/demo.core").write_text(
        "CAPI=2:\nname: booley::demo:0\ntargets: {}\n", encoding="utf-8"
    )
    _git(project_dir, "init", "-b", "main")
    _git(project_dir, "config", "user.name", "Test")
    _git(project_dir, "config", "user.email", "test@example.invalid")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-m", "initial project")
    tio = TicketIO(project_dir / "tickets", project_root=root)
    ticket = tio.create_ticket_file(
        "generated-input",
        TicketFileSpec(
            summary="Accept paired generated input",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("generated-input") is True
    return root, project_dir / "worktrees/generated-input", tio.load_basis("generated-input")


def _simulate_host_recorded_outer_worktree(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    workspace: Path,
    basis: AcceptanceBasis,
) -> Path:
    dot_git = workspace / ".git"
    admin_name = Path(dot_git.read_text(encoding="utf-8").partition(":")[2].strip()).name
    host_primary = root.parent / "host-only-project"
    host_worktree = host_primary / ".booley_project/worktrees/generated-input"
    dot_git.chmod(dot_git.stat().st_mode | stat.S_IWRITE)
    dot_git.replace(dot_git.with_name(".git-local"))
    dot_git.write_text(
        f"gitdir: {host_primary / '.git/worktrees' / admin_name}\n",
        encoding="utf-8",
    )
    participant = basis.participant("outer")
    listing = (
        f"worktree {host_primary}\nHEAD {_git(root, 'rev-parse', 'main')}\n"
        "branch refs/heads/main\n\n"
        f"worktree {host_worktree}\nHEAD {participant.authoring_sha}\n"
        f"branch {participant.ticket_ref}\n\n"
    )
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = kwargs.get("cwd")
        if (
            command[-3:] == ["worktree", "list", "--porcelain"]
            and cwd is not None
            and Path(str(cwd)).resolve() == root
        ):
            return subprocess.CompletedProcess(command, 0, listing, "")
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acceptance_basis_module.subprocess, "run", run)
    return host_worktree


def test_unchanged_generated_projection_passes_live_guard(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)

    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_altered_generated_projection_fails_live_guard(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    projection = workspace / ".booley-projected-demo.core"
    projection.write_text(
        projection.read_text(encoding="utf-8").replace("booley::demo:0", "booley::drift:0"),
        encoding="utf-8",
    )

    with pytest.raises(AcceptanceBasisError, match="protected path") as raised:
        assert_ticket_worktree_inputs_unchanged(root, basis, workspace)

    assert str(raised.value).count("acceptance-input-change-required") == 1


def test_missing_live_projection_is_not_candidate_drift(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)

    assert not (workspace / ".booley-projected-demo.core").exists()
    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_live_only_ordinary_core_remains_rejected(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    (workspace / "foreign.core").write_text(
        "CAPI=2:\nname: booley::foreign:0\ntargets: {}\n", encoding="utf-8"
    )

    with pytest.raises(AcceptanceBasisError, match=r"foreign\.core"):
        assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_isolated_projection_ignores_checkout_root_difference(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path, ignore_native_cores=True)
    reconcile_projected_cores(workspace)
    reconcile_isolated_registry(workspace)

    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_isolated_projection_accepts_recorded_host_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path, ignore_native_cores=True)
    reconcile_projected_cores(workspace)
    reconcile_isolated_registry(workspace)
    host_root = Path("/host/worktrees/generated-input")
    for projection in isolated_registry_root(workspace).glob("*.core"):
        projection.write_text(
            projection.read_text(encoding="utf-8").replace(str(workspace), str(host_root)),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        acceptance_basis_module,
        "_recorded_worktree_path",
        lambda *_args: host_root,
    )

    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_altered_isolated_projection_fails_live_guard(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path, ignore_native_cores=True)
    reconcile_projected_cores(workspace)
    reconcile_isolated_registry(workspace)
    projection = next(isolated_registry_root(workspace).glob("*.core"))
    projection.write_text(
        projection.read_text(encoding="utf-8").replace("booley::demo:0", "booley::drift:0"),
        encoding="utf-8",
    )

    with pytest.raises(AcceptanceBasisError, match="protected path"):
        assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_renderer_cannot_rewrite_tracked_projection(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path, tracked_projection=True)

    with pytest.raises(AcceptanceBasisError, match="renderer changed tracked"):
        assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_mismatched_live_checkout_fails_closed(tmp_path: Path) -> None:
    root, _workspace, basis = _enqueued_projection_ticket(tmp_path)

    with pytest.raises(AcceptanceBasisError, match="is not the registered worktree"):
        assert_ticket_worktree_inputs_unchanged(root, basis, root)


def test_unregistered_live_checkout_fails_closed(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    unregistered = tmp_path / "unregistered-workspace"
    workspace.rename(unregistered)
    _git(root, "worktree", "prune")

    with pytest.raises(AcceptanceBasisError, match="no registered worktree"):
        assert_ticket_worktree_inputs_unchanged(root, basis, unregistered)


def test_verified_bind_mount_alias_passes_worktree_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path, ignore_native_cores=True)
    reconcile_projected_cores(workspace)
    reconcile_isolated_registry(workspace)
    recorded = _simulate_host_recorded_outer_worktree(monkeypatch, root, workspace, basis)
    for projection in isolated_registry_root(workspace).glob("*.core"):
        projection.write_text(
            projection.read_text(encoding="utf-8").replace(str(workspace), str(recorded)),
            encoding="utf-8",
        )

    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_paired_participants_accept_root_and_isolated_projections(tmp_path: Path) -> None:
    root, workspace, basis = _paired_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    reconcile_isolated_registry(workspace)

    assert (workspace / ".booley-projected-demo.core").is_file()
    assert tuple(isolated_registry_root(workspace).glob("*.core"))
    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)


def test_external_reference_project_route_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "booley.toml").write_text(
        f'[project]\ndir = "{external.as_posix()}"\n', encoding="utf-8"
    )
    _git(root, "add", "booley.toml")
    _git(root, "commit", "-m", "external route")
    sha = _git(root, "rev-parse", "HEAD")
    ref = "refs/heads/booley-generation/0123456789abcdef/external"
    workspace = tmp_path / "workspace"
    _git(root, "worktree", "add", "-b", ref.removeprefix("refs/heads/"), str(workspace), sha)
    participant = BasisParticipant("outer", sha, ref, "refs/heads/main", sha)

    with pytest.raises(AcceptanceBasisError, match="outside"):
        assert_ticket_worktree_inputs_unchanged(
            root,
            AcceptanceBasis((participant,)),
            workspace,
        )


def test_setup_guard_uses_generated_reference_without_double_prefix(tmp_path: Path) -> None:
    from booley.harness.setup.workspace import _validate_materialized_acceptance_basis

    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    ctx = SimpleNamespace(project_root=root, acceptance_basis=basis)

    assert _validate_materialized_acceptance_basis(ctx, workspace) is None
    projection = workspace / ".booley-projected-demo.core"
    projection.write_text(projection.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    blocked = _validate_materialized_acceptance_basis(ctx, workspace)

    assert blocked is not None
    assert blocked.block_reason.count("acceptance-input-change-required") == 1


def test_flow_entry_uses_real_generated_projection_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, _basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    ticket = root / ".booley_project/tickets/board/queue/generated-input.md"
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr("booley.ticket_board.helpers.detect_project_root", lambda: root)
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
    monkeypatch.setenv("BOOLEY_SLUG", "generated-input")
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
    flow = _AcceptanceFlow()
    from booley.ticket_board.flow_execution import TicketBoardFlowExecution

    flow.execution_adapter = TicketBoardFlowExecution()
    flow.parse_args(["--target", "demo", "--work-dir", str(workspace)])

    assert flow._pre_state_gate() is None
    projection = workspace / ".booley-projected-demo.core"
    projection.write_text(projection.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    blocked = flow._pre_state_gate()

    assert blocked is not None
    assert blocked.exit_code != 0
    assert blocked.report_text.count("acceptance-input-change-required") == 1


def test_developer_handoff_uses_real_generated_projection_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    ctx = TicketContext(
        slug="generated-input",
        ticket_path=root / ".booley_project/tickets/board/queue/generated-input.md",
        ticket_type="feature",
        branch="main",
        summary="Accept generated input",
        project_root=root,
        acceptance_basis=basis,
        worktree_path=workspace,
    )

    assert developer._block_changed_acceptance_basis(ctx, run_index=1) is False
    projection = workspace / ".booley-projected-demo.core"
    projection.write_text(projection.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    block = MagicMock()
    monkeypatch.setattr(developer, "block_ticket", block)

    assert developer._block_changed_acceptance_basis(ctx, run_index=2) is True
    reason = block.call_args.args[1]
    assert reason.count("acceptance-input-change-required") == 1


def test_resumed_setup_uses_real_generated_projection_validation(tmp_path: Path) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    ctx = TicketContext(
        slug="generated-input",
        ticket_path=root / ".booley_project/tickets/board/queue/generated-input.md",
        ticket_type="feature",
        branch="main",
        summary="Accept generated input",
        project_root=root,
        acceptance_basis=basis,
        worktree_path=workspace,
    )

    assert developer._resumed_basis_failure(ctx) is None
    projection = workspace / ".booley-projected-demo.core"
    projection.write_text(projection.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    failure = developer._resumed_basis_failure(ctx)
    assert failure is not None
    assert failure.count("acceptance-input-change-required") == 1


def test_live_guard_materializes_once_without_running_project_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    destinations: list[Path] = []
    materialize = acceptance_validation.materialize_basis_checkout

    def record_materialization(
        project_root: Path | str,
        acceptance_basis: AcceptanceBasis,
        destination: Path | str,
    ) -> Path:
        destinations.append(Path(destination))
        return materialize(project_root, acceptance_basis, destination)

    monkeypatch.setattr(
        acceptance_validation,
        "materialize_basis_checkout",
        record_materialization,
    )
    monkeypatch.setattr(
        "booley.runtime.project_prepare.prepare_project",
        lambda *_args, **_kwargs: pytest.fail("live guard must not run Project Setup"),
    )

    assert_ticket_worktree_inputs_unchanged(root, basis, workspace)

    assert len(destinations) == 1
    assert not destinations[0].parent.exists()


def test_live_guard_cleans_materialized_reference_after_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, basis = _enqueued_projection_ticket(tmp_path)
    reconcile_projected_cores(workspace)
    projection = workspace / ".booley-projected-demo.core"
    projection.write_text(
        projection.read_text(encoding="utf-8") + "# drift\n",
        encoding="utf-8",
    )
    destinations: list[Path] = []
    materialize = acceptance_validation.materialize_basis_checkout

    def record_materialization(
        project_root: Path | str,
        acceptance_basis: AcceptanceBasis,
        destination: Path | str,
    ) -> Path:
        destinations.append(Path(destination))
        return materialize(project_root, acceptance_basis, destination)

    monkeypatch.setattr(
        acceptance_validation,
        "materialize_basis_checkout",
        record_materialization,
    )

    with pytest.raises(AcceptanceBasisError, match="acceptance-input-change-required"):
        assert_ticket_worktree_inputs_unchanged(root, basis, workspace)

    assert len(destinations) == 1
    assert not destinations[0].parent.exists()
