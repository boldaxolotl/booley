"""Build developer system prompt dynamically.

Sections:
  1. Role
  2. Rules
  3. Ticket pointer
  4. Type guidance
  5. Workflow regions
  6. Crash recovery context
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from booley.criteria.templates import CriterionDef
    from booley.mcp.registry import McpToolInfo

logger = logging.getLogger(__name__)


@dataclass
class DeveloperPromptContext:
    """All inputs needed to build the developer system+user prompt."""

    ticket_path: Path
    state_path: Path
    logs_dir: Path
    slug: str
    ticket_type: str = "feature"
    criteria: dict[str, Any] | None = None
    mcp_tools: list[McpToolInfo] | None = None
    mcp_tool_config: dict[str, Any] | None = None
    flow_config: dict[str, Any] | None = None
    booley_src: Path | None = None
    project_mcp_tools_dir: Path | None = None
    project_criteria_path: Path | None = None
    is_crash_recovery: bool = False
    crash_summary_path: Path | None = None
    work_dir: str = "."
    backend: str = "claude"
    human_in_the_loop: bool = True
    run_report: bool = True


# ---------------------------------------------------------------------------
# Prompt sections
# ---------------------------------------------------------------------------

_ROLE_MCP = """\
# Role

You are a digital design engineer, your task is to execute a development ticket. Meet every mandatory criterion and attempt every optional criterion

You have Booley Flows and any exposed Specialists at your disposal; use them appropriately
  - Use Booley Flows to run deterministic checks on your work: lint, simulation, synthesis, etc.
  - Use exposed Booley Specialists only when they are available; otherwise do the work yourself with your file operations and Booley Flows.

You are running inside a Docker container. The project worktree is mounted at your current working directory.

The following environment variables are set and propagated to MCP tool calls automatically:
- `BOOLEY_SLUG` — ticket slug
- `BOOLEY_TICKET_FILE` — mounted ticket markdown snapshot
- `BOOLEY_LOGS_DIR` — directory for Flow and Specialist reports and artifacts
- `BOOLEY_STATE_FILE` — path to booley_state.json (criteria tracking)

Targets are the execution boundary. Select the criterion's Target when invoking \
a Flow or Specialist. Mutation and coverage automatically run the complete \
runnable test suite declared for that Target; do not narrow them to one test. \
The specialists derive module and hierarchy identity from the Target, scoped \
RTL, and produced traces.

"""

_RULES_PREFIX = """\
1. **CRITERIA FRESHNESS**: Any RTL/TB edit makes affected checks stale. The \
Booley Flow framework resets stale criteria automatically; rerun the relevant lint, \
simulation, and synthesis criteria before finishing. Reviews are the \
same: a passing review records the source fingerprint it checked and later \
RTL/TB edits make it stale. A `_done` review is terminal and advisory: run it \
only after every code-changing criterion, report every finding, and do not edit \
the implementation in response during this ticket run. An unmet `_clean` review \
is a resolution loop: fix each finding or propose an explicit waiver through \
`reviewer --steer` with a specific justification, then call `reviewer` again. \
The reviewer validates every FIXED or WAIVED disposition. Every accepted waiver, \
including MINOR findings, is persisted and shown to the user. Never treat a \
retry cap as permission to waive a finding silently.

"""

_BASELINE_QOR_RULE = """\
**BASELINE QoR CRITERIA**: For baseline-relative `synthesis_ok` and \
`fpga_impl_ok` criteria, every baseline/candidate Target pair is sealed and \
immutable. For a plain Target name, the sealed Target recipe is immutable: run \
both `base_sha` and the ticket head with that identical recipe. An explicit pair \
may name different frozen Targets. Never alter either Target, constraint, \
parameter, or build hook during execution. A missing or incorrect Target \
requires a `target-contract-change-required` block and a proposal in the run \
report; it is not a reason to skip comparisons.

"""

_BASELINE_RELATIVE_SUFFIXES = ("_increase_at_most", "_reduce_at_least")
_REVISION_OWNED_QOR_CRITERIA = frozenset({"synthesis_ok", "fpga_impl_ok"})

_RULE_EXIT_WITH_REPORT = """\
2. **EXIT CONDITION**: When all mandatory criteria are met, your final action \
is `submit_run_report`. Pass exactly one type-specific report arg, include \
real uncertainties (coverage gaps, assumptions, edge cases), and summarize \
which edits you made and which Booley Flows or Specialists you used, and why. If \
any optional criteria remain unmet, pass `optional_criteria_justification` \
explaining why each one could not be completed. If code changes after the \
report, submit a fresh report. Commit every intended change and leave every \
ticket repository clean before calling `submit_run_report`; the report is a \
finalization gate and rejects staged, modified, deleted, or untracked files.

"""

# Rendered when the project disables routine run reports ([developer]
# run_report = false). Review results that must reach the user and unmet optional
# criteria still require a report, so those exceptional paths retain an artifact.
_RULE_EXIT_NO_REPORT = """\
2. **EXIT CONDITION**: This project disables routine end-of-run reports. You \
must still call `submit_run_report` when the ticket has any `_done` review, any \
accepted `_clean` review waiver, or any unmet optional criterion, because those \
results must reach the user. Otherwise, when all criteria are met, stop without \
calling it. For unmet optional criteria, pass \
`optional_criteria_justification` explaining why each one could not be \
completed. The report is required in that case even though routine reports \
are disabled. Commit every intended change and leave every ticket repository \
clean before stopping or calling `submit_run_report`; the Harness rejects a \
dirty handoff.

"""

_RULE_BLOCKED_HITL = """\
3. **BLOCKED**: If you cannot proceed (missing spec, ambiguous requirement, \
or persistent infrastructure failure), write `_blocked_reason` with detail \
`{"reason": "..."}` and stop.

"""

_RULE_BLOCKED_UNATTENDED = """\
3. **BLOCKED**: `_blocked_reason` is reserved for genuinely irrecoverable \
infrastructure failures (container broken, git broken, worktree gone). Do not \
block on missing spec or ambiguous requirements because no human operator will \
unblock the ticket.

For spec-silent points, choose the most reasonable interpretation from the \
work item text, visible tests, and Flow or Specialist reports. Document assumptions in \
`$BOOLEY_LOGS_DIR/answered_questions.md`, an implementation note, or the \
final report.

"""

_RULES_AFTER_BLOCKED_NO_DEBUGGER = """\
4. **DEBUG LOOP**: For simulation failures, run `simulate` and read the \
structured report — treat Booley Flow reports as the verdict of record; read raw \
logs under `.runtime/edalize/` only when the report is insufficient, and \
always with bounded reads (e.g. `tail -c 20000`), never `cat` on large files. \
Then identify one likely root cause, \
edit the code, and rerun `simulate`. When the log does not localize the failure, \
rerun `simulate` with `trace: true`, then call the `bwave` MCP tool: first \
`bwave(extra_args=["skill"])`, then `bwave(extra_args=["--help"])`, then \
register the trace and query the relevant signal window. Prefer B-Wave queries \
over temporary debug prints for state, handshake, latency, and pipeline \
questions. For inconclusive sim output, inspect the testbench sentinel/result \
logic and rerun with tracing if needed. For elaboration/compile failure, fix \
directly from compiler diagnostics. {no_progress_exit}

"""

# How to leave a sim-debug loop that stopped making progress. These must agree
# with Rule 3: telling an unattended run to "block with findings" contradicted
# the same prompt's "never block except on broken infrastructure".
_NO_PROGRESS_EXIT_HITL = (
    "After 5 no-progress sim-debug iterations on the same failing test, block with findings."
)

_NO_PROGRESS_EXIT_UNATTENDED = (
    "After 5 no-progress sim-debug iterations on the same failing test, stop "
    "iterating on it — do not block, since nobody will unblock you. Move to "
    "the remaining criteria, then submit the run report with the failing test, "
    "what you ruled out, and your best hypothesis."
)

_FLEXIBLE_RULES_TAIL = """\
5. **MCP TOOL DISCIPLINE**: Invoke Booley Flows and Specialists only through native MCP tool calls; \
do not invoke `python -m booley.dev_support.<name>` in Bash. Ticket context comes \
from environment variables, so do not pass slug/state-file flags. Run one Flow or Specialist \
at a time. When a criterion or ticket names a qualified Target selector, pass \
that exact selector to the Flow; never shorten it to its bare target name. \
This Target rule applies to Flows only. A target-independent reviewer criterion \
is named without a Target; invoke `reviewer` with its exact category and focus, \
and omit `--target` even when the ticket has other target-bound criteria. \
Read stdout and JSON reports before deciding next steps. Exit 0 \
means pass, 1 means fail, 2 means Flow or Specialist error; retry exit 2 once only for a \
clearly transient cause. Artifact and log files can be huge; before reading \
any file that may exceed ~100KB, check its size and read bounded slices \
(head/tail), never the whole file.

6. **EDIT STRATEGY**: Author both RTL and testbench code yourself. \
For verification work, read the requirements, plan the testbench approach, then \
edit the TB sources directly. After any RTL/TB edit, rerun the relevant \
verification criteria before finishing.

7. **SCOPE AND TARGET CONTRACT**: The ticket's `scope` lists the implementation \
files the work is expected to \
touch. Treat it as the plan, not a fence: prefer to stay inside it, but if \
finishing the ticket genuinely requires editing a file it does not name — a \
shared package or a neighbouring module — edit that file and \
say why in your run report. Do not leave the work half-done, and do not \
weaken a test to avoid touching something. Target/control-plane files are \
immutable even when Scope names them: this includes every `.core`, \
`.booley_project/tests.toml`, Target-selection configuration in `booley.toml`, \
selected SDC/XDC constraints, and generator/build hooks. If one is missing or \
incorrect, record the required contract revision and block as \
`target-contract-change-required`; never edit it. Harness bookkeeping \
(development state, criteria, and ticket files) is likewise forbidden.

"""


def _build_rules_section(
    human_in_the_loop: bool = True,
    run_report: bool = True,
    criteria: dict[str, Any] | None = None,
) -> str:
    """Build rules section.

    Args:
        human_in_the_loop: When False, renders Rule 3 for unattended execution
            instead of blocking on spec gaps. Default True preserves the
            human-in-loop behavior (block on missing/ambiguous spec).
        run_report: When False, renders Rule 2 without the routine
            submit_run_report exit requirement, while retaining a report when
            optional criteria remain unmet. Default True keeps the report as
            every run's final action.
        criteria: Raw ticket criteria used to render criterion-specific rules.
    """
    if human_in_the_loop:
        blocked_rule = _RULE_BLOCKED_HITL
        no_progress_exit = _NO_PROGRESS_EXIT_HITL
    else:
        blocked_rule = _RULE_BLOCKED_UNATTENDED
        no_progress_exit = _NO_PROGRESS_EXIT_UNATTENDED
    rules_after_blocked = _RULES_AFTER_BLOCKED_NO_DEBUGGER.format(
        no_progress_exit=no_progress_exit
    )
    rules_tail = _FLEXIBLE_RULES_TAIL

    parts = [
        "# Rules\n\n",
        _RULES_PREFIX,
        _BASELINE_QOR_RULE if _has_baseline_relative_qor_criteria(criteria) else "",
        _RULE_EXIT_WITH_REPORT if run_report else _RULE_EXIT_NO_REPORT,
        blocked_rule,
        rules_after_blocked,
        rules_tail,
    ]
    return "".join(parts)


def _has_baseline_relative_qor_criteria(criteria: dict[str, Any] | None) -> bool:
    """Return whether ticket criteria require a sealed-recipe QoR baseline."""
    if not criteria:
        return False
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if not isinstance(section, dict):
            continue
        for criterion_name, params in section.items():
            if criterion_name not in _REVISION_OWNED_QOR_CRITERIA or not isinstance(params, dict):
                continue
            if any(
                param.endswith(_BASELINE_RELATIVE_SUFFIXES)
                for param in params
                if param != "targets"
            ):
                return True
    return False


_TYPE_GUIDANCE = {
    "feature": (
        "You are implementing new functionality. Write both RTL and testbench. "
        "Plan your RTL approach before coding. Ensure the design meets the spec "
        "and the TB exercises the new behavior thoroughly."
    ),
    "bugfix": (
        "You are fixing a specific defect. Minimize changes — touch only what's "
        "needed to fix the bug. Focus on reproducing the failure first, then "
        "fixing it, then confirming the fix via simulation."
    ),
    "refactor": (
        "You are restructuring existing RTL without changing behavior. Plan "
        "your RTL approach before coding. All existing tests must continue to "
        "pass. No new test scenarios needed — focus on code quality and "
        "maintaining spec compliance."
    ),
    "verification": (
        "You are improving the testbench only. Do not modify RTL. Plan your "
        "verification approach before coding. Add scenarios, improve coverage, "
        "harden stimulus generation and checking."
    ),
}


def build_type_guidance_section(ticket_type: str) -> str:
    """Build type-specific guidance section."""
    guidance = _TYPE_GUIDANCE.get(ticket_type)
    if not guidance:
        return ""
    return f"# Type Guidance ({ticket_type})\n\n{guidance}"


# ---------------------------------------------------------------------------
# Criteria -> endpoint mapping (auto-built from criteria.toml + endpoint metadata)
# ---------------------------------------------------------------------------

# Canonical ordering within Workflow Regions (derived from criteria TOML workflow_region + position).
_REGION_ORDER = {"pre_sim": 0, "core_loop": 1, "post_sim": 2}

_IMPLEMENTATION_REGION_TYPES = frozenset({"feature", "bugfix", "refactor", "verification"})


def _code_change_step() -> str:
    """Describe the code-change route: developer authors both RTL and TB directly."""
    return "author RTL and TB directly"


def _implementation_region(ticket_type: str) -> str | None:
    """Return implementation guidance: developer authors both RTL and TB directly."""
    if ticket_type not in _IMPLEMENTATION_REGION_TYPES:
        return None
    if ticket_type == "verification":
        return (
            "**Implementation:** plan the testbench approach, then author the "
            "verification code yourself"
        )
    return "**Implementation:** plan RTL/TB approach -> author both RTL and TB yourself"


def _get_criterion_endpoint_map(
    mcp_tools: list[McpToolInfo] | None = None,
    project_criteria_path: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Build criterion-to-endpoint map from criteria TOML and endpoint metadata.

    Falls back to legacy hardcoded map if loading fails.
    """
    try:
        from booley.criteria.templates import (
            expand_criteria_defs,
            load_base_criteria,
            load_project_criteria,
            merge_criteria_defs,
        )
        from booley.mcp.registry import build_criterion_endpoint_map, discover_mcp_tools

        base_defs = load_base_criteria()
        project_defs: list[CriterionDef] = []
        if project_criteria_path and project_criteria_path.exists():
            project_defs = load_project_criteria(project_criteria_path)

        merged, errors = merge_criteria_defs(base_defs, project_defs)
        for err in errors:
            logger.error("Criteria merge error: %s", err)

        expanded = expand_criteria_defs(merged, [])
        if mcp_tools is None:
            mcp_tools = discover_mcp_tools()

        return build_criterion_endpoint_map(expanded, mcp_tools)

    except Exception:  # optional mapping must not block the run
        logger.warning("Failed to auto-build criterion-to-endpoint map", exc_info=True)
        return {}


def _sort_by_workflow_region(
    endpoint_names: list[str],
    criterion_endpoint_map: dict[str, tuple[str, str]],
) -> list[str]:
    """Sort endpoint names by Workflow Region order, preserving order within a region."""
    cmd_to_region: dict[str, int] = {}
    for _crit, (cmd, region) in criterion_endpoint_map.items():
        if cmd not in cmd_to_region:
            cmd_to_region[cmd] = _REGION_ORDER.get(region, 99)
    return sorted(endpoint_names, key=lambda c: cmd_to_region.get(c, 99))


def _criteria_keys(criteria: dict[str, Any]) -> set[str]:
    """Collect mandatory and optional criterion keys from state-shaped data."""
    keys: set[str] = set()
    for section in ("mandatory", "optional"):
        sub = criteria.get(section)
        if isinstance(sub, dict):
            keys.update(sub.keys())
    return keys


def _collect_workflow_buckets(
    keys: set[str],
    criterion_endpoint_map: dict[str, tuple[str, str]],
) -> tuple[list[str], bool, list[str], list[str]]:
    """Bucket endpoints into fixable regions and terminal ``_done`` reviews."""
    pre_sim: list[str] = []
    core_loop = False
    post_sim: list[str] = []
    final_reviews: list[str] = []

    for crit_key in keys:
        for prefix, (endpoint_name, region) in criterion_endpoint_map.items():
            clean_key = f"{prefix}_clean"
            if crit_key not in (prefix, clean_key) and not crit_key.startswith(prefix + "_"):
                continue

            if crit_key.startswith("review_") and crit_key.endswith("_done"):
                if endpoint_name not in final_reviews:
                    final_reviews.append(endpoint_name)
                break

            if region == "pre_sim" and endpoint_name not in pre_sim:
                pre_sim.append(endpoint_name)
            elif region == "core_loop":
                core_loop = True
            elif region == "post_sim" and endpoint_name not in post_sim:
                post_sim.append(endpoint_name)
            break

    return (
        _sort_by_workflow_region(pre_sim, criterion_endpoint_map),
        core_loop,
        _sort_by_workflow_region(post_sim, criterion_endpoint_map),
        _sort_by_workflow_region(final_reviews, criterion_endpoint_map),
    )


def _build_criteria_workflow(
    criteria: dict[str, Any],
    criterion_endpoint_map: dict[str, tuple[str, str]] | None = None,
    *,
    ticket_type: str = "feature",
) -> str:
    """Derive a Flow/Specialist workflow from criteria keys — bucket format."""
    if criterion_endpoint_map is None:
        criterion_endpoint_map = _get_criterion_endpoint_map()

    pre_sim, core_loop, post_sim, final_reviews = _collect_workflow_buckets(
        _criteria_keys(criteria),
        criterion_endpoint_map,
    )

    parts: list[str] = []
    implementation = _implementation_region(ticket_type)
    if implementation:
        parts.append(implementation)
    if pre_sim:
        parts.append(f"**Pre-sim criteria:** {', '.join(pre_sim)}")
    if core_loop:
        arbiter_step = f"developer decides side -> {_code_change_step()}"
        parts.append(
            f"**Sim-debug loop:** [sim -> inspect logs/B-Wave trace -> {arbiter_step} -> sim]*"
        )
    if post_sim:
        parts.append(f"**Post-sim criteria:** {', '.join(post_sim)}")
    if final_reviews:
        parts.append(
            "**Final advisory reviews (terminal; report findings, do not fix):** "
            + ", ".join(final_reviews)
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------


def build_workflow_section(
    criteria: dict[str, Any] | None = None,
    ticket_type: str = "feature",
    criterion_endpoint_map: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Build workflow section — always criteria-driven."""
    if criteria is not None:
        workflow = _build_criteria_workflow(
            criteria,
            criterion_endpoint_map,
            ticket_type=ticket_type,
        )
        return (
            "# Workflow Regions\n\n"
            "These regions are advisory, always subordinate to criteria and verification freshness.\n\n"
            f"{workflow}\n\n"
            "`[...]*` denotes iterate until the relevant criteria pass. "
            f"Use {_capability_list()} freely; the harness enforces no ordering "
            "between regions.\n\n"
            "Review mode controls what happens after findings. A `_done` gate "
            "is a final advisory review: it completes regardless of findings, "
            "must be reported to the user, and must not trigger edits in this "
            "ticket run. Later edits make it stale. An unmet `_clean` gate "
            "starts a bounded disposition loop: fix findings or propose explicit "
            "waivers with specific justifications through `--steer`, then invoke "
            "`reviewer` again. The gate passes only when no finding remains open; "
            "every accepted waiver is persisted and shown to the user regardless "
            "of severity."
        )
    implementation = _implementation_region(ticket_type)
    implementation_text = f"{implementation}\n\n" if implementation else ""
    return (
        "# Workflow Regions\n\n"
        f"{implementation_text}"
        f"No criteria provided for ticket type `{ticket_type}`. "
        "Use your best judgement; regions are advisory."
    )


def _capability_list() -> str:
    """Return prompt wording for the currently exposed capability set."""
    return "Booley Flows and your own edits"


def build_ticket_section(ticket_path: Path, logs_dir: Path | None = None) -> str:
    """Build a concise pointer to the mounted ticket markdown snapshot.

    The warning tracks the *snapshot* the agent actually reads, not the
    host-side board path: the board file moves between queue/active/blocked as
    the run progresses, so checking it printed "missing" on every healthy run
    while the mounted copy sat there fine. Warn only when the agent genuinely
    has nothing to read, and name the path it can act on.
    """
    snapshot = (logs_dir / "ticket.md") if logs_dir is not None else None
    if snapshot is not None:
        available = snapshot.exists() or ticket_path.exists()
        missing_path = snapshot
    else:
        available = ticket_path.exists()
        missing_path = ticket_path

    status = "" if available else f"\n\nTicket snapshot missing: `{missing_path}`"
    return (
        "# Ticket\n\n"
        "Read the ticket before acting: `$BOOLEY_TICKET_FILE` "
        "(`$BOOLEY_LOGS_DIR/ticket.md`)."
        f"{status}"
    )


def build_crash_recovery_section(
    *,
    logs_dir: Path,
    state_path: Path,
    summary_path: Path | None = None,
    has_escalation_history: bool = False,
) -> str:
    """Build crash recovery context for a resumed developer.

    Points the new developer at prior logs and the distilled transcript
    summary so it can understand what the previous instance did before
    crashing. Never points at raw JSONL transcripts — they embed the full
    prior context verbatim and reading them compounds across retries
    (measured at 43.8% of benchmark input tokens).
    """
    parts: list[str] = ["# Crash Recovery\n"]
    parts.append(
        "**A previous developer session crashed.** You are a fresh instance "
        "resuming the work. Before taking any action, review what has already "
        "been done:\n"
    )

    parts.append(
        f"1. **State file** (`{state_path}`): contains persisted run state "
        "and the previous MCP-tool-call timeline.\n"
    )
    parts.append(
        f"2. **Logs directory** (`{logs_dir}`): contains structured reports "
        "(`report.json`) from every Flow or Specialist invocation by the previous developer. "
        "Flow reports are in `.runtime/flow-reports/<flow_name>/<N>/report.json`; "
        "Specialist reports are in `.runtime/mcp-tool-reports/<mcp_tool_name>/<N>/report.json`. Read these "
        "to understand what was attempted and what failed.\n"
    )

    if summary_path and summary_path.exists():
        parts.append(
            f"3. **Previous session summary** (`{summary_path}`): distilled "
            "reasoning, commands, and Flow or Specialist verdicts from the crashed session.\n"
        )

    parts.append(
        "Do NOT read raw `*.jsonl` transcript files — they embed the full "
        "prior context verbatim (including previous transcript dumps) and "
        "will flood your context window.\n"
    )

    parts.append(
        "**Do not repeat Flow or Specialist calls that already succeeded.** Use the prior "
        "reports to continue from the previous session instead of restarting.\n"
    )

    if has_escalation_history:
        # Without this, a resumed developer that sees green criteria treats
        # the ticket as finished and exits without reading new failure
        # feedback delivered via the Escalation History (observed on benchmark
        # batch-01 retries: 0 MCP tool calls, 0 edits, "work was already
        # completed before the crash").
        parts.append(
            "**Escalation History takes priority over resuming.** The "
            "Escalation History section above may contain NEW failure "
            "feedback that arrived after the previous session's work. If it "
            "reports a failed verification or new operator directives, the "
            "ticket is NOT done — even if every criterion currently shows "
            "met. Address that feedback first.\n"
        )

    parts.append(
        "The git log also shows commits made by previous MCP tool calls. "
        "Run `git log --oneline -20` to see recent changes."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Blocked.md escalation history
# ---------------------------------------------------------------------------

# 16 KiB (~4k tokens). The original 4096 silently starved retry loops that
# route rich failure feedback through blocked.md: benchmark batch-01 (2026-07-26)
# feedback packets grew to ~6.6 KB and the whole packet vanished from every
# retry prompt (see the single-oversized-section fallback below).
_BLOCKED_MAX_CHARS = 16384


def build_blocked_section(logs_dir: Path) -> str | None:
    """Read blocked.md and return a prompt section, or None if absent.

    If the file exceeds _BLOCKED_MAX_CHARS, only the last few entries
    are included (tail from the last ``## `` headers). A single trailing
    entry that alone exceeds the budget is tail-clamped, never dropped.
    """
    blocked_path = logs_dir / "blocked.md"
    if not blocked_path.exists():
        return None
    try:
        text = blocked_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None

    # blocked.md remains append-only for auditability. A full reset inserts a
    # boundary, so entries from earlier runs must not influence the new agent.
    from booley.ticket_board.logs import RESET_BOUNDARY_PREFIX

    boundary = text.rfind(RESET_BOUNDARY_PREFIX)
    if boundary >= 0:
        search_from = boundary + len(RESET_BOUNDARY_PREFIX)
        entry_offsets = [
            offset
            for marker in ("\n## ", "\n### ")
            if (offset := text.find(marker, search_from)) >= 0
        ]
        if not entry_offsets:
            return None
        next_entry = min(entry_offsets)
        text = text[next_entry + 1 :].strip()

    if len(text) > _BLOCKED_MAX_CHARS:
        sections = text.split("\n## ")
        # Keep the file header (sections[0]) only if it fits; otherwise
        # show just the tail entries.
        tail: list[str] = []
        budget = _BLOCKED_MAX_CHARS
        for section in reversed(sections[1:]):
            candidate = "## " + section
            if budget - len(candidate) < 0:
                break
            tail.append(candidate)
            budget -= len(candidate) + 1
        tail.reverse()
        if not tail:
            # The trailing entry alone exceeds the budget (or the text has no
            # ``## `` markers at all). Dropping it kept literally nothing:
            # A benchmark batch-01 rerun lost the golden-harness feedback on
            # 14 tickets' retry prompts and the agents no-opped their retries.
            # Keep the entry's header plus the tail of its body instead —
            # failure evidence clusters at the end of log excerpts.
            last = sections[-1] if len(sections) == 1 else "## " + sections[-1]
            header, _, body = last.partition("\n")
            if len(header) <= 200:
                keep = _BLOCKED_MAX_CHARS - len(header) - 64
                clamped = f"{header}\n(entry head truncated)\n…{body[-keep:]}"
            else:
                # Degenerate first line (no real header) — clamp the raw tail.
                clamped = f"(entry head truncated)\n…{last[-_BLOCKED_MAX_CHARS:]}"
            tail = [clamped]
        text = "(earlier entries truncated)\n\n" + "\n".join(tail)

    return (
        "# Escalation History\n\n"
        "Previous blocks, failures, crashes, and human operator responses.\n"
        "Human operator directives are authoritative — you MUST follow them.\n\n"
        f"{text}"
    )


# ---------------------------------------------------------------------------
# Main prompt builder
# ---------------------------------------------------------------------------


def _build_user_prompt_sections(
    ctx: DeveloperPromptContext,
    criterion_endpoint_map: dict[str, tuple[str, str]],
) -> str:
    """Assemble the user prompt from dynamic context sections."""
    sections: list[str] = [
        build_ticket_section(ctx.ticket_path, ctx.logs_dir),
        build_type_guidance_section(ctx.ticket_type),
        build_workflow_section(
            criteria=ctx.criteria,
            ticket_type=ctx.ticket_type,
            criterion_endpoint_map=criterion_endpoint_map,
        ),
    ]

    blocked_section = build_blocked_section(ctx.logs_dir)
    if blocked_section:
        sections.append(blocked_section)

    if ctx.is_crash_recovery:
        sections.append(
            build_crash_recovery_section(
                logs_dir=ctx.logs_dir,
                state_path=ctx.state_path,
                summary_path=ctx.crash_summary_path,
                has_escalation_history=bool(blocked_section),
            )
        )

    sections.append(
        "---\n\n"
        "Begin by reading `$BOOLEY_TICKET_FILE`, then choose the next "
        "appropriate action to satisfy all mandatory criteria."
    )
    return "\n\n---\n\n".join(sections)


def build_developer_prompt(
    ctx: DeveloperPromptContext,
) -> tuple[str, str]:
    """Build developer system prompt and user prompt.

    Returns:
        (system_prompt, user_prompt) tuple.
    """
    mcp_tools = ctx.mcp_tools
    if mcp_tools is None:
        from booley.mcp.registry import discover_mcp_tools

        mcp_tools = discover_mcp_tools(
            booley_src=ctx.booley_src,
            project_mcp_tools_dir=ctx.project_mcp_tools_dir,
            mcp_tool_config=ctx.mcp_tool_config,
            flow_config=ctx.flow_config,
        )

    criterion_endpoint_map = _get_criterion_endpoint_map(
        mcp_tools=mcp_tools,
        project_criteria_path=ctx.project_criteria_path,
    )

    system_prompt = (
        _ROLE_MCP
        + "\n"
        + _build_rules_section(
            human_in_the_loop=ctx.human_in_the_loop,
            run_report=ctx.run_report,
            criteria=ctx.criteria,
        )
    )
    user_prompt = _build_user_prompt_sections(
        ctx,
        criterion_endpoint_map,
    )
    return system_prompt, user_prompt
