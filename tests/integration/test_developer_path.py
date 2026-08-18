"""E2E tests -- developer (criteria-based) execution path.

Tests verify that criteria-based tickets flow through _run_developer_path()
instead of the legacy step loop. The key mock target is
``harness.developer._launch_developer_agent`` -- a single agent call
that drives all work via tools. The mock simulates the agent by directly
manipulating the state file (booley_state.json).

Test matrix:
  1. Simple pass -- all mandatory criteria met
  2. Partial fail -- not all criteria met
  3. Blocked -- agent writes _blocked_reason
  4. Agent timeout -- AgentResult.timed_out=True
  5. Crash recovery -- prior transcript triggers is_crash_recovery
  6. Leftover-edit guardrail -- uncommitted files committed before handoff
  7. Env vars -- BOOLEY_SLUG, BOOLEY_LOGS_DIR, BOOLEY_STATE_FILE passed
  8. Criteria expansion -- per-config criteria expanded correctly
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.harness.models import AgentResult

from .conftest import make_setup_bypass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_result(
    *,
    timed_out: bool = False,
    max_turns_exhausted: bool = False,
) -> AgentResult:
    """Build a realistic AgentResult for the developer agent."""
    return AgentResult(
        output="done",
        input_tokens=1000,
        output_tokens=500,
        timed_out=timed_out,
        max_turns_exhausted=max_turns_exhausted,
    )


def _create_criteria_ticket(
    project_root: Path,
    slug: str,
    *,
    criteria_yaml: dict | None = None,
    ticket_type: str = "bugfix",
    sim_targets: list[str] | None = None,
) -> Path:
    """Create a criteria-based ticket .md file in queue/.

    Uses format_frontmatter for consistent serialization.
    Returns the ticket path.
    """
    from booley.ticket_board.frontmatter import format_frontmatter

    sim_targets = sim_targets or ["config_a"]
    scope = ["rtl/my_module.sv", "tb/my_module_tb.sv"]

    # Build scope stubs
    for f in scope:
        p = project_root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(f"// stub: {f}\n", encoding="utf-8")

    # Default criteria section
    if criteria_yaml is None:
        criteria_yaml = {
            "mandatory": {
                "lint_clean": sim_targets,
                "sim_pass": sim_targets,
            },
        }

    fields = {
        "summary": f"E2E developer test {slug}",
        "type": ticket_type,
        "branch": "main",
        "scope": scope,
        "criteria": criteria_yaml,
        "on_success": {"destination": "review", "merge": False, "cleanup": False},
        "priority": "high",
    }
    body = f"## Description\nE2E developer test: {slug}\n"
    content = format_frontmatter(fields, body)

    ticket_path = (
        project_root / ".booley" / "project" / "tickets" / "board" / "queue" / f"{slug}.md"
    )
    ticket_path.write_text(content, encoding="utf-8")
    return ticket_path


def _tickets_dir(project_root: Path) -> Path:
    return project_root / ".booley" / "project" / "tickets"


def _logs_dir(project_root: Path, slug: str) -> Path:
    return _tickets_dir(project_root) / "logs" / slug


def _state_path(project_root: Path, slug: str) -> Path:
    return _logs_dir(project_root, slug) / ".runtime" / "booley_state.json"


def _ensure_run_log(project_root: Path, slug: str) -> None:
    """Create the run.log file the handoff operation expects."""
    logs = _tickets_dir(project_root) / "logs"
    log_dir = logs / slug
    human_logs = log_dir / "human-logs"
    human_logs.mkdir(parents=True, exist_ok=True)
    (human_logs / "run.log").write_text("# E2E test run log\n", encoding="utf-8")


def _ticket_in_dir(project_root: Path, slug: str, status: str) -> bool:
    """Check if ticket file exists in the given status directory."""
    path = _tickets_dir(project_root) / "board" / status / f"{slug}.md"
    return path.exists()


# ---------------------------------------------------------------------------
# Mock factory for _launch_developer_agent
# ---------------------------------------------------------------------------


def _stamp_verification_fingerprints(state_path: Path, work_dir: Path) -> None:
    """Attach source fingerprints to met verification criteria.

    Mirrors ToolBase._stamp_source_fingerprint (tools/base.py): real tools
    stamp a source fingerprint when marking sim/lint/etc criteria met, and
    check_criteria_acceptance() marks unstamped passing criteria stale-unmet.
    The mock agent bypasses tools, so it must stamp explicitly.
    """
    from booley.dev_support.development_state import (
        SOURCE_FINGERPRINT_DETAIL_KEY,
        compute_source_fingerprint,
    )
    from booley.ticket_board.criteria_acceptance import (
        _verification_fingerprint_categories,
    )

    try:
        fingerprint = compute_source_fingerprint(Path(work_dir))
    except OSError:
        return
    state = DevelopmentState.load(state_path)
    changed = False
    for key, entry in state.criteria.items():
        if key.startswith("_") or not entry.met:
            continue
        categories = _verification_fingerprint_categories(key)
        if not categories:
            continue
        entry.detail = dict(entry.detail or {})
        entry.detail[SOURCE_FINGERPRINT_DETAIL_KEY] = {
            "categories": sorted(categories),
            "fingerprint": fingerprint,
        }
        changed = True
    if changed:
        state.save()


def _make_developer_mock(
    project_root: Path,
    slug: str,
    *,
    state_updater=None,
    timed_out: bool = False,
    max_turns_exhausted: bool = False,
    raises: Exception | None = None,
):
    """Build an AsyncMock for _launch_developer_agent.

    The mock:
      1. Calls ``state_updater(state_path)`` to simulate tool runs
      2. Returns an AgentResult with token counts
      3. Records the call kwargs for assertion
    """
    call_records: list[dict] = []

    async def _mock_agent(
        prompt,
        *,
        system_prompt,
        cwd,
        slug: str = "",
        ticket_type: str = "",
        transcript_path=None,
        state_path,
        logs_dir,
        mcp_tools=None,
        project_root=None,
        on_event=None,
    ):
        call_records.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "cwd": cwd,
                "slug": slug,
                "ticket_type": ticket_type,
                "transcript_path": transcript_path,
                "state_path": state_path,
                "logs_dir": logs_dir,
            }
        )
        if raises:
            raise raises
        if state_updater:
            state_updater(state_path)
            # Real tools stamp source fingerprints on met verification
            # criteria; without them acceptance marks the pass stale.
            _stamp_verification_fingerprints(state_path, cwd)
        return _make_agent_result(
            timed_out=timed_out,
            max_turns_exhausted=max_turns_exhausted,
        )

    _mock_agent.call_records = call_records
    return _mock_agent


# ---------------------------------------------------------------------------
# Harness runner helper
# ---------------------------------------------------------------------------


async def _run_developer_pipeline(
    project_root: Path,
    slug: str,
    *,
    developer_mock,
    worktree_factory=None,
    setup_bypass=None,
    extra_patches: list | None = None,
    check_uncommitted_return: list[str] | None = None,
    check_uncommitted_side_effect: list[list[str]] | None = None,
    commit_scope_mock=None,
):
    """Run the harness with developer path mocked.

    Applies standard patches:
      - _launch_developer_agent
      - run_summary_agent (returns stub path)
      - check_uncommitted_code_statuses + leftover-edit commit (configurable)
      - load_models_config (no-op)

    ``check_uncommitted_*`` args take plain path strings; they are converted
    to the ``DirtyFile`` entries that ``check_uncommitted_code_statuses``
    (the function the developer actually calls) returns. Status " M"
    (modified) is used so scope matching treats them as owned edits.
    """
    from booley.harness.developer_guardrails import DirtyFile

    if commit_scope_mock is None:
        commit_scope_mock = MagicMock(return_value=None)

    def _as_dirty(paths: list[str]) -> list[DirtyFile]:
        return [DirtyFile(path=p, status=" M") for p in paths]

    check_uncommitted_patch = (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[_as_dirty(paths) for paths in check_uncommitted_side_effect],
        )
        if check_uncommitted_side_effect is not None
        else patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            return_value=_as_dirty(check_uncommitted_return or []),
        )
    )

    patches = [
        patch(
            "booley.harness.developer._launch_developer_agent",
            side_effect=developer_mock,
        ),
        check_uncommitted_patch,
        patch(
            "booley.runtime.git.commit_scope",
            side_effect=commit_scope_mock,
        ),
        # Bypass model config loading (no booley.toml in test env)
        patch("booley.config.settings.load_models_config"),
    ]

    # Worktree setup bypass (for tests that go through workspace setup)
    if setup_bypass:
        patches.append(
            patch("booley.harness.setup.workspace.run", side_effect=setup_bypass),
        )

    if extra_patches:
        patches.extend(extra_patches)

    from booley.harness.developer import run_ticket

    for p in patches:
        p.start()
    try:
        await run_ticket(
            f".booley/project/tickets/board/queue/{slug}.md",
            project_root,
            save_transcripts=False,
        )
    finally:
        for p in reversed(patches):
            p.stop()


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.e2e
class TestDeveloperSimplePass:
    """Criteria-based ticket where all mandatory criteria are met."""

    SLUG = "e2e-orch-simple-pass"

    def test_developer_simple_pass(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        # Create criteria ticket with 2 mandatory criteria
        _create_criteria_ticket(
            project_root,
            slug,
            criteria_yaml={
                "mandatory": {
                    "lint_clean": ["config_a"],
                    "sim_pass": ["config_a"],
                },
            },
        )

        # Prepare summary.md so handoff succeeds
        _ensure_run_log(project_root, slug)

        # Mock: developer agent sets all criteria to met
        def _set_all_met(state_path):
            state = DevelopmentState.load(state_path)
            for key in list(state.criteria.keys()):
                state.set_criterion(key, True)
            state.save()

        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(
            project_root,
            slug,
            state_updater=_set_all_met,
        )

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Ticket should be in review/
        assert _ticket_in_dir(project_root, slug, "review"), (
            "Ticket should have moved to review/ after all criteria met"
        )


@pytest.mark.e2e
class TestDeveloperPartialFail:
    """Criteria-based ticket where not all mandatory criteria are met."""

    SLUG = "e2e-orch-partial-fail"

    def test_developer_partial_fail(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        _create_criteria_ticket(
            project_root,
            slug,
            criteria_yaml={
                "mandatory": {
                    "lint_clean": ["config_a"],
                    "sim_pass": ["config_a"],
                    "synthesis_ok": ["config_a"],
                },
            },
        )

        # Mock: only 2 of 3 criteria met (synthesis_ok_config_a left unmet)
        def _set_partial(state_path):
            state = DevelopmentState.load(state_path)
            state.set_criterion("lint_clean_config_a", True)
            state.set_criterion("sim_pass_config_a", True)
            # synthesis_ok_config_a stays unmet
            state.save()

        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(
            project_root,
            slug,
            state_updater=_set_partial,
        )

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Ticket should be in blocked/ (fail_ticket delegates to block)
        assert _ticket_in_dir(project_root, slug, "blocked"), (
            "Ticket should have moved to blocked/ with unmet criteria"
        )

        # Verify state file still has the unmet criterion
        state = DevelopmentState.load(_state_path(project_root, slug))
        assert not state.is_met("synthesis_ok_config_a")
        assert state.is_met("lint_clean_config_a")
        assert state.is_met("sim_pass_config_a")


@pytest.mark.e2e
class TestDeveloperBlocked:
    """Criteria-based ticket where agent writes _blocked_reason."""

    SLUG = "e2e-orch-blocked"

    def test_developer_blocked(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        _create_criteria_ticket(project_root, slug)

        # Mock: agent writes _blocked_reason to state
        def _set_blocked(state_path):
            state = DevelopmentState.load(state_path)
            state.set_criterion(
                "_blocked_reason",
                True,
                detail={"reason": "Missing specification for FFT module"},
            )
            state.save()

        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(
            project_root,
            slug,
            state_updater=_set_blocked,
        )

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Ticket should be in blocked/
        assert _ticket_in_dir(project_root, slug, "blocked"), (
            "Ticket should have moved to blocked/ with _blocked_reason"
        )


@pytest.mark.e2e
class TestDeveloperAgentTimeout:
    """Criteria-based ticket where agent times out."""

    SLUG = "e2e-orch-timeout"

    def test_developer_agent_timeout(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        _create_criteria_ticket(project_root, slug)

        # Mock: agent times out with no criteria met
        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(
            project_root,
            slug,
            timed_out=True,
        )

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Ticket should be in blocked/ (fail path due to unmet criteria)
        assert _ticket_in_dir(project_root, slug, "blocked"), (
            "Timed-out ticket should have moved to blocked/"
        )


@pytest.mark.e2e
class TestDeveloperCrashRecovery:
    """Criteria-based ticket with prior transcript = crash recovery."""

    SLUG = "e2e-orch-crash-recovery"

    def test_developer_crash_recovery(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        _create_criteria_ticket(
            project_root,
            slug,
            criteria_yaml={
                "mandatory": {
                    "lint_clean": ["config_a"],
                    "sim_pass": ["config_a"],
                },
            },
        )
        _ensure_run_log(project_root, slug)

        logs = _logs_dir(project_root, slug)

        # Create prior transcript to trigger crash recovery.
        # Transcripts live under .runtime/ (see _detect_crash_recovery,
        # which globs ticket_runtime_dir(logs_dir)/"developer").
        transcript_dir = logs / ".runtime" / "developer"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        prior_transcript = transcript_dir / "run_001.jsonl"
        prior_transcript.write_text(
            '{"role":"assistant","content":"working on lint..."}\n',
            encoding="utf-8",
        )

        # Create partial state (lint met, sim not)
        sp = _state_path(project_root, slug)
        sp.parent.mkdir(parents=True, exist_ok=True)
        state = DevelopmentState(slug=slug, ticket_type="bugfix")
        state._file_path = sp
        state.init_criteria(
            {
                "lint_clean_config_a": True,
                "sim_pass_config_a": True,
            }
        )
        state.set_criterion("lint_clean_config_a", True)
        state.save()

        def _complete_remaining(state_path):
            state = DevelopmentState.load(state_path)
            for key in list(state.criteria.keys()):
                state.set_criterion(key, True)
            state.save()

        mock_agent = _make_developer_mock(
            project_root,
            slug,
            state_updater=_complete_remaining,
        )

        setup_bypass = make_setup_bypass(worktree_factory)

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Ticket should be in review/ (all criteria met)
        assert _ticket_in_dir(project_root, slug, "review"), (
            "Crash-recovered ticket should move to review/ when all criteria met"
        )

        # Verify the prompt included crash recovery context
        assert len(mock_agent.call_records) == 1
        user_prompt = mock_agent.call_records[0]["prompt"]
        assert "Crash Recovery" in user_prompt or "crash" in user_prompt.lower(), (
            "Prompt should contain crash recovery context when prior transcript exists"
        )

        # Verify transcript path is run_002 (second run)
        transcript = mock_agent.call_records[0]["transcript_path"]
        assert "run_002" in str(transcript), f"Expected run_002 transcript, got {transcript}"


@pytest.mark.e2e
class TestDeveloperGuardrailRollback:
    """Developer Agent leaves uncommitted edits -- guardrail commits them."""

    SLUG = "e2e-orch-guardrail"

    def test_developer_guardrail_commits_leftover_edits(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        # Validation requires a mandatory sim_* criterion for RTL/TB scope.
        _create_criteria_ticket(
            project_root,
            slug,
            criteria_yaml={
                "mandatory": {
                    "lint_clean": ["config_a"],
                    "sim_pass": ["config_a"],
                },
            },
        )
        _ensure_run_log(project_root, slug)

        # Mock: all criteria met
        def _set_all_met(state_path):
            state = DevelopmentState.load(state_path)
            for key in list(state.criteria.keys()):
                state.set_criterion(key, True)
            state.save()

        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(
            project_root,
            slug,
            state_updater=_set_all_met,
        )

        # Simulate uncommitted files detected; track the guardrail commit.
        # Dirty paths must be IN ticket scope -- out-of-scope rtl/ dirt is
        # treated as scorer contamination and blocks instead of committing.
        commit_mock = MagicMock(return_value=None)

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
                check_uncommitted_side_effect=[
                    ["rtl/my_module.sv", "tb/my_module_tb.sv"],
                    [],
                ],
                commit_scope_mock=commit_mock,
            )
        )

        assert commit_mock.called, "commit_scope should have committed the dirty worktree"
        # The mocked dirty statuses must flow through scope matching into
        # the commit -- proves the check_uncommitted_code_statuses patch
        # is live (it broke silently once when prod switched functions).
        committed_paths = commit_mock.call_args.args[1]
        assert committed_paths == ["rtl/my_module.sv", "tb/my_module_tb.sv"], (
            f"Expected in-scope dirty files committed, got {committed_paths}"
        )

        assert _ticket_in_dir(project_root, slug, "review"), (
            "Run should move to review/ after leftover edits are committed"
        )


@pytest.mark.e2e
class TestDeveloperEnvVars:
    """Verify slug, state_path, logs_dir are passed to the developer agent.

    Inside _launch_developer_agent these become BOOLEY_SLUG,
    BOOLEY_STATE_FILE, BOOLEY_LOGS_DIR env vars for the Docker container.
    We verify at the mock boundary that the correct values arrive.
    """

    SLUG = "e2e-orch-env-vars"

    def test_developer_env_vars_passed(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        _create_criteria_ticket(project_root, slug)

        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(project_root, slug)

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Verify kwargs passed to _launch_developer_agent
        assert len(mock_agent.call_records) == 1
        record = mock_agent.call_records[0]

        # slug kwarg (becomes BOOLEY_SLUG env var)
        assert record["slug"] == slug, f"Expected slug={slug!r}, got {record['slug']!r}"

        # state_path kwarg (becomes BOOLEY_STATE_FILE env var)
        sp = record["state_path"]
        assert sp is not None, "state_path must be passed"
        assert slug in str(sp), "state_path should contain the slug"
        assert str(sp).endswith("booley_state.json"), (
            f"state_path should end with booley_state.json, got {sp}"
        )

        # logs_dir kwarg (becomes BOOLEY_LOGS_DIR env var)
        logs = record["logs_dir"]
        assert logs is not None, "logs_dir must be passed"
        assert slug in str(logs), "logs_dir should contain the slug"


@pytest.mark.e2e
class TestDeveloperCriteriaExpansion:
    """Per-config criteria are expanded correctly in the state file."""

    SLUG = "e2e-orch-criteria-expand"

    def test_developer_criteria_expansion(
        self,
        project_root,
        worktree_factory,
    ):
        slug = self.SLUG

        # Create ticket with per-config criteria (2 configs)
        _create_criteria_ticket(
            project_root,
            slug,
            sim_targets=["lite", "full"],
            criteria_yaml={
                "mandatory": {
                    "lint_clean": ["lite", "full"],
                    "sim_pass": ["lite", "full"],
                },
                "optional": {
                    "review_rtl_bugs_done": "approved",
                },
            },
        )
        _ensure_run_log(project_root, slug)

        # Mock: set all criteria met
        def _set_all_met(state_path):
            state = DevelopmentState.load(state_path)
            for key in list(state.criteria.keys()):
                state.set_criterion(key, True)
            state.save()

        setup_bypass = make_setup_bypass(worktree_factory)
        mock_agent = _make_developer_mock(
            project_root,
            slug,
            state_updater=_set_all_met,
        )

        asyncio.run(
            _run_developer_pipeline(
                project_root,
                slug,
                developer_mock=mock_agent,
                setup_bypass=setup_bypass,
            )
        )

        # Verify the state file has expanded criteria
        state = DevelopmentState.load(_state_path(project_root, slug))

        # Per-config mandatory criteria should be expanded. Plan criteria
        # (rtl_plan_done / verification_plan_done) are no longer auto-injected
        # from scope (flexible development, commit 806a672).
        expected_mandatory = {
            "lint_clean_lite",
            "lint_clean_full",
            "sim_pass_lite",
            "sim_pass_full",
        }
        actual_mandatory = {
            k for k, e in state.criteria.items() if e.mandatory and not k.startswith("_")
        }
        assert expected_mandatory == actual_mandatory, (
            f"Expanded mandatory criteria mismatch.\n"
            f"  Expected: {sorted(expected_mandatory)}\n"
            f"  Got:      {sorted(actual_mandatory)}"
        )

        # Optional criteria should not be config-expanded
        assert "review_rtl_bugs_done" in state.criteria, (
            "Non-config criterion 'review_rtl_bugs_done' should be present"
        )
        assert not state.criteria["review_rtl_bugs_done"].mandatory, (
            "review_rtl_bugs_done should be optional"
        )

        # All should be met
        assert state.all_mandatory_met(), "All mandatory criteria should be met after mock agent"

        # Ticket should be in review/
        assert _ticket_in_dir(project_root, slug, "review"), (
            "Ticket should move to review/ when all criteria met"
        )
