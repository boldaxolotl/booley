"""The Run wizard's review text: queue verdict, timeout budget and warnings.

Pure text building, no Textual, so the wording of each line has one owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from booley.runtime.job_slots import CLASS_MEMORY
from booley.runtime.timefmt import format_duration

if TYPE_CHECKING:
    from booley.harness.dashboard.launch import RunFacts
    from booley.runtime.job_slots import AdmissionOutlook


@dataclass(frozen=True)
class Occupants:
    """Who is in a worktree: connected-session labels and unfinished-Job handles, or why unknown."""

    sessions: tuple[str, ...] = ()
    running_jobs: tuple[str, ...] = ()
    problem: str = ""


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def queue_line(facts: RunFacts, outlook: AdmissionOutlook | str | None) -> str:
    """Whether the Job runs now or queues; a string *outlook* is why the queue cannot be read.

    ``None`` is only valid when *facts* take no slot or the Job is admitted by
    memory: its fit depends on an estimate the preview does not have.
    """
    if not facts.takes_slot:
        return "Runs now (takes no Job slot)"
    if facts.job_class == CLASS_MEMORY:
        return "Admitted by memory: waits while its estimate does not fit the Memory Budget"
    assert outlook is not None  # a run that takes a slot always has a reading or a reason
    if isinstance(outlook, str):
        return f"Cannot check the queue: {outlook}"
    if outlook.runs_now:
        return "Runs now"
    return (
        f"Queues behind {_plural(outlook.ahead, 'Job')} (position {outlook.position})"
        f" for a {outlook.job_class} slot"
    )


def refusal(outlook: AdmissionOutlook) -> str:
    """Why Submit does nothing while the queue is full."""
    return f"Refused: {outlook.refusal}. Retry when queued work drains; Copy command still works."


def timeout_line(facts: RunFacts) -> str:
    """The per-work-unit timeout of a long-running Flow, or its problem; empty for the others."""
    if facts.timeout_problem:
        return f"Timeout problem: {facts.timeout_problem}; the Flow will reject this run"
    if facts.timeout_ms is None:
        return ""
    return f"Timeout: {format_duration(facts.timeout_ms / 1000)} per work unit"


def warning_lines(occupants: Occupants, uncommitted: int | str) -> list[str]:
    """Warnings, not blocks: sessions, running Jobs and changes in the worktree.

    A string *uncommitted* is why the changes cannot be counted; ``occupants.problem``
    is why sessions and Jobs cannot be told.
    """
    lines: list[str] = []
    if occupants.problem:
        lines.append(f"Cannot check sessions and running Jobs: {occupants.problem}")
    for label in occupants.sessions:
        lines.append(f"Warning: session {label} is connected to this worktree")
    if occupants.running_jobs:
        handles = ", ".join(occupants.running_jobs)
        lines.append(
            f"Warning: {_plural(len(occupants.running_jobs), 'running Job')} in this worktree"
            f" ({handles})"
        )
    if isinstance(uncommitted, str):
        lines.append(f"Cannot check uncommitted changes: {uncommitted}")
    elif uncommitted:
        lines.append(f"Warning: {_plural(uncommitted, 'uncommitted change')} in this worktree")
    return lines
