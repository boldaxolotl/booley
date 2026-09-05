"""Focused boundary tests for Acceptance Basis helper modules."""

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


def test_attach_and_registration_helpers_cover_failure_and_success_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already points"):
        workspace_ops._attach_worktree(tmp_path, destination, "ticket", "main")
    destination.mkdir()
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "a" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="path already exists"):
        workspace_ops._attach_worktree(tmp_path, destination, "ticket", "main")

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
    assert workspace_ops._is_authoring_path(tmp_path, "generated.core", set()) is True
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repository: ["rtl/design.sv"])
    monkeypatch.setattr(
        workspace_ops,
        "_local_manifest_paths",
        lambda _surface, _project_repository: set(),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="non-authoring changes"):
        workspace_ops._validate_authoring_changes(tmp_path, tmp_path, False, set())


def test_changed_core_targets_report_parse_and_identity_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "changed.core"
    core.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(
        workspace_ops.fusesoc_registry,
        "read_core",
        lambda _path: (_ for _ in ()).throw(
            workspace_ops.fusesoc_registry.FuseSocError("invalid core")
        ),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="invalid core"):
        workspace_ops._changed_targets(tmp_path, [core.name], None, [])

    monkeypatch.setattr(workspace_ops.fusesoc_registry, "read_core", lambda _path: {})
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="no valid name"):
        workspace_ops._changed_targets(tmp_path, [core.name], None, [])


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


def test_attachment_rollback_and_open_base_validation_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_remove_attachment_worktree", lambda *_args: ([], True))
    monkeypatch.setattr(workspace_ops, "_restore_attachment_upstream", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_delete_created_branch", lambda *_args, **_kwargs: None)
    assert workspace_ops._rollback_attachment(attachment) == ([], True)
    assert workspace_ops._rollback_open(attachment, None) == []

    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "b" * 40)
    with pytest.raises(
        workspace_ops.AcceptanceBasisOperationError, match="moved during preflight"
    ):
        workspace_ops._validate_open_bases(tmp_path, "main", "a" * 40, None)

    project = SimpleNamespace(source=tmp_path / "project", base_branch="main", base_sha="a" * 40)
    monkeypatch.setattr(
        workspace_ops,
        "_full_commit",
        lambda repository, _branch: "a" * 40 if repository == tmp_path else "b" * 40,
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="paired project"):
        workspace_ops._validate_open_bases(tmp_path, "main", "a" * 40, project)


def test_git_helpers_report_process_and_command_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="git missing"):
        workspace_ops._git(tmp_path, "status")
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=3, stderr="bad ref"),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="bad ref"):
        workspace_ops._require_git(tmp_path, "rev-parse", "HEAD")


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


def test_attachment_planning_rejects_moved_branch_and_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already points"):
        workspace_ops._plan_open_attachment(tmp_path, tmp_path / "new", "ticket", "a" * 40)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already exists"):
        workspace_ops._plan_open_attachment(tmp_path, existing, "ticket", "a" * 40)


def test_attach_worktree_reuses_matching_branch_or_creates_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "worktrees/ticket"
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    commands: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *args, **_kwargs: commands.append(args) or "",
    )
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "a" * 40)
    assert workspace_ops._attach_worktree(tmp_path, destination, "ticket", "main") == "a" * 40
    assert commands[-1][-4:] == ("worktree", "add", str(destination), "ticket")

    commands.clear()
    second = tmp_path / "worktrees/second"
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "")
    workspace_ops._attach_worktree(tmp_path, second, "second", "main")
    assert commands[-1][-6:] == (
        "worktree",
        "add",
        "-b",
        "second",
        str(second),
        "a" * 40,
    )


def test_current_and_project_branch_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="detached"):
        workspace_ops._current_branch(tmp_path)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="does not exist"):
        workspace_ops._project_base_branch(tmp_path, "refs/heads/main")


def test_preflight_project_repository_validates_destination_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    assert workspace_ops._preflight_project_repository(tmp_path, "main", None) is None
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="full refs/heads"):
        workspace_ops._preflight_project_repository(tmp_path, "main", 3)


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
    path = workspace_ops._generation_file(tmp_path, "ticket")
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="invalid"):
        workspace_ops._draft_generation(tmp_path, "ticket")
    path.unlink()
    monkeypatch.setattr(
        workspace_ops,
        "atomic_write_once",
        lambda *_args: (_ for _ in ()).throw(workspace_ops.WriteOnceConflictError("race")),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="conflicting"):
        workspace_ops._draft_generation(tmp_path, "ticket")


def test_draft_generation_reuses_valid_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "runtime_dir", lambda _root: tmp_path / ".runtime")
    path = workspace_ops._generation_file(tmp_path, "ticket")
    path.write_text('{"generation": "0123456789abcdef"}\n', encoding="utf-8")
    assert workspace_ops._draft_generation(tmp_path, "ticket") == "0123456789abcdef"


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
    monkeypatch.setattr(
        workspace_ops,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="hook failed"),
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: True)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="hook failed"):
        workspace_ops._prepare_workspace_project(
            tmp_path, tmp_path, tmp_path / "ticket.md", "ticket"
        )
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="status failed"),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="status failed"):
        workspace_ops._status_paths(tmp_path)


def test_reset_project_source_validation_rejects_repository_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = BasisParticipant("outer", "a" * 40, "refs/heads/ticket", "refs/heads/main", "b" * 40)
    project = BasisParticipant(
        "project", "c" * 40, "refs/heads/ticket", "refs/heads/main", "d" * 40
    )

    paired = AcceptanceBasis((outer, project))
    native = AcceptanceBasis((outer,))
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="unavailable"):
        workspace_ops._validate_reset_project_source(None, paired)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="no project"):
        workspace_ops._validate_reset_project_source(tmp_path, native)
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "c" * 40)
    workspace_ops._validate_reset_project_source(tmp_path, paired)
