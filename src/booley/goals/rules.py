"""The rules an agent follows inside Goal Mode (ADR 0067 "Entry surface").

Entry returns this text with the Goal list, and ``goal_status(rules=true)``
returns it again after the agent's context is compacted, so it is written for
an agent reading it cold. Projects add their own rules in ``AGENTS.md``, not
here. The protected-input list comes from :mod:`booley.goals.protected_inputs`
so the rules and the check cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from booley.goals.declared_inputs import DeclaredCapture
from booley.goals.protected_inputs import PROTECTED_INPUT_NAMES

WORK_DIR_GUIDANCE = (
    "Pass `work_dir` (the absolute worktree root) to Booley Flows, Specialists, "
    "Goal tools{accepting}. {nonaccepting} accept no `work_dir`."
)
WORK_DIR_REQUIREMENT = (
    "While any Goal Record occupies the Project, Flows, "
    "Specialists, Goal tools, and `booley_report` refuse a call without "
    "`work_dir` (`booley_targets` may omit it), and Project-defined endpoints "
    "must declare `work_dir` to run."
)


def work_dir_guidance(
    inventory: tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
) -> str:
    """Render routing prose from schemas supplied by the calling entry point."""
    accepting, nonaccepting = inventory
    utilities = [
        f"`{tool['name']}`"
        for tool in accepting
        if not tool.get("module") and not tool.get("is_goal")
    ]
    suffix = ", " + ", and ".join(utilities) if utilities else ""
    return (
        WORK_DIR_GUIDANCE.format(
            accepting=suffix,
            nonaccepting=", ".join(f"`{tool['name']}`" for tool in nonaccepting),
        )
        + " "
        + WORK_DIR_REQUIREMENT
    )


def goal_mode_rules(declared: DeclaredCapture | None = None, *, routing_guidance: str) -> str:
    """The Goal Mode rules as Markdown bullets, ending with a newline."""
    protected = ", ".join(f"`{name}`" for name in PROTECTED_INPUT_NAMES)
    if declared is not None:
        protected += "; Project-declared files: " + ", ".join(
            f"`{row.path}`" for row in declared.working
        )
        protected += ". Declarations: " + ", ".join(f"`{rule}`" for rule in declared.declarations)
        protected += "; changing these declarations requires abandon and re-entry"
    bullets = (
        routing_guidance,
        "Only Booley Flows and Specialists produce evidence. Nothing you write or "
        "report meets a Goal.",
        f"Protected inputs: {protected}. Editing one blocks Finish until it is "
        "reverted, and evidence from a run that started or ended while one differed "
        "from its entry state is discarded. A change made and reverted while a run is "
        "in flight is not detected, so do not edit them at all.",
        "Every Goal is mandatory. Adding or relaxing a Goal needs the human's "
        "approval through `goal_propose_change`. Never record approvals yourself when the "
        "client form is available. Decline/cancel/invalid form content leaves the proposal "
        "pending; then obtain the human's instruction in chat and record approve/reject "
        "for the exact saved proposal ID with their quoted words and nonblank reason.",
        "Never weaken tests or Targets to meet a Goal: `.core` and `tests.toml` "
        "edits, and `.sdc`/`.xdc` edits, are flagged in the review package.",
        "When fixing a bug, reproduce it first: before changing code, run the Booley "
        "Flow test that should catch it and confirm the test fails for the reported bug, not for an unrelated "
        "build or setup error. If it already "
        "passes, the test does not detect the bug and a later pass proves nothing; "
        "tell the human in chat, and propose a Goal change through "
        "`goal_propose_change` if a Goal depends on it.",
        "Booley records each SIM Goal's baseline-to-final Test Run transitions. "
        "A new Test Run that was never seen failing is visible in status and the review "
        "package. Baselines run as background Jobs; pending or unavailable baseline "
        "evidence is reported and never changes a Goal or blocks Finish.",
        "Finish needs every Goal met at a clean, committed HEAD plus a Session "
        "Summary. Commit your work on the Goal Branch.",
        "Check `goal_status` when unsure; `goal_status(rules=true)` repeats these rules.",
    )
    return LIFECYCLE_RETRY_RULE + "\n" + "".join(f"- {bullet}\n" for bullet in bullets)


LIFECYCLE_RETRY_RULE = (
    "Pass record_id and a caller-stable operation_id to goal_finish, including abandonment. "
    "Retry with identical summary/quote/options to recover its saved result, even after re-entry. "
    "A revalidation_required result needs a fresh operation_id and Session Summary. "
    "Open done review findings do not require a second approval to finish."
)
