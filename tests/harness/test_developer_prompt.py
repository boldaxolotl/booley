"""Tests for harness.developer_prompt -- prompt builder functions."""

from __future__ import annotations

import json
from pathlib import Path

from booley.harness.developer_prompt import (
    DeveloperPromptContext,
    build_blocked_section,
    build_crash_recovery_section,
    build_developer_prompt,
    build_ticket_section,
    build_type_guidance_section,
    build_workflow_section,
)
from booley.mcp.registry import McpToolInfo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mcp_tools() -> list[McpToolInfo]:
    return [
        McpToolInfo(
            name="tb_coder",
            path="mcp_tools/tb_coder.py",
            description="Implement testbench changes",
            code_modifying=True,
        ),
        McpToolInfo(name="sim", path="flows/sim/flow.py", description="Run simulation"),
    ]


def _make_state(
    tmp_path: Path, *, criteria: dict | None = None, timeline: list | None = None
) -> Path:
    """Write a booley_state.json and return its path."""
    state_path = tmp_path / "booley_state.json"
    data: dict = {}
    if criteria is not None:
        data["criteria"] = criteria
    if timeline is not None:
        data["timeline"] = timeline
    state_path.write_text(json.dumps(data), encoding="utf-8")
    return state_path


# ---------------------------------------------------------------------------
# build_developer_prompt
# ---------------------------------------------------------------------------


class TestBuildDeveloperPrompt:
    def test_returns_two_strings(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: test\n---\nBody", encoding="utf-8")
        state = _make_state(
            tmp_path,
            criteria={
                "lint": {"met": True, "mandatory": True},
            },
        )
        logs = tmp_path / "logs"
        logs.mkdir()

        result = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=logs,
                slug="test-slug",
                mcp_tools=_make_mcp_tools(),
            )
        )

        assert isinstance(result, tuple)
        assert len(result) == 2
        system, user = result
        assert isinstance(system, str)
        assert isinstance(user, str)

    def test_system_prompt_has_role_and_rules(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")
        state = tmp_path / "no_state.json"

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
            )
        )

        expected_role = """# Role

You are a digital design engineer, your task is to execute a development ticket. Meet every mandatory criterion and attempt every optional criterion

You have Booley Flows and any exposed Specialists at your disposal; use them appropriately
  - Use Booley Flows to run deterministic checks on your work: lint, simulation, synthesis, etc.
  - Use exposed Booley Specialists only when they are available; otherwise do the work yourself with your file operations and Booley Flows.
"""
        assert "# Role" in system
        assert expected_role in system
        assert system.count("# Role") == 1
        assert system.index("# Role") < system.index("# Rules")
        assert "You are a digital design engineer" in system
        assert "execute a development ticket" in system
        assert "Meet every mandatory criterion" in system
        assert "attempt every optional criterion" in system
        assert "Use Booley Flows to run deterministic checks" in system
        assert "Use exposed Booley Specialists only when they are available" in system
        assert "RTL engineering developer" not in system
        assert "high-level development" not in system
        assert "Specialists handle the low-level code" not in system
        assert "# Rules" in system
        assert "CRITERIA FRESHNESS" in system
        assert "TOOL DISCIPLINE" in system
        assert "never shorten it to its bare target name" in system
        assert "EDIT STRATEGY" in system
        assert "SPECIALIST DELEGATION" not in system
        assert "CRITERIA RESET" not in system
        assert "BOOLEY_TICKET_FILE" in system

    def test_run_report_rule_requires_unmet_optional_justification(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "state.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                run_report=True,
            )
        )

        assert "`optional_criteria_justification`" in system
        assert "explaining why each one could not be completed" in system
        assert "Commit every intended change" in system
        assert "clean before calling `submit_run_report`" in system

    def test_disabled_run_report_requires_justification_only_when_optional_unmet(
        self, tmp_path: Path
    ):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "state.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                run_report=False,
            )
        )

        assert "attempt every optional criterion" in system
        assert "disables routine end-of-run reports" in system
        assert "must still call `submit_run_report`" in system
        assert "any `_done` review" in system
        assert "optional_criteria_justification" in system
        assert "required in that case" in system
        assert "Commit every intended change" in system
        assert "clean before stopping" in system

    def test_criteria_freshness_rule_covers_terminal_reviews(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "no_state.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=_make_mcp_tools(),
            )
        )
        assert "simulation, and synthesis criteria before finishing" in system
        assert "A `_done` review is terminal and advisory" in system
        assert "later RTL/TB edits make it stale" in system
        assert "fix each finding or propose an explicit waiver" in system
        assert "Every accepted waiver" in system

    def test_baseline_qor_rule_requires_relative_implementation_criterion(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")
        phrase = "the sealed Target recipe is immutable"

        for criterion_name, threshold in (
            ("synthesis_ok", "cell_count_increase_at_most"),
            ("fpga_impl_ok", "clk.critical_path_ps_reduce_at_least"),
        ):
            system, _ = build_developer_prompt(
                DeveloperPromptContext(
                    ticket_path=ticket,
                    state_path=tmp_path / "state.json",
                    logs_dir=tmp_path,
                    slug="s",
                    mcp_tools=[],
                    criteria={
                        "optional": {
                            criterion_name: {
                                "targets": ["qor"],
                                threshold: 5,
                            }
                        }
                    },
                )
            )

            assert phrase in system
            assert "both `base_sha` and the ticket head with that identical recipe" in system
            assert "target-contract-change-required" in system

    def test_baseline_qor_rule_omitted_without_relative_implementation_criterion(
        self, tmp_path: Path
    ):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        for criteria in (
            None,
            {"mandatory": {"lint_clean": ["lint_core"]}},
            {
                "mandatory": {
                    "synthesis_ok": {
                        "targets": ["synth_core"],
                        "cell_count_max": 500,
                    }
                }
            },
        ):
            system, _ = build_developer_prompt(
                DeveloperPromptContext(
                    ticket_path=ticket,
                    state_path=tmp_path / "state.json",
                    logs_dir=tmp_path,
                    slug="s",
                    mcp_tools=[],
                    criteria=criteria,
                )
            )

            assert "BASELINE QoR CRITERIA" not in system
            assert "the sealed Target recipe is immutable" not in system

    def test_system_prompt_uses_targets_as_execution_boundary(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")
        state = tmp_path / "no_state.json"

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
            )
        )

        assert "Targets are the execution boundary" in system
        assert "complete runnable test suite" in system
        assert "dut_info" not in system

    def test_startup_instruction_points_to_ticket_snapshot(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: fix bug\n---\nDetails", encoding="utf-8")
        state = _make_state(
            tmp_path,
            criteria={
                "lint": {"met": False, "mandatory": True},
            },
        )

        _, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=tmp_path / "logs",
                slug="fix-bug",
                mcp_tools=_make_mcp_tools(),
            )
        )

        assert "BOOLEY_TICKET_FILE" in user
        assert "$BOOLEY_LOGS_DIR/ticket.md" in user

    def test_codex_backend_system_prompt(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "s.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                backend="codex",
            )
        )

        assert "You are a digital design engineer" in system
        assert "TOOL HELP" not in system
        assert "do not invoke `python -m booley.dev_support.<name>`" in system

    def test_claude_backend_system_prompt(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "s.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                backend="claude",
            )
        )

        assert "You are a digital design engineer" in system
        assert "TOOL HELP" not in system
        assert "do not invoke `python -m booley.dev_support.<name>`" in system

    def test_human_in_loop_true_omits_override(self, tmp_path: Path):
        """Default (human-in-loop=True) keeps original block-on-spec-gap rule."""
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "s.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                human_in_the_loop=True,
            )
        )

        # Original rule 3 phrasing remains and the override block is absent
        assert "missing spec, ambiguous requirement" in system
        assert "OVERRIDE (unattended mode" not in system
        assert "SPEC-INTERPRETATION" not in system

    def test_human_in_loop_false_renders_unattended_rule_3(self, tmp_path: Path):
        """Unattended mode renders the correct Rule 3 instead of an override."""
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "s.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=_make_mcp_tools(),
                human_in_the_loop=False,
            )
        )

        assert system.count("3. **BLOCKED**") == 1
        assert "OVERRIDE (unattended mode" not in system
        assert "Rule 3 above is overridden" not in system
        assert "block with those questions" not in system
        assert "no human operator will unblock the ticket" in system
        assert "answered_questions.md" in system
        assert system.index("3. **BLOCKED**") < system.index("4. **DEBUG LOOP**")

    def test_spec_arbiter_disabled_prompt_has_direct_flow(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")
        state = _make_state(
            tmp_path,
            criteria={
                "sim_pass_default": {"met": False, "mandatory": True},
            },
        )

        system, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=_make_mcp_tools(),
                human_in_the_loop=False,
                criteria={"mandatory": {"sim_pass": True}},
            )
        )

        assert "Workflow Regions" in user
        assert "advisory" in user.lower()
        assert "SPEC-ARBITER FLOW" not in system
        assert "spec_arbiter --mode" not in system
        assert "spec_decisions.json" not in system
        assert "spec_arbiter --mode" not in user

    def test_no_specialist_prompt_guides_direct_simdebug_and_tb_edits(
        self,
        tmp_path: Path,
    ):
        item = tmp_path / "item.md"
        item.write_text("---\nsummary: x\n---\n", encoding="utf-8")
        mcp_tools = [
            McpToolInfo(name="sim", path="flows/sim/flow.py", description="Run simulation"),
            McpToolInfo(name="elab", path="flows/elab/flow.py", description="Run elaboration"),
            McpToolInfo(name="lint", path="flows/lint/flow.py", description="Run lint"),
            McpToolInfo(
                name="submit_run_report",
                path="mcp_tools/submit_run_report.py",
                description="Submit final report",
            ),
            McpToolInfo(
                name="synth",
                path="flows/synth/flow.py",
                description="Run synthesis",
            ),
        ]

        prompt_kwargs = {
            "state_path": tmp_path / "s.json",
            "logs_dir": tmp_path,
            "slug": "s",
            "mcp_tools": mcp_tools,
            "mcp_tool_config": {
                "builtin": [mcp_tool.name for mcp_tool in mcp_tools],
                "custom": [],
            },
            "human_in_the_loop": False,
            "criteria": {"mandatory": {"sim_pass": True}},
        }
        prompt_kwargs["tick" + "et_path"] = item
        system, user = build_developer_prompt(DeveloperPromptContext(**prompt_kwargs))

        assert "run `simulate` and read the structured report" in system
        assert "treat Booley Flow reports as the verdict of record" in system
        assert 'bwave(extra_args=["skill"])' in system
        assert 'bwave(extra_args=["--help"])' in system
        assert "Prefer B-Wave queries" in system
        # Both RTL and TB code are authored by the developer (tb_coder is hidden).
        assert "Author both RTL and testbench code yourself" in system
        assert "`tb_coder`" not in system
        assert "use `debugger`" not in system
        assert "call `debugger`" not in system
        assert "fix with `coder`" not in system
        assert "coder instruction file" not in system
        assert (
            "**Implementation:** plan RTL/TB approach -> author both RTL and TB yourself" in user
        )
        assert "Use Booley Flows and your own edits freely" in user
        assert "spec_decisions.json" not in user

    def test_user_prompt_has_key_sections(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: fix bug\n---\nDetails", encoding="utf-8")
        state = _make_state(
            tmp_path,
            criteria={
                "lint": {"met": False, "mandatory": True},
            },
        )
        logs = tmp_path / "logs"
        logs.mkdir()

        _, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=logs,
                slug="fix-bug",
                mcp_tools=_make_mcp_tools(),
            )
        )

        assert "# Available Tools" not in user
        assert "# Ticket" in user
        assert user.lstrip().startswith("# Ticket")
        assert "How to Call Tools" not in user
        assert "# Workflow Regions" in user
        assert "# Plan" not in user
        assert "# Current Criteria State" not in user
        assert "Begin by reading `$BOOLEY_TICKET_FILE`" in user
        assert "choose the next appropriate action" in user
        assert "ticket markdown is embedded above" not in user
        assert "check the criteria state" not in user
        assert "existing plan paths" not in user
        assert "start calling Flows or Specialists" not in user
        assert "embedded above" not in user
        assert "Details" not in user

    def test_generated_prompt_uses_flexible_development(self, tmp_path: Path):
        case_path = tmp_path / "case.md"
        case_path.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=case_path,
                state_path=tmp_path / "state.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=_make_mcp_tools(),
                criteria={"mandatory": {"sim_pass": True}},
            )
        )
        combined = system + "\n" + user

        assert "MUST NOT write code directly" not in combined
        assert "TOOL ORDERING GATES" not in combined
        assert "Phase 1 (Planning)" not in combined
        assert "Workflow Regions" in combined
        assert "advisory" in combined
        assert "PLANNER TRIGGERS" not in combined
        assert "CODER TRIGGERS" not in combined
        assert "Author both RTL and testbench code yourself" in combined
        assert "`tb_coder`" not in combined
        assert "spec_arbiter --mode" not in combined
        assert "MULTI-TB PROTOCOL" not in combined
        assert "Sim-debug loop (x2)" not in combined

    def test_full_prompt_does_not_advertise_tb_coder(self, tmp_path: Path):
        """tb_coder is hidden: both RTL and TB are authored by the developer."""
        case_path = tmp_path / "case.md"
        case_path.write_text("---\nsummary: x\n---\n", encoding="utf-8")

        system, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=case_path,
                state_path=tmp_path / "state.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=_make_mcp_tools(),
                criteria={"mandatory": {"sim_pass": True}},
            )
        )

        combined = system + "\n" + user
        assert "Author both RTL and testbench code yourself" in combined
        assert "TB Coder" not in combined
        assert "`tb_coder`" not in combined
        # No coder addressing survives the hide.
        assert "`coder --category tb`" not in combined
        assert "Use `coder` for complex or broad edits" not in combined
        assert "Use RTL Coder" not in combined

    def test_ticket_type_sets_workflow(self, tmp_path: Path):
        case_path = tmp_path / "case.md"
        case_path.write_text("bugfix case", encoding="utf-8")
        state = tmp_path / "state.json"

        _, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=case_path,
                state_path=state,
                logs_dir=tmp_path,
                slug="s",
                ticket_type="bugfix",
                mcp_tools=[],
            )
        )

        assert "bugfix" in user

    def test_type_guidance_has_no_final_step_report_args(self):
        feature = build_type_guidance_section("feature")
        bugfix = build_type_guidance_section("bugfix")
        refactor = build_type_guidance_section("refactor")
        verification = build_type_guidance_section("verification")

        assert feature == (
            "# Type Guidance (feature)\n\n"
            "You are implementing new functionality. Write both RTL and testbench. "
            "Plan your RTL approach before coding. Ensure the design meets the spec "
            "and the TB exercises the new behavior thoroughly."
        )
        combined = "\n".join([feature, bugfix, refactor, verification])
        assert "Final step" not in combined
        assert "submit_run_report" not in combined
        assert "--design-decisions" not in combined
        assert "--root-cause" not in combined
        assert "--behavior-preservation" not in combined
        assert "--coverage-added" not in combined

    def test_crash_recovery_section_included(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("ticket content", encoding="utf-8")
        state = _make_state(tmp_path, criteria={})
        logs = tmp_path / "logs"
        logs.mkdir()
        _, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=logs,
                slug="crash-test",
                mcp_tools=[],
                is_crash_recovery=True,
            )
        )

        assert "# Crash Recovery" in user
        assert "previous developer session crashed" in user.lower()

    def test_no_crash_section_by_default(self, tmp_path: Path):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("body", encoding="utf-8")
        state = tmp_path / "state.json"

        _, user = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=state,
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                is_crash_recovery=False,
            )
        )

        assert "Crash Recovery" not in user


# ---------------------------------------------------------------------------
# build_ticket_section
# ---------------------------------------------------------------------------


class TestBuildTicketSection:
    def test_points_to_mounted_ticket_snapshot(self, tmp_path: Path):
        ticket = tmp_path / "my-ticket.md"
        ticket.write_text("---\nsummary: FSM fix\n---\n## Details\nSome bug", encoding="utf-8")

        section = build_ticket_section(ticket)

        assert "# Ticket" in section
        assert "$BOOLEY_TICKET_FILE" in section
        assert "$BOOLEY_LOGS_DIR/ticket.md" in section
        assert "FSM fix" not in section
        assert "Some bug" not in section

    def test_missing_file(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.md"

        section = build_ticket_section(missing)

        assert "# Ticket" in section
        assert "$BOOLEY_TICKET_FILE" in section
        assert str(missing) in section


# ---------------------------------------------------------------------------
# build_crash_recovery_section
# ---------------------------------------------------------------------------


class TestBuildCrashRecoverySection:
    def test_basic_crash_section(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        state = tmp_path / "state.json"

        section = build_crash_recovery_section(
            logs_dir=logs,
            state_path=state,
        )

        assert "# Crash Recovery" in section
        assert "previous developer session crashed" in section.lower()
        assert str(state) in section
        assert str(logs) in section
        assert "Do not repeat Flow or Specialist calls" in section
        assert "criteria state" not in section

    def test_includes_summary_when_exists(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        state = tmp_path / "state.json"
        summary = tmp_path / "run_001.summary.md"
        summary.write_text("# Distilled transcript\n- **Agent:** hi", encoding="utf-8")

        section = build_crash_recovery_section(
            logs_dir=logs,
            state_path=state,
            summary_path=summary,
        )

        assert "Previous session summary" in section
        assert str(summary) in section
        assert "distilled reasoning, commands, and Flow or Specialist verdicts" in section

    def test_prohibits_raw_jsonl_regardless_of_summary(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        state = tmp_path / "state.json"

        # Without a summary
        section = build_crash_recovery_section(
            logs_dir=logs,
            state_path=state,
            summary_path=None,
        )
        assert "Do NOT read raw `*.jsonl` transcript files" in section
        assert "Previous session summary" not in section
        # The old wording pointed the agent at the raw transcript — that
        # single line cost 43.8% of benchmark input tokens. Never again.
        assert "Scan for reasoning" not in section
        assert "raw JSONL log" not in section

        # With a summary: prohibition still present
        summary = tmp_path / "run_001.summary.md"
        summary.write_text("summary", encoding="utf-8")
        section = build_crash_recovery_section(
            logs_dir=logs,
            state_path=state,
            summary_path=summary,
        )
        assert "Do NOT read raw `*.jsonl` transcript files" in section

    def test_no_summary_pointer_when_missing(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        state = tmp_path / "state.json"
        missing = tmp_path / "no_such_summary.md"

        section = build_crash_recovery_section(
            logs_dir=logs,
            state_path=state,
            summary_path=missing,
        )

        # summary_path exists check fails, so no summary pointer
        assert "Previous session summary" not in section

    def test_hints_about_log_structure(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        state = tmp_path / "state.json"

        section = build_crash_recovery_section(
            logs_dir=logs,
            state_path=state,
        )

        assert "report.json" in section
        assert "git log" in section


# ---------------------------------------------------------------------------
# build_workflow_section
# ---------------------------------------------------------------------------


class TestBuildWorkflowSection:
    def test_feature_no_criteria_fallback(self):
        section = build_workflow_section(ticket_type="feature")
        assert "# Workflow Regions" in section
        assert (
            "**Implementation:** plan RTL/TB approach -> author both RTL and "
            "TB yourself" in section
        )
        assert "No criteria provided" in section
        assert "feature" in section

    def test_refactor_no_criteria_fallback(self):
        section = build_workflow_section(ticket_type="refactor")
        assert (
            "**Implementation:** plan RTL/TB approach -> author both RTL and "
            "TB yourself" in section
        )
        assert "No criteria provided" in section
        assert "refactor" in section

    def test_bugfix_no_criteria_fallback(self):
        section = build_workflow_section(ticket_type="bugfix")
        assert (
            "**Implementation:** plan RTL/TB approach -> author both RTL and "
            "TB yourself" in section
        )
        assert "No criteria provided" in section
        assert "bugfix" in section

    def test_unknown_type_fallback(self):
        section = build_workflow_section(ticket_type="mystery")
        assert "No criteria provided" in section
        assert "mystery" in section

    def test_advisory_guidance(self):
        """Criteria-driven workflow presents advisory regions, not gates."""
        section = build_workflow_section(
            criteria={
                "mandatory": {
                    "lint_clean": True,
                    "sim_pass": True,
                }
            }
        )
        assert "advisory" in section.lower()
        assert "enforces no ordering" in section.lower()

    def test_distinguishes_done_review_from_clean_verify_loop(self):
        """Workflow guidance distinguishes terminal reviews from disposition loops."""
        section = build_workflow_section(
            criteria={
                "mandatory": {
                    "sim_pass": True,
                    "review_tb_quality_done": True,
                    "review_rtl_bugs_clean": True,
                }
            }
        )
        lowered = section.lower()
        assert "final advisory review" in lowered
        assert "regardless of findings" in lowered
        assert "an unmet `_clean` gate" in lowered
        assert "invoke `reviewer` again" in section
        assert "waivers with specific justifications" in lowered

    # --- Criteria-driven workflow tests ---

    def test_criteria_feature_like(self):
        criteria = {
            "mandatory": {
                "lint_clean": True,
                "sim_pass": True,
                "synthesis_ok": True,
                "review_rtl_bugs_done": True,
                "mutation_score": True,
            }
        }
        section = build_workflow_section(criteria=criteria)
        assert (
            "**Implementation:** plan RTL/TB approach -> author both RTL and "
            "TB yourself" in section
        )
        assert "lint" in section
        assert "sim" in section
        assert "synth" in section
        assert "reviewer --category rtl --focus bugs" in section
        assert "mutation_tester" in section
        assert "author both RTL and TB yourself" in section
        assert "Write your plan and code" not in section
        assert "spec_arbiter --mode arbitrate" not in section

    def test_criteria_feature_like_plannerless(self):
        criteria = {
            "mandatory": {
                "lint_clean": True,
                "sim_pass": True,
                "review_tb_quality_clean": True,
            }
        }
        section = build_workflow_section(criteria=criteria)

        assert "sim" in section
        assert "developer decides side -> author RTL and TB directly" in section
        assert "spec_arbiter" not in section
        assert "debugger" not in section

    def test_workflow_uses_current_endpoint_names(self):
        criteria = {
            "mandatory": {
                "sim_pass": True,
                "review_tb_quality_clean": True,
            }
        }

        section = build_workflow_section(criteria=criteria)

        assert "**Implementation:**" in section
        assert "**Pre-sim criteria:**" in section
        assert "reviewer --category tb" in section
        assert "review --category tb" not in section
        assert "developer decides side -> author RTL and TB directly" in section
        assert section.index("Implementation") < section.index("Pre-sim criteria")
        assert section.index("reviewer --category tb") < section.index("Sim-debug loop")

    def test_sim_workflow_uses_generic_path(self):
        criteria = {
            "mandatory": {
                "sim_pass": True,
                "review_tb_quality_clean": True,
            }
        }

        section = build_workflow_section(criteria=criteria)

        assert "reviewer --category tb" in section
        assert "developer decides side -> author RTL and TB directly" in section
        assert "Sim-debug loop:" in section
        assert "spec_arbiter" not in section

    def test_criteria_bugfix_like(self):
        criteria = {
            "mandatory": {
                "lint_clean": True,
                "sim_pass": True,
            }
        }
        section = build_workflow_section(criteria=criteria)
        assert "lint" in section
        assert "sim" in section
        assert "synth" not in section
        assert "mutation_tester" not in section

    def test_criteria_no_sim(self):
        criteria = {
            "mandatory": {
                "lint_clean": True,
            }
        }
        section = build_workflow_section(criteria=criteria)
        assert "lint" in section
        assert "sim_pass" not in section

    def test_criteria_config_suffix_matching(self):
        criteria = {
            "mandatory": {
                "lint_clean_config_a": True,
                "sim_pass_config_b": True,
            }
        }
        section = build_workflow_section(criteria=criteria)
        assert "lint" in section
        assert "sim" in section

    def test_plannerless_workflow_omits_plan_steps(self):
        """Plan criteria map to no endpoint now — no planner steps are emitted."""
        criteria = {
            "mandatory": {
                "rtl_plan_done": True,
                "verification_plan_done": True,
                "lint_clean": True,
                "sim_pass": True,
            }
        }
        section = build_workflow_section(criteria=criteria)
        assert "planner --category rtl" not in section
        assert "planner --category tb" not in section
        assert "planner" not in section
        assert "Use Booley Flows and your own edits" in section

    def test_capability_list_omits_tb_coder(self):
        """Capability list advertises Booley Flows plus the developer's edits."""
        criteria = {
            "mandatory": {
                "lint_clean": True,
                "sim_pass": True,
            }
        }
        section = build_workflow_section(criteria=criteria)
        assert "Use Booley Flows and your own edits" in section
        assert "TB Coder" not in section

    def test_criteria_none_falls_back_to_type(self):
        section = build_workflow_section(criteria=None, ticket_type="feature")
        assert "feature" in section
        assert "No criteria provided" in section


# ---------------------------------------------------------------------------
# build_blocked_section
# ---------------------------------------------------------------------------


class TestBuildBlockedSection:
    def test_returns_none_when_no_file(self, tmp_path: Path):
        assert build_blocked_section(tmp_path) is None

    def test_returns_content_when_file_exists(self, tmp_path: Path):
        blocked = tmp_path / "blocked.md"
        blocked.write_text(
            "# Escalation History\n\n"
            "## Setup -- Blocked (2026-05-01T00:00:00Z)\n\n"
            "Need info on register width.\n",
            encoding="utf-8",
        )
        result = build_blocked_section(tmp_path)
        assert result is not None
        assert "MUST follow them" in result
        assert "register width" in result

    def test_returns_none_when_file_empty(self, tmp_path: Path):
        (tmp_path / "blocked.md").write_text("", encoding="utf-8")
        assert build_blocked_section(tmp_path) is None

    def test_ignores_entries_before_latest_reset_boundary(self, tmp_path: Path):
        blocked = tmp_path / "blocked.md"
        blocked.write_text(
            "# Escalation History\n\n"
            "### Oracle Feedback (2026-05-01T00:00:00Z)\n\nstale golden result\n\n"
            "### Reset Boundary (2026-05-02T00:00:00Z)\n\nold entries archived\n",
            encoding="utf-8",
        )
        assert build_blocked_section(tmp_path) is None

        with blocked.open("a", encoding="utf-8") as file:
            file.write("\n### Human Response (2026-05-03T00:00:00Z)\n\nnew guidance\n")

        result = build_blocked_section(tmp_path)
        assert result is not None
        assert "new guidance" in result
        assert "stale golden result" not in result

    def test_truncates_large_files(self, tmp_path: Path):
        blocked = tmp_path / "blocked.md"
        entries = []
        for i in range(200):
            entries.append(f"## Run {i} -- Blocked (2026-05-01T00:00:00Z)\n\n{'x' * 200}\n")
        blocked.write_text(
            "# Escalation History\n\n" + "\n".join(entries),
            encoding="utf-8",
        )
        result = build_blocked_section(tmp_path)
        assert result is not None
        assert "truncated" in result
        assert "Run 199" in result

    def test_single_oversized_entry_is_clamped_not_dropped(self, tmp_path: Path):
        """Regression: benchmark batch-01 retry feedback (one >cap trailing entry)
        vanished entirely — 14 tickets retried blind and no-opped."""
        blocked = tmp_path / "blocked.md"
        payload = "y" * 200 + " AssertionError: Expected lockout=False, got 1"
        blocked.write_text(
            "# Escalation History\n\n"
            "## Oracle Feedback -- golden harness FAILED (2026-07-26T22:00:00Z)\n\n"
            + "x" * 30000
            + "\n"
            + payload
            + "\n",
            encoding="utf-8",
        )
        result = build_blocked_section(tmp_path)
        assert result is not None
        # Header and the tail of the body must survive; the failure evidence
        # lives at the end of the entry.
        assert "Oracle Feedback -- golden harness FAILED" in result
        assert "Expected lockout=False" in result
        assert "(entry head truncated)" in result

    def test_oversized_text_without_section_markers_keeps_tail(self, tmp_path: Path):
        blocked = tmp_path / "blocked.md"
        blocked.write_text("z" * 30000 + "\nFINAL VERDICT LINE\n", encoding="utf-8")
        result = build_blocked_section(tmp_path)
        assert result is not None
        assert "FINAL VERDICT LINE" in result


# ---------------------------------------------------------------------------
# Contradicting-instruction fixes (benchmark batch-01 prompt audit)
# ---------------------------------------------------------------------------


class TestBlockRuleAgreesWithDebugLoop:
    """Rule 3 said "never block except on broken infra"; Rule 4's tail said
    "after 5 no-progress iterations, block with findings". Same prompt."""

    def _system(self, tmp_path: Path, *, human_in_the_loop: bool) -> str:
        ticket = tmp_path / "ticket.md"
        ticket.write_text("---\nsummary: x\n---\n", encoding="utf-8")
        system, _ = build_developer_prompt(
            DeveloperPromptContext(
                ticket_path=ticket,
                state_path=tmp_path / "s.json",
                logs_dir=tmp_path,
                slug="s",
                mcp_tools=[],
                human_in_the_loop=human_in_the_loop,
            )
        )
        return system

    def test_unattended_debug_loop_does_not_tell_the_agent_to_block(self, tmp_path: Path):
        system = self._system(tmp_path, human_in_the_loop=False)

        assert "no human operator will unblock the ticket" in system
        assert "block with findings" not in system
        assert "do not block, since nobody will unblock you" in system

    def test_hitl_debug_loop_still_blocks(self, tmp_path: Path):
        system = self._system(tmp_path, human_in_the_loop=True)

        assert "block with findings" in system
        assert "do not block, since nobody will unblock you" not in system


class TestTicketSnapshotWarning:
    """The board file moves queue -> active mid-run, so checking it reported
    "missing" on every healthy run while the mounted snapshot was fine."""

    def test_no_warning_when_snapshot_exists(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "ticket.md").write_text("---\nsummary: x\n---\n", encoding="utf-8")
        moved_board_path = tmp_path / "board" / "queue" / "s.md"

        section = build_ticket_section(moved_board_path, logs)

        assert "missing" not in section.lower()
        assert "$BOOLEY_TICKET_FILE" in section

    def test_warns_when_the_agent_truly_has_nothing(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()

        section = build_ticket_section(tmp_path / "gone.md", logs)

        # Names the path the agent can act on, not the host board path.
        assert "Ticket snapshot missing" in section
        assert str(logs / "ticket.md") in section
