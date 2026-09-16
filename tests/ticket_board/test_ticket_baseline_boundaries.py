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
    acceptance_validation,
    ticket_baseline,
)
from booley.ticket_board.ticket_baseline import (
    BasisParticipant,
    TicketBaseline,
    TicketBaselineError,
    ticket_baseline_from_machine,
    ticket_machine_from_participants,
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


@pytest.mark.parametrize(
    ("amendment_patch", "message"),
    [
        ({"reason": None}, "reason"),
        ({"actor": ""}, "actor must be a non-empty string"),
        ({"operation_id": "b" * 32}, "generation identity is invalid"),
        ({"changes": []}, "has no changes"),
        ({"changes": [{"criterion": "review_rtl_bugs_clean"}]}, "Criterion change is invalid"),
        (
            {
                "changes": [
                    {
                        "criterion": 4,
                        "before_mandatory": True,
                        "after_mandatory": False,
                        "thresholds": {},
                    }
                ]
            },
            "Criterion values are invalid",
        ),
        (
            {
                "changes": [
                    {
                        "criterion": "review_rtl_bugs_clean",
                        "before_mandatory": 1,
                        "after_mandatory": False,
                        "thresholds": {},
                    }
                ]
            },
            "mandatory values are invalid",
        ),
        ({"optional_conversions": [3]}, "Scope or optional conversions are invalid"),
    ],
)
def test_ticket_machine_rejects_malformed_amendment_metadata(
    amendment_patch: dict[str, object], message: str
) -> None:
    generation = "a" * 32
    machine = ticket_machine_from_participants(
        (_participant(),), authored_sha256="c" * 64, generation=generation
    )
    amendment = {
        "slug": "blocked-ticket",
        "operation_id": generation,
        "previous_generation": "d" * 32,
        "actor": "maintainer",
        "reason": "approved relaxation",
        "changes": [
            {
                "criterion": "review_rtl_bugs_clean",
                "before_mandatory": True,
                "after_mandatory": False,
                "thresholds": {},
            }
        ],
        "scope_added": [],
        "optional_conversions": [],
    }
    amendment.update(amendment_patch)
    machine["amendment"] = amendment

    with pytest.raises(TicketBaselineError, match=message):
        ticket_baseline_from_machine(machine)


def test_path_policy_and_basis_require_supported_schema_and_outer_participant() -> None:
    with pytest.raises(TicketBaselineError, match="unsupported Acceptance Path Policy"):
        ticket_baseline.AcceptancePathPolicy(schema=2).discover(Path.cwd())
    with pytest.raises(TicketBaselineError, match="requires an outer"):
        TicketBaseline((_participant("project"),))
    with pytest.raises(TicketBaselineError, match="participants must be a list"):
        TicketBaseline.from_mapping({"schema": 1, "participants": {}})


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
    with pytest.raises(TicketBaselineError, match=message):
        TicketBaseline.from_mapping({"schema": 1, "participants": [row]})


def test_git_path_and_worktree_command_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ticket_baseline,
        "_worktree_git_command",
        lambda *_args: ["git"],
    )
    monkeypatch.setattr(
        ticket_baseline.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="bad worktree"),
    )
    with pytest.raises(TicketBaselineError, match="bad worktree"):
        ticket_baseline._git_paths(tmp_path, "status")

    dot_git = tmp_path / ".git"
    dot_git.write_text("malformed", encoding="utf-8")
    assert ticket_baseline._worktree_git_command(tmp_path) == ["git"]
    dot_git.write_text("other: relative", encoding="utf-8")
    assert ticket_baseline._worktree_git_command(tmp_path) == ["git"]
    dot_git.write_text("gitdir: relative", encoding="utf-8")
    monkeypatch.setattr(ticket_baseline, "_git_common_dir", lambda *_args: None)
    assert ticket_baseline._worktree_git_command(tmp_path) == ["git"]


def test_worktree_command_remaps_inaccessible_admin_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dot_git = tmp_path / ".git"
    dot_git.write_text("gitdir: /host/repo/.git/worktrees/ticket\n", encoding="utf-8")
    common = tmp_path / "common"
    mounted = common / "worktrees/ticket"
    mounted.mkdir(parents=True)
    monkeypatch.setattr(ticket_baseline, "_git_common_dir", lambda *_args: common)
    assert ticket_baseline._worktree_git_command(tmp_path) == [
        "git",
        f"--git-dir={mounted}",
        f"--work-tree={tmp_path}",
    ]


def test_destination_and_ticket_commit_inputs_require_complete_full_sha_maps(
    tmp_path: Path,
) -> None:
    basis = TicketBaseline((_participant(),))
    with pytest.raises(TicketBaselineError, match="cover every participant"):
        ticket_baseline.validate_destination_refs(tmp_path, basis, {})
    with pytest.raises(TicketBaselineError, match="must be a full Git SHA"):
        ticket_baseline.validate_destination_refs(tmp_path, basis, {"outer": "bad"})
    with pytest.raises(TicketBaselineError, match="cover every Basis participant"):
        ticket_baseline.materialize_ticket_commits(tmp_path, basis, tmp_path / "out", {})
    with pytest.raises(TicketBaselineError, match="must be a full Git SHA"):
        ticket_baseline.materialize_ticket_commits(
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
    with pytest.raises(TicketBaselineError, match="acceptance-input-change-required: broken"):
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
        ticket_baseline,
        "_worktree_records",
        lambda _root: (
            (Path("/host/repo"), "refs/heads/main"),
            (recorded, _participant().ticket_ref),
        ),
    )
    with pytest.raises(TicketBaselineError, match="could not be identified"):
        ticket_baseline.worktree_for_ref(tmp_path, _participant().ticket_ref)

    responses = iter(
        [
            _completed("git", returncode=1),
            _completed("git", stdout=str(tmp_path / "other") + "\n"),
        ]
    )
    monkeypatch.setattr(
        ticket_baseline,
        "_worktree_git_command",
        lambda *_args: ["git"],
    )
    monkeypatch.setattr(
        ticket_baseline.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    assert ticket_baseline._worktree_has_identity(tmp_path, "refs/heads/ticket", tmp_path) is False
    assert ticket_baseline._worktree_has_identity(tmp_path, "refs/heads/ticket", tmp_path) is False


def test_descendant_and_project_repository_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ticket_baseline.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )
    with pytest.raises(TicketBaselineError, match="ref is unavailable"):
        ticket_baseline.validate_current_basis_refs(tmp_path, TicketBaseline((_participant(),)))
    paired = TicketBaseline((_participant(), _participant("project")))
    monkeypatch.setattr(ticket_baseline, "paired_project_repository", lambda _root: None)
    monkeypatch.setattr(ticket_baseline, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        ticket_baseline,
        "_descendant_ref_commit",
        lambda *_args, **_kwargs: "a" * 40,
    )
    with pytest.raises(TicketBaselineError, match="paired project repository"):
        ticket_baseline.validate_current_basis_refs(tmp_path, paired)


def test_clone_commit_reports_clone_and_checkout_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter([_completed("git", returncode=1, stderr="clone failed")])
    monkeypatch.setattr(
        ticket_baseline.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(TicketBaselineError, match="clone failed"):
        ticket_baseline._clone_commit(tmp_path, tmp_path / "clone", "a" * 40)
    responses = iter(
        [_completed("git"), _completed("git", returncode=1, stderr="checkout failed")]
    )
    with pytest.raises(TicketBaselineError, match="checkout failed"):
        ticket_baseline._clone_commit(tmp_path, tmp_path / "clone", "a" * 40)


def test_generated_path_equivalence_handles_symlinks_and_mode_mismatch(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.symlink_to("target")
    right.symlink_to("target")
    assert ticket_baseline._same_generated_path(left, right) is True
    right.unlink()
    right.symlink_to("other")
    assert ticket_baseline._same_generated_path(left, right) is False
    left.unlink()
    right.unlink()
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX executable mode bits")
    left.write_text("same", encoding="utf-8")
    right.write_text("same", encoding="utf-8")
    left.chmod(0o755)
    right.chmod(0o644)
    assert ticket_baseline._same_generated_path(left, right) is False


@pytest.mark.parametrize(
    ("retired_field", "value"),
    [
        ("target_contract", {}),
        ("target_contract", None),
        ("target_contract_history", []),
        ("base_sha", None),
    ],
)
def test_load_ticket_baseline_rejects_every_retired_field(
    tmp_path: Path,
    retired_field: str,
    value: object,
) -> None:
    with pytest.raises(TicketBaselineError, match="hard cutoff"):
        ticket_baseline.load_ticket_baseline(
            tmp_path,
            "ticket",
            {retired_field: value},
        )


def test_ticket_machine_rejects_authored_drift() -> None:
    basis = TicketBaseline((_participant(),))
    fields = {"summary": "Ticket", "branch": "main"}
    body = "## Description\n\nTest."
    fields["machine"] = ticket_baseline.ticket_machine_fields(
        basis, fields=fields, body=body, generation="1" * 32
    )
    fields["summary"] = "Changed"
    with pytest.raises(TicketBaselineError, match="authored Ticket changed"):
        ticket_baseline.ticket_baseline_from_fields(fields, body)
    fields["summary"] = "Ticket"
    with pytest.raises(TicketBaselineError, match="authored Ticket changed"):
        ticket_baseline.ticket_baseline_from_fields(fields, "different")


@pytest.mark.parametrize("retired", ["acceptance_basis", "base_sha", "target_contract"])
def test_ticket_baseline_rejects_retired_fields_even_when_null(retired: str) -> None:
    with pytest.raises(TicketBaselineError, match=r"unsupported|hard cutoff"):
        ticket_baseline.ticket_baseline_from_fields({retired: None}, "")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda machine: machine.update(authored_sha256="short"), "authored_sha256"),
        (lambda machine: machine.update(baseline={"project": {}}), "requires outer"),
        (lambda machine: machine["baseline"].update(outer=[]), "must be a mapping"),
        (lambda machine: machine["baseline"]["outer"].update(extra="x"), "invalid fields"),
        (lambda machine: machine.update(providers={}), "providers must be a list"),
        (
            lambda machine: machine.update(
                providers=[
                    {
                        "provider": "dependency",
                        "ticket_generation": "2" * 32,
                        "target": "acme:lib:toy:1.0#future",
                        "role": "persistent",
                        "surface_sha256": "3" * 64,
                    }
                ]
                * 2
            ),
            "sorted and unique",
        ),
    ],
)
def test_machine_metadata_rejects_malformed_authority(change, message: str) -> None:
    basis = TicketBaseline((_participant(),))
    machine = ticket_baseline.ticket_machine_fields(
        basis, fields={"summary": "ticket"}, body="body", generation="1" * 32
    )
    change(machine)
    with pytest.raises(TicketBaselineError, match=message):
        ticket_baseline.ticket_baseline_from_machine(machine)


def test_machine_metadata_rejects_non_mapping_and_unavailable_identity() -> None:
    with pytest.raises(TicketBaselineError, match="must be a mapping"):
        ticket_baseline.ticket_baseline_from_machine(None)
    with pytest.raises(TicketBaselineError, match="metadata is unavailable"):
        TicketBaseline((_participant(),)).ticket_identity()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda row: row.update(extra="x"), "invalid schema"),
        (lambda row: row.update(provider=" "), "names must be non-empty"),
        (lambda row: row.update(role="other"), "role is not exportable"),
        (lambda row: row.update(ticket_generation="short"), "ticket_generation is invalid"),
        (lambda row: row.update(surface_sha256="short"), "surface_sha256 is invalid"),
    ],
)
def test_provider_binding_rejects_malformed_identity(change, message: str) -> None:
    row = {
        "provider": "dependency",
        "ticket_generation": "2" * 32,
        "target": "acme:lib:toy:1.0#future",
        "role": "persistent",
        "surface_sha256": "3" * 64,
    }
    change(row)
    with pytest.raises(TicketBaselineError, match=message):
        ticket_baseline.provider_binding_from_mapping(row)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (None, "must be a mapping"),
        ({"schema": 1}, "exactly schema and participants"),
        ({"schema": True, "participants": []}, "must be an integer"),
        ({"schema": 2, "participants": []}, "schema must be"),
    ],
)
def test_baseline_parser_rejects_noncanonical_container(row: object, message: str) -> None:
    with pytest.raises(TicketBaselineError, match=message):
        TicketBaseline.from_mapping(row)


def test_ticket_routing_rejects_unpinned_destinations() -> None:
    outer = _participant()
    project = BasisParticipant(
        "project",
        "a" * 40,
        _participant("project").ticket_ref,
        "refs/heads/project-main",
        "b" * 40,
    )
    with pytest.raises(TicketBaselineError, match="outer destination"):
        ticket_baseline._validate_ticket_routing(TicketBaseline((outer,)), {"branch": "other"})
    with pytest.raises(TicketBaselineError, match="without a baseline participant"):
        ticket_baseline._validate_ticket_routing(
            TicketBaseline((outer,)),
            {"branch": "main", "project_destination_ref": "refs/heads/project-main"},
        )
    with pytest.raises(TicketBaselineError, match="project destination"):
        ticket_baseline._validate_ticket_routing(
            TicketBaseline((outer, project)),
            {"branch": "main", "project_destination_ref": "refs/heads/other"},
        )
    assert TicketBaseline((outer, project)).participant("project") == project
    with pytest.raises(TicketBaselineError, match="no 'project' participant"):
        TicketBaseline((outer,)).participant("project")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"on_success": []}, "on_success must be a mapping"),
        ({"on_success": {"destination": "unknown"}}, "destination"),
        ({"target_plan": {"unexpected": "shape"}}, "Target Plan|target_plan"),
    ],
)
def test_authored_digest_rejects_invalid_human_fields(fields: dict, message: str) -> None:
    with pytest.raises(TicketBaselineError, match=message):
        ticket_baseline.authored_ticket_digest(fields, "body")


def test_ticket_commit_trailers_fail_when_authoring_commit_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ticket_baseline.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=128),
    )
    with pytest.raises(TicketBaselineError, match="authoring commit is unavailable"):
        ticket_baseline.validate_ticket_commit_trailers(
            tmp_path, "ticket", TicketBaseline((_participant(),)), {}
        )


def test_executable_ticket_requires_human_body(tmp_path: Path) -> None:
    with pytest.raises(TicketBaselineError, match="body is required"):
        ticket_baseline.load_ticket_baseline(tmp_path, "ticket", {})


def test_live_basis_rejects_disappearing_registered_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = TicketBaseline((_participant(),))
    monkeypatch.setattr(
        ticket_baseline, "_partition_protected_inputs", lambda *_args: ("project", set(), set())
    )
    monkeypatch.setattr(ticket_baseline, "worktree_for_ref", lambda *_args: tmp_path)
    monkeypatch.setattr(ticket_baseline, "_recorded_worktree_path", lambda *_args: None)
    with pytest.raises(TicketBaselineError, match="disappeared during validation"):
        ticket_baseline.assert_live_inputs_unchanged(basis, tmp_path, tmp_path)


def test_control_discovery_reports_failure_in_current_and_baseline_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = TicketBaseline((_participant(),))
    with pytest.raises(TicketBaselineError, match="protected-input discovery failed"):
        ticket_baseline._basis_control_paths(
            tmp_path, basis, lambda *_args: (_ for _ in ()).throw(OSError("unreadable"))
        )
    monkeypatch.setattr(ticket_baseline, "materialize_basis_checkout", lambda *_args: None)
    calls = iter([set(), OSError("baseline unreadable")])

    def discover(_root: Path) -> set[str]:
        value = next(calls)
        if isinstance(value, OSError):
            raise value
        return value

    with pytest.raises(TicketBaselineError, match="baseline unreadable"):
        ticket_baseline._basis_control_paths(tmp_path, basis, discover)
