"""The unread-Job notice every Booley MCP tool response ends with (#2539).

An agent that starts a background Job and moves on can forget to poll it. The
notice names each of the caller's finished Jobs whose result it has not read yet,
until a poll returns that result. Building the notice only reads: it never marks
a Job read.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import replace

from booley.mcp import catalog
from booley.mcp.application import McpTextBlock, McpToolPayload
from booley.mcp.job_lifecycle import JobLifecycle
from booley.mcp.job_ownership import Caller
from booley.presentation.report_results import present_job_report
from booley.runtime import job_records as jobrec

logger = logging.getLogger(__name__)

# Job lines in one notice; the rest are counted on a closing line.
_MAX_NOTICE_LINES = 10
_LINE_PREFIX = "UNREAD JOB RESULT:"


def target_label(argv: list[str]) -> str:
    """The Targets a Job was started with, for listings; ``default`` when none were named."""
    targets = [value for flag, value in itertools.pairwise(argv) if flag == "--target"] + [
        arg.partition("=")[2] for arg in argv if arg.startswith("--target=")
    ]
    return ", ".join(targets) or "default"


class UnreadNotice:
    """Appends the caller's unread finished Jobs to a tool response."""

    def __init__(self, jobs: JobLifecycle) -> None:
        self._jobs = jobs

    def append(self, payload: McpToolPayload, caller: Caller) -> McpToolPayload:
        """Return *payload* with a closing notice block, or unchanged when nothing is unread.

        The tool call has already finished and may have had side effects, so a
        failure here is logged and the result is returned as it was.
        """
        try:
            text = self._notice(caller)
        except Exception:
            logger.warning("Unread Job notice unavailable", exc_info=True)
            return payload
        if not text:
            return payload
        return replace(payload, content=(*payload.content, McpTextBlock(text)))

    def _notice(self, caller: Caller) -> str:
        """The notice text for *caller*; empty when it has no unread Job."""
        unread = self._jobs.unread_owned(caller)
        shown = unread[:_MAX_NOTICE_LINES]
        text = [self._line(rec) for rec in shown]
        hidden = len(unread) - len(shown)
        if hidden > 0:
            text.append(
                f"… and {hidden} more finished Jobs you have not read; call "
                f"'{catalog.POLL_MCP_TOOL_NAME}' with no run_id to get them one by one."
            )
        return "\n".join(text)

    def _line(self, rec: jobrec.JobRecord) -> str:
        """One Job: id, Flow, Target, verdict with its key metric, and how to read it."""
        poll = catalog.POLL_MCP_TOOL_NAME
        return (
            f"{_LINE_PREFIX} {rec.run_id} · {rec.endpoint} · Target {target_label(rec.argv)}"
            f" · {self._verdict(rec)}"
            f" · call '{poll}' with run_id={rec.run_id} for the full result."
        )

    def _verdict(self, rec: jobrec.JobRecord) -> str:
        """``WORD`` or ``WORD · metric``, as the Dashboard shows the same Job."""
        result = self._jobs.terminal_result(rec.run_id)
        report = result.report if result.report_fresh and result.report is not None else {}
        flow_result = present_job_report(
            report, endpoint=rec.endpoint, exit_code=result.exit_code, argv=rec.argv
        ).flow_result.value
        if flow_result.startswith("unavailable"):
            # An exit code outside the known set: fall back to the recorded status.
            return rec.status.upper()
        return flow_result
