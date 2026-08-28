"""Tests for the setup stage's ticket intake — focused on killing mutation survivors.

Targets: resume action dispatch, dependency checking, path resolution,
context defaults, activation logic, and unanswered question detection.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.harness.blocking import FatalError
from booley.harness.models import TicketContext
from booley.ticket_board.target_contract import (
    ContractParticipant,
    ContractTargetBinding,
    TargetContract,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_FIELDS = {
    "type": "bugfix",
    "branch": "master",
    "summary": "test",
    "scope_current": [],
    "scope_new": [],
    "criteria": {},
}


def test_schema_four_contract_seeds_callable_selector_for_prompt_rendering(
    tmp_path: Path,
) -> None:
    from booley.dev_support.criteria_actions import planned_invocation
    from booley.dev_support.development_state import CriterionEntry
    from booley.harness.setup.intake import _apply_contract_selectors

    contract = TargetContract(
        outer_sha="a" * 40,
        project_sha=None,
        surface_digest="b" * 64,
        targets=("acme:ip:uart:1.0#lint_uart",),
        bindings=(
            ContractTargetBinding(
                flow="lint",
                criterion="lint_clean",
                baseline="acme:ip:uart:1.0#lint_uart",
                candidate="acme:ip:uart:1.0#lint_uart",
                baseline_selector="uart#lint_uart",
                candidate_selector="uart#lint_uart",
            ),
        ),
        participants=(
            ContractParticipant(
                role="outer",
                sealed_sha="a" * 40,
                ticket_ref="refs/heads/ticket",
                destination_ref="refs/heads/main",
                destination_sha="c" * 40,
            ),
        ),
    )
    ctx = TicketContext(
        slug="qualified-target",
        ticket_path=tmp_path / "ticket.md",
        ticket_type="bugfix",
        branch="main",
        summary="Qualified target",
        project_root=tmp_path,
        target_contract=contract,
    )
    expanded = {"lint_clean_acme:ip:uart:1.0#lint_uart": True}
    criterion_params: dict[str, dict[str, object]] = {}

    _apply_contract_selectors(ctx, expanded, criterion_params)

    assert criterion_params == {
        "lint_clean_acme:ip:uart:1.0#lint_uart": {
            "target": "acme:ip:uart:1.0#lint_uart",
            "_target_selector": "uart#lint_uart",
        }
    }
    entry = CriterionEntry(
        met=False,
        mandatory=True,
        params=criterion_params["lint_clean_acme:ip:uart:1.0#lint_uart"],
    )
    assert (
        planned_invocation("lint_clean_acme:ip:uart:1.0#lint_uart", entry)
        == "lint --target uart#lint_uart"
    )


def _mock_cli_defaults(mock_cli, *, action="fresh", stage="", fields=None):
    """Set up mock_cli with common defaults."""
    mock_cli.validate_ticket.return_value = {"valid": True}
    mock_cli.parse_ticket.return_value = {
        "fields": fields if fields is not None else dict(_MINIMAL_FIELDS),
        "body": "",
    }
    mock_cli.resume.return_value = {"action": action, "stage": stage}


def _write_progress(project_root: Path, slug: str, data: dict):
    """Write a progress.json for the given slug."""
    logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "progress.json").write_text(json.dumps(data), encoding="utf-8")


def test_schema_three_contract_verifies_refs_and_fields(tmp_path: Path) -> None:
    from booley.harness.setup.intake import _verify_target_contract

    contract = MagicMock()
    contract.as_dict.return_value = {"schema": 3}
    ctx = TicketContext(
        slug="sealed-ticket",
        ticket_path=tmp_path / "ticket.md",
        ticket_type="feature",
        branch="main",
        summary="Sealed Ticket",
        criteria={"mandatory": {}},
        project_root=tmp_path,
        base_sha="a" * 40,
        target_contract=contract,
    )

    with (
        patch(
            "booley.ticket_board.contract_ops.validate_sealed_refs",
            return_value=[],
        ) as validate_refs,
        patch(
            "booley.ticket_board.target_contract.validate_contract_fields",
            return_value=[],
        ) as validate_fields,
    ):
        _verify_target_contract(ctx, "fresh")

    validate_refs.assert_called_once_with(
        tmp_path,
        contract,
        slug="sealed-ticket",
        destination_branch="main",
    )
    validate_fields.assert_called_once_with(
        {
            "base_sha": "a" * 40,
            "target_contract": {"schema": 3},
            "criteria": {"mandatory": {}},
        }
    )


# ---------------------------------------------------------------------------
# Return value
# ---------------------------------------------------------------------------


class TestReturnValue:
    """Kills: L174 return_None"""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_run_returns_ticket_context(self, mock_cli, project_root, sample_ticket):
        _mock_cli_defaults(mock_cli)
        from booley.harness.setup.intake import run

        result = await run(str(sample_ticket), project_root)
        assert result is not None
        assert isinstance(result, TicketContext)
        assert result.slug == sample_ticket.stem


def test_fpga_relative_criterion_freezes_recipe_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FPGA QoR intake pins the same sealed-recipe evidence as synthesis."""
    from booley.dev_support.criteria import BASELINE_TARGET_PARAM
    from booley.flows.recipe_evidence import (
        BASELINE_REF_PARAM,
        RECIPE_FINGERPRINT_PARAM,
        RECIPE_SNAPSHOT_PARAM,
    )
    from booley.fusesoc import fusesoc_registry
    from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
    from booley.harness.setup.intake import _freeze_fpga_recipe_fingerprints

    xdc = tmp_path / "timing.xdc"
    xdc.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
    resolved = ResolvedTarget(
        name="fpga_core",
        vlnv="::core:0",
        toplevel="top",
        eda_tool="vivado",
        files=(ResolvedFile(name="timing.xdc", file_type="xdc"),),
        parameters={},
        build_root=tmp_path,
        edam_path=tmp_path / "core.eda.yml",
        flow_options={"tool": "vivado", "part": "xc7a35tcpg236-1"},
    )
    monkeypatch.setattr(fusesoc_registry, "resolve_ref", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        fusesoc_registry,
        "resolve_target",
        lambda *_args, **_kwargs: resolved,
    )
    from booley.flows import baseline_worktree as baseline_module

    @contextmanager
    def fake_baseline_worktree(project_root, _ref):
        yield Path(project_root)

    monkeypatch.setattr(baseline_module, "baseline_worktree", fake_baseline_worktree)
    ctx = TicketContext(
        slug="fpga-qor",
        ticket_path=tmp_path / "ticket.md",
        ticket_type="feature",
        branch="main",
        summary="FPGA QoR",
        project_root=tmp_path,
        worktree_path=tmp_path,
        base_sha="a" * 40,
    )
    params = {
        "fpga_impl_ok_fpga_after": {
            "lut_count_increase_at_most": 10,
            BASELINE_TARGET_PARAM: "fpga_before",
        }
    }

    _freeze_fpga_recipe_fingerprints(
        ctx,
        {"fpga_impl_ok_fpga_after": True},
        params,
    )

    frozen = params["fpga_impl_ok_fpga_after"]
    assert frozen[BASELINE_REF_PARAM] == "a" * 40
    assert frozen[RECIPE_FINGERPRINT_PARAM]
    assert frozen[RECIPE_SNAPSHOT_PARAM]["target"] == "fpga_before"
    assert frozen[RECIPE_SNAPSHOT_PARAM]["flow_options"]["part"] == "xc7a35tcpg236-1"


def test_cycle_count_relative_criterion_pins_ticket_baseline(tmp_path: Path) -> None:
    from booley.flows.recipe_evidence import BASELINE_REF_PARAM
    from booley.harness.setup.intake import _pin_cycle_count_baselines

    ctx = TicketContext(
        slug="cycle-qor",
        ticket_path=tmp_path / "ticket.md",
        ticket_type="feature",
        branch="main",
        summary="Cycle QoR",
        project_root=tmp_path,
        base_sha="a" * 40,
    )
    params = {
        "cycle_count_binding": {
            "target": "sim_core",
            "test": "coremark",
            "cycle_count_reduce_at_least": 5,
        }
    }

    _pin_cycle_count_baselines(ctx, params)

    assert params["cycle_count_binding"][BASELINE_REF_PARAM] == "a" * 40


def test_cycle_count_absolute_criterion_needs_no_ticket_baseline(tmp_path: Path) -> None:
    from booley.harness.setup.intake import _pin_cycle_count_baselines

    ctx = TicketContext(
        slug="cycle-cap",
        ticket_path=tmp_path / "ticket.md",
        ticket_type="feature",
        branch="main",
        summary="Cycle cap",
        project_root=tmp_path,
    )
    params = {
        "cycle_count_binding": {
            "target": "sim_core",
            "test": "coremark",
            "cycle_count_max": 100,
        }
    }

    _pin_cycle_count_baselines(ctx, params)

    assert "_baseline_ref" not in params["cycle_count_binding"]


# ---------------------------------------------------------------------------
# Resume action dispatch
# ---------------------------------------------------------------------------


class TestResumeActions:
    """Kills: action == comparisons (L115, L120, L127, L148, L155)
    and their negate_if twins."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_fresh_inits_and_clears_stages(self, mock_cli, project_root, sample_ticket):
        _mock_cli_defaults(mock_cli, action="fresh")
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        mock_cli.init_ticket.assert_called_once()
        assert ctx.completed_steps == []
        assert ctx.current_step == ""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_continue_restores_stages(self, mock_cli, project_root, sample_ticket):
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="continue")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning", "lint-check"],
            },
        )
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.completed_steps == ["planning", "lint-check"]
        # current_step must be LAST completed (not next)
        assert ctx.current_step == "lint-check"
        assert ctx.workspace_intent == "resume"
        mock_cli.init_ticket.assert_not_called()

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_unblock_marker_survives_missing_step_history(
        self, mock_cli, project_root, sample_ticket
    ):
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="fresh")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": [],
                "workspace_intent": "resume",
            },
        )
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)

        assert ctx.workspace_intent == "resume"

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_continue_empty_steps(self, mock_cli, project_root, sample_ticket):
        """Kills: L125 off-by-one ([-1] → [-2]) on empty list."""
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="continue")
        _write_progress(project_root, slug, {"steps_completed": []})
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.completed_steps == []
        assert ctx.current_step == ""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_continue_single_step(self, mock_cli, project_root, sample_ticket):
        """Kills: L125 off-by-one — [-1] vs [-2] differs with single element."""
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="continue")
        _write_progress(project_root, slug, {"steps_completed": ["planning"]})
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.current_step == "planning"


# ---------------------------------------------------------------------------
# Resume-blocked: unanswered question checks
# ---------------------------------------------------------------------------


class TestResumeBlocked:
    """Kills: L127 cmpop/negate, L134 boolop/cmpop/negate, L136 negate,
    L142 negate."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_unanswered_questions_raises(self, mock_cli, project_root, sample_ticket):
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="resume_blocked")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning"],
                "blocked_reason": "question: needs clarification",
            },
        )
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
        (logs_dir / "questions.md").write_text("## Q1\n**Answer:**\n\n## Q2", encoding="utf-8")
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match="not yet answered"):
            await run(str(sample_ticket), project_root)

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_answered_questions_continues(self, mock_cli, project_root, sample_ticket):
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="resume_blocked")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning"],
                "blocked_reason": "question: needs clarification",
            },
        )
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
        (logs_dir / "questions.md").write_text(
            "## Q1\n**Answer:**\nThe fix is to widen the bus.\n\n", encoding="utf-8"
        )
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.current_step == "planning"

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_unresolved_keyword_also_triggers_check(
        self, mock_cli, project_root, sample_ticket
    ):
        """Kills: L134 boolop Or→And — both 'question' and 'unresolved' should trigger."""
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="resume_blocked")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning"],
                "blocked_reason": "unresolved: waiting for input",
            },
        )
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
        (logs_dir / "questions.md").write_text("## Q1\n**Answer:**\n\n", encoding="utf-8")
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match="not yet answered"):
            await run(str(sample_ticket), project_root)

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_non_question_block_skips_check(self, mock_cli, project_root, sample_ticket):
        """An EDA-tool failure should not trigger the question check."""
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="resume_blocked")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning"],
                "blocked_reason": "eda_tool_failure: vivado crashed",
            },
        )
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.current_step == "planning"

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_no_questions_file_continues(self, mock_cli, project_root, sample_ticket):
        """Kills: L136 negate_if — missing questions.md should not crash."""
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="resume_blocked")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning"],
                "blocked_reason": "question: something",
            },
        )
        from booley.harness.setup.intake import run

        # No questions.md exists — should continue
        ctx = await run(str(sample_ticket), project_root)
        assert ctx is not None


# ---------------------------------------------------------------------------
# Activation logic
# ---------------------------------------------------------------------------


class TestActivation:
    """Kills: L171 cmpop NotEq→Eq and negate_if."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_fresh_does_not_activate(self, mock_cli, project_root, sample_ticket):
        _mock_cli_defaults(mock_cli, action="fresh")
        from booley.harness.setup.intake import run

        await run(str(sample_ticket), project_root)
        mock_cli.activate.assert_not_called()
        mock_cli.init_ticket.assert_called_once()
        assert mock_cli.init_ticket.call_args.kwargs["execution_id"]
        assert mock_cli.init_ticket.call_args.kwargs["owner_pid"] == os.getpid()

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_continue_activates(self, mock_cli, project_root, sample_ticket):
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="continue")
        _write_progress(project_root, slug, {"steps_completed": ["planning"]})
        from booley.harness.setup.intake import run

        await run(str(sample_ticket), project_root)
        mock_cli.activate.assert_called_once()
        args, kwargs = mock_cli.activate.call_args
        assert args == (project_root, slug)
        assert kwargs["owner_pid"] == os.getpid()
        assert kwargs["execution_id"]


# ---------------------------------------------------------------------------
# Context defaults
# ---------------------------------------------------------------------------


class TestContextDefaults:
    """Kills: L69 has_synth=False default, on_success defaults."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_defaults_when_fields_missing(self, mock_cli, project_root, sample_ticket):
        _mock_cli_defaults(mock_cli, action="fresh", fields={})
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.ticket_type == "feature"
        assert ctx.branch == "master"
        assert ctx.has_synth is False
        assert ctx.on_success.destination == "review"
        assert ctx.on_success.merge is True
        assert ctx.on_success.cleanup is True
        assert ctx.priority == "medium"
        assert ctx.scope == []
        assert ctx.sim_targets == []


# ---------------------------------------------------------------------------
# Progress.json loading
# ---------------------------------------------------------------------------


class TestProgressLoading:
    """Kills: L109 negate_if (progress.json exists check)."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_progress_json_loaded(self, mock_cli, project_root, sample_ticket):
        slug = sample_ticket.stem
        _mock_cli_defaults(mock_cli, action="continue")
        _write_progress(
            project_root,
            slug,
            {
                "steps_completed": ["planning", "lint-check", "rtl-review-1"],
            },
        )
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.completed_steps == ["planning", "lint-check", "rtl-review-1"]
        assert ctx.current_step == "rtl-review-1"

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_missing_progress_json_uses_ticket_fields(
        self, mock_cli, project_root, sample_ticket
    ):
        """No progress.json — falls back to fields from parse_ticket."""
        fields = dict(_MINIMAL_FIELDS, steps_completed=["planning"])
        _mock_cli_defaults(mock_cli, action="continue", fields=fields)
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx.completed_steps == ["planning"]


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------


class TestDependencies:
    """Kills: L90 negate_if, L93 not-in check."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_all_deps_met_continues(self, mock_cli, project_root, sample_ticket):
        fields = dict(_MINIMAL_FIELDS, dependencies=["dep-a", "dep-b"])
        _mock_cli_defaults(mock_cli, action="fresh", fields=fields)
        done_dir = project_root / ".booley" / "project" / "tickets" / "board" / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / "dep-a.md").write_text("---\n---\n", encoding="utf-8")
        (done_dir / "dep-b.md").write_text("---\n---\n", encoding="utf-8")
        from booley.harness.setup.intake import run

        ctx = await run(str(sample_ticket), project_root)
        assert ctx is not None

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_partial_deps_raises(self, mock_cli, project_root, sample_ticket):
        fields = dict(_MINIMAL_FIELDS, dependencies=["dep-a", "dep-missing"])
        _mock_cli_defaults(mock_cli, action="fresh", fields=fields)
        done_dir = project_root / ".booley" / "project" / "tickets" / "board" / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / "dep-a.md").write_text("---\n---\n", encoding="utf-8")
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match=r"Unmet dependencies.*dep-missing"):
            await run(str(sample_ticket), project_root)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    """Kills: L182-207 return_None survivors, boolop And→Or."""

    def test_absolute_path(self, project_root, sample_ticket):
        from booley.harness.setup.intake import _resolve_ticket_path

        result = _resolve_ticket_path(project_root, str(sample_ticket))
        assert result == sample_ticket

    def test_relative_to_project_root(self, project_root, sample_ticket):
        from booley.harness.setup.intake import _resolve_ticket_path

        rel = sample_ticket.relative_to(project_root)
        result = _resolve_ticket_path(project_root, str(rel))
        assert result == sample_ticket

    def test_relative_to_tickets_dir(self, project_root, sample_ticket):
        from booley.harness.setup.intake import _resolve_ticket_path

        # .tickets/queue/fix-fsm-counter.md → relative to .tickets/
        rel = sample_ticket.relative_to(project_root / ".booley" / "project" / "tickets")
        result = _resolve_ticket_path(project_root, str(rel))
        assert result == sample_ticket

    def test_slug_search_in_board_dirs(self, project_root):
        """Slug search finds tickets in board/ subdirectories."""
        from booley.harness.setup.intake import _resolve_ticket_path

        board_queue = project_root / ".booley" / "project" / "tickets" / "board" / "queue"
        board_queue.mkdir(parents=True, exist_ok=True)
        path = board_queue / "my-slug.md"
        path.write_text("---\n---\n", encoding="utf-8")
        result = _resolve_ticket_path(project_root, "my-slug")
        assert result == path

    def test_adds_md_extension(self, project_root):
        from booley.harness.setup.intake import _resolve_ticket_path

        board_queue = project_root / ".booley" / "project" / "tickets" / "board" / "queue"
        board_queue.mkdir(parents=True, exist_ok=True)
        path = board_queue / "some-case.md"
        path.write_text("---\n---\n", encoding="utf-8")
        # Pass without .md — should find it
        result = _resolve_ticket_path(project_root, "some-case")
        assert result.suffix == ".md"

    def test_not_found_raises(self, project_root):
        from booley.harness.setup.intake import _resolve_ticket_path

        with pytest.raises(FatalError, match="Ticket not found"):
            _resolve_ticket_path(project_root, "nonexistent-ticket-xyz")


# ---------------------------------------------------------------------------
# Auto-select
# ---------------------------------------------------------------------------


class TestAutoSelect:
    """Kills: auto-selection with atomic claim (§7 concurrency fix)."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_auto_select_claims_ticket(self, mock_cli, project_root):
        """claim() is called for each candidate until one succeeds."""
        from booley.harness.setup.intake import run

        # Create two tickets — claim succeeds on first try
        active = project_root / ".booley" / "project" / "tickets" / "board" / "active"
        active.mkdir(parents=True, exist_ok=True)
        (active / "first.md").write_text("---\nsummary: first\n---\n", encoding="utf-8")
        mock_cli.classify.return_value = {
            "executable": [
                {"file": "board/queue/first.md", "slug": "first"},
                {"file": "board/queue/second.md", "slug": "second"},
            ]
        }
        mock_cli.claim.return_value = True
        _mock_cli_defaults(mock_cli, action="fresh")
        ctx = await run("", project_root)
        assert ctx.slug == "first"
        mock_cli.claim.assert_called_once_with(project_root, "first")

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_auto_select_skips_claimed_tickets(self, mock_cli, project_root):
        """When first ticket is already claimed, tries the next one."""
        from booley.harness.setup.intake import run

        active = project_root / ".booley" / "project" / "tickets" / "board" / "active"
        active.mkdir(parents=True, exist_ok=True)
        (active / "second.md").write_text("---\nsummary: second\n---\n", encoding="utf-8")
        mock_cli.classify.return_value = {
            "executable": [
                {"file": "board/queue/first.md", "slug": "first"},
                {"file": "board/queue/second.md", "slug": "second"},
            ]
        }
        mock_cli.claim.side_effect = [False, True]
        _mock_cli_defaults(mock_cli, action="fresh")
        ctx = await run("", project_root)
        assert ctx.slug == "second"
        assert mock_cli.claim.call_count == 2

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_auto_select_all_claimed_raises(self, mock_cli, project_root):
        """If all executable tickets are claimed, raises FatalError."""
        from booley.harness.setup.intake import run

        mock_cli.classify.return_value = {
            "executable": [
                {"file": "board/queue/first.md", "slug": "first"},
            ]
        }
        mock_cli.claim.return_value = False
        with pytest.raises(FatalError, match="All executable tickets claimed"):
            await run("", project_root)

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_auto_select_reports_blocked_and_waiting(self, mock_cli, project_root):
        """Error message should mention blocked and waiting counts."""
        mock_cli.classify.return_value = {
            "executable": [],
            "blocked": [{"slug": "a"}, {"slug": "b"}],
            "waiting": [{"slug": "c"}],
        }
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match=r"2 blocked.*1 waiting"):
            await run("", project_root)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_invalid_ticket_raises(self, mock_cli, project_root, sample_ticket):
        mock_cli.validate_ticket.return_value = {
            "valid": False,
            "errors": ["missing summary", "bad scope"],
        }
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match=r"missing summary.*bad scope"):
            await run(str(sample_ticket), project_root)


# ---------------------------------------------------------------------------
# Migration guards
# ---------------------------------------------------------------------------


class TestMigrationGuards:
    """Verify old-style plan fields / criteria fail loudly."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_old_plan_file_rejected(self, mock_cli, project_root, sample_ticket):
        fields = dict(_MINIMAL_FIELDS, plan_file="plans/plan.md")
        _mock_cli_defaults(mock_cli, fields=fields)
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match="plan_file"):
            await run(str(sample_ticket), project_root)

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_old_plan_done_criterion_rejected(self, mock_cli, project_root, sample_ticket):
        fields = dict(_MINIMAL_FIELDS)
        fields["criteria"] = {"mandatory": {"plan_done": True, "sim_pass": True}}
        _mock_cli_defaults(mock_cli, fields=fields)
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match="plan_done"):
            await run(str(sample_ticket), project_root)

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_old_plan_created_criterion_rejected(
        self, mock_cli, project_root, sample_ticket
    ):
        fields = dict(_MINIMAL_FIELDS)
        fields["criteria"] = {"mandatory": {"plan_created": True}}
        _mock_cli_defaults(mock_cli, fields=fields)
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError, match="plan_created"):
            await run(str(sample_ticket), project_root)

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_new_plan_fields_accepted(self, mock_cli, project_root, sample_ticket):
        fields = dict(
            _MINIMAL_FIELDS,
            rtl_plan_file="",
            verification_plan_file="",
        )
        _mock_cli_defaults(mock_cli, fields=fields)
        from booley.harness.setup.intake import run

        # Should not raise
        result = await run(str(sample_ticket), project_root)
        assert result is not None

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    async def test_scoped_ticket_does_not_inject_plan_criteria(
        self,
        mock_cli,
        project_root,
        sample_ticket,
    ):
        fields = dict(
            _MINIMAL_FIELDS,
            scope=["rtl/foo.sv", "tb/foo_tb.sv"],
            criteria={
                "mandatory": {
                    "sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"],
                }
            },
        )
        _mock_cli_defaults(mock_cli, fields=fields)
        from booley.dev_support.development_state import DevelopmentState
        from booley.harness.setup.intake import run
        from booley.ticket_board.helpers import tickets_dir_from_project_root

        await run(str(sample_ticket), project_root)

        state_path = (
            tickets_dir_from_project_root(project_root)
            / "logs"
            / sample_ticket.stem
            / ".runtime"
            / "booley_state.json"
        )
        state = DevelopmentState.load(state_path)
        assert "rtl_plan_done" not in state.criteria
        assert "verification_plan_done" not in state.criteria

    @pytest.mark.asyncio
    @patch("booley.harness.setup.intake.ticket_cli")
    @pytest.mark.parametrize(
        ("retired", "hint"),
        [
            ("rtl_plan_done", "remove it"),
            ("verification_plan_done", "remove it"),
            ("review_rtl_functional", "review_rtl_bugs"),
            ("review_rtl_quality", "review_rtl_code_style"),
            ("review_rtl_ifdef", "review_rtl_bugs"),
        ],
    )
    async def test_retired_criteria_are_rejected(
        self,
        mock_cli,
        project_root,
        sample_ticket,
        retired,
        hint,
    ):
        """Retired keys must hard-error, not be created as silent optional no-ops."""
        fields = dict(
            _MINIMAL_FIELDS,
            scope=["rtl/foo.sv"],
            criteria={
                "mandatory": {
                    retired: True,
                    "sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"],
                }
            },
        )
        _mock_cli_defaults(mock_cli, fields=fields)
        from booley.harness.blocking import FatalError
        from booley.harness.setup.intake import run

        with pytest.raises(FatalError) as excinfo:
            await run(str(sample_ticket), project_root)
        assert retired in str(excinfo.value)
        assert hint in str(excinfo.value)
        # slug MUST be set: the developer's FatalError handler only persists the
        # error (via ticket_cli.fail) and moves the ticket out of active/ when
        # e.slug is truthy. Without it the real error is lost to the console and
        # the ticket is orphaned, later mislabeled as a "SIGINT" crash.
        assert excinfo.value.slug == sample_ticket.stem
