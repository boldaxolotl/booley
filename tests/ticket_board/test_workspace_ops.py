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
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
)


def _completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


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
    monkeypatch.setattr(
        workspace_ops,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        "booley.runtime.submodule_materialization.materialize_ticket_submodules",
        lambda *_args: None,
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    monkeypatch.setattr(
        workspace_ops,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    return root, ticket, outer


def _reset_plan(
    tmp_path: Path,
    basis: AcceptanceBasis,
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
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="non-authoring changes"):
        workspace_ops.prepare_acceptance_basis(root, ticket, "ticket")


def test_authoring_preparation_materializes_submodules_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    outer = tmp_path / "outer"
    ticket = tmp_path / "ticket.md"
    calls: list[str] = []
    monkeypatch.setattr(
        "booley.runtime.submodule_materialization.materialize_ticket_submodules",
        lambda source, destination: calls.append(f"materialize:{source}:{destination}"),
    )

    def prepare(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("prepare")
        return SimpleNamespace(ok=True, error="")

    monkeypatch.setattr(workspace_ops, "prepare_project", prepare)
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)

    workspace_ops._prepare_workspace_project(root, outer, ticket, "ticket")

    assert calls == [f"materialize:{root}:{outer}", "prepare"]


def test_changed_core_targets_report_parse_and_identity_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ticket, outer = _authoring_workspace(tmp_path, monkeypatch)
    core = outer / "changed.core"
    core.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repository: [core.name])
    monkeypatch.setattr(
        workspace_ops, "_local_manifest_paths", lambda *_args, **_kwargs: {core.name}
    )
    monkeypatch.setattr(
        workspace_ops.fusesoc_registry,
        "read_core",
        lambda _path: (_ for _ in ()).throw(
            workspace_ops.fusesoc_registry.FuseSocError("invalid core")
        ),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="invalid core"):
        workspace_ops.prepare_acceptance_basis(root, ticket, "ticket")

    monkeypatch.setattr(workspace_ops.fusesoc_registry, "read_core", lambda _path: {})
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="no valid name"):
        workspace_ops.prepare_acceptance_basis(root, ticket, "ticket")


def test_attachment_rollback_helpers_surface_git_inspection_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    attachment.upstream_changed = True
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("git unavailable")
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
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="git missing"):
        workspace_ops.prepare_acceptance_basis(root, ticket, "ticket")


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
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="locked"):
        workspace_ops._strict_branch_sha(tmp_path, "ticket")


def test_ensure_workspace_rejects_moved_branch_and_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    ticket = root / "ticket.md"
    ticket.parent.mkdir()
    ticket.write_text("---\nbranch: main\n---\n", encoding="utf-8")
    project_data = tmp_path / "project-data"
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: project_data)
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already points"):
        workspace_ops.ensure_ticket_workspace(root, ticket, "ticket")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    existing = project_data / "worktrees/ticket"
    existing.parent.mkdir(parents=True)
    existing.symlink_to("missing")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already exists"):
        workspace_ops.ensure_ticket_workspace(root, ticket, "ticket")


def test_reset_worktrees_reuses_matching_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "worktrees/ticket"
    basis = AcceptanceBasis((_participant(),))
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
    basis = AcceptanceBasis((participant,))
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
    ticket.write_text("---\nbranch: main\n---\nbody\n", encoding="utf-8")
    prepared = SimpleNamespace(
        outer=tmp_path,
        project=None,
        outer_changes=[],
        project_changes=[],
    )
    monkeypatch.setattr(workspace_ops, "load_basis_publication", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_prepare_basis", lambda *_args: prepared)
    monkeypatch.setattr(workspace_ops, "_prepare_basis_inputs", lambda *_args: ((), ()))
    monkeypatch.setattr(workspace_ops, "_staged_tree", lambda *_args: ("a" * 40, "b" * 40))
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="detached"):
        workspace_ops.prepare_acceptance_basis(tmp_path, ticket, "ticket")

    project = tmp_path / "project"
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: project)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="does not exist"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_preflight_project_repository_validates_destination_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\nproject_destination_ref: 3\n---\n", encoding="utf-8")
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path / "data")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="full refs/heads"):
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
            workspace_ops.AcceptanceBasisOperationError("update failed")
        ),
    )

    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="retained"):
        workspace_ops._create_attachment(attachment)


def test_create_attachment_records_partial_path_and_moved_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)

    def fail_add(*_args: object, **_kwargs: object) -> str:
        attachment.worktree.mkdir()
        raise workspace_ops.AcceptanceBasisOperationError("attach failed")

    monkeypatch.setattr(workspace_ops, "_require_git", fail_add)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="attach failed"):
        workspace_ops._create_attachment(attachment)
    assert attachment.partial_path is True

    attachment = _attachment(tmp_path / "moved")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="moved"):
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
            workspace_ops.AcceptanceBasisOperationError("set failed")
        ),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="set failed"):
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
            workspace_ops.AcceptanceBasisOperationError("cannot inspect")
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
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\n", encoding="utf-8")
    path = tmp_path / ".runtime/acceptance/drafts/ticket.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="invalid"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")
    path.unlink()
    monkeypatch.setattr(
        workspace_ops,
        "atomic_write_once",
        lambda *_args: (_ for _ in ()).throw(workspace_ops.WriteOnceConflictError("race")),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="conflicting"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_draft_generation_reuses_valid_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\n", encoding="utf-8")
    path = tmp_path / ".runtime/acceptance/drafts/ticket.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"generation": "0123456789abcdef"}\n', encoding="utf-8")
    expected = workspace_ops.AuthoringWorkspace(
        tmp_path / "outer", None, "a" * 40, "", "0123456789abcdef"
    )
    monkeypatch.setattr(workspace_ops, "resolve_project_dir", lambda _root: tmp_path)
    monkeypatch.setattr(workspace_ops, "_open_generation", lambda *_args: expected)
    assert workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket") == expected


def test_ensure_ticket_workspace_rejects_bad_slug_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: ''\n---\n", encoding="utf-8")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="unsafe"):
        workspace_ops.ensure_ticket_workspace(tmp_path, "missing", "bad/slug")
    monkeypatch.setattr(workspace_ops, "_draft_generation", lambda *_args: "0" * 16)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="destination branch"):
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
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="hook failed"):
        workspace_ops.prepare_acceptance_basis(root, ticket, "ticket")
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
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="status failed"):
        workspace_ops.prepare_acceptance_basis(root, ticket, "ticket")


def test_reset_project_source_validation_rejects_repository_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = BasisParticipant("outer", "a" * 40, "refs/heads/ticket", "refs/heads/main", "b" * 40)
    project = BasisParticipant(
        "project", "c" * 40, "refs/heads/ticket", "refs/heads/main", "d" * 40
    )

    paired = AcceptanceBasis((outer, project))
    native = AcceptanceBasis((outer,))
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="unavailable"):
        workspace_ops.preflight_basis_reset(tmp_path, "ticket", paired, "main")
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="no project"):
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
