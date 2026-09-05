"""Public Acceptance Basis contracts plus deterministic Git fault injection.

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


def _record() -> dict[str, object]:
    return acceptance_basis.authored_ticket_record(
        {
            "summary": "Ticket",
            "type": "feature",
            "branch": "main",
            "scope": [],
            "criteria": {"mandatory": {"review_rtl_bugs": True}},
        },
        "## Description\n\nTest.\n",
        (),
    )


def test_path_policy_and_basis_require_supported_schema_and_outer_participant() -> None:
    with pytest.raises(AcceptanceBasisError, match="unsupported Acceptance Path Policy"):
        acceptance_basis.AcceptancePathPolicy(schema=2).discover(Path.cwd())
    with pytest.raises(AcceptanceBasisError, match="requires an outer"):
        AcceptanceBasis((_participant("project"),))
    with pytest.raises(AcceptanceBasisError, match="participants must be a list"):
        AcceptanceBasis.from_mapping({"schema": 1, "participants": {}})


def test_basis_participant_lookup_and_record_routing_fail_loudly() -> None:
    basis = AcceptanceBasis((_participant(),))
    with pytest.raises(AcceptanceBasisError, match="no 'project'"):
        basis.participant("project")
    with pytest.raises(AcceptanceBasisError, match="frontmatter is invalid"):
        basis.with_record({"bindings": [], "ticket": {"frontmatter": []}})
    record = _record()
    record["ticket"]["frontmatter"]["branch"] = "release"  # type: ignore[index]
    with pytest.raises(AcceptanceBasisError, match="outer destination disagrees"):
        basis.with_record(record)
    record = _record()
    record["ticket"]["frontmatter"]["project_destination_ref"] = (  # type: ignore[index]
        "refs/heads/main"
    )
    with pytest.raises(AcceptanceBasisError, match="without a participant"):
        basis.with_record(record)


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


def test_authored_record_rejects_unknown_field_and_invalid_on_success() -> None:
    fields = {"branch": "main", "unknown": True}
    with pytest.raises(AcceptanceBasisError, match="unknown authored"):
        acceptance_basis.authored_ticket_record(fields, "body", ())
    with pytest.raises(AcceptanceBasisError, match="on_success must be a mapping"):
        acceptance_basis.authored_ticket_record({"branch": "main", "on_success": []}, "body", ())


def test_binding_record_parser_rejects_invalid_schema() -> None:
    record = _record()
    record["bindings"] = [{"flow": "sim"}]
    with pytest.raises(AcceptanceBasisError, match="invalid schema"):
        AcceptanceBasis((_participant(),)).with_record(record)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda record: record.update(extra=True), "invalid top-level"),
        (lambda record: record.update(schema=2), "unsupported schema"),
        (lambda record: record["ticket"].update(extra=True), "ticket has an invalid schema"),
        (
            lambda record: record["ticket"]["frontmatter"].update(extra=True),
            "unknown authored field",
        ),
        (
            lambda record: record["ticket"]["frontmatter"].pop("scope"),
            "authored defaults are not canonical",
        ),
        (
            lambda record: record["ticket"]["frontmatter"].update(on_success="bad"),
            "on_success must be a mapping",
        ),
    ],
)
def test_record_validation_rejects_noncanonical_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    message: str,
) -> None:
    record = _record()
    mutate(record)  # type: ignore[operator]
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 0, acceptance_basis.canonical_json(record), b""
        ),
    )
    with pytest.raises(AcceptanceBasisError, match=message):
        acceptance_basis.load_basis_record(tmp_path, "ticket", AcceptanceBasis((_participant(),)))


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (subprocess.CompletedProcess(["git"], 1, b"", b"missing"), "record is unavailable"),
        (subprocess.CompletedProcess(["git"], 0, b"not json", b""), "invalid JSON"),
        (subprocess.CompletedProcess(["git"], 0, b'{"schema":1}\n', b""), "record.ticket"),
    ],
)
def test_load_basis_record_reports_git_and_payload_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
    message: str,
) -> None:
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(acceptance_basis.subprocess, "run", lambda *_args, **_kwargs: result)
    with pytest.raises(AcceptanceBasisError, match=message):
        acceptance_basis.load_basis_record(tmp_path, "ticket", AcceptanceBasis((_participant(),)))


def test_load_basis_record_rejects_noncanonical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    payload = (" " + acceptance_basis.canonical_json(record).decode()).encode()
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["git"], 0, payload, b""),
    )
    with pytest.raises(AcceptanceBasisError, match="not canonical JSON"):
        acceptance_basis.load_basis_record(tmp_path, "ticket", AcceptanceBasis((_participant(),)))


def test_record_validation_rejects_invalid_on_success_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    record["ticket"]["frontmatter"]["on_success"] = {  # type: ignore[index]
        "destination": "invalid",
        "merge": True,
        "cleanup": True,
        "triage_report": True,
        "remove_targets": [],
    }
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 0, acceptance_basis.canonical_json(record), b""
        ),
    )
    with pytest.raises(AcceptanceBasisError, match="destination"):
        acceptance_basis.load_basis_record(tmp_path, "ticket", AcceptanceBasis((_participant(),)))


def test_receipt_validation_reports_missing_and_mismatched_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    record = _record()
    monkeypatch.setattr(
        acceptance_basis,
        "_receipt_path",
        lambda *_args: tmp_path / "receipt.json",
    )
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(acceptance_basis, "load_basis_record", lambda *_args: record)
    with pytest.raises(AcceptanceBasisError, match="receipt is unavailable"):
        acceptance_basis.load_basis_receipt(tmp_path, "ticket", basis.as_dict())
    receipt = acceptance_basis.write_basis_receipt(
        tmp_path,
        "ticket",
        basis,
        source_sha256="1" * 64,
        operation_id="2" * 32,
    )
    path = tmp_path / "receipt.json"
    path.write_bytes(acceptance_basis.canonical_json({**receipt, "schema": 2}))
    with pytest.raises(AcceptanceBasisError, match="receipt mismatch"):
        acceptance_basis.load_basis_receipt(tmp_path, "ticket", basis.as_dict())


def test_write_basis_receipt_rejects_source_drift_and_write_once_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    record = _record()
    path = tmp_path / "receipt.json"
    path.write_text("present", encoding="utf-8")
    monkeypatch.setattr(acceptance_basis, "load_basis_record", lambda *_args: record)
    monkeypatch.setattr(acceptance_basis, "_receipt_path", lambda *_args: path)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(
        acceptance_basis,
        "_validate_receipt",
        lambda *_args: {"source_sha256": "old"},
    )
    with pytest.raises(AcceptanceBasisError, match="different source"):
        acceptance_basis.write_basis_receipt(
            tmp_path, "ticket", basis, source_sha256="new", operation_id="2" * 32
        )
    path.unlink()
    monkeypatch.setattr(
        acceptance_basis,
        "atomic_write_once",
        lambda *_args: (_ for _ in ()).throw(acceptance_basis.WriteOnceConflictError("race")),
    )
    with pytest.raises(AcceptanceBasisError, match="conflicting Acceptance Basis receipt"):
        acceptance_basis.write_basis_receipt(
            tmp_path, "ticket", basis, source_sha256="1" * 64, operation_id="2" * 32
        )


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


def test_validate_ticket_view_wraps_generated_projection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(
        core_projection,
        "reconcile_projected_cores",
        lambda *_args: (_ for _ in ()).throw(core_projection.CoreProjectionError("broken")),
    )
    with pytest.raises(AcceptanceBasisError, match="could not prepare generated"):
        acceptance_basis.validate_ticket_view(
            tmp_path, AcceptanceBasis((_participant(),)), allow_generated=True
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


def test_load_acceptance_basis_rejects_authored_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    record = _record()
    monkeypatch.setattr(acceptance_basis, "load_basis_record", lambda *_args: record)
    monkeypatch.setattr(acceptance_basis, "_validate_receipt", lambda *_args: {})
    fields = dict(record["ticket"]["frontmatter"])  # type: ignore[index]
    fields["acceptance_basis"] = basis.as_dict()
    fields["summary"] = "Changed"
    with pytest.raises(AcceptanceBasisError, match="frontmatter changed"):
        acceptance_basis.load_acceptance_basis(tmp_path, "ticket", fields)
    fields["summary"] = "Ticket"
    with pytest.raises(AcceptanceBasisError, match="body changed"):
        acceptance_basis.load_acceptance_basis(tmp_path, "ticket", fields, "different")
