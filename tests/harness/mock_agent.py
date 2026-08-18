"""Mock agent response generator for Booley Flow tests.

Provides factory functions that return pre-recorded AgentResult instances,
allowing step handlers to be tested without real agent/SDK calls.
"""

from __future__ import annotations

from booley.harness.models import AgentResult

# ---------------------------------------------------------------------------
# Planning step responses
# ---------------------------------------------------------------------------


def planning_draft_clean() -> AgentResult:
    """Plan with no unresolved questions and an acceptance schedule."""
    return AgentResult(
        output=(
            "# Plan: fix-fsm-counter\n\n"
            "## Approach\nFix the FSM counter overflow issue.\n\n"
            "## Acceptance Schedule\n"
            "| # | Criterion | Verify After | How |\n"
            "|---|-----------|-------------|-----|\n"
            "| 1 | Counter clean | sim-debug-loop | Check sim logs |\n"
            "| 2 | No regressions | synthesis | Compare reports |\n\n"
            "## Unresolved Questions\n\n"
        ),
        input_tokens=5000,
        output_tokens=3000,
    )


def planning_draft_with_questions() -> AgentResult:
    """Plan containing unresolved questions -- should trigger blocking."""
    return AgentResult(
        output=(
            "# Plan: fix-fsm-counter\n\n"
            "## Approach\nFix the FSM counter overflow issue.\n\n"
            "## Acceptance Schedule\n"
            "| # | Criterion | Verify After | How |\n"
            "|---|-----------|-------------|-----|\n"
            "| 1 | Counter clean | sim-debug-loop | Check sim logs |\n\n"
            "## Unresolved Questions\n\n"
            "- Should the fix apply to both read and write FSM paths?\n"
            "- Is the 2-cycle latency budget firm or flexible?\n"
        ),
        input_tokens=5000,
        output_tokens=3000,
    )


def planning_review_no_issues() -> AgentResult:
    """Reviewer finds nothing critical."""
    return AgentResult(
        output=("## Review Summary\n\nNo critical or major issues found.\nVerdict: APPROVE\n"),
        input_tokens=3000,
        output_tokens=1000,
    )


def planning_review_critical() -> AgentResult:
    """Reviewer finds a CRITICAL issue -- triggers plan revision."""
    return AgentResult(
        output=(
            "## Review Summary\n\n"
            "Severity: **CRITICAL** -- the plan does not address config_b mode.\n"
            "Verdict: REVISE\n"
        ),
        input_tokens=3000,
        output_tokens=1500,
    )


def planning_revision() -> AgentResult:
    """Agent revises the plan after review feedback."""
    return AgentResult(
        output="Revised the plan to address config_b mode coverage.",
        input_tokens=6000,
        output_tokens=4000,
    )


def codex_review_clean() -> AgentResult:
    """Codex review with no issues."""
    return AgentResult(output="No significant issues found. Plan looks solid.")


def codex_review_major() -> AgentResult:
    """Codex review flagging a MAJOR issue."""
    return AgentResult(
        output="**MAJOR**: Missing edge case for zero-length input.\nVerdict: REVISE"
    )


# ---------------------------------------------------------------------------
# Compile-check step responses
# ---------------------------------------------------------------------------


def compile_fix_agent(*, passed: bool = False) -> AgentResult:
    """Agent that claims to have fixed a compile error."""
    return AgentResult(
        output="Fixed missing port connection on my_module.sv:42",
        input_tokens=2000,
        output_tokens=800,
        structured={"passed": passed},
    )


# ---------------------------------------------------------------------------
# Review step responses (RTL review + TB review)
# ---------------------------------------------------------------------------


def review_all_fixed() -> AgentResult:
    """Review agent: found issues, fixed them all."""
    return AgentResult(
        output="RTL review complete. Fixed 3 issues including 1 CRITICAL.",
        structured={
            "issues_found": 3,
            "issues_fixed": 3,
            "critical_found": 1,
            "critical_fixed": 1,
            "major_found": 1,
            "major_fixed": 1,
            "issues": [
                {"severity": "CRITICAL", "summary": "Missing reset on counter"},
                {"severity": "MAJOR", "summary": "Off-by-one in loop bound"},
                {"severity": "MINOR", "summary": "Unused signal"},
            ],
        },
        input_tokens=4000,
        output_tokens=2000,
    )


def review_critical_unfixed() -> AgentResult:
    """Review agent: found CRITICAL issue but could NOT fix it."""
    return AgentResult(
        output="RTL review complete. 1 CRITICAL issue could not be auto-fixed.",
        structured={
            "issues_found": 2,
            "issues_fixed": 1,
            "critical_found": 1,
            "critical_fixed": 0,
            "major_found": 0,
            "major_fixed": 0,
            "issues": [
                {"severity": "CRITICAL", "summary": "Architectural timing violation"},
                {"severity": "MINOR", "summary": "Style issue (fixed)"},
            ],
        },
        input_tokens=4000,
        output_tokens=2000,
    )


def review_no_issues() -> AgentResult:
    """Review agent: clean review, no issues."""
    return AgentResult(
        output="RTL review complete. No issues found.",
        structured={
            "issues_found": 0,
            "issues_fixed": 0,
            "critical_found": 0,
            "critical_fixed": 0,
            "major_found": 0,
            "major_fixed": 0,
            "issues": [],
        },
        input_tokens=3000,
        output_tokens=1000,
    )


# ---------------------------------------------------------------------------
# Sim debug loop responses
# ---------------------------------------------------------------------------


def sim_debug_all_pass(configs: list[str] | None = None) -> AgentResult:
    """Debug agent: all configs pass on first run (COULD_NOT_REPRODUCE)."""
    configs = configs or ["config_a", "config_b"]
    return AgentResult(
        output="All simulations passed. No issues found.",
        structured={
            "config_results": {c: {"passed": True} for c in configs},
            "debug_summary": {
                "hypothesis": "N/A",
                "root_cause": "N/A",
                "fix_description": "None needed",
                "remaining_error": "",
                "stop_reason": "COULD_NOT_REPRODUCE",
            },
        },
        input_tokens=5000,
        output_tokens=2000,
    )


def sim_debug_diagnosis(
    configs: list[str] | None = None, failing: list[str] | None = None
) -> AgentResult:
    """Debug agent: diagnoses a bug, reports per-config results.

    Pass ``failing`` to mark specific configs as FAIL (default: all pass).
    """
    configs = configs or ["config_a", "config_b"]
    failing = failing or []
    remaining = "" if not failing else ", ".join(failing) + " still failing"
    return AgentResult(
        output="Found off-by-one in counter. Diagnosis complete.",
        structured={
            "config_results": {c: {"passed": c not in failing} for c in configs},
            "debug_summary": {
                "hypothesis": "counter overflow",
                "root_cause": "off-by-one in loop counter",
                "fix_description": "change >= to >",
                "remaining_error": remaining,
                "stop_reason": "DIAGNOSIS_COMPLETE",
                "fail_sim_time": "12450 ns",
            },
        },
        input_tokens=5000,
        output_tokens=2000,
    )


# --- Validator agent mock responses ---


def validator_fixed_all_pass(configs: list[str] | None = None, bug_id: int = 1) -> AgentResult:
    """Validator: all configs pass after fix."""
    configs = configs or ["config_a", "config_b"]
    return AgentResult(
        output="All configs pass.",
        structured={
            "configs_run": [{"config": c, "passed": True} for c in configs],
            "primary_config": configs[0],
            "classification": "FIXED_ALL_PASS",
            "bug_id": bug_id,
        },
        input_tokens=2000,
        output_tokens=1000,
    )


def validator_same_bug(primary_config: str = "config_a", bug_id: int = 1) -> AgentResult:
    """Validator: same bug still present."""
    return AgentResult(
        output="Same bug persists.",
        structured={
            "configs_run": [
                {
                    "config": primary_config,
                    "passed": False,
                    "error_summary": "assertion failed at 12450 ns",
                }
            ],
            "primary_config": primary_config,
            "classification": "SAME_BUG_NOT_FIXED",
            "bug_id": bug_id,
            "counter_evidence": "Fix changed comparator but assertion still fires at same sim time",
        },
        input_tokens=2000,
        output_tokens=1000,
    )


# ---------------------------------------------------------------------------
# Acceptance check responses
# ---------------------------------------------------------------------------


def acceptance_pass() -> AgentResult:
    """Acceptance reviewer: PASS."""
    return AgentResult(
        output="All criteria met.",
        structured={
            "verdict": "PASS",
            "failures": [],
            "warnings": [],
            "confidence": "high",
        },
        input_tokens=8000,
        output_tokens=3000,
    )


def acceptance_conditional() -> AgentResult:
    """Acceptance reviewer: CONDITIONAL with warnings."""
    return AgentResult(
        output="Acceptance check passed with warnings about synthesis timing.",
        structured={
            "verdict": "CONDITIONAL",
            "failures": [],
            "warnings": ["Synthesis timing margin is tight (0.1ns slack)"],
            "confidence": "medium",
        },
        input_tokens=8000,
        output_tokens=3000,
    )


def acceptance_fail() -> AgentResult:
    """Acceptance reviewer: FAIL verdict."""
    return AgentResult(
        output="Acceptance check failed. Output does not match golden reference.",
        structured={
            "verdict": "FAIL",
            "failures": [
                {
                    "criterion": "Output matches golden",
                    "reason": "Output mismatch on config_b",
                    "return_to_step": "implementation",
                },
            ],
            "warnings": [],
            "confidence": "high",
        },
        input_tokens=8000,
        output_tokens=3000,
    )


# ---------------------------------------------------------------------------
# Implementation step responses (agent-loop implementation phase)
# ---------------------------------------------------------------------------


def implementation_rtl_agent() -> AgentResult:
    """Implementation RTL agent that writes code changes."""
    return AgentResult(
        output="Implemented FSM counter fix in my_module.sv. "
        "Changed pipeline feedback path from 2-stage to single-cycle.",
        input_tokens=8000,
        output_tokens=4000,
    )


def implementation_tb_agent() -> AgentResult:
    """Implementation TB agent that updates the testbench."""
    return AgentResult(
        output="Updated my_module_tb.sv: added boundary tests for counter feedback path.",
        input_tokens=6000,
        output_tokens=3000,
    )


# ---------------------------------------------------------------------------
# RTL mutation testing responses (agent-loop mutation phase)
# ---------------------------------------------------------------------------


def mutation_creator(mutation_count: int = 3) -> AgentResult:
    """Mutation creator agent: designed mutation specifications."""
    import json

    mutations = [
        {
            "index": i + 1,
            "category": ["timing", "logic", "boundary"][i % 3],
            "file": "rtl/my_module.sv",
            "line": 42 + i * 10,
            "original_code": f"assign out = in_{i};",
            "mutated_code": f"assign out = ~in_{i};",
            "detectability_argument": f"TB checks out against golden for path {i}",
        }
        for i in range(mutation_count)
    ]
    return AgentResult(
        output=json.dumps({"mutations": mutations}),
        structured=None,
        input_tokens=6000,
        output_tokens=3000,
    )


def mutation_creator_empty() -> AgentResult:
    """Mutation creator: returned no mutations."""
    return AgentResult(
        output="Could not identify suitable mutation points.",
        structured={"faults": []},
        input_tokens=4000,
        output_tokens=1000,
    )
