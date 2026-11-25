"""The ``booley_review`` MCP tool: ``booley review`` as a submit, poll and cancel Job (#5599).

The tool starts ``booley review`` as an MCP Job and returns its ``run_id`` at once;
``booley_poll`` shows reviews finished of the total and the review running, then the CLI's
own report and the exit status; ``booley_cancel`` stops the Job and its post-setup hook. The
tool is gated by the ``reviewed-commits`` Feature Flag (ADR 0095): while it is off the tool is
absent from the catalog.
"""

from __future__ import annotations

import sys
from typing import Any

from booley.measurement.mcp_job import REVIEW_ENDPOINT

REVIEW_TOOL_NAME = "booley_review"

_DESCRIPTION = (
    "Review a commit as `booley review` does: run the Declared Reviews that cover the files "
    "the commit changed and store the Review Run. Returns a 'run_id' at once, not the result. "
    "Poll it with booley_poll: a running poll shows reviews finished of the total and the "
    "review running; the final result carries the exit status (EXIT_CODE) and the command's "
    "own report, ending with the stored Review Run. Exit 0: no finding is open. Exit 1: a "
    "finding the commit introduced is open (or, with 'status', the commit is not Reviewed). "
    "Exit 2: a Reviewer failed or the options were refused; nothing is stored for a refusal. "
    "'verify' re-checks the open findings of the latest discovery run against the commit, "
    "with 'waive' proposing a justified waiver per finding; 'status' only asks whether the "
    "commit is Reviewed. The command refuses a bad mix of options itself. booley_cancel "
    "stops the review."
)

_WAIVE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "With 'verify': a justified waiver for each open finding it names.",
    "items": {
        "type": "object",
        "properties": {
            "finding_id": {"type": "string"},
            "justification": {"type": "string"},
        },
        "required": ["finding_id", "justification"],
        "additionalProperties": False,
    },
}


def review_tool_def() -> dict[str, Any]:
    """The tool definition: the options of ``booley review`` as JSON Schema properties."""
    flag = "boolean"
    return {
        "name": REVIEW_TOOL_NAME,
        "description": _DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "The commit to review (default: HEAD); uncommitted work "
                    "is never reviewed.",
                },
                "base": {"type": "string", "description": "Compare with this commit."},
                "target": {
                    "type": "string",
                    "description": "Review only this Target's reviews; the run is printed "
                    "and never stored.",
                },
                "json": {"type": flag, "description": "Print the Review Run as JSON."},
                "verify": {
                    "type": flag,
                    "description": "Re-check the open findings of the latest discovery run.",
                },
                "status": {
                    "type": flag,
                    "description": "Only report whether the commit is Reviewed.",
                },
                "waive": _WAIVE_SCHEMA,
                "work_dir": {
                    "type": "string",
                    "description": "Linked worktree root whose HEAD and Project are reviewed "
                    "(same validation as the Booley Flows' work_dir).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    }


def review_argv(arguments: dict[str, Any]) -> list[str]:
    """The ``booley review`` command line for *arguments*; the CLI refuses a bad mix itself."""
    argv = [sys.executable, "-m", "booley.harness.booley"]
    if arguments.get("work_dir"):
        argv += ["--project", str(arguments["work_dir"])]
    argv.append("review")
    for option in ("base", "target"):
        if arguments.get(option):
            argv.append(f"--{option}={arguments[option]}")
    argv += [f"--{flag}" for flag in ("json", "verify", "status") if arguments.get(flag) is True]
    for waiver in arguments.get("waive") or ():
        argv += ["--waive", str(waiver["finding_id"]), str(waiver["justification"])]
    if arguments.get("revision"):
        argv += ["--", str(arguments["revision"])]  # after "--": never read as an option
    return argv


__all__ = ["REVIEW_ENDPOINT", "REVIEW_TOOL_NAME", "review_argv", "review_tool_def"]
