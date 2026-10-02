"""Operational Git failures never become negative ancestry verdicts."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("code", [0, 1, 128])
@pytest.mark.parametrize(
    "site", ["archive", "journal", "repositories", "workspace", "detached", "attached"]
)
def test_ancestry_sites_split_git_exit_codes(tmp_path, monkeypatch, code, site):
    calls = []

    def git(*args, **kwargs):
        command = args[1] if isinstance(args[1], list) else args[1:]
        calls.append(command)
        ancestry = "merge-base" in command
        return subprocess.CompletedProcess(
            command,
            code if ancestry else 0,
            "refs/heads/ticket" if "symbolic-ref" in command else "",
            "missing object" if ancestry else "",
        )

    operation, ctx = _operation(site, tmp_path, monkeypatch, git)
    if code == 0 or (site == "journal" and code == 1):
        operation()
    elif site in ("detached", "attached"):
        result = operation()
        assert result is not None
        assert ("cannot verify ancestry" in result.block_reason) == (code == 128)
        if site == "attached":
            assert ctx.feature_branch == "unchanged"
        assert not any("checkout" in call for call in calls)
    else:
        with pytest.raises(RuntimeError) as caught:
            operation()
        assert ("cannot verify ancestry" in str(caught.value)) == (code == 128)
        if code == 128:
            assert "missing object" in str(caught.value)
            assert "no longer descends" not in str(caught.value)


def _operation(site, tmp_path, monkeypatch, git):
    from functools import partial

    from booley.harness.setup import workspace as setup
    from booley.ticket_board import archive_generation, ticket_repositories, workspace_ops
    from booley.ticket_board.acceptance_journal import _advance

    if site == "archive":
        monkeypatch.setattr(archive_generation, "_git", git)
        monkeypatch.setattr(archive_generation, "_ref_sha", lambda *a: "child")
        monkeypatch.setattr(archive_generation, "_records", lambda *a: [])
        monkeypatch.setattr(archive_generation, "worktree_for_ref", lambda *a: None)
        operation = partial(
            archive_generation._participant,
            "outer",
            tmp_path,
            "ref",
            tmp_path / "canonical",
            "parent",
        )
    elif site == "journal":
        monkeypatch.setattr(_advance, "_git", git)
        operation = partial(_advance._is_ancestor, tmp_path, "parent", "child")
    elif site == "repositories":
        monkeypatch.setattr(ticket_repositories, "_git", git)
        monkeypatch.setattr(ticket_repositories, "_ref_sha", lambda *a: "child")
        operation = partial(
            ticket_repositories._basis_branch, tmp_path, "refs/heads/ticket", "parent"
        )
    elif site == "workspace":
        monkeypatch.setattr(workspace_ops, "_git", git)
        operation = partial(
            workspace_ops._require_ancestor, tmp_path, "parent", "child", "not descendant"
        )
    else:
        monkeypatch.setattr(setup, "git_run", git)
        basis = SimpleNamespace(
            outer_sha="parent",
            participant=lambda role: SimpleNamespace(ticket_ref="refs/heads/ticket"),
        )
        ctx = SimpleNamespace(ticket_baseline=basis, feature_branch="unchanged")
        operation = (
            partial(setup._attach_clean_detached_basis_branch, tmp_path, "refs/heads/ticket")
            if site == "detached"
            else partial(setup._attach_basis_branch, ctx, tmp_path)
        )
    return operation, ctx if site in ("attached", "detached") else None


@pytest.mark.parametrize(
    "boundary",
    [
        "review-load",
        "review-refs",
        "intake",
        "intake-refs",
        "intake-document",
        "completion",
        "recording",
        "flow",
        "setup-validation",
        "setup-preparation",
        "developer",
    ],
)
def test_operational_ancestry_boundary_outcomes(tmp_path, monkeypatch, capsys, boundary):
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    failure = TicketAncestryVerificationError("cannot verify ancestry: fatal: missing object")
    outcome = _boundary_outcome(boundary, tmp_path, monkeypatch, failure)
    text = str(outcome) + capsys.readouterr().err
    assert str(failure) in text
    assert "acceptance-input-change-required" not in text
    assert "invalid Ticket baseline" not in text
    assert "no valid Ticket baseline" not in text


def _raise_failure(failure):
    def fail(*args, **kwargs):
        raise failure

    return fail


def _boundary_outcome(boundary, root, monkeypatch, failure):
    from booley.harness.setup import workspace
    from booley.ticket_board import completion

    fail = _raise_failure(failure)
    ticket = root / "ticket.md"
    ticket.write_text("authored Ticket")
    tio = SimpleNamespace(load_basis=fail, find_ticket=lambda slug: {"status": "review"})
    if boundary.startswith("review"):
        return _review_boundary_outcome(boundary, root, tio, monkeypatch, fail, failure)
    if boundary.startswith("intake"):
        return _intake_boundary_outcome(boundary, root, ticket, tio, monkeypatch, fail, failure)
    if boundary == "completion":
        assert (
            completion._completion_inputs(
                tio, "ticket", SimpleNamespace(merge=True, cleanup=False)
            )
            is None
        )
        return ""
    if boundary in ("recording", "flow"):
        return _flow_boundary_outcome(boundary, root, ticket, tio, monkeypatch, fail, failure)
    ctx = SimpleNamespace(
        ticket_baseline=object(),
        slug="ticket",
        project_root=root,
        work_dir=root,
        ticket_path=ticket,
    )
    if boundary == "developer":
        return _developer_boundary_outcome(ctx, monkeypatch, fail)
    from booley.ticket_board import acceptance_validation

    monkeypatch.setattr(acceptance_validation, "assert_ticket_worktree_inputs_unchanged", fail)
    monkeypatch.setattr(acceptance_validation, "prepare_acceptance_checkout", fail)
    monkeypatch.setattr(workspace, "_current_ticket_path", lambda ctx: ticket)
    result = (
        workspace._validate_materialized_ticket_baseline(ctx, root)
        if boundary == "setup-validation"
        else workspace._prepare_ticket_checkout(ctx, ticket, False)
    )
    return result.block_reason


def _review_boundary_outcome(boundary, root, tio, monkeypatch, fail, failure):
    from booley.ticket_board import review_preparation

    monkeypatch.setattr(review_preparation, "validate_current_basis_refs", fail)
    with pytest.raises(review_preparation.ReviewPrepError) as caught:
        if boundary == "review-load":
            review_preparation._load_review_basis(tio, "ticket")
        else:
            review_preparation._resolve_review_repositories(root, object(), None)
    assert caught.value.__cause__ is failure
    return caught.value


def _flow_boundary_outcome(boundary, root, ticket, tio, monkeypatch, fail, failure):
    from booley.flows.execution_persistence import AcceptanceRecordingError
    from booley.ticket_board import flow_execution

    if boundary == "recording":
        monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
        monkeypatch.setattr(flow_execution, "detect_project_root", lambda: root)
        monkeypatch.setattr(flow_execution, "resolve_runtime_ticket_slug", lambda path: "ticket")
        monkeypatch.setattr(flow_execution, "resolve_checkout_project_dir", lambda root: root)
        monkeypatch.setattr(flow_execution, "TicketIO", lambda *a, **kw: tio)
        with pytest.raises(AcceptanceRecordingError) as caught:
            flow_execution.TicketAcceptanceRecorder()._validated_ticket_identity()
        assert caught.value.__cause__ is failure
        return caught.value
    execution = flow_execution.TicketBoardFlowExecution()
    monkeypatch.setattr(execution, "_load_basis", lambda: (object(), root, "ticket", ticket))
    monkeypatch.setattr(flow_execution, "assert_ticket_worktree_inputs_unchanged", fail)
    result = execution.validate_and_resolve(SimpleNamespace(work_dir=root))
    assert result.exit_code != 0
    return result.report_text


def _developer_boundary_outcome(ctx, monkeypatch, fail):
    from booley.harness import developer
    from booley.ticket_board import acceptance_validation

    monkeypatch.setattr(acceptance_validation, "assert_ticket_worktree_inputs_unchanged", fail)
    outcomes = []
    monkeypatch.setattr(
        developer, "fail_ticket", lambda ctx, text, *a, **kw: outcomes.append(text)
    )
    monkeypatch.setattr(
        developer,
        "block_ticket",
        lambda *a, **kw: pytest.fail("operational error policy-blocked Ticket"),
    )
    assert developer._block_changed_ticket_baseline(ctx, 0)
    assert len(outcomes) == 1
    return outcomes[0]


@pytest.mark.parametrize("boundary", ["live-inputs", "preparation"])
@pytest.mark.parametrize("code", [1, 128])
def test_acceptance_validation_preserves_ancestry_error_identity(
    tmp_path, monkeypatch, boundary, code
):
    from booley.ticket_board import acceptance_validation, ticket_baseline

    def git(*args, **kwargs):
        return subprocess.CompletedProcess(args, code, "", "fatal: missing object")

    monkeypatch.setattr(ticket_baseline.subprocess, "run", git)
    with pytest.raises(Exception) as original:
        ticket_baseline._descendant_commit(tmp_path, "child", "parent", role="outer")
    failure = original.value
    fail = _raise_failure(failure)
    monkeypatch.setattr(acceptance_validation, "_validate_live_worktree", fail)
    monkeypatch.setattr(acceptance_validation, "_require_contained_project_directory", fail)
    with pytest.raises(type(failure)) as caught:
        if boundary == "live-inputs":
            acceptance_validation.assert_ticket_worktree_inputs_unchanged(
                tmp_path, object(), tmp_path, slug="ticket", ticket_path=tmp_path / "ticket.md"
            )
        else:
            acceptance_validation.prepare_acceptance_checkout(
                tmp_path, tmp_path, slug="ticket", ticket_path=tmp_path / "ticket.md"
            )
    if code == 128:
        assert caught.value is failure
        assert "acceptance-input-change-required" not in str(caught.value)
    else:
        assert "acceptance-input-change-required" in str(caught.value)


def _intake_boundary_outcome(boundary, root, ticket, tio, monkeypatch, fail, failure):
    import asyncio

    from booley.harness.setup import intake
    from booley.ticket_board import workspace_ops

    monkeypatch.setattr(intake, "TicketIO", lambda *a, **kw: tio)
    monkeypatch.setattr(workspace_ops, "validate_basis_refs", fail)
    with pytest.raises(intake.FatalError) as caught:
        if boundary == "intake":
            intake._load_context_basis(root, ticket, "ticket")
        elif boundary == "intake-refs":
            ctx = SimpleNamespace(
                ticket_baseline=object(), project_root=root, slug="ticket", branch="main"
            )
            intake._verify_ticket_baseline(ctx, "fresh")
        else:
            monkeypatch.setattr(intake, "_resolve_ticket_path", lambda *a: ticket)
            monkeypatch.setattr(intake, "_board_state", lambda *a: None)
            monkeypatch.setattr(intake, "_resolve_and_validate", lambda *a: (ticket, "ticket"))
            monkeypatch.setattr(intake, "_load_progress", lambda *a: {})
            tio.load_document = fail
            asyncio.run(intake.run("ticket", root))
    assert caught.value.__cause__ is failure
    return caught.value


@pytest.mark.parametrize("operation", ["reset", "preflight", "acceptance-sources"])
def test_workspace_operational_error_is_contained_at_operation_boundary(
    tmp_path, monkeypatch, capsys, operation
):
    from booley.ticket_board import operations, workspace_ops
    from booley.ticket_board.acceptance_journal import _advance
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    failure = TicketAncestryVerificationError("cannot verify ancestry: missing object")
    fail = _raise_failure(failure)
    if operation == "acceptance-sources":
        monkeypatch.setattr(_advance, "pin_basis_refs", fail)
        transaction = SimpleNamespace(
            journal=SimpleNamespace(sources={}), root=tmp_path, basis=object(), slug="ticket"
        )
        with pytest.raises(_advance.AcceptanceOperationError) as caught:
            _advance._ensure_sources(transaction, "main", None)
        assert caught.value.__cause__ is failure
        text = str(caught.value)
    else:
        monkeypatch.setattr(workspace_ops, "reset_basis_worktrees", fail)
        monkeypatch.setattr(workspace_ops, "preflight_basis_reset", fail)
        entry = {"machine": {}, "branch": "main"}
        if operation == "reset":
            assert not operations._reset_ticket_branches(tmp_path, "ticket", entry, object())
        else:
            assert (
                operations._preflight_reset_branches(tmp_path, "ticket", entry, object()) is None
            )
        text = capsys.readouterr().err
    assert str(failure) in text
    assert "acceptance-input-change-required" not in text


@pytest.mark.parametrize("failure_kind", ["timeout", "oserror"])
def test_workspace_ancestry_transport_failure_keeps_original_cause(
    tmp_path, monkeypatch, failure_kind
):
    from booley.ticket_board import workspace_ops
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    failure = (
        subprocess.TimeoutExpired("git", 120)
        if failure_kind == "timeout"
        else OSError("git unavailable")
    )
    monkeypatch.setattr(workspace_ops.subprocess, "run", _raise_failure(failure))
    with pytest.raises(TicketAncestryVerificationError, match="cannot verify ancestry") as caught:
        workspace_ops._require_ancestor(tmp_path, "parent", "child", "not descendant")
    assert caught.value.__cause__.__cause__ is failure
    assert "acceptance-input-change-required" not in str(caught.value)


@pytest.mark.parametrize("site", ["baseline", "workspace"])
def test_existing_ref_with_missing_object_stays_operational(tmp_path, monkeypatch, site):
    from booley.ticket_board import ticket_baseline, workspace_ops
    from tests.ticket_board.test_basis_refresh import _git, _paired_refresh_repositories

    root, _project = _paired_refresh_repositories(tmp_path, monkeypatch)
    sha = _git(root, "rev-parse", "refs/heads/main")
    (root / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    with pytest.raises(
        ticket_baseline.TicketAncestryVerificationError, match="cannot verify ancestry"
    ) as caught:
        if site == "baseline":
            ticket_baseline._descendant_ref_commit(
                root, "refs/heads/main", sha, kind="destination", role="outer"
            )
        else:
            workspace_ops._verified_basis_commit(root, "refs/heads/main")
    assert "acceptance-input-change-required" not in str(caught.value)
    assert "no longer descends" not in str(caught.value)


def test_acceptance_secondary_inspection_preserves_failure_guidance():
    from booley.ticket_board import operations
    from booley.ticket_board.ticket_baseline import TicketAncestryVerificationError

    tio = SimpleNamespace(
        inspect_ticket=_raise_failure(TicketAncestryVerificationError("cannot verify ancestry"))
    )
    assert (
        operations._acceptance_failure_detail(tio, "ticket")
        == "inspect the Ticket and Acceptance Journal before retrying"
    )


@pytest.mark.parametrize("boundary", ["recording", "flow"])
def test_flow_discovery_failure_is_clean_operational_outcome(tmp_path, monkeypatch, boundary):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    failure = ProjectRootDiscoveryError("Cannot determine Project checkout")
    fail = _raise_failure(failure)
    ticket = tmp_path / "ticket.md"
    ticket.write_text("Ticket")
    tio = SimpleNamespace(load_basis=fail)
    result = _flow_boundary_outcome(boundary, tmp_path, ticket, tio, monkeypatch, fail, failure)
    assert str(failure) in str(result)
    assert "acceptance-input-change-required" not in str(result)
