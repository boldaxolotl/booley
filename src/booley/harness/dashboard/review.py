"""The Run wizard's review text: queue verdict, timeout budget and warnings.

Pure text building, no Textual, so the wording of each line has one owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from booley.runtime.timefmt import format_duration

if TYPE_CHECKING:
    from booley.harness.dashboard.launch import RunFacts, SpecialistFacts


@dataclass(frozen=True)
class Occupants:
    """Who is in a worktree: connected-session labels and unfinished-Job handles, or why unknown."""

    sessions: tuple[str, ...] = ()
    unfinished_jobs: tuple[str, ...] = ()
    problem: str = ""


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def queue_line(facts: RunFacts) -> str:
    """Whether the Job runs now or is admitted by memory, whose fit depends on an estimate the preview lacks."""
    if not facts.takes_slot:
        return "Runs now (not admitted by memory)"
    return "Admitted by memory: waits while its estimate does not fit the Memory Budget"


def timeout_line(facts: RunFacts) -> str:
    """The timeout of a long-running Flow (per work unit, or for the whole run), or its problem; empty for the others."""
    if facts.timeout_problem:
        return f"Timeout problem: {facts.timeout_problem}; the Flow will reject this run"
    if facts.timeout_ms is None:
        return ""
    covers = "for the whole run" if facts.timeout_whole_run else "per work unit"
    return f"Timeout: {format_duration(facts.timeout_ms / 1000)} {covers}"


def model_line(model: str, cli: str) -> str:
    """The model and Agent CLI a Specialist run uses."""
    return f"Model: {model} on the {cli} Agent CLI"


def specialist_lines(facts: SpecialistFacts) -> list[str]:
    """A Specialist's model, token use, per-call timeout and sign-in warning."""
    lines: list[str] = []
    if facts.model:
        lines += [
            model_line(facts.model, facts.cli),
            f"Uses model tokens on the {facts.cli} Agent CLI's credential (auth = {facts.policy})",
        ]
    timeout = f"Timeout: {format_duration(facts.timeout_ms / 1000)} per model call"
    if facts.timeout_ms != facts.asked_ms:
        timeout += (
            f" (raised to the Specialist's minimum; the form asked for"
            f" {format_duration(facts.asked_ms / 1000)})"
        )
    lines.append(timeout)
    if facts.warning:
        lines.append(
            f"Warning: no {facts.cli} credential found in this Sandbox; a proxy token or"
            f" cloud provider may still work. To sign in: {facts.warning}"
        )
    return lines


def agent_refusal(facts: SpecialistFacts) -> str:
    """Why Submit does nothing when the Agent CLI cannot sign in or its settings cannot be checked."""
    return (
        f"Refused: {facts.refusal.rstrip('.')}. Copy command still works." if facts.refusal else ""
    )


def warning_lines(occupants: Occupants, uncommitted: int | str) -> list[str]:
    """Warnings, not blocks: sessions, unfinished Jobs and changes in the worktree.

    A string *uncommitted* is why the changes cannot be counted; ``occupants.problem``
    is why sessions and Jobs cannot be told.
    """
    lines: list[str] = []
    if occupants.problem:
        lines.append(f"Cannot check sessions and unfinished Jobs: {occupants.problem}")
    for label in occupants.sessions:
        lines.append(f"Warning: session {label} is connected to this worktree")
    if occupants.unfinished_jobs:
        handles = ", ".join(occupants.unfinished_jobs)
        lines.append(
            f"Warning: {_plural(len(occupants.unfinished_jobs), 'unfinished Job')} in this worktree"
            f" ({handles})"
        )
    if isinstance(uncommitted, str):
        lines.append(f"Cannot check uncommitted changes: {uncommitted}")
    elif uncommitted:
        lines.append(f"Warning: {_plural(uncommitted, 'uncommitted change')} in this worktree")
    return lines


def review_lines(facts: RunFacts, occupants: Occupants, uncommitted: int | str) -> list[str]:
    """The review screen's text, a line each: queue, timeout, Specialist, then the warnings."""
    lines = [queue_line(facts), timeout_line(facts)]
    if facts.specialist is not None:
        lines += specialist_lines(facts.specialist)
    lines += warning_lines(occupants, uncommitted)
    return [line for line in lines if line]
