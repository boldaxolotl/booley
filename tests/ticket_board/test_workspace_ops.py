"""Public workspace contracts plus deterministic Git rollback fault injection.

Direct private-helper tests are limited to attachment/rollback checkpoints whose
ambiguous partial states cannot be reproduced safely through a full public workflow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board import (
    workspace_ops,
)
from booley.ticket_board.ticket_baseline import (
    BasisParticipant,
    TicketBaseline,
)


def _completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _attachment(tmp_path: Path) -> workspace_ops._OpenAttachment:
    return workspace_ops._OpenAttachment(
        tmp_path / "repository",
        tmp_path / "worktree",
        "ticket",
        "a" * 40,
    )


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def _draft_ticket(*, extra: str = "") -> str:
    return (
        "---\nsummary: Ticket\ntype: feature\nbranch: main\nscope: []\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: done}}}\n"
        f"{extra}"
        "---\n## Description\nCheck the ticket.\n"
    )


def _authoring_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    project_data = tmp_path / "project-data"
    outer = project_data / "worktrees/ticket"
    outer.mkdir(parents=True)
    ticket = root / "ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nbranch: main\nscope: []\ncriteria: {}\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: project_data)
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda _root: None)
    monkeypatch.setattr(workspace_ops, "load_basis_publication", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_pin_authoring_bases", lambda *_args: ("a" * 40, ""))
    monkeypatch.setattr(
        workspace_ops,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        "booley.runtime.submodule_materialization.materialize_project_submodules",
        lambda *_args: None,
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    return root, ticket, outer


def _reset_plan(
    tmp_path: Path,
    basis: TicketBaseline,
    destination: Path,
) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path.resolve(),
        basis_id=basis.basis_id,
        requested_branch="main",
        project_source=None,
        outer_worktree=destination,
        paired_worktree=None,
        participants=(),
    )


def _capture_reset_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[object, ...]]:
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_remove_authoring_worktrees", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "validate_basis_refs", lambda *_args, **_kwargs: [])
    commands: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *args, **_kwargs: commands.append(args) or "",
    )
    return commands


def test_registration_and_attachment_checkpoint_success_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    assert workspace_ops._registered_worktree(tmp_path, destination) is False

    attachment = _attachment(tmp_path / "success")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    workspace_ops._create_attachment(attachment)
    assert attachment.worktree_attached is True


def test_authoring_change_validation_rejects_non_acceptance_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, _outer = _authoring_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repository: ["rtl/design.sv"])
    monkeypatch.setattr(
        workspace_ops,
        "_local_manifest_paths",
        lambda _surface, _project_repository: set(),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="non-authoring changes"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")


def test_authoring_change_validation_rejects_modified_protected_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, outer = _authoring_workspace(tmp_path, monkeypatch)
    source = outer / "rtl/design.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module changed; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repository: ["rtl/design.sv"])
    monkeypatch.setattr(
        workspace_ops,
        "_local_manifest_paths",
        lambda _surface, _project_repository: {"rtl/design.sv"},
    )
    monkeypatch.setattr(workspace_ops, "_git", lambda *_args, **_kwargs: _completed("git"))

    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="non-authoring"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")


def test_authoring_change_validation_rejects_unscoped_empty_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, outer = _authoring_workspace(tmp_path, monkeypatch)
    source = outer / "rtl/new_design.sv"
    source.parent.mkdir(parents=True)
    source.touch()
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repo: ["rtl/new_design.sv"])
    monkeypatch.setattr(
        workspace_ops,
        "_local_manifest_paths",
        lambda _surface, _project: {"rtl/new_design.sv"},
    )
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )

    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="non-authoring"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")


def test_authoring_path_allows_untracked_scoped_empty_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placeholder = tmp_path / "rtl/new_design.sv"
    placeholder.parent.mkdir()
    placeholder.touch()
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )

    assert workspace_ops._is_authoring_path(
        tmp_path, "rtl/new_design.sv", set(), ["rtl/new_design.sv [new]"]
    )


def test_staged_tree_captures_ignored_controls_and_skips_lexical_roots(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / ".gitignore").write_text("coverage-waivers/\nproofs/\n", encoding="utf-8")
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    parent = _git(repository, "rev-parse", "HEAD")
    approval = repository / "coverage-waivers/rtl/counter.sv.toml"
    approval.parent.mkdir(parents=True)
    approval.write_text("approval\n", encoding="utf-8")
    proof = repository / "proofs/counter.sby"
    proof.parent.mkdir()
    proof.write_text("proof\n", encoding="utf-8")
    (repository / "empty-waivers").mkdir()

    tree = workspace_ops._staged_tree(
        repository,
        ["coverage-waivers", "proofs/counter.sby", "empty-waivers", "missing-waivers"],
        parent,
    )

    names = _git(repository, "ls-tree", "-r", "--name-only", tree).splitlines()
    assert "coverage-waivers/rtl/counter.sv.toml" in names
    assert "proofs/counter.sby" in names
    assert "empty-waivers" not in names
    assert "missing-waivers" not in names


@pytest.mark.parametrize(
    ("path", "manifest"),
    [
        ("coverage-waivers/rtl/counter.sv.toml", {"coverage-waivers"}),
        ("proofs/counter.sby", {"proofs/counter.sby"}),
    ],
)
def test_untracked_waiver_controls_are_valid_authoring_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    manifest: set[str],
) -> None:
    candidate = tmp_path / path
    candidate.parent.mkdir(parents=True)
    candidate.write_text("policy input\n", encoding="utf-8")
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )

    assert workspace_ops._is_authoring_path(tmp_path, path, manifest, [])


def test_authoring_manifest_discovery_failure_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "acceptance_control_paths",
        lambda _root: (_ for _ in ()).throw(ValueError("unsafe waiver config")),
    )

    with pytest.raises(
        workspace_ops.TicketBaselineOperationError,
        match=r"protected-input discovery failed.*unsafe waiver config",
    ):
        workspace_ops._local_manifest_paths(tmp_path, project_repository=False)


def test_project_authoring_path_uses_outer_scope_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    placeholder = project / "rtl/new_design.sv"
    placeholder.parent.mkdir(parents=True)
    placeholder.touch()
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repo: ["rtl/new_design.sv"])
    monkeypatch.setattr(workspace_ops, "_local_manifest_paths", lambda *_args: set())
    monkeypatch.setattr(
        workspace_ops,
        "paired_project_repository",
        lambda _root: SimpleNamespace(path_prefix=".booley_project"),
    )
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )

    changes = workspace_ops._validate_authoring_changes(
        project,
        tmp_path,
        project_repository=True,
        recovery_paths=set(),
        scope=[".booley_project/rtl/new_design.sv [new]"],
    )

    assert changes == ["rtl/new_design.sv"]


def test_authoring_preparation_materializes_submodules_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    outer = tmp_path / "outer"
    ticket = tmp_path / "ticket.md"
    calls: list[str] = []
    monkeypatch.setattr(
        "booley.runtime.submodule_materialization.materialize_project_submodules",
        lambda source, destination: calls.append(f"materialize:{source}:{destination}"),
    )

    def prepare(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("prepare")
        return SimpleNamespace(ok=True, error="")

    monkeypatch.setattr(workspace_ops, "prepare_project", prepare)
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)

    workspace_ops._prepare_workspace_project(root, outer, ticket, "ticket")

    assert calls == [f"materialize:{root}:{outer}", "prepare"]


def test_changed_core_targets_report_shape_and_identity_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, outer = _authoring_workspace(tmp_path, monkeypatch)
    core = outer / "changed.core"
    core.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repository: [core.name])
    monkeypatch.setattr(
        workspace_ops, "_local_manifest_paths", lambda *_args, **_kwargs: {core.name}
    )
    monkeypatch.setattr(workspace_ops, "_baseline_surface_file", lambda *_args: None)
    monkeypatch.setattr(
        workspace_ops,
        "validate_planned_dependencies",
        lambda *_args: workspace_ops.ProviderMaterialization(),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="is not a mapping"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")

    core.write_text("targets: {}\n", encoding="utf-8")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="no valid name"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")


def test_attachment_rollback_helpers_surface_git_inspection_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    attachment.upstream_changed = True
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.TicketBaselineOperationError("git unavailable")
        ),
    )
    assert workspace_ops._restore_attachment_upstream(attachment) == "git unavailable"

    attachment.worktree_attached = True
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    assert workspace_ops._remove_attachment_worktree(attachment) == (
        ["git unavailable"],
        False,
    )

    attachment.branch_created = True
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    assert workspace_ops._delete_created_branch(attachment, False) == "git unavailable"


def test_attachment_rollback_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_remove_attachment_worktree", lambda *_args: ([], True))
    monkeypatch.setattr(workspace_ops, "_restore_attachment_upstream", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_delete_created_branch", lambda *_args, **_kwargs: None)
    assert workspace_ops._rollback_attachment(attachment) == ([], True)
    assert workspace_ops._rollback_open(attachment, None) == []


def test_public_basis_preparation_reports_git_process_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, _outer = _authoring_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workspace_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="git missing"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")


def test_strict_branch_lookup_distinguishes_missing_and_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )
    assert workspace_ops._strict_branch_sha(tmp_path, "ticket") is None
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="locked"),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="locked"):
        workspace_ops._strict_branch_sha(tmp_path, "ticket")


def test_ensure_workspace_rejects_moved_branch_and_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    ticket = root / "ticket.md"
    ticket.parent.mkdir()
    ticket.write_text(_draft_ticket(), encoding="utf-8")
    project_data = tmp_path / "project-data"
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: project_data)
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)

    def moved_generation_branch(_repository: Path, branch: str) -> str:
        return "a" * 40 if branch == "main" else "b" * 40

    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", moved_generation_branch)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="already points"):
        workspace_ops.ensure_ticket_workspace(root, ticket, "ticket")

    def only_destination_branch(_repository: Path, branch: str) -> str | None:
        return "a" * 40 if branch == "main" else None

    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", only_destination_branch)
    existing = project_data / "worktrees/ticket"
    existing.parent.mkdir(parents=True)
    existing.symlink_to("missing")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="already exists"):
        workspace_ops.ensure_ticket_workspace(root, ticket, "ticket")


def test_reset_worktrees_reuses_matching_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "worktrees/ticket"
    basis = TicketBaseline((_participant(),))
    plan = _reset_plan(tmp_path, basis, destination)
    commands = _capture_reset_commands(monkeypatch)
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "a" * 40)
    workspace_ops.reset_basis_worktrees(tmp_path, "ticket", basis, "main", plan=plan)
    branch = basis.participant("outer").ticket_ref.removeprefix("refs/heads/")
    assert commands[-1][-4:] == ("worktree", "add", str(destination), branch)


def test_reset_worktrees_creates_new_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "worktrees/second"
    participant = BasisParticipant(
        "outer",
        "a" * 40,
        "refs/heads/booley-generation/0123456789abcdef/second",
        "refs/heads/main",
        "b" * 40,
    )
    basis = TicketBaseline((participant,))
    plan = _reset_plan(tmp_path, basis, destination)
    commands = _capture_reset_commands(monkeypatch)
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "")
    workspace_ops.reset_basis_worktrees(tmp_path, "ticket", basis, "main", plan=plan)
    assert commands[-1][-6:] == (
        "worktree",
        "add",
        "-b",
        participant.ticket_ref.removeprefix("refs/heads/"),
        str(destination),
        "a" * 40,
    )


def test_current_and_project_branch_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(_draft_ticket(), encoding="utf-8")
    prepared = SimpleNamespace(
        outer=tmp_path,
        project=None,
        outer_changes=[],
        project_changes=[],
        outer_base_sha="a" * 40,
        project_base_sha="",
    )
    monkeypatch.setattr(workspace_ops, "load_basis_publication", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_prepare_basis", lambda *_args, **_kwargs: prepared)
    monkeypatch.setattr(workspace_ops, "_prepare_basis_inputs", lambda *_args: ((), ()))
    monkeypatch.setattr(workspace_ops, "_staged_tree", lambda *_args: "b" * 40)
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(workspace_ops, "authored_ticket_digest", lambda *_args: "digest")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="detached"):
        workspace_ops.prepare_ticket_baseline(tmp_path, ticket, "ticket")

    project = tmp_path / "project"
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: project)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="does not exist"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_preflight_project_repository_validates_destination_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    ticket = tmp_path / "ticket.md"
    ticket.write_text(_draft_ticket(extra="project_destination_ref: main\n"), encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="full refs/heads"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_registered_worktree_and_branch_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda _repo, *args, **_kwargs: (
            f"worktree {tmp_path / 'other'}\n\nworktree {worktree}\n"
            if args == ("worktree", "list", "--porcelain")
            else "ticket"
        ),
    )
    assert workspace_ops._registered_worktree(tmp_path, worktree) is True
    assert workspace_ops._worktree_owns_branch(tmp_path, worktree, "ticket") is True
    assert workspace_ops._worktree_owns_branch(tmp_path, tmp_path / "missing", "ticket") is False


def test_create_attachment_retains_unconfirmed_created_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    responses = iter([None, "a" * 40])
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: next(responses))
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.TicketBaselineOperationError("update failed")
        ),
    )

    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="retained"):
        workspace_ops._create_attachment(attachment)


def test_create_attachment_records_partial_path_and_moved_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)

    def fail_add(*_args: object, **_kwargs: object) -> str:
        attachment.worktree.mkdir()
        raise workspace_ops.TicketBaselineOperationError("attach failed")

    monkeypatch.setattr(workspace_ops, "_require_git", fail_add)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="attach failed"):
        workspace_ops._create_attachment(attachment)
    assert attachment.partial_path is True

    attachment = _attachment(tmp_path / "moved")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="moved"):
        workspace_ops._create_attachment(attachment)


def test_upstream_change_tracks_ambiguous_success_and_restores_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    upstreams = iter([None, "main"])
    monkeypatch.setattr(workspace_ops, "_branch_upstream", lambda *_args: next(upstreams))
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.TicketBaselineOperationError("set failed")
        ),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="set failed"):
        workspace_ops._set_attachment_upstream(attachment, "main")
    assert attachment.upstream_changed is True

    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="unset failed"),
    )
    assert "could not restore upstream" in workspace_ops._restore_attachment_upstream(attachment)


def test_upstream_noop_and_successful_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_branch_upstream", lambda *_args: "main")
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *_args, **_kwargs: pytest.fail("matching upstream must be a no-op"),
    )
    workspace_ops._set_attachment_upstream(attachment, "main")
    assert attachment.upstream_changed is False

    attachment.upstream_changed = True
    attachment.previous_upstream = "old"
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git"),
    )
    assert workspace_ops._restore_attachment_upstream(attachment) is None
    attachment.branch_created = True
    assert workspace_ops._restore_attachment_upstream(attachment) is None


def test_remove_attachment_handles_ambiguous_partial_and_failed_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    failures, clear = workspace_ops._remove_attachment_worktree(attachment)
    assert failures == [f"retained ambiguously created worktree {attachment.worktree}"]
    assert clear is False

    attachment.worktree.mkdir()
    attachment.partial_path = True
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: False)
    failures, clear = workspace_ops._remove_attachment_worktree(attachment)
    assert failures == [] and clear is True
    assert not attachment.worktree.exists()

    attachment.worktree_attached = True
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1, stderr="busy"),
    )
    failures, clear = workspace_ops._remove_attachment_worktree(attachment)
    assert "busy" in failures[0] and clear is False


def test_created_branch_cleanup_retains_moved_ref_and_reports_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    attachment.branch_created = True
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "b" * 40)
    assert "retained moved branch" in workspace_ops._delete_created_branch(attachment, False)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1, stderr="locked"),
    )
    assert "could not delete" in workspace_ops._delete_created_branch(attachment, False)


def test_created_branch_cleanup_handles_lookup_failure_and_absent_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    assert workspace_ops._delete_created_branch(attachment, False) is None
    attachment.branch_created = True
    assert workspace_ops._delete_created_branch(attachment, True) is None
    monkeypatch.setattr(
        workspace_ops,
        "_strict_branch_sha",
        lambda *_args: (_ for _ in ()).throw(
            workspace_ops.TicketBaselineOperationError("cannot inspect")
        ),
    )
    assert workspace_ops._delete_created_branch(attachment, False) == "cannot inspect"


def test_rollback_open_retains_outer_when_paired_cleanup_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _attachment(tmp_path / "outer")
    project = _attachment(tmp_path / "project")
    monkeypatch.setattr(
        workspace_ops,
        "_rollback_attachment",
        lambda attachment: (
            (["project failed"], False)
            if attachment is project
            else pytest.fail("outer must be retained")
        ),
    )

    assert workspace_ops._rollback_open(outer, project) == [
        "project failed",
        f"retained outer worktree {outer.worktree} with paired state",
    ]


def test_draft_generation_rejects_invalid_and_conflicting_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    ticket = tmp_path / "board" / "drafts" / "ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "---\nsummary: Ticket\ntype: feature\nbranch: main\nscope: []\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: done}}}\n"
        "---\n## Description\nCheck the ticket.\n",
        encoding="utf-8",
    )
    path = tmp_path / ".runtime/acceptance/drafts/ticket.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="invalid"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")
    path.unlink()
    monkeypatch.setattr(
        workspace_ops,
        "atomic_write_once",
        lambda *_args: (_ for _ in ()).throw(workspace_ops.WriteOnceConflictError("race")),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="conflicting"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_draft_generation_reuses_valid_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    ticket = tmp_path / "board" / "drafts" / "ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "---\nsummary: Ticket\ntype: feature\nbranch: main\nscope: []\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: done}}}\n"
        "---\n## Description\nCheck the ticket.\n",
        encoding="utf-8",
    )
    path = tmp_path / ".runtime/acceptance/drafts/ticket.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"generation": "0123456789abcdef"}\n', encoding="utf-8")
    expected = workspace_ops.AuthoringWorkspace(
        tmp_path / "outer", None, "a" * 40, "", "0123456789abcdef"
    )
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path)
    monkeypatch.setattr(workspace_ops, "open_authoring_generation", lambda *_args: expected)
    assert workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket") == expected


def test_ensure_ticket_workspace_rejects_bad_slug_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\nsummary: Ticket\ntype: feature\nbranch: ''\nscope: []\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: done}}}\n"
        "---\n## Description\nCheck the ticket.\n",
        encoding="utf-8",
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="unsafe"):
        workspace_ops.ensure_ticket_workspace(tmp_path, "missing", "bad/slug")
    monkeypatch.setattr(workspace_ops, "_draft_generation", lambda *_args: "0" * 16)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="branch"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_workspace_preparation_and_status_fail_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, _outer = _authoring_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workspace_ops,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="hook failed"),
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: True)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="hook failed"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")
    monkeypatch.setattr(
        workspace_ops,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="status failed"),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="status failed"):
        workspace_ops.prepare_ticket_baseline(root, ticket, "ticket")


def test_reset_project_source_validation_rejects_repository_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = BasisParticipant("outer", "a" * 40, "refs/heads/ticket", "refs/heads/main", "b" * 40)
    project = BasisParticipant(
        "project", "c" * 40, "refs/heads/ticket", "refs/heads/main", "d" * 40
    )

    paired = TicketBaseline((outer, project))
    native = TicketBaseline((outer,))
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="unavailable"):
        workspace_ops.preflight_basis_reset(tmp_path, "ticket", paired, "main")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="no project"):
        workspace_ops.preflight_basis_reset(tmp_path, "ticket", native, "main")
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "c" * 40)
    monkeypatch.setattr(
        workspace_ops,
        "pin_basis_refs",
        lambda *_args, **_kwargs: {"outer": "a" * 40, "project": "c" * 40},
    )
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    plan = workspace_ops.preflight_basis_reset(tmp_path, "ticket", paired, "main")
    assert plan.project_source == tmp_path


def test_missing_refresh_workspace_rejects_advanced_execution_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "source.txt").write_text("basis\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "basis")
    authoring = _git(repository, "rev-parse", "HEAD")
    ticket_branch = "booley-generation/0123456789abcdef/ticket"
    _git(repository, "switch", "-qc", ticket_branch)
    (repository / "source.txt").write_text("executed\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "execution")
    _git(repository, "switch", "-q", "main")
    basis = TicketBaseline(
        (
            BasisParticipant(
                "outer",
                authoring,
                f"refs/heads/{ticket_branch}",
                "refs/heads/main",
                authoring,
            ),
        )
    )
    monkeypatch.setattr(
        workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "project-data"
    )

    with pytest.raises(
        workspace_ops.TicketBaselineOperationError,
        match="acceptance-input-change-required",
    ):
        workspace_ops.load_refresh_source_workspace(
            repository, basis, "ticket", tmp_path / "operation"
        )


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("new.core", "CAPI=2:\nname: acme:lib:new:1.0\ntargets: {}\n"),
        ("rtl/new.sv", "module new; endmodule\n"),
    ],
)
def test_authoring_basis_rejects_commits_beyond_destination(
    tmp_path: Path, path: str, content: str
) -> None:
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    _git(repository, "worktree", "add", "-qb", "ticket", str(worktree), "main")
    authored = worktree / path
    authored.parent.mkdir(parents=True, exist_ok=True)
    authored.write_text(content, encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "unauthorized authoring commit")

    with pytest.raises(
        workspace_ops.TicketBaselineOperationError,
        match="commits beyond the destination baseline",
    ):
        workspace_ops._pin_authoring_bases(repository, worktree, None, {"branch": "main"})


def test_refresh_workspace_relocation_and_discard_cover_owned_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    project_data = tmp_path / "project-data"
    operation = tmp_path / "operation"
    outer = operation / "new-outer"
    outer.mkdir(parents=True)
    workspace = workspace_ops.AuthoringWorkspace(outer, None, "a" * 40, "", "0" * 16)
    commands: list[tuple[object, ...]] = []
    removals: list[Path] = []
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: project_data)
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(workspace_ops, "_worktree_owns_branch", lambda *_args: False)
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda _outer: None)
    monkeypatch.setattr(
        workspace_ops, "_require_git", lambda *args, **_kwargs: commands.append(args) or ""
    )

    workspace_ops.relocate_refresh_workspace(
        root, "ticket", "0" * 16, operation, workspace, has_project=False
    )

    assert commands[-1][-4:] == (
        "worktree",
        "move",
        str(outer),
        str(project_data / "worktrees/ticket"),
    )

    project_source = tmp_path / "project-source"
    holding = operation / "new-project-moving"
    holding.mkdir(parents=True)
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: project_source)
    monkeypatch.setattr(workspace_ops, "_worktree_owns_branch", lambda *_args: True)
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    monkeypatch.setattr(
        workspace_ops,
        "_remove_authoring_worktrees",
        lambda _root, candidate, *_args: removals.append(candidate),
    )

    repositories = workspace_ops.discard_refresh_workspace(root, "ticket", "0" * 16, operation)

    assert repositories == {"outer": root, "project": project_source}
    assert removals == [project_data / "worktrees/ticket", operation / "new-outer"]
    assert commands[-1][-4:] == ("worktree", "remove", "--force", str(holding))


def test_refresh_project_staging_restoration_and_participation_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    paired = SimpleNamespace(worktree=tmp_path / "paired")
    workspace = workspace_ops.AuthoringWorkspace(outer, paired.worktree, "a" * 40, "b" * 40)
    holding = tmp_path / "holding"
    project_source = tmp_path / "project-source"
    commands: list[tuple[object, ...]] = []
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda _outer: paired)
    monkeypatch.setattr(
        workspace_ops, "_require_git", lambda *args, **_kwargs: commands.append(args) or ""
    )

    workspace_ops._stage_refresh_project(workspace, project_source, holding)
    holding.mkdir()
    workspace_ops._restore_refresh_project(outer, project_source, holding)

    assert commands[0][-4:] == ("worktree", "move", str(paired.worktree), str(holding))
    assert commands[1][-4:] == (
        "worktree",
        "move",
        str(holding),
        str(workspace_ops.ticket_project_worktree(outer)),
    )

    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="unavailable"):
        workspace_ops._stage_refresh_project(workspace, None, tmp_path / "other")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="unavailable"):
        workspace_ops._restore_refresh_project(outer, None, holding)


def test_refresh_relocation_rejects_participant_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    operation = tmp_path / "operation"
    canonical = tmp_path / "data/worktrees/ticket"
    canonical.mkdir(parents=True)
    workspace = workspace_ops.AuthoringWorkspace(tmp_path / "new", None, "a" * 40, "", "0" * 16)
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(workspace_ops, "_worktree_owns_branch", lambda *_args: True)
    monkeypatch.setattr(workspace_ops, "_restore_refresh_project", lambda *_args: None)
    monkeypatch.setattr(
        workspace_ops,
        "paired_project_repository",
        lambda _outer: SimpleNamespace(worktree=tmp_path / "project"),
    )

    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="participation"):
        workspace_ops.relocate_refresh_workspace(
            root, "ticket", "0" * 16, operation, workspace, has_project=False
        )

    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda _outer: None)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="unavailable"):
        workspace_ops.relocate_refresh_workspace(
            root, "ticket", "0" * 16, operation, workspace, has_project=True
        )


def test_discard_generation_refs_handles_absent_invalid_and_owned_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    deleted: list[tuple[object, ...]] = []
    responses = iter(
        [
            _completed("git", returncode=1),
            _completed("git", stdout="a" * 40 + "\n"),
        ]
    )
    monkeypatch.setattr(workspace_ops, "_git", lambda *_args: next(responses))
    monkeypatch.setattr(
        workspace_ops, "_require_git", lambda *args, **_kwargs: deleted.append(args) or ""
    )

    workspace_ops.discard_generation_refs({"outer": first, "project": second}, "ticket", "0" * 16)

    assert deleted[0][1:3] == ("update-ref", "-d")

    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args: _completed("git", returncode=2, stderr="locked"),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="locked"):
        workspace_ops.discard_generation_refs({"outer": first}, "ticket", "0" * 16)


def test_load_refresh_source_rebuilds_invalid_cached_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    operation = tmp_path / "operation"
    checkout = operation / "old-basis"
    checkout.mkdir(parents=True)
    basis = TicketBaseline((_participant(),))
    expected = workspace_ops.AuthoringWorkspace(checkout, None, "a" * 40, "")
    calls: list[str] = []
    attempts = iter((False, True))
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    monkeypatch.setattr(workspace_ops, "_require_unexecuted_refresh_refs", lambda *_args: None)
    monkeypatch.setattr(
        workspace_ops, "safe_rmtree", lambda path, **_kwargs: calls.append(f"rm:{path}")
    )
    monkeypatch.setattr(
        workspace_ops,
        "materialize_basis_checkout",
        lambda *_args: calls.append("materialize"),
    )

    def load_workspace(*_args: object) -> workspace_ops.AuthoringWorkspace:
        if not next(attempts):
            raise workspace_ops.TicketBaselineOperationError("stale")
        return expected

    monkeypatch.setattr(workspace_ops, "_workspace_from_basis_checkout", load_workspace)

    assert (
        workspace_ops.load_refresh_source_workspace(root, basis, "ticket", operation) == expected
    )
    assert calls == [f"rm:{checkout}", "materialize"]


def test_workspace_from_basis_checkout_validates_participants_and_pristine_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    project = tmp_path / "project"
    paired = SimpleNamespace(worktree=project)
    basis = TicketBaseline((_participant(), _participant("project")))
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda _outer: paired)
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda repository, *args: (
            ""
            if args[0] == "status"
            else basis.participant("project" if repository == project else "outer").authoring_sha
        ),
    )

    result = workspace_ops._workspace_from_basis_checkout(tmp_path, outer, basis)

    assert result.project == project
    assert result.project_base_sha == basis.participant("project").destination_sha

    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda _outer: None)
    monkeypatch.setattr(
        workspace_ops, "checkout_project_dir_relative_to", lambda _root: Path("project")
    )
    changed = TicketBaseline((_participant(), _participant("zeta")))
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="participants changed"):
        workspace_ops._workspace_from_basis_checkout(tmp_path, outer, changed)

    native = TicketBaseline((_participant(),))
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda _repository, *args: "dirty" if args[0] == "status" else "a" * 40,
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="not pristine"):
        workspace_ops._workspace_from_basis_checkout(tmp_path, outer, native)


def test_prepare_replacement_basis_resumes_or_starts_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\nmachine: {}\n---\nbody\n", encoding="utf-8")
    spec = SimpleNamespace(semantic_digest=lambda: "a" * 64)
    document = SimpleNamespace(spec=spec)
    monkeypatch.setattr(workspace_ops, "_replacement_inputs", lambda *_args: (document, "source"))
    workspace = workspace_ops.AuthoringWorkspace(tmp_path / "outer", None, "a" * 40, "", "0" * 16)
    publication = SimpleNamespace(operation_id="operation")
    published: list[tuple[object, ...]] = []
    monkeypatch.setattr(workspace_ops, "load_basis_publication", lambda *_args: publication)
    monkeypatch.setattr(
        workspace_ops,
        "publish_ticket_commits",
        lambda *args, **kwargs: (
            published.append((args, kwargs)) or (TicketBaseline((_participant(),)), "sha")
        ),
    )

    result = workspace_ops.prepare_replacement_ticket_baseline(
        tmp_path, ticket, "ticket", workspace, (), operation_id="operation"
    )

    assert result[1] == "sha"
    assert published[-1][1] == {}

    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="operation IDs"):
        workspace_ops.prepare_replacement_ticket_baseline(
            tmp_path, ticket, "ticket", workspace, (), operation_id="other"
        )

    prepared = SimpleNamespace(
        target_plan=SimpleNamespace(removal_targets=("old",)),
        providers=SimpleNamespace(bindings=()),
    )
    monkeypatch.setattr(workspace_ops, "load_basis_publication", lambda *_args: None)
    prepared.fields = {"branch": "main"}
    prepared.outer = tmp_path / "outer"
    monkeypatch.setattr(
        workspace_ops, "_prepare_converted_basis", lambda *_args, **_kwargs: (prepared, spec)
    )
    monkeypatch.setattr(
        workspace_ops, "canonical_acceptance_bindings", lambda *_args: ("binding",)
    )
    monkeypatch.setattr(workspace_ops, "criterion_targets_from_spec", lambda *_args: ())
    monkeypatch.setattr(
        workspace_ops, "_participant_preparations", lambda *_args: ("participant",)
    )

    workspace_ops.prepare_replacement_ticket_baseline(
        tmp_path, ticket, "ticket", workspace, (), operation_id="new-operation"
    )

    request = published[-1][0][1]
    assert request.operation_id == "new-operation"
    assert request.bindings == ("binding",)
    assert request.removal_targets == ("old",)


def test_branch_preflight_rejects_git_failure_and_conflicting_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="locked"),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="locked"):
        workspace_ops._strict_branch_sha(tmp_path, "ticket")

    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="already points"):
        workspace_ops._attach_worktree(tmp_path, tmp_path / "new", "ticket", "main")
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "a" * 40)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="path already exists"):
        workspace_ops._attach_worktree(tmp_path, tmp_path, "ticket", "main")


def test_open_preflight_rejects_moved_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "changed")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="destination branch"):
        workspace_ops._validate_open_bases(tmp_path, "main", "original", None)
    project = workspace_ops._ProjectOpenPlan(tmp_path / "project", "main", "original")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "original")
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "changed")
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="paired project destination"
    ):
        workspace_ops._validate_open_bases(tmp_path, "main", "original", project)


@pytest.mark.parametrize("content", ["not JSON", '{"generation": "short"}'])
def test_draft_generation_rejects_corrupt_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    descriptor = tmp_path / "generation.json"
    descriptor.write_text(content, encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "_generation_file", lambda *_args: descriptor)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="invalid draft"):
        workspace_ops._draft_generation(tmp_path, "ticket")


def test_open_authoring_requires_destination_branch(tmp_path: Path) -> None:
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="destination branch"):
        workspace_ops.open_authoring_generation(
            tmp_path, tmp_path / "ticket.md", "ticket", {}, "1" * 16, tmp_path / "outer"
        )


def test_refresh_materialization_failure_remains_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.ticket_board.ticket_baseline import TicketBaselineError

    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path)
    monkeypatch.setattr(workspace_ops, "_require_unexecuted_refresh_refs", lambda *_args: None)
    monkeypatch.setattr(
        workspace_ops,
        "materialize_basis_checkout",
        lambda *_args: (_ for _ in ()).throw(TicketBaselineError("commit unavailable")),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="commit unavailable"):
        workspace_ops.load_refresh_source_workspace(
            tmp_path, TicketBaseline((_participant(),)), "ticket", tmp_path / "operation"
        )


@pytest.mark.parametrize("returncode", [1, 2])
def test_ancestor_check_rejects_non_ancestor_and_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args: _completed("git", returncode=returncode, stderr="git failed"),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match=r"moved|git failed"):
        workspace_ops._require_ancestor(tmp_path, "old", "new", "destination moved")


def test_baseline_preparation_requires_open_workspace_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="not open"):
        workspace_ops._prepare_basis(tmp_path, ticket, "ticket")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="destination branch"):
        workspace_ops._pin_authoring_bases(tmp_path, tmp_path, None, {})


def test_attachment_rollback_reports_registration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "_registered_worktree",
        lambda *_args: (_ for _ in ()).throw(
            workspace_ops.TicketBaselineOperationError("worktree list failed")
        ),
    )
    failures, clear = workspace_ops._remove_attachment_worktree(_attachment(tmp_path))
    assert failures == ["worktree list failed"]
    assert clear is False


def test_resume_rejects_wrong_paired_repository_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paired = SimpleNamespace(worktree=tmp_path / "paired")
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda *_args: paired)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="unexpected paired"):
        workspace_ops._resume_project_attachment(
            tmp_path, tmp_path / "ticket.md", "ticket", tmp_path, "ticket-ref", None
        )
    monkeypatch.setattr(workspace_ops, "_worktree_owns_branch", lambda *_args: False)
    project = workspace_ops._ProjectOpenPlan(tmp_path / "repository", "main", "a" * 40)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="wrong branch"):
        workspace_ops._resume_project_attachment(
            tmp_path, tmp_path / "ticket.md", "ticket", tmp_path, "ticket-ref", project
        )


def test_resume_reports_incomplete_project_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = workspace_ops._ProjectOpenPlan(tmp_path / "repository", "main", "a" * 40)
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda *_args: None)
    monkeypatch.setattr(
        workspace_ops, "_project_open_attachment", lambda *_args: _attachment(tmp_path)
    )
    monkeypatch.setattr(
        workspace_ops,
        "_create_attachment",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("attach failed")),
    )
    monkeypatch.setattr(
        workspace_ops, "_rollback_attachment", lambda *_args: (["remove failed"], False)
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="rollback incomplete"):
        workspace_ops._resume_project_attachment(
            tmp_path, tmp_path / "ticket.md", "ticket", tmp_path, "ticket-ref", project
        )


def test_refresh_relocation_rejects_missing_checkout_and_repository_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    operation = tmp_path / "operation"
    workspace = workspace_ops.AuthoringWorkspace(
        tmp_path / "missing", None, "a" * 40, "", "1" * 32
    )
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_stage_refresh_project", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "paired_project_repository", lambda *_args: None)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="disappeared"):
        workspace_ops.relocate_refresh_workspace(
            root, "ticket", "1" * 32, operation, workspace, has_project=False
        )
    workspace.outer.mkdir()
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args: "")
    monkeypatch.setattr(workspace_ops, "_restore_refresh_project", lambda *_args: None)
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="paired project workspace"
    ):
        workspace_ops.relocate_refresh_workspace(
            root, "ticket", "1" * 32, operation, workspace, has_project=True
        )


def test_pinning_rejects_repository_roles_and_destination_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda *_args: None)
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="participants do not match"
    ):
        workspace_ops.pin_basis_refs(
            tmp_path,
            TicketBaseline((_participant(), _participant("project"))),
            slug="ticket",
            destination_branch="main",
        )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="outer destination"):
        workspace_ops._validate_basis_participant(
            tmp_path,
            _participant(),
            slug="ticket",
            destination_branch="other",
            exact_ticket_head=False,
            exact_destination_head=False,
        )


def test_reset_rejects_stale_plan_and_wrong_worktree_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = TicketBaseline((_participant(),))
    plan = _reset_plan(tmp_path, basis, tmp_path / "destination")
    plan.basis_id = "stale"
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="does not match"):
        workspace_ops.reset_basis_worktrees(tmp_path, "ticket", basis, "main", plan=plan)
    outer = tmp_path / "outer"
    outer.mkdir()
    monkeypatch.setattr(workspace_ops, "_worktree_owns_branch", lambda *_args: False)
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="does not own"):
        workspace_ops._validate_reset_worktrees(tmp_path, None, outer, None, basis)
    paired = SimpleNamespace(worktree=tmp_path / "paired")
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="project repository is unavailable"
    ):
        workspace_ops._validate_reset_worktrees(tmp_path, None, tmp_path / "absent", paired, basis)


def test_reset_rejects_missing_project_repository(tmp_path: Path) -> None:
    basis = TicketBaseline((_participant(), _participant("project")))
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="project repository is unavailable"
    ):
        workspace_ops._reset_participants(tmp_path, None, basis, {"outer": "a" * 40})


def test_pinning_rejects_uncommitted_paired_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda repository, ref: "a" * 40)
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="project_destination_ref"
    ):
        workspace_ops._pin_authoring_bases(
            tmp_path, tmp_path / "outer", tmp_path / "project", {"branch": "main"}
        )
    monkeypatch.setattr(
        workspace_ops,
        "_full_commit",
        lambda repository, ref: (
            "b" * 40 if repository == tmp_path / "project" and ref == "HEAD" else "a" * 40
        ),
    )
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="contains commits beyond"
    ):
        workspace_ops._pin_authoring_bases(
            tmp_path,
            tmp_path / "outer",
            tmp_path / "project",
            {"branch": "main", "project_destination_ref": "refs/heads/project-main"},
        )


def test_surface_baseline_read_reports_git_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops, "_git", lambda *_args: _completed("git", returncode=128, stderr="tree gone")
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="tree gone"):
        workspace_ops._baseline_surface_file(tmp_path, "a" * 40, "core/toy.core")
    monkeypatch.setattr(
        workspace_ops, "_git", lambda *_args: _completed("git", stdout="core/toy.core\n")
    )
    monkeypatch.setattr(
        workspace_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["git"], 128, b"", b"blob gone"),
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="blob gone"):
        workspace_ops._baseline_surface_file(tmp_path, "a" * 40, "core/toy.core")


def test_publication_preparation_requires_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = SimpleNamespace(project=tmp_path / "project")
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="destination branch"):
        workspace_ops._participant_preparations("ticket", {}, prepared)
    monkeypatch.setattr(workspace_ops, "_staged_tree", lambda *_args: "a" * 40)
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="project_destination_ref"
    ):
        workspace_ops._project_participant_preparation(
            "ticket",
            {},
            SimpleNamespace(project=tmp_path, project_changes=[], project_base_sha="a" * 40),
        )


def test_reset_reports_failed_postcondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = TicketBaseline((_participant(),))
    plan = _reset_plan(tmp_path, basis, tmp_path / "outer")
    monkeypatch.setattr(workspace_ops, "_remove_authoring_worktrees", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_attach_worktree", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_restore_project_workspace", lambda *_args: None)
    monkeypatch.setattr(
        workspace_ops, "validate_basis_refs", lambda *_args, **_kwargs: ["head moved"]
    )
    with pytest.raises(workspace_ops.TicketBaselineOperationError, match="head moved"):
        workspace_ops._apply_basis_reset(plan, basis, "ticket")


def test_reset_rejects_wrong_paired_worktree_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = TicketBaseline((_participant(), _participant("project")))
    monkeypatch.setattr(workspace_ops, "_worktree_owns_branch", lambda *_args: False)
    paired = SimpleNamespace(worktree=tmp_path / "paired")
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="paired Ticket Workspace"
    ):
        workspace_ops._validate_reset_worktrees(
            tmp_path, tmp_path / "project", tmp_path / "absent", paired, basis
        )


def test_git_transport_failure_is_reported_as_baseline_operation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git executable unavailable")),
    )
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="git executable unavailable"
    ):
        workspace_ops._git(tmp_path, "status")


def test_project_preparation_reports_offline_submodule_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import submodule_materialization

    monkeypatch.setattr(
        submodule_materialization,
        "materialize_project_submodules",
        lambda *_args: (_ for _ in ()).throw(
            submodule_materialization.SubmoduleMaterializationError("object missing")
        ),
    )
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="Submodule setup failed offline"
    ):
        workspace_ops._prepare_workspace_project(
            tmp_path, tmp_path / "outer", tmp_path / "ticket.md", "ticket"
        )


def test_paired_publication_requires_attached_project_workspace() -> None:
    with pytest.raises(
        workspace_ops.TicketBaselineOperationError, match="paired Ticket Workspace"
    ):
        workspace_ops._project_participant_preparation("ticket", {}, SimpleNamespace(project=None))
