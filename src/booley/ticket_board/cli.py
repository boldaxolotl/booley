"""CLI parser and main() entry point for the ticket board system."""

from __future__ import annotations

import argparse

from .cli_handlers import (
    _cmd_activate,
    _cmd_approve,
    _cmd_archive,
    _cmd_block,
    _cmd_board,
    _cmd_classify,
    _cmd_collect_evidence,
    _cmd_complete,
    _cmd_create_file,
    _cmd_detect_orphans,
    _cmd_enqueue,
    _cmd_fail,
    _cmd_handoff,
    _cmd_init,
    _cmd_log_incident,
    _cmd_log_transition,
    _cmd_move_ticket,
    _cmd_mutation_config,
    _cmd_next_step,
    _cmd_parse_ticket,
    _cmd_promote_waiting,
    _cmd_read_board,
    _cmd_requeue,
    _cmd_reset,
    _cmd_reset_to_deprecated,
    _cmd_resume,
    _cmd_return_to_draft,
    _cmd_show,
    _cmd_slug,
    _cmd_steps,
    _cmd_timing,
    _cmd_unblock,
    _cmd_update_board,
    _cmd_usage,
    _cmd_validate_logs,
    _cmd_validate_ticket,
)
from .constants import (
    TICKET_DIRS,
    VALID_TYPES,
)
from .helpers import (
    detect_tickets_dir,
    ensure_utf8_output,
)
from .io import TicketIO


def _add_query_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register read-only board/ticket inspection subcommands."""
    # board (default)
    sub.add_parser("board", help="Display board as markdown table")
    # show: with a slug, print one ticket's paths/branch/criteria; without, an
    # alias for 'board' (back-compat).
    p = sub.add_parser("show", help="Show a ticket's paths/branch/criteria (or the board)")
    p.add_argument("slug", nargs="?", default=None, help="Ticket slug to inspect")

    # slug
    p = sub.add_parser("slug", help="Generate slug from summary text")
    p.add_argument("summary", help="Summary text to slugify")

    # read-board
    sub.add_parser("read-board", help="Print all tickets as JSON")


def _add_ticket_edit_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register subcommands that mutate a single ticket's frontmatter/logs."""
    # update-board
    p = sub.add_parser("update-board", help="Update a ticket's frontmatter fields")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument(
        "--set", nargs="+", metavar="K=V", help="Field updates (e.g. status=running step=planning)"
    )
    p.add_argument("--append-step", metavar="STEP", help="Append a step to steps_completed")
    reset_group = p.add_mutually_exclusive_group()
    reset_group.add_argument(
        "--reset-steps", action="store_true", help="Clear steps_completed before applying updates"
    )
    reset_group.add_argument(
        "--reset-steps-from",
        metavar="STEP",
        help="Reset steps_completed to include up to STEP, removing later steps and their artifacts",
    )
    p.add_argument(
        "--log",
        action="store_true",
        help="Auto-append transition log (uses old->new step from ticket state)",
    )

    # log-transition
    p = sub.add_parser("log-transition", help="Append to transitions.log")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument(
        "--from", dest="from_state", required=True, help="Old state (e.g. running:planning)"
    )
    p.add_argument("--to", dest="to_state", required=True, help="New state")
    p.add_argument("--actor", required=True, help="Actor name (e.g. ticket-execute)")
    p.add_argument("--detail", required=True, help="Transition detail")

    # parse-ticket
    p = sub.add_parser("parse-ticket", help="Parse ticket YAML frontmatter -> JSON")
    p.add_argument("path", help="Path to ticket .md file")

    # validate-ticket
    p = sub.add_parser("validate-ticket", help="Validate ticket fields")
    p.add_argument("path", help="Path to ticket .md file")
    p.add_argument(
        "--check-git", action="store_true", help="Also check branch existence and dirty tree"
    )


def _add_lifecycle_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register ticket lifecycle subcommands (move/block/queue/activate/etc.)."""
    # move-ticket
    p = sub.add_parser("move-ticket", help="Move ticket file between directories")
    p.add_argument("slug", help="Ticket slug")
    # Accept both bare names (queue) and board/-prefixed (board/queue)
    _VALID_TO_DIRS = TICKET_DIRS + [d.split("/", 1)[1] for d in TICKET_DIRS]
    p.add_argument("--to", required=True, choices=_VALID_TO_DIRS, help="Target directory")

    # block
    p = sub.add_parser("block", help="Block a ticket")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--reason", required=True, help="Block reason")
    p.add_argument("--step", required=True, help="Step where blocked")

    # fail (alias for block with error semantics)
    p = sub.add_parser("fail", help="Block a ticket due to a run error (alias for block)")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--error", required=True, help="Error description")
    p.add_argument("--step", required=True, help="Step where blocked")

    # requeue
    p = sub.add_parser("requeue", help="Requeue a ticket (atomic move + log)")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--reason", default="requeued", help="Reason for requeueing")

    # handoff
    p = sub.add_parser("handoff", help="Hand off ticket to review")
    p.add_argument("slug", help="Ticket slug")

    # init
    p = sub.add_parser("init", help="Initialize a fresh ticket for execution")
    p.add_argument("ticket_path", help="Path to ticket .md file")

    # resume
    p = sub.add_parser("resume", help="Detect resume state for a ticket")
    p.add_argument("slug", help="Ticket slug")


def _add_planning_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register step-planning and classification subcommands."""
    # next-stage
    p = sub.add_parser("next-step", help="Get next step")
    p.add_argument("type_or_slug", help="Ticket type (feature/bugfix/refactor) or ticket slug")
    p.add_argument("current", help="Current step name")
    p.add_argument("--skip", default="", help="Comma-separated extra steps to skip")

    # stages
    p = sub.add_parser("steps", help="List all steps for a ticket type")
    p.add_argument("type_or_slug", help="Ticket type (feature/bugfix/refactor) or ticket slug")
    p.add_argument("--skip", default="", help="Comma-separated extra steps to skip")

    # classify
    p = sub.add_parser(
        "classify", help="Classify tickets into executable/blocked/waiting/review/orphaned"
    )
    p.add_argument(
        "--format",
        choices=["json", "counts"],
        default="json",
        help="Output format: json (default) or counts (shell-eval KEY=N lines)",
    )

    # detect-orphans
    p = sub.add_parser("detect-orphans", help="Find running tickets with no active agent (stale)")
    p.add_argument(
        "--threshold",
        type=int,
        default=30,
        help="Minutes since last_update to consider orphaned (default: 30)",
    )
    p.add_argument(
        "--force-fail",
        action="store_true",
        help="Automatically move orphaned tickets to blocked state",
    )

    # mutation-config
    p = sub.add_parser("mutation-config", help="Select widest target for RTL mutation testing")
    p.add_argument("targets", nargs="+", help="Target names (e.g. config_a/v01 config_d/variant)")


def _add_create_file_args(p: argparse.ArgumentParser) -> None:
    """Register arguments for the create-file subcommand."""
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--summary", required=True, help="Ticket summary")
    p.add_argument(
        "--type",
        required=True,
        dest="ticket_type",
        choices=sorted(VALID_TYPES),
        help="Ticket type",
    )
    p.add_argument("--branch", required=True, help="Base branch")
    p.add_argument(
        "--project-destination-ref",
        default="",
        help="Paired repository destination as a full refs/heads/... name",
    )
    p.add_argument(
        "--scope",
        nargs="*",
        default=[],
        help="Files in scope (append ' [new]' suffix for new files)",
    )
    p.add_argument("--spec", default="", help="Path to architecture spec")
    p.add_argument("--dependencies", nargs="*", default=[], help="Dependency slugs")
    p.add_argument(
        "--priority", default="medium", choices=["low", "medium", "high"], help="Priority"
    )
    p.add_argument(
        "--criteria",
        default=None,
        help="JSON dict: {mandatory: {...}, optional: {...}} — criteria",
    )
    p.add_argument(
        "--criteria-file",
        default="",
        help="Read criteria JSON/YAML from file instead of --criteria",
    )
    p.add_argument(
        "--on-success",
        default=None,
        help=(
            "JSON dict: {destination, merge, cleanup, triage_report, remove_targets} — "
            "successful-run disposition"
        ),
    )
    p.add_argument("--body", default="", help="Ticket body (markdown)")
    p.add_argument("--body-file", default="", help="Read ticket body from file instead of --body")


def _add_creation_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register ticket creation, queueing, and approval subcommands."""
    # create-file
    p = sub.add_parser("create-file", help="Create a new ticket .md file in drafts/")
    _add_create_file_args(p)

    p = sub.add_parser("return-to-draft", help="Start a new authoring generation")
    p.add_argument("slug", help="Blocked ticket slug")

    # enqueue
    p = sub.add_parser("enqueue", help="Enqueue a ticket (stamp frontmatter + log)")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--summary", default=None, help="Ticket summary (reads from file if omitted)")
    p.add_argument(
        "--type",
        default=None,
        dest="ticket_type",
        choices=sorted(VALID_TYPES),
        help="Ticket type (reads from file if omitted)",
    )
    p.add_argument("--branch", default=None, help="Base branch (reads from file if omitted)")
    p.add_argument(
        "--destination",
        choices=["review", "done"],
        default=None,
        help="Where ticket goes after success (review or done)",
    )
    p.add_argument(
        "--merge",
        action="store_true",
        default=None,
        dest="merge",
        help="Merge feature branch on completion",
    )
    p.add_argument(
        "--no-merge",
        action="store_false",
        dest="merge",
        help="Skip merge on completion (also use --no-cleanup when cleanup is configured)",
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        default=None,
        dest="cleanup",
        help="Delete worktree/branch on completion",
    )
    p.add_argument(
        "--no-cleanup",
        action="store_false",
        dest="cleanup",
        help="Keep worktree/branch on completion",
    )
    p.add_argument(
        "--triage-report",
        action="store_true",
        default=None,
        dest="triage_report",
        help="Prepare the rich HTML change explanation before handoff",
    )
    p.add_argument(
        "--no-triage-report",
        action="store_false",
        dest="triage_report",
        help="Skip the agent-prepared HTML explanation",
    )
    p.add_argument(
        "--integration-base",
        default="",
        help="Original dev branch (for integration branch tickets)",
    )

    # activate
    p = sub.add_parser("activate", help="Activate a ticket for execution (move to active/)")
    p.add_argument("slug", help="Ticket slug")

    # unblock
    p = sub.add_parser("unblock", help="Unblock a ticket (move blocked->queue)")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--feedback", default="", help="Feedback for next run (appended to blocked.md)")

    # reset
    p = sub.add_parser(
        "reset",
        help="Reset a ticket (move to queue, clear state, wipe logs, delete worktree+branch)",
    )
    p.add_argument("slug", help="Ticket slug")
    p.add_argument(
        "--force",
        action="store_true",
        help="Reset even while a live process owns the ticket (stop the run first; this does not stop it for you)",
    )
    p.add_argument(
        "--reason",
        default="user reset ticket",
        help="Why a clean run is required (recorded in transition history)",
    )

    # reset-to (removed — deprecated stub prints error)
    p = sub.add_parser("reset-to", help="[REMOVED] Use 'reset' for a full reset")
    p.add_argument("slug", nargs="?", help="Ticket slug")
    p.add_argument("stage", nargs="?", help="(ignored)")

    # approve
    p = sub.add_parser("approve", help="Validate and complete a review ticket")
    p.add_argument("slug", help="Ticket slug")

    # complete
    p = sub.add_parser(
        "complete",
        help="Complete a ticket: approve, merge/cleanup per on_success from frontmatter",
    )
    p.add_argument("slug", help="Ticket slug")


def _add_reporting_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register reporting/evidence/incident subcommands."""
    # promote-waiting
    sub.add_parser(
        "promote-waiting", help="Check waiting tickets and print any that are now executable"
    )

    # archive
    p = sub.add_parser("archive", help="Archive done tickets (or a specific ticket by slug)")
    p.add_argument(
        "slug", nargs="?", default=None, help="Specific ticket to archive (default: all done/)"
    )
    p.add_argument(
        "--keep-logs", action="store_true", help="Keep log directories (default: remove them too)"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Archive a ticket that is not 'done' (discards its state)",
    )

    # log-incident
    p = sub.add_parser("log-incident", help="Append an incident to logs/<slug>/incidents.md")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument(
        "--type",
        required=True,
        dest="incident_type",
        help="Incident type (compilation_failure, context_exhaustion, sim_timeout)",
    )
    p.add_argument("--step", required=True, help="Step where incident occurred")
    p.add_argument("--description", required=True, help="What happened")
    p.add_argument(
        "--resolution", default="unresolved", help="How it was resolved (default: unresolved)"
    )

    # validate-logs
    p = sub.add_parser(
        "validate-logs", help="Validate that all required log artifacts exist for a ticket"
    )
    p.add_argument("slug", help="Ticket slug")

    # collect-evidence
    p = sub.add_parser(
        "collect-evidence",
        help="Collect tamper-resistant evidence bundle for adversarial reviewer",
    )
    p.add_argument("slug", help="Ticket slug")

    # timing
    p = sub.add_parser("timing", help="Generate per-step wall-time report from transitions.log")
    p.add_argument("slug", help="Ticket slug")
    p.add_argument("--save", action="store_true", help="Save report to logs/<slug>/timing.md")
    p.add_argument(
        "--by-endpoint",
        action="store_true",
        help="Emit the per-endpoint execution table (endpoint, exit, duration, cost, Δcriteria) "
        "from booley_state.json's timeline instead of per-step wall-time",
    )

    # usage
    p = sub.add_parser("usage", help="Analyze token usage from a ticket run transcript")
    p.add_argument(
        "transcript",
        nargs="?",
        default=None,
        help="Path to developer JSONL transcript (auto-discovered when --slug is given)",
    )
    p.add_argument(
        "--transitions", help="Path to transitions.log (auto-detected from slug if not given)"
    )
    p.add_argument(
        "--slug",
        help="Ticket slug (for auto-detecting current-run transcripts and transitions.log)",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print one combined current-run token and cost total",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with all ticket_board subcommands."""
    parser = argparse.ArgumentParser(
        prog="ticket_board",
        description="Ticket board CLI -- mechanical operations for the ticket system.",
    )
    sub = parser.add_subparsers(dest="command")

    _add_query_subcommands(sub)
    _add_ticket_edit_subcommands(sub)
    _add_lifecycle_subcommands(sub)
    _add_planning_subcommands(sub)
    _add_creation_subcommands(sub)
    _add_reporting_subcommands(sub)

    return parser


# ---------------------------------------------------------------------------
# Dispatch table: command name -> handler function
# ---------------------------------------------------------------------------

HANDLERS = {
    # Pure output commands
    "board": _cmd_board,
    "show": _cmd_show,
    "slug": _cmd_slug,
    "read-board": _cmd_read_board,
    "parse-ticket": _cmd_parse_ticket,
    "validate-ticket": _cmd_validate_ticket,
    "next-step": _cmd_next_step,
    "steps": _cmd_steps,
    "classify": _cmd_classify,
    "detect-orphans": _cmd_detect_orphans,
    "mutation-config": _cmd_mutation_config,
    "resume": _cmd_resume,
    # Side-effect commands
    "update-board": _cmd_update_board,
    "log-transition": _cmd_log_transition,
    "move-ticket": _cmd_move_ticket,
    "activate": _cmd_activate,
    "block": _cmd_block,
    "fail": _cmd_fail,
    "requeue": _cmd_requeue,
    "handoff": _cmd_handoff,
    "unblock": _cmd_unblock,
    "reset": _cmd_reset,
    "reset-to": _cmd_reset_to_deprecated,
    "approve": _cmd_approve,
    "complete": _cmd_complete,
    "promote-waiting": _cmd_promote_waiting,
    "init": _cmd_init,
    "create-file": _cmd_create_file,
    "return-to-draft": _cmd_return_to_draft,
    "enqueue": _cmd_enqueue,
    "archive": _cmd_archive,
    "log-incident": _cmd_log_incident,
    "validate-logs": _cmd_validate_logs,
    "collect-evidence": _cmd_collect_evidence,
    "timing": _cmd_timing,
    "usage": _cmd_usage,
}


def main(argv: list[str] | None = None) -> int:
    """Entry point -- parse args and dispatch to the appropriate subcommand handler."""
    ensure_utf8_output()

    parser = build_parser()
    args = parser.parse_args(argv)

    # Default to 'board' if no subcommand
    command = args.command or "board"

    # The board prints ticket names as ``<slug>.md`` (VS Code's terminal only
    # auto-links plain file paths), so users paste that straight into commands.
    # Strip a trailing ``.md`` from slug-bearing args so the copied name works.
    # No-op for ticket types and feature branches, which never end in ``.md``.
    for attr in ("slug", "type_or_slug"):
        val = getattr(args, attr, None)
        if isinstance(val, str) and val.endswith(".md"):
            setattr(args, attr, val[:-3])

    tio = TicketIO(detect_tickets_dir())

    handler = HANDLERS.get(command)
    if handler is not None:
        return handler(tio, args)

    parser.print_help()
    return 1
