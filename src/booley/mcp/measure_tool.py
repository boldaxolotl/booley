"""The ``booley_measure`` MCP tool: ``booley measure`` as a submit, poll and cancel Job (#4134).

The tool starts ``booley measure`` as an MCP Job and returns its ``run_id`` at once;
``booley_poll`` shows Flows finished of the total, then the verdict line, the failing rows,
the report paths and the exit status; ``booley_cancel`` stops the Job and its children. The
rules in its description are :data:`booley.measurement.measuring_rules.MEASURING_RULES`.
"""

from __future__ import annotations

import sys
from typing import Any

from booley.mcp.report_encoding import _format_mcp_tool_result
from booley.measurement.mcp_job import MEASURE_ENDPOINT, progress_text
from booley.measurement.measuring_rules import MEASURING_RULES

MEASURE_TOOL_NAME = "booley_measure"
# A measurement has no Job budget, like a Standalone ``booley measure`` run.
MEASURE_TIMEOUT_S = 7 * 24 * 3600

_DESCRIPTION = (
    "Measure a commit against the Target Metrics rules in metrics.toml, as `booley measure` "
    "does: one child Flow per rules table in a clean checkout, and the Measurement Run stored. "
    "Returns a 'run_id' at once, not the result. Poll it with booley_poll: a running poll shows "
    "Flows finished of the total and the Flows running; the final result carries the exit "
    "status (EXIT_CODE), the verdict line, the failing rows, the Markdown and JSON report "
    "paths and, after exit 1 or 2, the rule to follow next. booley_cancel stops the "
    "measurement and its Flows (up to 45 s). A commit already Measured shows its stored run "
    "unless 'again' is set.\n\nRules:\n"
)


def measure_tool_def() -> dict[str, Any]:
    """The tool definition; its description ends with every measuring rule, verbatim."""
    flag = "boolean"
    return {
        "name": MEASURE_TOOL_NAME,
        "description": _DESCRIPTION + "\n".join(f"- {rule}" for rule in MEASURING_RULES),
        "schema": {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "The commit to measure (default: HEAD); uncommitted work "
                    "is never measured.",
                },
                "base": {"type": "string", "description": "Compare with this commit."},
                "target": {
                    "type": "string",
                    "description": "Measure only this Target's tables; the run is never "
                    "stored or Measured.",
                },
                "all": {
                    "type": flag,
                    "description": "Evaluate every table even after an unmet or error verdict.",
                },
                "again": {
                    "type": flag,
                    "description": "Measure again even when the commit is already Measured.",
                },
                "validate": {
                    "type": flag,
                    "description": "Check metrics.toml against the Flows, Targets and tests; "
                    "measure nothing. Takes no other option.",
                },
                "work_dir": {
                    "type": "string",
                    "description": "Linked worktree root whose HEAD and Project are measured "
                    "(same validation as the Booley Flows' work_dir).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    }


def measure_argv(arguments: dict[str, Any]) -> list[str]:
    """The ``booley measure`` command line for *arguments*; the CLI refuses a bad mix itself."""
    argv = [sys.executable, "-m", "booley.harness.booley"]
    if arguments.get("work_dir"):
        argv += ["--project", str(arguments["work_dir"])]
    argv.append("measure")
    for option in ("base", "target"):
        if arguments.get(option):
            argv.append(f"--{option}={arguments[option]}")
    argv += [f"--{flag}" for flag in ("all", "again", "validate") if arguments.get(flag) is True]
    if arguments.get("revision"):
        argv += ["--", str(arguments["revision"])]  # after "--": never read as an option
    return argv


def running_text(progress: dict[str, Any]) -> str:
    """What a poll of a running measurement adds: Flows finished of the total, and running."""
    return f"Progress: {progress_text(progress)}."


def result_text(exit_code: int, stdout: str, stderr: str, report: dict[str, Any] | None) -> str:
    """The final result: the exit status, the whole summary, then the notes and exit rule.

    The summary is the bounded text the process published; it sits outside the truncated
    stdout section. Any other result, such as a refusal or ``validate`` that published none,
    renders as any other tool result.
    """
    summary = None if report is None else report.get("summary")
    if (
        report is None
        or report.get("endpoint") != MEASURE_ENDPOINT
        or not isinstance(summary, str)
        or report.get("exit_code") != exit_code
    ):
        return _format_mcp_tool_result(exit_code, stdout, stderr, report)
    head, _, notes = _format_mcp_tool_result(exit_code, "", stderr, None).partition("\n")
    return "\n".join([head, summary, *([notes] if notes else [])])


__all__ = [
    "MEASURE_ENDPOINT",
    "MEASURE_TIMEOUT_S",
    "MEASURE_TOOL_NAME",
    "measure_argv",
    "measure_tool_def",
    "result_text",
    "running_text",
]
