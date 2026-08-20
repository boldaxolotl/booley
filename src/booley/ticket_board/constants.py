"""Execution constants, step requirements, and validators for the ticket system."""

from __future__ import annotations

from .lifecycle import TicketState

# ---------------------------------------------------------------------------
# Booley Flow steps (ordered by canonical execution sequence)
# ---------------------------------------------------------------------------

STEP_ORDER = [
    "setup",
    "planning",
    "run-config",
    "implementation",
    "implementation-tb",
    "lint-check",
    "rtl-review-1",
    "tb-review",
    "sim-debug-loop",
    "rtl-mutation-testing",
    "rtl-review-final",
    "post-review-sim",
    "synthesis",
    "acceptance-check",
    "summary",
    "review",
]

VALID_TYPES = {"feature", "bugfix", "refactor", "verification"}

VALID_PRIORITIES = {"low", "medium", "high"}


# Numeric ordering for priority sorting (lower = higher priority)
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# booley.toml Flow section → execution step gated by that Flow.
# When a Flow is disabled, the corresponding step should be
# excluded from planned_stages at ticket creation and rejected at
# execution time.
FLOW_STEP_MAP = {
    "lint": "lint-check",
    "synth": "synthesis",
    "fpga": "synthesis",
}

# ---------------------------------------------------------------------------
# Directory & status mappings
# ---------------------------------------------------------------------------

# Directories tickets can live in (relative to tickets_dir) and their status
# strings — both DERIVED from the single TicketState source of truth so the
# dir↔status pairing can never drift. Values are identical to the former
# hand-written literals (member order matches, dict values unchanged).
TICKET_DIRS = [s.board_dir for s in TicketState]

# Directory name → status string (keys use board/ prefix)
DIR_STATUS_MAP = {s.board_dir: s.status for s in TicketState}


def normalize_dir(d: str) -> str:
    """Normalize a bare status dir name to board/-prefixed form.

    Accepts both 'queue' and 'board/queue'; always returns 'board/queue'.
    """
    if d.startswith("board/"):
        return d
    return f"board/{d}"


# Required ticket fields
REQUIRED_FIELDS = {"summary", "type", "branch", "scope", "criteria"}

# All known frontmatter fields (required + optional).
# Unknown fields trigger a validation warning.
KNOWN_FIELDS = REQUIRED_FIELDS | {
    "spec",
    "auto_approve",
    "integration_base",
    "dependencies",
    "priority",
    "synthesis",
    "baseline_tests",
    "scope_current",
    "scope_new",
    "feature_branch",
    "created",
    "base_sha",
    "on_success",
}

# Deprecated fields that must not appear in new tickets.
DEPRECATED_FIELDS = {
    "test": "use 'criteria' instead",
    "plan_file": "removed — the planner specialists were pruned; put the plan in the ticket body",
    "rtl_plan_file": "removed — the planner specialists were pruned; put the plan in the ticket body",
    "verification_plan_file": (
        "removed — the planner specialists were pruned; put the plan in the ticket body"
    ),
    "known_good": "removed — agents auto-discover known-good references",
}

# Runtime fields that live in .runtime/progress.json, not in ticket frontmatter.
# These are mutable execution state managed by the developer.
RUNTIME_FIELDS = {
    "step",
    "steps_completed",
    "workspace_intent",
    "last_update",
    "failed_step",
    "error",
    "blocked_reason",
    "blocked_step",
    "execution_id",
    "execution_owner_pid",
}

# ---------------------------------------------------------------------------
# Criteria system
# ---------------------------------------------------------------------------

# Maps criterion-key prefix → Booley Flow name for disabled-Flow coherence checks.
CRITERION_FLOW_MAP = {
    "lint_": "lint",
    "synth": "synth",
    "fpga_impl_": "fpga",
}
