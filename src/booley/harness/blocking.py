"""Block/fail helpers -- enforce exit invariant.

Exit invariant: ticket MUST be transitioned out of active/ before
the developer stops. These helpers ensure that.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

from booley.runtime.agent_errors import (
    AgentTimeoutError,
    BlockingError,
    ContextExhaustedError,
    TransientAPIError,
    UsageLimitError,
    is_context_exhausted,
    is_usage_limit,
)
from booley.runtime.timefmt import format_human_datetime

from . import ticket_cli
from .models import TicketContext

logger = logging.getLogger(__name__)

__all__ = [
    "AgentTimeoutError",
    "BlockingError",
    "ContextExhaustedError",
    "TransientAPIError",
    "UsageLimitError",
]

# Server-side failures that are safe to retry unattended, as
# (incident_type, pattern) pairs.  Deliberately narrow: every signature here
# short-circuits human triage, so a false positive silently burns a whole
# developer run.  Only add a pattern once it is known to be independent of
# ticket content — a crash a human could not have prevented and cannot fix.
TRANSIENT_CRASH_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("api_stream_stall", re.compile(r"response stalled mid-?stream", re.IGNORECASE)),
    (
        "developer_cancelled",
        re.compile(r"^Developer Agent cancelled: CancelledError", re.IGNORECASE),
    ),
]


def classify_transient_crash(text: str) -> str | None:
    """Return the incident type for a known retry-safe crash, else None.

    Usage limits and context exhaustion are excluded even if their text
    happens to match: retrying either reproduces it, and both already have
    their own handling upstream.
    """
    if is_usage_limit(text) or is_context_exhausted(text):
        return None
    for incident_type, pattern in TRANSIENT_CRASH_SIGNATURES:
        if pattern.search(text):
            return incident_type
    return None


class FatalError(Exception):
    """Raised on unrecoverable errors within a step."""

    def __init__(self, error: str, *, slug: str = "") -> None:
        self.error = error
        self.slug = slug
        super().__init__(error)


# Exit code the harness returns when the user quits the Console TUI.
EXIT_USER_QUIT = 75


class UserQuitError(Exception):
    """User pressed Q in the Console TUI — stop processing tickets."""


_KIND_LABELS: dict[str, str] = {"blocked": "Blocked", "failed": "Failed", "crashed": "Crashed"}


def _append_blocked_entry(
    logs_dir: Path,
    reason: str,
    step: str,
    kind: Literal["blocked", "failed", "crashed"] = "blocked",
    run_index: int | None = None,
    questions: list[str] | None = None,
) -> None:
    """Append an entry to the append-only blocked.md log.

    *reason* doubles as the "### Error" section body for "failed"/"crashed"
    kinds -- both callers already pass the same string for both roles.
    """
    from .logging_utils import now_iso

    blocked_path = logs_dir / "blocked.md"
    blocked_path.parent.mkdir(parents=True, exist_ok=True)

    run_label = f"Run {run_index}" if run_index is not None else "Setup"
    timestamp = format_human_datetime(now_iso(), seconds=True)

    lines = [f"## {run_label} -- {_KIND_LABELS[kind]} ({timestamp})", ""]
    lines.append(f"**Step:** {step}")
    lines.append(f"**Reason:** {reason}")
    lines.append("")
    if kind != "blocked":
        lines.extend(["### Error", "", reason, ""])
    if questions:
        lines.append("### Questions")
        lines.append("")
        for i, q in enumerate(questions, 1):
            lines.extend(
                [
                    f"#### Q{i}: {q}",
                    "**Context:** See rtl_plan.md / verification_plan.md, if present",
                    "**Answer:**",
                    "",
                ]
            )

    entry = "\n".join(lines) + "\n"
    header = (
        "# Escalation History\n\n"
        "Chronological log of blocks, failures, crashes, and human responses.\n"
        "Human operator directives are authoritative — agents MUST follow them.\n\n"
    )
    # Atomic append avoids TOCTOU race on exists() check (§7)
    with blocked_path.open("a", encoding="utf-8") as f:
        if f.tell() == 0:
            f.write(header)
        f.write(entry)


def block_ticket(
    ctx: TicketContext,
    reason: str,
    step: str,
    questions: list[str] | None = None,
    run_index: int | None = None,
) -> None:
    """Block ticket and append entry to blocked.md."""
    logger.warning("Blocking %s at %s: %s", ctx.slug, step, reason)
    ownership = {"expected_execution_id": ctx.execution_id} if ctx.execution_id else {}
    ticket_cli.block(ctx.project_root, ctx.slug, reason=reason, step=step, **ownership)
    _append_blocked_entry(
        ctx.logs_dir, reason, step, "blocked", run_index=run_index, questions=questions
    )


def fail_ticket(
    ctx: TicketContext, error: str, step: str, run_index: int | None = None, crashed: bool = False
) -> None:
    """Fail ticket — delegates to block with error semantics."""
    logger.error("Failing %s at %s: %s", ctx.slug, step, error)
    ownership = {"expected_execution_id": ctx.execution_id} if ctx.execution_id else {}
    ticket_cli.block(ctx.project_root, ctx.slug, reason=error, step=step, **ownership)
    _append_blocked_entry(
        ctx.logs_dir, error, step, "crashed" if crashed else "failed", run_index=run_index
    )
