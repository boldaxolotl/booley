"""Public Ticket baseline contracts plus deterministic Git fault injection.

Direct private-helper tests are limited to worktree identity, materialization, and
generated-file comparisons whose failure states cannot be injected through a stable
public contract without performing unsafe or platform-dependent Git mutations.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from booley.fusesoc import core_projection
from booley.ticket_board import (
    acceptance_basis,
    acceptance_validation,
)
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
)


def _completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def test_path_policy_and_basis_require_supported_schema_and_outer_participant() -> None:
    with pytest.raises(AcceptanceBasisError, match="unsupported Acceptance Path Policy"):
        acceptance_basis.AcceptancePathPolicy(schema=2).discover(Path.cwd())
    with pytest.raises(AcceptanceBasisError, match="requires an outer"):
        AcceptanceBasis((_participant("project"),))
    with pytest.raises(AcceptanceBasisError, match="participants must be a list"):
        AcceptanceBasis.from_mapping({"schema": 1, "participants": {}})


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ([], "must be a mapping"),
        ({"role": "outer"}, "must contain exactly"),
        (
            {
                "role": 3,
                "authoring_sha": "a" * 40,
                "ticket_ref": "refs/heads/booley-generation/0123456789abcdef/outer",
                "destination_ref": "refs/heads/main",
                "destination_sha": "b" * 40,
            },
            "must be a non-empty string",
        ),
        ({**_participant().as_dict(), "role": "other"}, "must be outer or project"),
        ({**_participant().as_dict(), "authoring_sha": "short"}, "full Git SHAs"),
        ({**_participant().as_dict(), "ticket_ref": "refs/heads/ticket"}, "generation-qualified"),
        ({**_participant().as_dict(), "destination_ref": "main"}, "full branch ref"),
    ],
)
def test_participant_parser_rejects_malformed_rows(row: object, message: str) -> None:
    with pytest.raises(AcceptanceBasisError, match=message):
        AcceptanceBasis.from_mapping({"schema": 1, "participants": [row]})


def test_git_path_and_worktree_command_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_basis,
        "_worktree_git_command",
        lambda *_args: ["git"],
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="bad worktree"),
    )
    with pytest.raises(AcceptanceBasisError, match="bad worktree"):
        acceptance_basis._git_paths(tmp_path, "status")

    dot_git = tmp_path / ".git"
    dot_git.write_text("malformed", encoding="utf-8")
    assert acceptance_basis._worktree_git_command(tmp_path) == ["git"]
    dot_git.write_text("other: relative", encoding="utf-8")
    assert acceptance_basis._worktree_git_command(tmp_path) == ["git"]
    dot_git.write_text("gitdir: relative", encoding="utf-8")
    monkeypatch.setattr(acceptance_basis, "_git_common_dir", lambda *_args: None)
    assert acceptance_basis._worktree_git_command(tmp_path) == ["git"]


def test_worktree_command_remaps_inaccessible_admin_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dot_git = tmp_path / ".git"
    dot_git.write_text("gitdir: /host/repo/.git/worktrees/ticket\n", encoding="utf-8")
    common = tmp_path / "common"
    mounted = common / "worktrees/ticket"
    mounted.mkdir(parents=True)
    monkeypatch.setattr(acceptance_basis, "_git_common_dir", lambda *_args: common)
    assert acceptance_basis._worktree_git_command(tmp_path) == [
        "git",
        f"--git-dir={mounted}",
        f"--work-tree={tmp_path}",
    ]


def test_destination_and_ticket_commit_inputs_require_complete_full_sha_maps(
    tmp_path: Path,
) -> None:
    basis = AcceptanceBasis((_participant(),))
    with pytest.raises(AcceptanceBasisError, match="cover every participant"):
        acceptance_basis.validate_destination_refs(tmp_path, basis, {})
    with pytest.raises(AcceptanceBasisError, match="must be a full Git SHA"):
        acceptance_basis.validate_destination_refs(tmp_path, basis, {"outer": "bad"})
    with pytest.raises(AcceptanceBasisError, match="cover every Basis participant"):
        acceptance_basis.materialize_ticket_commits(tmp_path, basis, tmp_path / "out", {})
    with pytest.raises(AcceptanceBasisError, match="must be a full Git SHA"):
        acceptance_basis.materialize_ticket_commits(
            tmp_path, basis, tmp_path / "out", {"outer": "bad"}
        )


def test_prepare_acceptance_checkout_wraps_generated_projection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(
        acceptance_validation,
        "reconcile_projected_cores",
        lambda *_args: (_ for _ in ()).throw(core_projection.CoreProjectionError("broken")),
    )
    with pytest.raises(AcceptanceBasisError, match="acceptance-input-change-required: broken"):
        acceptance_validation.prepare_acceptance_checkout(
            tmp_path,
            tmp_path,
            slug="ticket",
            ticket_path=tmp_path / "ticket.md",
        )


def test_worktree_mapping_and_identity_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = Path("/host/repo/not-an-index/ticket")
    monkeypatch.setattr(
        acceptance_basis,
        "_worktree_records",
        lambda _root: (
            (Path("/host/repo"), "refs/heads/main"),
            (recorded, _participant().ticket_ref),
        ),
    )
    with pytest.raises(AcceptanceBasisError, match="could not be identified"):
        acceptance_basis.worktree_for_ref(tmp_path, _participant().ticket_ref)

    responses = iter(
        [
            _completed("git", returncode=1),
            _completed("git", stdout=str(tmp_path / "other") + "\n"),
        ]
    )
    monkeypatch.setattr(
        acceptance_basis,
        "_worktree_git_command",
        lambda *_args: ["git"],
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    assert (
        acceptance_basis._worktree_has_identity(tmp_path, "refs/heads/ticket", tmp_path) is False
    )
    assert (
        acceptance_basis._worktree_has_identity(tmp_path, "refs/heads/ticket", tmp_path) is False
    )


def test_descendant_and_project_repository_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )
    with pytest.raises(AcceptanceBasisError, match="ref is unavailable"):
        acceptance_basis.validate_current_basis_refs(tmp_path, AcceptanceBasis((_participant(),)))
    paired = AcceptanceBasis((_participant(), _participant("project")))
    monkeypatch.setattr(acceptance_basis, "paired_project_repository", lambda _root: None)
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "_descendant_ref_commit",
        lambda *_args, **_kwargs: "a" * 40,
    )
    with pytest.raises(AcceptanceBasisError, match="paired project repository"):
        acceptance_basis.validate_current_basis_refs(tmp_path, paired)


def test_clone_commit_reports_clone_and_checkout_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter([_completed("git", returncode=1, stderr="clone failed")])
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(AcceptanceBasisError, match="clone failed"):
        acceptance_basis._clone_commit(tmp_path, tmp_path / "clone", "a" * 40)
    responses = iter(
        [_completed("git"), _completed("git", returncode=1, stderr="checkout failed")]
    )
    with pytest.raises(AcceptanceBasisError, match="checkout failed"):
        acceptance_basis._clone_commit(tmp_path, tmp_path / "clone", "a" * 40)


def test_generated_path_equivalence_handles_symlinks_and_mode_mismatch(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.symlink_to("target")
    right.symlink_to("target")
    assert acceptance_basis._same_generated_path(left, right) is True
    right.unlink()
    right.symlink_to("other")
    assert acceptance_basis._same_generated_path(left, right) is False
    left.unlink()
    right.unlink()
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX executable mode bits")
    left.write_text("same", encoding="utf-8")
    right.write_text("same", encoding="utf-8")
    left.chmod(0o755)
    right.chmod(0o644)
    assert acceptance_basis._same_generated_path(left, right) is False


@pytest.mark.parametrize(
    ("retired_field", "value"),
    [
        ("target_contract", {}),
        ("target_contract", None),
        ("target_contract_history", []),
        ("base_sha", None),
    ],
)
def test_load_acceptance_basis_rejects_every_retired_field(
    tmp_path: Path,
    retired_field: str,
    value: object,
) -> None:
    with pytest.raises(AcceptanceBasisError, match="hard cutoff"):
        acceptance_basis.load_acceptance_basis(
            tmp_path,
            "ticket",
            {retired_field: value},
        )


def test_ticket_machine_rejects_authored_drift() -> None:
    basis = AcceptanceBasis((_participant(),))
    fields = {"summary": "Ticket", "branch": "main"}
    body = "## Description\n\nTest."
    fields["machine"] = acceptance_basis.ticket_machine_fields(
        basis, fields=fields, body=body, generation="1" * 32
    )
    fields["summary"] = "Changed"
    with pytest.raises(AcceptanceBasisError, match="authored Ticket changed"):
        acceptance_basis.ticket_baseline_from_fields(fields, body)
    fields["summary"] = "Ticket"
    with pytest.raises(AcceptanceBasisError, match="authored Ticket changed"):
        acceptance_basis.ticket_baseline_from_fields(fields, "different")


@pytest.mark.parametrize("retired", ["acceptance_basis", "base_sha", "target_contract"])
def test_ticket_baseline_rejects_retired_fields_even_when_null(retired: str) -> None:
    with pytest.raises(AcceptanceBasisError, match=r"unsupported|hard cutoff"):
        acceptance_basis.ticket_baseline_from_fields({retired: None}, "")
