"""Scriptable CLI for Booley upgrade review state."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.harness import upgrade_review
from booley.runtime.project_dir import resolve_checkout_project_dir


@dataclass(frozen=True)
class StatusPresentation:
    """Shared human and Doctor presentation for one upgrade status."""

    summary: str
    doctor_check_id: str | None = None
    action: str | None = None


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``booley upgrade`` and its status/acknowledge commands."""
    parser = subparsers.add_parser(
        "upgrade",
        help="Inspect or acknowledge version-upgrade review state",
    )
    commands = parser.add_subparsers(
        dest="upgrade_command",
        metavar="{status,acknowledge}",
    )
    status = commands.add_parser("status", help="Report pending version review state")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    acknowledge = commands.add_parser(
        "acknowledge",
        help="Acknowledge the exact reviewed target after successful verification",
    )
    acknowledge.add_argument("--expected-target", required=True)
    acknowledge.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def status_presentation(status: upgrade_review.ReviewStatus) -> StatusPresentation:
    """Return the shared summary, action, and Doctor identity for *status*."""
    if status.condition is upgrade_review.ReviewCondition.CURRENT:
        return StatusPresentation(
            f"Booley release review current through {status.reviewed_through}"
        )
    if status.condition is upgrade_review.ReviewCondition.PENDING:
        return StatusPresentation(
            f"Booley version changed from {status.reviewed_through} to {status.pending_target}",
            "upgrade.review-pending",
            "invoke /booley-heal",
        )
    if status.condition is upgrade_review.ReviewCondition.STALE_RUNTIME:
        target = status.pending_target or status.reviewed_through
        return StatusPresentation(
            f"This runtime has Booley {status.running_version}, behind review target {target}",
            "upgrade.runtime-stale",
            "invoke /booley-heal",
        )
    check_id = {
        upgrade_review.ReviewCondition.CORRUPT: "upgrade.review-state-corrupt",
        upgrade_review.ReviewCondition.UNSUPPORTED: "upgrade.version-unsupported",
        upgrade_review.ReviewCondition.UNAVAILABLE: "upgrade.review-state-unavailable",
    }[status.condition]
    return StatusPresentation(
        f"Booley upgrade review {status.condition.value}: {status.diagnostic}",
        check_id,
        f"preserve and inspect {status.state_path}; repair the state before invoking /booley-heal",
    )


def render_status(status: upgrade_review.ReviewStatus) -> str:
    """Render one concise status for humans and startup advisories."""
    presentation = status_presentation(status)
    if presentation.action is None:
        return f"{presentation.summary}."
    return f"{presentation.summary}. {presentation.action.capitalize()}."


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Execute one upgrade review subcommand."""
    project_dir = resolve_checkout_project_dir(project_root)
    command = getattr(args, "upgrade_command", None) or "status"
    is_status = command == "status"
    if is_status:
        status = upgrade_review.observe(project_dir)
    elif command == "acknowledge":
        try:
            status = upgrade_review.acknowledge(project_dir, args.expected_target)
        except upgrade_review.AcknowledgmentError as exc:
            if getattr(args, "json", False):
                print(json.dumps({"acknowledged": False, "error": str(exc)}, indent=2))
            else:
                print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        print(f"ERROR: unknown upgrade subcommand {command!r}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(status.as_dict(), indent=2))
    else:
        print(render_status(status))
    return 0 if is_status or status.condition is upgrade_review.ReviewCondition.CURRENT else 1
