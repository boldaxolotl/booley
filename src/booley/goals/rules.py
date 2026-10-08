"""The rules an agent follows inside Goal Mode (ADR 0067 "Entry surface").

Entry returns this text with the Goal list, and ``goal_status(rules=true)``
returns it again after the agent's context is compacted, so it is written for
an agent reading it cold. Projects add their own rules in ``AGENTS.md``, not
here. The protected-input list comes from :mod:`booley.goals.protected_inputs`
so the rules and the check cannot drift apart.
"""

from __future__ import annotations

from booley.goals.protected_inputs import PROTECTED_INPUT_NAMES


def goal_mode_rules() -> str:
    """The Goal Mode rules as Markdown bullets, ending with a newline."""
    protected = ", ".join(f"`{name}`" for name in PROTECTED_INPUT_NAMES)
    bullets = (
        "Pass `work_dir` (this worktree's root) on every Booley call. While any Goal "
        "Mode is active, a Booley Flow, Specialist, or Goal call without `work_dir` is "
        "refused.",
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
        "Flow test that should catch it and confirm the test fails. If it already "
        "passes, the test does not detect the bug and a later pass proves nothing; "
        "tell the human in chat, and propose a Goal change through "
        "`goal_propose_change` if a Goal depends on it.",
        "Finish needs every Goal met at a clean, committed HEAD plus a Session "
        "Summary. Commit your work on the Goal Branch.",
        "Check `goal_status` when unsure; `goal_status(rules=true)` repeats these rules.",
    )
    return "".join(f"- {bullet}\n" for bullet in bullets)
