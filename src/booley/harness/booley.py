#!/usr/bin/env python3
"""booley -- CLI entry point for the Booley RTL development harness.

Subcommands:
    run       Persistent ticket execution loop
    chat      Open the Project's configured agent CLI
    board     Print the ticket board
    cheat     Print quick-reference cheatsheet
    doctor    Run environment health checks
    bootstrap Prepare Project-independent host resources
    init      Initialize a Project
    auth      Mint + store the agent's long-lived auth token
    flow      Run a deterministic Booley Flow directly
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from booley.feedback import cli as feedback_cli
from booley.harness import cheatsheet, doctor_stamp, upgrade_cli, upgrade_review
from booley.harness.auth_cmd import run_auth
from booley.harness.blocking import EXIT_USER_QUIT
from booley.harness.booley_status_display import (  # noqa: F401  # re-exported for backward compatibility
    _STEP_GERUNDS,
    _active_endpoint_from_display,
    _find_latest_status_file,
    _format_age,
    _read_checkpoint_status,
    _run_with_heartbeat,
)
from booley.harness.bootstrap_cli import run_bootstrap
from booley.harness.chat_cmd import run as run_chat
from booley.harness.colors import (
    bold_accent,
    bold_amber,
    bold_green,
    bold_red,
    dim,
    green,
    yellow,
)
from booley.harness.doctor import run_doctor
from booley.harness.init_cmd import run_init
from booley.harness.orphan_handler import handle_post_run_orphans, handle_startup_orphans
from booley.harness.render_md import render
from booley.harness.setup.common import configure_progress_output
from booley.harness.subscription_limit import detect_subscription_limit
from booley.harness.terminal import status, status_indent
from booley.projects import cli as project_inventory_cli
from booley.runtime import runtime_context
from booley.runtime.paths import cheatsheet_path
from booley.runtime.project_dir import PROJECT_DIR_NAME
from booley.runtime.timefmt import format_human_datetime
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketFileSpec, TicketIO

if TYPE_CHECKING:
    # Type-only: keep the MCP tool registry (and endpoint packages it leads to) out
    # of the import path of every `booley` invocation.
    from booley.harness.image_lifecycle import LifecycleResult
    from booley.mcp.registry import McpToolInfo

# --- Constants ---
LOOP_LOG_REL = Path("logs") / "booley.log"
BOARD_MODULE = "booley.ticket_board"  # run as: python -m booley.ticket_board

# Graceful shutdown: use threading.Event for thread-safe signaling.
# Avoid a separate boolean flag -- signal handlers and the main thread
# would race on it without synchronization.
_shutdown_event = None  # threading.Event, created in main()

# How long `booley run` keeps polling a fully drained board before giving up
# and exiting. Without this an unattended runner sits in the poll loop forever
# after the last ticket goes terminal (F-50). Short enough that a finished
# overnight batch does not leave a live process behind; long enough to ride out
# a board that is momentarily empty between tickets. 0 disables (daemon mode).
DEFAULT_IDLE_TIMEOUT_S = 300


class CommandLocation(Enum):
    """Where one advertised top-level command is valid."""

    HOST = "host"
    SESSION_RUNTIME = "Session Runtime"
    EITHER = "either"
    MIXED = "mixed"

    @property
    def label(self) -> str:
        return f"[{self.value}]"


COMMAND_LOCATIONS = {
    "run": CommandLocation.SESSION_RUNTIME,
    "chat": CommandLocation.SESSION_RUNTIME,
    "board": CommandLocation.SESSION_RUNTIME,
    "cheat": CommandLocation.EITHER,
    "doctor": CommandLocation.EITHER,
    "bootstrap": CommandLocation.HOST,
    "init": CommandLocation.HOST,
    "eda": CommandLocation.HOST,
    "auth": CommandLocation.HOST,
    "session": CommandLocation.HOST,
    "projects": CommandLocation.HOST,
    "upgrade": CommandLocation.EITHER,
    "targets": CommandLocation.MIXED,
    "flow": CommandLocation.MIXED,
    "feedback": CommandLocation.MIXED,
}


class _RetiredEdaToolOptionAction(argparse.Action):
    """Reject retired scaffold flags with their canonical EDA-tool spelling."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        replacement = {
            "--sim-tool": "--sim-eda-tool",
            "--lint-tool": "--lint-eda-tool",
        }[str(option_string)]
        parser.error(f"{option_string} is retired; use {replacement}")


def _agent_auth_arg(value: str) -> str:
    """Normalize the CLI's hyphenated auth spelling to the TOML vocabulary."""
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"auto", "subscription", "api_key"}:
        raise argparse.ArgumentTypeError("choose auto, subscription, or api-key")
    return normalized


def _signal_handler(signum, frame):
    """Set shutdown event on SIGINT/SIGTERM and wake any interruptible sleep."""
    if _shutdown_event:
        _shutdown_event.set()
    print()  # newline before shutdown message
    status(f"{yellow('Shutdown requested')} (signal {signum}), finishing current ticket...")


logger = logging.getLogger("booley")


def setup_logging(project_root: Path, verbose: bool = False) -> None:
    """Configure dual logging: console (terse) + persistent file (verbose).

    The loop runner log persists across all tickets so the full session
    history is available for post-mortem even if individual tickets fail.
    """
    # Console handler -- terse by default, verbose for debugging
    console = logging.StreamHandler(sys.stdout)
    if verbose:
        console.setLevel(logging.DEBUG)
        console.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    else:
        console.setLevel(logging.INFO)
        from booley.harness.logging_utils import TerseFormatter

        console.setFormatter(TerseFormatter(datefmt="%H:%M:%S"))
    logger.addHandler(console)

    # File handler (full timestamps, always DEBUG).
    # NOTE: concurrent runners share this log file.  Python FileHandler uses
    # append mode and small writes are effectively atomic on most OSes, but
    # cross-process interleaving of long messages is possible (§7).
    log_path = tickets_dir_from_project_root(project_root) / LOOP_LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    from booley.harness.logging_utils import HumanDateFormatter

    fh.setFormatter(HumanDateFormatter("%(asctime)s %(levelname)-8s %(message)s"))
    logger.addHandler(fh)

    logger.setLevel(logging.DEBUG)


def _shutdown_requested() -> bool:
    """Thread-safe check for shutdown request via the event object."""
    return _shutdown_event is not None and _shutdown_event.is_set()


def interruptible_sleep(seconds: int) -> bool:
    """Sleep that wakes immediately on shutdown signal.

    Returns True if sleep completed normally, False if interrupted.
    """
    if _shutdown_event:
        return not _shutdown_event.wait(timeout=seconds)
    time.sleep(seconds)
    return True


# ---------------------------------------------------------------------------
# Project root detection
# ---------------------------------------------------------------------------


def find_project_root() -> Path:
    """Find the main project root by walking up from cwd looking for .git.

    Falls back to $RTL_PROJECT_ROOT env var if set.
    """
    env = os.environ.get("RTL_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    p = Path.cwd().resolve()
    while p != p.parent:
        if (p / ".git").exists() and p.name != ".booley":
            return p
        p = p.parent
    # Last resort: cwd
    return Path.cwd().resolve()


def find_venv_python(project_root: Path) -> str:
    """Return Python interpreter for harness subprocesses.

    With pip-installed booley, just use the current interpreter — no
    separate venv needed.
    """
    return sys.executable


# ---------------------------------------------------------------------------
# ticket_board helpers
# ---------------------------------------------------------------------------


def _run_board(project_root: Path, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run ticket_board as a module (python -m booley.ticket_board).

    Uses sys.executable — the package is pip-installed, no cwd tricks needed.
    """
    cmd = [sys.executable, "-m", BOARD_MODULE, *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(project_root),
        timeout=kwargs.get("timeout", 30),
        check=False,
    )
    if result.returncode != 0:
        subcmd = args[0] if args else "?"
        logger.warning(
            "ticket_board %s failed (rc=%d): %s", subcmd, result.returncode, result.stderr.strip()
        )
    return result


def get_ticket_counts(project_root: Path) -> dict[str, int]:
    """Get ticket classification counts."""
    try:
        result = _run_board(project_root, ["classify", "--format", "counts"])
        if result.returncode == 0:
            # Output is shell-eval format: executable=N\nblocked=N\n...
            counts = {}
            for line in result.stdout.strip().splitlines():
                if "=" in line:
                    key, val = line.split("=", 1)
                    counts[key.strip()] = int(val.strip())
            return counts
    except (OSError, subprocess.SubprocessError, ValueError) as _err:
        logger.debug("Ticket Board classify failed: %s", _err)
    return {"executable": 0, "active": 0, "blocked": 0, "waiting": 0, "review": 0, "orphaned": 0}


def get_active_slugs(project_root: Path) -> list[str]:
    """Return slugs of tickets currently in active/."""
    tickets_dir = tickets_dir_from_project_root(project_root)
    active_dir = tickets_dir / "board" / "active"
    if not active_dir.exists():
        return []
    return [p.stem for p in active_dir.glob("*.md")]


def get_ticket_summary(project_root: Path, slug: str) -> str:
    """Extract ticket summary from frontmatter."""
    tickets_dir = tickets_dir_from_project_root(project_root)
    for d in ("drafts", "queue", "waiting", "active", "blocked", "review", "done"):
        p = tickets_dir / "board" / d / f"{slug}.md"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("summary:"):
                    return line[len("summary:") :].strip().strip('"')
    return slug


# ---------------------------------------------------------------------------
# Heartbeat-aware execution
# ---------------------------------------------------------------------------

# How often the heartbeat prints status while harness runs (seconds)
HEARTBEAT_INTERVAL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Main loop — helpers
# ---------------------------------------------------------------------------


def _force_utf8() -> None:
    """Force UTF-8 for all text I/O — prevents CP1252 encoding errors on Windows."""
    os.environ.setdefault("PYTHONUTF8", "1")
    if sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _baked_commit() -> str | None:
    """Commit stamped into the package at wheel-build time, if any.

    A wheel install has no adjacent ``.git``, so ``_source_commit`` returns
    ``None`` and every image reports a bare ``booley 0.1.0`` — leaving no way
    to confirm which commit was actually baked, which is exactly what the
    freshness check asks you to verify (F-5). ``build.sh`` writes this module
    just before building the wheel; it is generated, not tracked.
    """
    try:
        from booley import _build_commit  # type: ignore[attr-defined]
    except ImportError:
        return None
    commit = getattr(_build_commit, "COMMIT", "")
    return commit or None


def _source_commit() -> str | None:
    """Short commit (``+dirty``) attributed to the imported Booley code.

    Live source and installed distributions are disjoint: a source failure
    never borrows a wheel stamp, and a wheel never borrows an enclosing repo.
    """
    import booley

    attribution = booley.version_attribution
    if attribution.distribution_name is not None:
        return _baked_commit()
    revision, _updated_at = attribution.source_git_metadata()
    return revision or None


def _version_string() -> str:
    """`booley <version>` plus the source commit when running from a checkout."""
    from booley import __version__

    commit = _source_commit()
    return f"booley {__version__}" + (f" ({commit})" if commit else "")


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with subcommands + legacy flags."""
    parser = argparse.ArgumentParser(
        prog="booley",
        description="Booley — RTL development harness.",
        epilog=(
            "Run bare `booley` to open this Project's configured agent CLI. "
            "Locations: [host] host terminal only; [Session Runtime] container only; "
            "[either] either location; [mixed] depends on the nested operation."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Show the Booley version (and source commit in a checkout) and exit",
    )
    # metavar hides unadvertised subcommands (e.g. the debugging-only `shell`,
    # ADR 0028 Decision 2: kept working, stripped from help/docs) from the
    # usage line; subparsers added without `help=` stay out of the listing.
    sub = parser.add_subparsers(
        dest="command",
        metavar=(
            "{run,chat,board,cheat,doctor,bootstrap,init,eda,auth,session,projects,upgrade,targets,flow,feedback}"
        ),
    )

    run_p = sub.add_parser("run", help="Run the ticket execution loop")
    run_p.add_argument(
        "--ticket",
        "-t",
        type=str,
        default="",
        help="Ticket slug to execute (default: auto-select from queue)",
    )
    run_p.add_argument(
        "--project-root", "-p", type=str, default="", help="Path to the RTL project root"
    )
    run_p.add_argument(
        "--wait", type=int, default=5, help="Seconds to wait between loop iterations (default: 5)"
    )
    run_p.add_argument(
        "-n", "--count", type=int, default=0, help="Max tickets to run (0 = unlimited)"
    )
    run_p.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_S,
        help=(
            "Exit after this many seconds with a fully drained queue — nothing "
            f"executable, active, or waiting (default: {DEFAULT_IDLE_TIMEOUT_S}; "
            "0 = poll forever)"
        ),
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup without executing tickets (implies -n 1 and --no-console)",
    )
    run_p.add_argument(
        "--check-ready",
        action="store_true",
        help="Prepare and fully validate one ticket without agents or board transitions",
    )
    run_p.add_argument(
        "--no-console",
        "-L",
        action="store_true",
        help="Disable full-screen Console TUI (use log mode)",
    )
    run_p.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging output"
    )

    _add_board_subparsers(sub)
    _add_utility_subparsers(sub)

    # Backward compat: legacy flat flags (hidden)
    parser.add_argument("--board", "-b", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cheat", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--doctor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--slug", "-s", type=str, default="", help=argparse.SUPPRESS)

    _decorate_command_help(sub)

    return parser


def _decorate_command_help(subparsers: argparse._SubParsersAction) -> None:
    """Prefix advertised command summaries from the location catalog."""
    for action in subparsers._choices_actions:
        location = COMMAND_LOCATIONS.get(action.dest)
        if location is not None and action.help != argparse.SUPPRESS:
            action.help = f"{location.label} {action.help}"


def _project_root_parent() -> argparse.ArgumentParser:
    """A reusable ``--project-root`` option for subcommands that accept one.

    ``default=SUPPRESS`` is load-bearing: the option is attached to BOTH the
    ``board`` parser and each of its subparsers so it works on either side of
    the subcommand word, and subparsers write into the same namespace. With a
    normal default the subparser would overwrite the value the parent just
    parsed, so ``booley board -p X show`` would silently lose ``X``.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--project-root",
        "-p",
        type=str,
        default=argparse.SUPPRESS,
        help="Path to the RTL project root (default: discovered from cwd)",
    )
    return parent


def _add_board_subparsers(sub) -> None:
    """Add 'board' subcommand with its subparsers."""
    # F-41: `booley run` took --project-root and `booley board` did not, so
    # driving the board for another checkout meant cd-ing or exporting env.
    root_opt = _project_root_parent()
    board_p = sub.add_parser("board", help="Ticket board operations", parents=[root_opt])
    board_sub = board_p.add_subparsers(dest="board_command")

    board_sub.add_parser(
        "show", help="Display the board (default when no subcommand)", parents=[root_opt]
    )

    create_p = board_sub.add_parser("create", help="Create a new ticket draft", parents=[root_opt])
    create_p.add_argument("slug", help="Ticket slug")

    move_p = board_sub.add_parser("move", help="Move ticket between states", parents=[root_opt])
    move_p.add_argument("slug", help="Ticket slug")
    move_p.add_argument("target", choices=["queue", "done"], help="Target state")
    move_p.add_argument("--feedback", default="", help="Feedback when moving blocked->queue")
    move_p.add_argument(
        "--no-merge", action="store_true", help="Skip merge even if on_success.merge is set"
    )
    move_p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip worktree cleanup even if on_success.cleanup is set",
    )

    reset_p = board_sub.add_parser(
        "reset", help="Full reset (wipe logs, worktree, branch)", parents=[root_opt]
    )
    reset_p.add_argument("slug", help="Ticket slug")
    reset_p.add_argument(
        "--force",
        action="store_true",
        help="Reset even while a live process owns the ticket (stop the run first; this does not stop it for you)",
    )
    reset_p.add_argument(
        "--reason",
        default="user reset ticket",
        help="Why a clean run is required (recorded in transition history)",
    )

    archive_p = board_sub.add_parser(
        "archive", help="Archive done tickets or a specific ticket", parents=[root_opt]
    )
    archive_p.add_argument(
        "slug", nargs="?", default=None, help="Specific ticket (default: all done/)"
    )
    archive_p.add_argument("--keep-logs", action="store_true", help="Keep log directories")
    archive_p.add_argument(
        "--force",
        action="store_true",
        help="Archive a ticket that is not 'done' (discards its state)",
    )

    prepare_p = board_sub.add_parser(
        "prepare-review",
        help="Generate or refresh a review/blocked ticket's HTML change explanation",
        parents=[root_opt],
    )
    prepare_p.add_argument("slug", help="Review or blocked ticket slug")
    prepare_p.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even when the saved report matches the current commit",
    )

    briefing_p = board_sub.add_parser(
        "review-briefing",
        help="Render a prepared review/blocked briefing without running an agent",
        parents=[root_opt],
    )
    briefing_p.add_argument("slug", help="Review or blocked ticket slug")
    briefing_p.add_argument(
        "--no-open-diffs",
        action="store_true",
        help="Render the briefing without opening prepared VS Code diffs",
    )

    blocked_p = board_sub.add_parser(
        "blocked-briefing",
        help="Render a prepared blocked-ticket dossier without running an agent",
        parents=[root_opt],
    )
    blocked_p.add_argument("slug", help="Blocked ticket slug")


def _add_cheat_subparser(sub) -> None:
    """Add the `cheat` subparser: one `--<section>` flag per cheatsheet section."""
    cheat_p = sub.add_parser("cheat", help="Print quick-reference cheatsheet")
    # None given prints the whole sheet. Callers that need a single table (a
    # skill authoring criteria, a user chasing one command) skip the rest.
    cheat_sections = cheat_p.add_argument_group(
        "sections", "Print only these sections (combinable; default: the whole sheet)"
    )
    for slug in cheatsheet.section_slugs():
        cheat_sections.add_argument(
            *(f"--{flag}" for flag in cheatsheet.section_flags(slug)),
            dest=slug,
            action="store_true",
            help=cheatsheet.section_help(slug),
        )
    cheat_p.add_argument(
        "--list", action="store_true", help="List section flags instead of printing the sheet"
    )


def _add_auth_subparser(sub) -> None:
    """Add the host credential-management command."""
    auth_p = sub.add_parser(
        "auth", help="Mint + store the agent's long-lived auth token (claude setup-token)"
    )
    auth_p.add_argument(
        "--app",
        choices=("claude", "codex"),
        default="claude",
        help="Which agent app to authenticate (default: claude)",
    )
    auth_p.add_argument(
        "--status", action="store_true", help="Report which credential each agent would use"
    )
    auth_p.add_argument("--clear", action="store_true", help="Remove the stored credential")
    auth_p.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the credential from stdin instead of prompting",
    )


def _add_doctor_subparser(sub) -> None:
    """Add setup and environment diagnostics."""
    doctor_p = sub.add_parser(
        "doctor",
        help="Run setup and environment health checks",
        parents=[_project_root_parent()],
    )
    doctor_p.add_argument("--verbose", "-v", action="store_true")
    doctor_p.add_argument(
        "--deep",
        action="store_true",
        help="Run real first-config sim/lint/synthesis smoke checks",
    )
    doctor_p.add_argument(
        "--skip-agent-checks",
        action="store_true",
        help=(
            "With --deep, skip agent credential inspection and the live "
            "Developer authorization probe"
        ),
    )


def _add_init_scaffold_arguments(init_p) -> None:
    """Add the optional new-IP scaffold controls to ``booley init``."""
    init_p.add_argument(
        "--scaffold",
        metavar="IP_NAME",
        help="Scaffold a new IP from scratch in a fresh repo: starter RTL, testbench, "
        ".core, and populated config, then run the regular init steps. Interactive "
        "wizard on a TTY; the flags below preset/replace its answers",
    )
    init_p.add_argument(
        "--sim-eda-tool",
        choices=("verilator", "icarus"),
        help="Scaffold simulator (default: verilator)",
    )
    init_p.add_argument("--sim-tool", action=_RetiredEdaToolOptionAction, help=argparse.SUPPRESS)
    init_p.add_argument(
        "--tb-style",
        choices=("sv", "cocotb"),
        help="Scaffold: testbench style (default sv; cocotb is sandbox-sim only)",
    )
    init_p.add_argument(
        "--lint-eda-tool",
        choices=("verilator", "verible"),
        help="Scaffold: lint EDA tool (default verilator --lint-only; verible = style lint)",
    )
    init_p.add_argument("--lint-tool", action=_RetiredEdaToolOptionAction, help=argparse.SUPPRESS)
    init_p.add_argument(
        "--asic",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Scaffold: ASIC synthesis Target + SDC (default on)",
    )
    init_p.add_argument(
        "--fpga-part",
        metavar="PART",
        help="Scaffold: enable fpga for this Vivado part (default off)",
    )


def _add_init_subparser(sub) -> None:
    """Add project initialization and Session Runtime seeding."""
    init_p = sub.add_parser("init", help="Set up a new Booley project")
    init_p.add_argument(
        "--check-only", action="store_true", help="Run health checks without modifying anything"
    )
    init_p.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Skip agent credential inspection; provider and auth policy are still recorded",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Refresh Booley-managed host and Project resources; preserve user-owned files and caches",
    )
    init_p.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging output"
    )
    init_p.add_argument(
        "--fix-line-endings",
        action="store_true",
        help="Compatibility option; clean CRLF checkouts are repaired automatically",
    )
    init_p.add_argument(
        "--seed",
        action="store_true",
        help="Seed only the Interactive Mode devcontainer for this folder/worktree "
        "(no project scaffolding); run once per user/Ticket-Mode worktree",
    )
    init_p.add_argument(
        "--provider",
        choices=("claude", "codex"),
        help="Agent provider to record for this project (default: claude)",
    )
    init_p.add_argument(
        "--auth",
        type=_agent_auth_arg,
        metavar="{auto,subscription,api-key}",
        help="Agent authentication policy to record (default: auto)",
    )
    _add_init_scaffold_arguments(init_p)


def _add_bootstrap_subparser(sub) -> None:
    """Add Project-independent Host Bootstrap."""
    parser = sub.add_parser("bootstrap", help="Prepare reusable Booley host resources")
    intent = parser.add_mutually_exclusive_group()
    intent.add_argument(
        "--check-only",
        action="store_true",
        help="Inspect Host Bootstrap readiness without modifying anything",
    )
    intent.add_argument(
        "--force",
        action="store_true",
        help="Refresh Booley-managed host resources even when they are current",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed reconciliation output"
    )


def _add_flow_subparser(sub) -> None:
    """Add direct Flow execution."""
    flow_p = sub.add_parser(
        "flow",
        help="Run a Booley Flow directly (e.g. `booley flow lint --target lint`)",
    )
    flow_p.add_argument(
        "endpoint_name",
        nargs="?",
        help="Flow to run (omit to list the available Flows)",
    )
    flow_p.add_argument(
        "endpoint_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed verbatim to the Flow",
    )


def _add_session_subparser(sub) -> None:
    """Add lifecycle controls for the Session Runtime."""
    session_p = sub.add_parser(
        "session",
        help="Start/enter/stop the Session Runtime container without VS Code",
    )
    session_sub = session_p.add_subparsers(
        dest="session_command",
        metavar="{up,enter,down,status,validate,refresh}",
    )
    up_p = session_sub.add_parser(
        "up",
        help="Create or start the Session Runtime (default subcommand)",
    )
    up_p.add_argument(
        "--rebuild",
        action="store_true",
        help="Remove an existing container first, then recreate it",
    )
    enter_p = session_sub.add_parser(
        "enter",
        help="Open a shell in the Session Runtime, or run `-- <cmd>` in it",
    )
    enter_p.add_argument(
        "exec_cmd",
        nargs=argparse.REMAINDER,
        help="Optional command, e.g. `booley session enter -- booley doctor`",
    )
    session_sub.add_parser("down", help="Stop and remove the Session Runtime")
    session_sub.add_parser("status", help="Print running/stopped/absent")
    session_sub.add_parser("validate", help="Validate the host-issued runtime specification")
    prepare_p = session_sub.add_parser("prepare")
    prepare_p.add_argument("--project-root", help=argparse.SUPPRESS)
    session_sub.add_parser(
        "refresh",
        help="Rebuild the configured image from current Booley sources and recreate the session",
    )


def _add_shell_subparser(sub) -> None:
    """Add the deliberately undocumented host debugging shell."""
    # `booley shell` is deliberately undocumented (no `help=`, hidden via the
    # subparsers metavar): a host-side debugging hatch, kept working but out
    # of the advertised Session Runtime workflow (ADR 0028).
    shell_p = sub.add_parser(
        "shell",
        description="Open an interactive shell in a fresh sandbox container "
        "(worktree at /work); or run a one-off command with `-- <cmd>`",
    )
    shell_p.add_argument(
        "--net",
        action="store_true",
        help="Enable network egress (via the Booley proxy). Off by default: "
        "the shell runs offline, like the Session Runtime.",
    )
    shell_p.add_argument(
        "shell_cmd",
        nargs=argparse.REMAINDER,
        help="Optional command to run non-interactively, e.g. "
        "`booley shell -- verilator --version`. Omit for an interactive shell.",
    )


def _add_utility_subparsers(sub) -> None:
    """Add host utilities, setup commands, and direct Flow commands."""
    sub.add_parser("chat", help="Open this Project's configured agent CLI")
    _add_cheat_subparser(sub)

    from booley.eda import cli as eda_cli

    eda_cli.add_subparser(sub)
    _add_auth_subparser(sub)
    _add_doctor_subparser(sub)
    _add_bootstrap_subparser(sub)
    _add_init_subparser(sub)
    _add_flow_subparser(sub)
    _add_targets_subparser(sub)

    # Feedback spans runtime contexts: logging is in-container, submission host-only.
    feedback_cli.add_subparser(sub)
    project_inventory_cli.add_subparser(sub)
    upgrade_cli.add_subparser(sub)
    _add_session_subparser(sub)
    _add_shell_subparser(sub)


def _add_targets_subparser(sub) -> None:
    """Add the `booley targets` subparser (ADR 0030 Target listing)."""
    # The positional does double duty: a glob (contains * ? [) filters the
    # listing, anything else selects one Target for the resolved detail view.
    targets_p = sub.add_parser(
        "targets",
        help="List the project's .core Targets (a glob filters; a name shows "
        "the resolved detail view)",
    )
    targets_p.add_argument(
        "selector",
        nargs="?",
        default=None,
        metavar="NAME|GLOB",
        help="Target to detail (bare name or vlnv#name), or a glob like "
        "'soc*' to filter the listing",
    )
    targets_p.add_argument(
        "--for-flow",
        dest="for_flow",
        metavar="FLOW",
        default=None,
        help="Only Targets this Booley Flow could drive (synth, fpga, sim, lint)",
    )
    targets_p.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output (same data as the listing/detail view)",
    )


_RUN_DEFAULTS = {
    "wait": 5,
    "count": 0,
    "dry_run": False,
    "check_ready": False,
    "verbose": False,
    "ticket": "",
    "no_console": False,
}


def _normalize_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.Namespace:
    """Map legacy flat flags → subcommands and fill missing defaults."""
    if args.command is None:
        if getattr(args, "board", False):
            args.command = "board"
        elif getattr(args, "cheat", False):
            args.command = "cheat"
        elif getattr(args, "doctor", False):
            args.command = "doctor"
        elif getattr(args, "slug", ""):
            args.command = "run"
            args.ticket = args.slug
        else:
            args.command = "chat"

    _validate_doctor_args(parser, args)

    if args.command == "run":
        for attr, default in _RUN_DEFAULTS.items():
            if not hasattr(args, attr):
                setattr(args, attr, default)
        if not args.ticket:
            args.ticket = getattr(args, "slug", "")
        args.slug = args.ticket
        # A named ticket is a one-shot request. Leaving the general queue
        # runner alive after it reaches review/done lets a later, unrelated
        # queued ticket wake the loop and re-activate the completed slug.
        if args.ticket:
            args.count = 1
        _validate_run_mode(parser, args)
        _apply_dry_run_implications(args)

    return args


def _validate_run_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject incompatible or incomplete observational run modes."""
    if args.check_ready and not args.ticket:
        parser.error("--check-ready requires --ticket")
    if args.check_ready and args.dry_run:
        parser.error("--check-ready cannot be combined with --dry-run")


def _validate_doctor_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Reject incomplete Doctor automation profiles at the CLI boundary."""
    if (
        args.command == "doctor"
        and getattr(args, "skip_agent_checks", False)
        and not getattr(args, "deep", False)
    ):
        parser.error("--skip-agent-checks requires --deep")


def _apply_dry_run_implications(args: argparse.Namespace) -> None:
    """``--dry-run`` implies one-shot and log mode (F-12).

    A dry run validates setup and executes nothing, so the two behaviors that
    make a bare `booley run` a long-lived service are wrong for it: the idle
    poll (waits for a ticket that dry-run would refuse to execute anyway, i.e.
    forever) and the full-screen TUI (nothing to watch, and it takes the
    terminal from a script that just wanted the validation output). Both made
    `booley run --dry-run` unusable headlessly without also passing
    `--no-console -n 1`. An explicit `-n` still wins.
    """
    if not getattr(args, "dry_run", False):
        return
    if not args.count:
        args.count = 1
    args.no_console = True


def _parse_cli() -> argparse.Namespace:
    """Parse CLI args with subcommands."""
    _force_utf8()
    parser = _build_parser()
    return _normalize_args(parser, parser.parse_args())


def _cmd_cheat(args: argparse.Namespace, project_root: Path) -> int:
    if getattr(args, "list", False):
        for slug in cheatsheet.section_slugs():
            aliases = cheatsheet.section_flags(slug)[1:]
            alias_names = ", ".join(f"--{name}" for name in aliases)
            alias_note = f" (alias: {alias_names})" if aliases else ""
            print(f"  --{slug:<14}{cheatsheet.section_help(slug)}{alias_note}")
        return 0

    cs = cheatsheet_path()
    if not cs.exists():
        print("cheatsheet not found", file=sys.stderr)
        return 1
    text = cs.read_text(encoding="utf-8")

    # Splice Booley Flows and Specialists into separate sections so callers can ask
    # for either catalog. Both stay live from the active project's registry.
    try:
        from booley.dev_support.flow_specialist_reference import (
            render_flow_reference,
            render_specialists_reference,
            splice_generated,
        )
        from booley.mcp.server import get_project_mcp_tools_dir

        # No Execution column: keep the terminal table within a terminal width
        # — a 4th column overflowed and truncated the Sets column (QA_REPORT
        # A3). Its reader is an agent, which doesn't pick execution backends
        # anyway; the docs blocks carry the matrix.
        project_mcp_tools_dir = get_project_mcp_tools_dir()
        body = render_flow_reference(
            project_mcp_tools_dir=project_mcp_tools_dir,
            execution_column=False,
        )
        text = splice_generated(text, body)
        text = splice_generated(
            text,
            render_specialists_reference(project_mcp_tools_dir=project_mcp_tools_dir),
            name="specialists",
        )
    except (
        Exception  # noqa: BLE001 — best-effort live splice; fall back to committed block
    ):
        # Markers absent, registry unavailable, or optional deps missing:
        # fall back to the committed block so `booley cheat` always renders.
        pass

    # Splice the criteria table live from the single source of truth
    # (criteria.toml + MCP tool registry), including any project-defined criteria.
    try:
        from booley.criteria.reference import (
            render_criteria_reference,
        )
        from booley.criteria.reference import (
            splice_generated as splice_criteria,
        )

        project_criteria = project_root / ".booley_project" / "criteria.toml"
        text = splice_criteria(
            text,
            render_criteria_reference(project_criteria_path=project_criteria),
            name="criteria",
        )
    except Exception:  # noqa: BLE001 — best-effort live criteria splice; committed block remains on failure
        pass

    # Splice the synthesis_ok / fpga_impl_ok threshold-flavour table live from the
    # param registry so documented flavours never drift from the validator.
    try:
        from booley.criteria.reference import (
            render_criteria_params_reference,
        )
        from booley.criteria.reference import (
            splice_generated as splice_criteria,
        )

        text = splice_criteria(
            text,
            render_criteria_params_reference(),
            name="criteria-params",
        )
    except Exception:  # noqa: BLE001 — best-effort live splice; committed block remains on failure
        pass

    # Narrow to the requested sections (after splicing, so a filtered view still
    # shows live registry/criteria tables). No flags = the whole sheet.
    text = cheatsheet.select(
        text, [slug for slug in cheatsheet.section_slugs() if getattr(args, slug, False)]
    )

    # Drop the HTML-comment markers so they don't reach the terminal.
    text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("<!--"))

    print(render(text), end="")
    return 0


def _cmd_board(args: argparse.Namespace, project_root: Path) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    board_cmd = getattr(args, "board_command", None)

    if board_cmd is None or board_cmd == "show":
        from booley.ticket_board.io import scan_all_tickets
        from booley.ticket_board.reporting import display_board

        tickets_dir = tickets_dir_from_project_root(project_root)
        display_board(scan_all_tickets(tickets_dir), tickets_dir=tickets_dir)
        return 0

    tio = TicketIO(tickets_dir_from_project_root(project_root), project_root=project_root)

    if board_cmd == "create":
        # The stub must spell out what queueing requires (A-4): a draft with
        # no scope/criteria and no '## Description' fails validation on the
        # first `board move <slug> queue`, and the schema was otherwise only
        # discoverable by reading booley.criteria.templates.
        stub_body = (
            "\n## Description\n"
            "\nTODO: describe the change.\n"
            "\n<!-- Before queueing (`booley board move <slug> queue`), fill in\n"
            "the frontmatter above:\n"
            "  scope:                    # paths the ticket may touch\n"
            "    - rtl/verilog/\n"
            "  criteria:\n"
            "    mandatory:\n"
            "      sim_pass: {targets: [sim]}\n"
            "      lint_clean: {targets: [lint]}\n"
            "-->\n"
        )
        result = tio.create_ticket_file(
            args.slug,
            TicketFileSpec(
                summary="TODO: one-line description",
                ticket_type="feature",
                branch="main",
                body=stub_body,
            ),
        )
        return 0 if result else 1

    if board_cmd == "move":
        from booley.ticket_board.operations import op_board_move

        ok = op_board_move(
            tio,
            args.slug,
            args.target,
            feedback=args.feedback,
            no_merge=args.no_merge,
            no_cleanup=args.no_cleanup,
        )
        return 0 if ok else 1

    if board_cmd == "reset":
        from booley.ticket_board.operations import op_reset

        ok = op_reset(
            tio,
            args.slug,
            force=getattr(args, "force", False),
            reason=getattr(args, "reason", "user reset ticket"),
        )
        return 0 if ok else 1

    special = {
        "archive": lambda: _cmd_board_archive(args, tio),
        "prepare-review": lambda: _cmd_board_prepare_review(args, project_root),
        "review-briefing": lambda: _cmd_board_review_briefing(args, project_root),
        "blocked-briefing": lambda: _cmd_board_blocked_briefing(args, project_root),
    }.get(board_cmd)
    if special is not None:
        return special()

    return 1


def _cmd_board_archive(args: argparse.Namespace, tio: TicketIO) -> int:
    from booley.ticket_board.archive import op_archive

    archived = op_archive(
        tio,
        slug=args.slug,
        keep_logs=getattr(args, "keep_logs", False),
        force=getattr(args, "force", False),
    )
    if archived:
        print(f"Archived {len(archived)} ticket(s):")
        for name in archived:
            print(f"  - {name}")
        return 0
    print("No tickets to archive.")
    # A named ticket that was not archived (missing or refused) is a
    # failure; the no-slug sweep legitimately finds nothing.
    return 1 if args.slug else 0


def _cmd_board_prepare_review(args: argparse.Namespace, project_root: Path) -> int:
    """Generate or refresh the agent-prepared HTML explanation."""
    import asyncio

    from booley.review.preparation import prepare_review_command

    outcome = asyncio.run(
        prepare_review_command(project_root, args.slug, force=getattr(args, "force", False))
    )
    if not outcome.ready:
        print(f"ERROR: {outcome.message}", file=sys.stderr)
        return 2
    if outcome.package_path is None:
        print(f"ERROR: {outcome.message}: package path unavailable", file=sys.stderr)
        return 2
    print(f"Review package ready: {outcome.package_path}")
    if outcome.html_path is not None:
        print(f"HTML explanation ready: {outcome.html_path}")
    return 0


def _cmd_board_review_briefing(args: argparse.Namespace, project_root: Path) -> int:
    """Print and open an already prepared review package without agent work."""
    from booley.review.preparation import review_briefing_command

    outcome = review_briefing_command(
        project_root,
        args.slug,
        open_diffs=not getattr(args, "no_open_diffs", False),
    )
    if outcome.status != "ready":
        print(f"ERROR: {outcome.message}", file=sys.stderr)
        return 2
    print(outcome.briefing)
    return 0


def _cmd_board_blocked_briefing(args: argparse.Namespace, project_root: Path) -> int:
    """Print an already prepared blocked-ticket dossier without agent work."""
    from booley.harness.blocked_prep import render_blocked_dossier

    outcome = render_blocked_dossier(project_root, args.slug)
    if not outcome.ready:
        print(f"ERROR: {outcome.message}", file=sys.stderr)
        return 2
    print(outcome.message)
    return 0


def _report_session_health(project_root: Path, *, startup_due_reason: str | None = None) -> None:
    """Surface the result, or the scheduled check, after Session Runtime start."""
    from booley.harness import auto_doctor

    due_reason = startup_due_reason or auto_doctor.due_reason(project_root)
    if due_reason is not None:
        print(
            f"Automatic Doctor is running in the Session Runtime ({due_reason}); "
            "persisted findings from before startup will not be reported as current.",
            file=sys.stderr,
        )
        return
    summary = auto_doctor.consume_changed_summary(project_root, channel="session-up")
    if summary:
        report = auto_doctor.load_report(project_root) or {}
        prefix = "warning: " if any(auto_doctor.issue_counts(report)) else ""
        print(f"{prefix}{summary}", file=sys.stderr)
        return


def _session_up(args: argparse.Namespace, project_root: Path) -> int:
    """Create or resume the headless Session Runtime."""
    from booley.harness import auto_doctor
    from booley.harness import session_runtime as sr

    _report_upgrade_before_session(project_root)
    vscode = sr.conflicting_vscode_session(project_root)
    startup_due_reason = auto_doctor.due_reason(project_root)
    name = sr.up(project_root, rebuild=getattr(args, "rebuild", False))
    if vscode:
        print(
            f"warning: VS Code is already running a Session Runtime for this "
            f"folder ({vscode}).\n"
            f"  Both mount the agent's home-state volume read-write, so two "
            f"agents now share one\n"
            f"  set of credentials, transcripts, and todos. Prefer the VS Code "
            f"container, or close it.",
            file=sys.stderr,
        )
    _report_session_health(project_root, startup_due_reason=startup_due_reason)
    print(f"Session Runtime ready: {name}")
    print("  enter it with: booley session enter")
    return 0


def _report_upgrade_before_session(project_root: Path) -> None:
    """Observe host version state and advise before starting a Session Runtime."""
    try:
        from booley.runtime.project_dir import resolve_checkout_project_dir

        status = upgrade_review.observe(resolve_checkout_project_dir(project_root))
    except Exception:  # noqa: BLE001 — advisory state must never block Session startup
        return
    if status.condition is upgrade_review.ReviewCondition.CURRENT:
        return
    print(f"warning: {upgrade_cli.render_status(status)}", file=sys.stderr)


def _replace_refreshed_session(
    project_root: Path, result: LifecycleResult, *, verbose: bool
) -> None:
    """Reissue host state and replace the runtime as one recoverable transaction."""
    from booley.harness import session_runtime as sr
    from booley.harness.init_cmd import (
        capture_session_spec,
        reissue_session_spec,
        restore_session_spec,
    )

    assert result.selected_id is not None
    snapshot = capture_session_spec(project_root)
    try:
        reissue_session_spec(project_root, result.selected_id, verbose=verbose)
        sr.up(
            project_root,
            rebuild=True,
            expected_image_id=result.selected_id,
            expected_payload_fingerprint=result.payload_fingerprint,
        )
    except BaseException:
        restore_session_spec(project_root, snapshot)
        raise


def _session_refresh(args: argparse.Namespace, project_root: Path) -> int:
    """Reconcile the Session Image and replace its Session Runtime."""
    configure_progress_output()
    from booley.harness import auto_doctor
    from booley.harness import session_runtime as sr
    from booley.harness.init_cmd import refresh_session_image

    vscode = sr.conflicting_vscode_session(project_root)
    if vscode:
        raise sr.SessionError(
            f"VS Code owns the active Session Runtime {vscode!r}; use "
            "'Dev Containers: Rebuild Container' so the editor can replace it safely"
        )
    verbose = getattr(args, "verbose", False)
    result = refresh_session_image(project_root, verbose=verbose)
    if result.selected_id is None:
        raise sr.SessionError("image refresh did not return an immutable Session Image ID")
    _replace_refreshed_session(project_root, result, verbose=verbose)
    _report_session_health(
        project_root,
        startup_due_reason=auto_doctor.due_reason(project_root),
    )
    print(f"Refreshed Session Runtime: {result.selected_reference} ({result.selected_id})")
    return 0


def _session_enter(args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness import session_runtime as sr

    raw = list(getattr(args, "exec_cmd", []) or [])
    if raw and raw[0] == "--":
        raw = raw[1:]
    return sr.enter(project_root, raw or None, tty=sys.stdin.isatty() and sys.stdout.isatty())


def _session_down(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness import session_runtime as sr

    if sr.down(project_root):
        print(f"removed {sr.session_container_name(project_root)}")
    else:
        print("no Session Runtime container for this folder")
    return 0


def _session_status(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness import session_runtime as sr

    print(sr.status(project_root))
    return 0


def _session_validate(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness import session_runtime as sr

    print(sr.validate(project_root))
    return 0


def _session_prepare(_args: argparse.Namespace, project_root: Path) -> int:
    from booley.harness import session_runtime as sr

    print(sr.prepare(project_root))
    return 0


def _cmd_session(args: argparse.Namespace, project_root: Path) -> int:
    """Drive the Session Runtime container headlessly (no VS Code, no UI)."""
    from booley.harness import session_runtime as sr

    handlers: dict[str, Callable[[argparse.Namespace, Path], int]] = {
        "up": _session_up,
        "enter": _session_enter,
        "down": _session_down,
        "status": _session_status,
        "validate": _session_validate,
        "prepare": _session_prepare,
        "refresh": _session_refresh,
    }
    sub = getattr(args, "session_command", None) or "up"
    handler = handlers.get(sub)
    if handler is None:
        print(f"ERROR: unknown session subcommand {sub!r}", file=sys.stderr)
        return 2
    try:
        return handler(args, project_root)
    except sr.SessionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ERROR: docker not found on PATH.", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _cmd_shell(args: argparse.Namespace, project_root: Path) -> int:
    """Open an interactive shell (or run a one-off command) in a fresh sandbox.

    A **host** command by design: it spawns a throwaway `booley-sandbox`
    container with the worktree bind-mounted at `/work` and the same security /
    resource flags as the MCP-tool-call path, so a host-side agent (or a person) can
    poke at the toolchain during setup without Reopen-in-Container. It cannot run
    inside the sandbox — the container has no Docker access (ADR 0016).
    """
    if runtime_context.inside_session_runtime():
        print(
            "ERROR: `booley shell` cannot run inside the Booley container.\n\n"
            "  The sandbox has no Docker access by design (ADR 0016), so it "
            "cannot spawn\n  another container. You are already inside a sandbox "
            "— just use this shell.\n"
            "  Run `booley shell` from a HOST terminal to get a fresh sandbox.",
            file=sys.stderr,
        )
        return 2

    from booley.config.settings import get_backend_config, load_models_config
    from booley.harness.sandbox import DockerRunner, DockerSandboxConfig

    # Load the project's booley.toml ([sandbox].image, memory, ...) — without
    # this the lazy default config silently spawns the base image even when
    # the project configured a custom one. Early setup (no booley.toml yet)
    # still gets a shell on the defaults.
    try:
        load_models_config(project_root)
    except (OSError, ValueError, RuntimeError) as exc:
        # RuntimeError covers BackendConfigError (invalid [agent] provider /
        # sandbox mode) — mirror doctor, which warns-and-defaults rather than
        # tracebacking a `booley shell` on a fixable toml typo.
        print(
            f"warning: could not load project config ({exc}); using sandbox defaults",
            file=sys.stderr,
        )

    cfg = get_backend_config()
    docker_cfg = DockerSandboxConfig(
        image=cfg.sandbox.image,
        needs_network=bool(getattr(args, "net", False)),
        # Generous ceiling: this is a human/agent poking at real EDA tools,
        # not a metered MCP tool call. A project that declared its own
        # [sandbox].memory (e.g. a 415K-LOC core whose sv2v pass brushes 4g)
        # gets that limit here too — the shell must not be tighter than the
        # Session Runtime the same Flows and Specialists normally run in.
        memory_limit=cfg.sandbox.memory or "4g",
    )
    error = docker_cfg.verify()
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    # Strip an optional leading `--` separator: `booley shell -- cmd` and
    # `booley shell cmd` both mean "run cmd".
    raw = list(getattr(args, "shell_cmd", []) or [])
    if raw and raw[0] == "--":
        raw = raw[1:]

    is_interactive = not raw
    payload = raw if raw else ["/bin/bash", "-l"]
    # Only allocate a TTY when both ends are real terminals; `-t` on a pipe
    # (e.g. an agent capturing output) fails with "the input device is not a TTY".
    tty = is_interactive and sys.stdin.isatty() and sys.stdout.isatty()

    runner = DockerRunner(docker_cfg, project_root, label="shell")
    argv = runner.ephemeral_argv(payload, tty=tty)
    try:
        return subprocess.run(argv, check=False).returncode
    except FileNotFoundError:
        print("ERROR: docker not found on PATH.", file=sys.stderr)
        return 2
    finally:
        runner.cleanup_ephemeral()


def _discover_project_mcp_tools(project_root: Path) -> list[McpToolInfo]:
    """All MCP endpoints visible to the diagnostic CLI commands.

    Discovery is deliberately unfiltered so the diagnostic commands work while
    a project is being configured.
    """
    from booley.mcp.registry import discover_mcp_tools

    return discover_mcp_tools(project_mcp_tools_dir=project_root / PROJECT_DIR_NAME / "mcp_tools")


def _load_mcp_tool_class(info: McpToolInfo) -> type | None:
    """Import the module behind a discovered endpoint.

    Built-in paths include their canonical package. Project-defined endpoints
    are absolute files below ``.booley_project/mcp_tools``.
    """
    import importlib
    import importlib.util

    from booley.mcp.base import McpTool

    path = Path(info.path)
    if path.is_absolute():
        spec = importlib.util.spec_from_file_location(f"booley_custom_tools.{path.stem}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module.__name__] = module
        spec.loader.exec_module(module)
    else:
        package = path.parent.as_posix().replace("/", ".")
        module = importlib.import_module(f"booley.{package}.{path.stem}")

    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, McpTool)
            and obj is not McpTool
            and getattr(obj, "name", "") == info.name
        ):
            return obj
    return None


_FLOW_BLURB_CHARS = 72


def _flow_blurb(description: str) -> str:
    """Return one terminal line from a Flow description.

    Endpoint descriptions can run to paragraphs; the CLI listing stays terse.
    """
    first = description.split(". ", maxsplit=1)[0].strip()
    if len(first) > _FLOW_BLURB_CHARS:
        first = first[: _FLOW_BLURB_CHARS - 1].rstrip() + "…"
    return first


def _flow_listing(flows: list[McpToolInfo]) -> str:
    """Render available Flows for the CLI."""
    if not flows:
        return "  (none discovered)"
    width = max(len(flow.name) for flow in flows)
    return "\n".join(
        f"  {flow.name:<{width}}  {_flow_blurb(flow.description)}"
        for flow in sorted(flows, key=lambda item: item.name)
    )


def _cmd_flow(args: argparse.Namespace, project_root: Path) -> int:
    """Run a deterministic Flow in-process.

    The endpoint's own entry point performs admission and returns its exit code.
    """
    mcp_tools = _discover_project_mcp_tools(project_root)
    from booley.targets.flow_names import canonical

    raw_name = getattr(args, "endpoint_name", None)
    name = canonical(raw_name) if raw_name else None
    if not name:
        print(
            f"ERROR: `booley {args.command}` needs a name.\n\nAvailable Flows:\n"
            + _flow_listing([item for item in mcp_tools if item.kind == "flow"]),
            file=sys.stderr,
        )
        return 2

    info = next(
        (t for t in mcp_tools if t.name == name and t.kind == "flow"),
        None,
    )
    if info is None:
        print(
            f"ERROR: {name!r} is not a flow.\n",
            file=sys.stderr,
        )
        return 2

    endpoint_cls = _load_mcp_tool_class(info)
    if endpoint_cls is None:
        print(
            f"ERROR: could not load endpoint {name!r} from {info.path}.",
            file=sys.stderr,
        )
        return 2

    # Strip an optional separator before forwarding endpoint arguments.
    argv = list(getattr(args, "endpoint_args", []) or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    return endpoint_cls().main(argv)


def _target_detail_payload(
    project_root: Path, selector: str, *, as_json: bool
) -> dict[str, object]:
    """Resolve Target detail in-runtime, or return actionable host metadata."""
    from booley.targets import target_surface

    inside_runtime = runtime_context.inside_session_runtime()
    payload = target_surface.detail_payload(project_root, selector, resolve=inside_runtime)
    if inside_runtime:
        return payload
    command = ["booley", "session", "enter", "--", "booley", "targets", selector]
    if as_json:
        command.append("--json")
    payload["resolved_error"] = "detailed Target resolution requires the Session Runtime"
    payload["resolution_command"] = shlex.join(command)
    return payload


def _cmd_targets(args: argparse.Namespace, project_root: Path) -> int:
    """List the project's ``.core`` Targets, or detail one: `booley targets`.

    Pure ``.core``-YAML enumeration works on either side of the Session Runtime
    boundary. Single-Target resolution runs only inside the Session Runtime;
    host detail degrades to the cheap half with an entry command.
    """
    import json as _json

    from booley.fusesoc import fusesoc_registry
    from booley.targets import target_surface

    selector: str | None = getattr(args, "selector", None)
    for_flow: str | None = getattr(args, "for_flow", None)
    if for_flow:
        from booley.targets.flow_names import canonical

        for_flow = canonical(for_flow)
    as_json: bool = getattr(args, "json", False)

    if selector and not target_surface.is_glob(selector):
        # Detail view. --for-flow is a listing filter — combining it with a single
        # Target would silently answer a different question, so refuse.
        if for_flow:
            print(
                "ERROR: --for-flow filters the listing; it cannot combine with a "
                "single-Target detail view.",
                file=sys.stderr,
            )
            return 2
        try:
            payload = _target_detail_payload(project_root, selector, as_json=as_json)
        except fusesoc_registry.FuseSocError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(_json.dumps(payload, indent=2) if as_json else target_surface.render_detail(payload))
        return 0

    try:
        surface = target_surface.collect_surface(project_root)
        nothing_authored = not surface.groups
        surface = target_surface.filter_surface(surface, for_flow=for_flow, glob=selector)
    except fusesoc_registry.FuseSocError as exc:  # e.g. cross-root VLNV collision
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # --for-flow names a non-Target-aware endpoint
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(_json.dumps(target_surface.surface_payload(surface, project_root), indent=2))
    elif nothing_authored:
        print("no .core Targets authored yet")
    else:
        print(target_surface.render_listing(surface, project_root))
    return 0


_EARLY_COMMANDS: dict[str, Callable] = {
    "chat": run_chat,
    "cheat": _cmd_cheat,
    "board": _cmd_board,
    "doctor": run_doctor,
    "init": run_init,
    "auth": run_auth,
    "shell": _cmd_shell,
    "session": _cmd_session,
    "targets": _cmd_targets,
    "flow": _cmd_flow,
    "feedback": feedback_cli.run,
    "upgrade": upgrade_cli.run,
}

from booley.eda import cli as _eda_cli

_EARLY_COMMANDS["eda"] = _eda_cli.run


def _handle_early_exits(args: argparse.Namespace, project_root: Path) -> int | None:
    """Dispatch non-run subcommands; returns exit code or None for 'run'."""
    handler = _EARLY_COMMANDS.get(args.command)
    if handler is not None:
        return handler(args, project_root)
    return None


def _reject_source_project_command(command: str | None, project_root: Path) -> int | None:
    """Reject Project commands in Booley source while allowing dogfood feedback."""
    if command in {None, "feedback"}:
        return None
    from booley.runtime.checkout_role import SourceCheckoutProjectError, require_project_checkout

    try:
        require_project_checkout(project_root)
    except SourceCheckoutProjectError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return None


def _setup_runtime(args: argparse.Namespace, project_root: Path) -> str:
    """Set up venv, logging, PID stamp, signal handlers, and clear screen."""
    venv_py = find_venv_python(project_root)

    # Setup dual logging (console + persistent file)
    setup_logging(project_root, verbose=args.verbose)

    # Stamp our PID as the developer PID so ticket.lock files written
    # by _run_board activate/claim point to this long-lived process,
    # enabling orphan detection if we get killed.
    try:
        from booley.config.project_config import ENV_PREFIX as _proj_env

        _orch_env = f"{_proj_env}_DEVELOPER_PID"
    except Exception:  # noqa: BLE001 — optional import may fail; fall back to the default env-var name
        _orch_env = "BOOLEY_DEVELOPER_PID"
    os.environ.setdefault(_orch_env, str(os.getpid()))

    # Install signal handlers for graceful shutdown (event makes sleeps interruptible)
    global _shutdown_event
    _shutdown_event = threading.Event()
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    os.system("cls" if os.name == "nt" else "clear")
    return venv_py


def _preview_ticket_run(args: argparse.Namespace, project_root: Path) -> int:
    """Describe one Ticket Mode run without changing runtime or board state."""
    counts = get_ticket_counts(project_root)
    if counts.get("executable", 0) == 0:
        _handle_idle(args, counts, _IdleState())
        return 0
    venv_py = find_venv_python(project_root)
    _log_attempt(args, 1, counts)
    _show_dry_run(venv_py)
    return 0


def _check_ticket_readiness(args: argparse.Namespace, project_root: Path) -> int:
    """Run deterministic preparation and ticket/Target validation only."""
    from booley.ticket_board.readiness import check_ticket_ready

    result = check_ticket_ready(project_root, args.ticket)
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    if result.errors:
        print(f"Ticket {args.ticket!r} is not ready:", file=sys.stderr)
        for error in result.errors:
            print(f"  - {error}", file=sys.stderr)
        return 2
    print(f"Ticket {args.ticket!r} is ready")
    return 0


def _will_use_console(args: argparse.Namespace) -> bool:
    """Mirror harness `_detect_console`: would the child run the TUI?

    When True, the parent suppresses its mascot/attempt chrome so the
    Textual app can take over the terminal immediately instead of flashing
    placeholder lines that get cleared a moment later.
    """
    if getattr(args, "no_console", False):
        return False
    if os.environ.get("BOOLEY_CONSOLE") == "0":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _print_banner(args: argparse.Namespace) -> None:
    """Print the Booley ASCII art banner with mode and timestamp.

    Skipped in console mode -- the TUI takes the screen instead, and any
    banner printed here would just flash before the TUI clears it.
    """
    if _will_use_console(args):
        return
    booley_name = f"{bold_accent('B')} {bold_amber('0')} {bold_amber('0')} {bold_accent('L E Y')}"
    mode_label = f"Run {args.count}" if args.count else "Ticket Loop"
    print()
    print("  ╭━━━━━━━━━╮")
    print(f"  ┃  {bold_amber('0')}   {bold_amber('0')}  ┃  {booley_name}")
    print(f"  ┃    ᴗ    ┃  {dim(mode_label)}")
    print(f"  ╰┯┯┯┯─┯┯┯┯╯  {dim(format_human_datetime(datetime.now(), seconds=True))}")


@dataclass
class _IdleState:
    """Bookkeeping for a run of consecutive idle polls in the ticket loop.

    ``prev_counts`` suppresses repeated identical status lines; ``drained_since``
    is the monotonic stamp of the first poll on which the board was *fully*
    drained, and drives the idle-shutdown timer.
    """

    prev_counts: tuple[int, int, int, int] | None = None
    drained_since: float | None = None

    def reset(self) -> None:
        self.prev_counts = None
        self.drained_since = None


def _handle_idle(
    args: argparse.Namespace,
    counts: dict[str, int],
    idle: _IdleState,
) -> str:
    """Handle the no-executable-tickets case; returns "break" or "continue"."""
    a, w, b, r = (
        counts.get("active", 0),
        counts.get("waiting", 0),
        counts.get("blocked", 0),
        counts.get("review", 0),
    )
    logger.debug(
        "No executable tickets (active=%d, waiting=%d, blocked=%d, review=%d)", a, w, b, r
    )

    if args.count:
        why = "--dry-run" if getattr(args, "dry_run", False) else f"-n {args.count}"
        status(f"{yellow('No executable tickets')} — exiting ({why})")
        return "break"

    if _idle_shutdown_due(args, active=a, waiting=w, idle=idle):
        return "break"

    idle_counts = (a, w, b, r)
    if idle_counts != idle.prev_counts:
        status(
            f"{yellow('No executable tickets')} {dim(f'(active={a}, waiting={w}, blocked={b}, review={r})')} — polling every {args.wait}s"
        )
        idle.prev_counts = idle_counts

    logger.debug("%d waiting ticket(s), sleeping %ds", w, args.wait)
    if not interruptible_sleep(args.wait):
        return "break"
    return "continue"


def _idle_shutdown_due(
    args: argparse.Namespace,
    *,
    active: int,
    waiting: int,
    idle: _IdleState,
) -> bool:
    """True when the board has stayed drained past ``--idle-timeout`` (F-50).

    "Drained" means nothing executable (the caller's precondition) and nothing
    that could *become* executable on its own: no active ticket to finish and
    unblock a dependant, and nothing waiting on one. Blocked and in-review
    tickets need a human, so they do not hold the runner open — that was
    exactly the case where an unattended `booley run` outlived its work.
    """
    timeout = getattr(args, "idle_timeout", 0)
    if timeout <= 0:
        return False
    if active or waiting:
        idle.drained_since = None
        return False

    now = time.monotonic()
    if idle.drained_since is None:
        idle.drained_since = now
        status(f"{yellow('Queue drained')} — exiting in {timeout}s unless new work arrives")
        return False

    elapsed = now - idle.drained_since
    if elapsed < timeout:
        return False
    logger.info("Queue drained for %.0fs (--idle-timeout %ds), exiting", elapsed, timeout)
    status(f"{yellow('Queue drained')} for {timeout}s — exiting")
    return True


def _claim_ticket_slot(
    project_root: Path,
) -> tuple[object, object] | tuple[None, None]:
    """Claim a TICKET slot for this Runner (ADR 0028 Decision 7).

    Blocks in queue order when `max_tickets` Developers are already
    running, narrating the position. Returns (store, token) to release when
    the harness child exits, or (None, None) when no store is resolvable
    (bare/test invocations). Raises QueueFullError when even the queue is
    full — the caller surfaces that and backs off.
    """
    from booley.runtime import job_slots

    root = job_slots.slots_dir()
    if root is None:
        return (None, None)
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        caps = job_slots.parse_caps(_load_rtl_config(project_root) or {})
    except Exception:  # noqa: BLE001 — defaults are safe
        caps = job_slots.SlotCaps()
    store = job_slots.SlotStore(root, caps)
    from booley.runtime.job_records import _proc_cmdline

    pid = os.getpid()

    def _narrate(position: int) -> None:
        status(f"waiting for ticket slot (position {position + 1})")

    # No timeout_s: a ticket has no natural budget (a Developer Agent can run for
    # hours legitimately), so ticket holders rely on the PID/argv guards —
    # the Runner blocks in proc.wait() for the claim's whole life, so entry
    # lifetime == supervision lifetime. should_abort makes a queued wait
    # Ctrl+C-responsive: the SIGINT handler only sets _shutdown_event, so
    # without the hook a Runner queued behind max_tickets busy Developers
    # would print "finishing…" and then wait for a slot indefinitely.
    token = store.acquire(
        job_slots.CLASS_TICKET,
        pid=pid,
        argv=_proc_cmdline(pid) or [],
        role=job_slots.ROLE_TICKET,
        on_queued=_narrate,
        should_abort=_shutdown_requested,
    )
    return (store, token)


def _run_harness(
    args: argparse.Namespace,
    project_root: Path,
    venv_py: str,
) -> tuple[int, float]:
    """Build command, optionally pre-activate slug, run harness; returns (exit_code, elapsed)."""
    from booley.runtime import job_slots

    cmd = [venv_py, "-m", "booley.harness", "--project-root", str(project_root)]

    # TICKET slot first (may wait in queue), Ticket Board activation second — a
    # ticket must not sit in active/ while its Developer Agent has no slot.
    # (acquire withdraws its own entry on any exception, so no leak even on
    # an unexpected error out of the claim.)
    slot_store: object | None = None
    slot_token: object | None = None
    try:
        slot_store, slot_token = _claim_ticket_slot(project_root)
    except job_slots.QueueFullError as exc:
        status(f"BLOCKED: {exc} — backing off")
        return 2, 0.0
    except job_slots.ClaimLostError:
        status("BLOCKED: queued ticket run was cancelled before it started")
        return 2, 0.0
    except job_slots.ClaimAbortedError:
        status("shutdown requested — abandoning the queued ticket slot")
        return 2, 0.0

    try:
        if args.slug:
            # Claim early so the ticket moves to active/ before the harness
            # subprocess starts. Without this, killing booley between launch
            # and harness's init_ticket leaves the ticket stuck in queue/.
            _run_board(project_root, ["activate", args.slug])
            cmd.extend(["--ticket", args.slug])
        if getattr(args, "no_console", False):
            cmd.append("--no-console")
        if args.verbose:
            cmd.append("--verbose")

        # Package is pip-installed — run from project root
        harness_cwd = str(project_root)
        logger.debug("Launching harness: %s", " ".join(cmd))
        t0 = time.monotonic()
        exit_code = _run_with_heartbeat(cmd, harness_cwd, project_root)
        elapsed = time.monotonic() - t0
        return exit_code, elapsed
    finally:
        if slot_store is not None and slot_token is not None:
            slot_store.release(slot_token)


def _check_fast_failure(
    args: argparse.Namespace,
    project_root: Path,
    exit_code: int,
    elapsed: float,
) -> str | None:
    """Detect race conditions and infra errors on fast harness failures; returns action or None."""
    if exit_code == 0 or elapsed >= 5.0:
        return None
    # The "race with another runner" heuristic only applies in queue-polling
    # mode. When a specific --ticket was requested, _run_harness pre-activates
    # that slug itself before launching the harness, so a follow-up
    # "0 executable" recheck reflects OUR OWN activation — not another runner.
    # Treating that as a race masks fatal infra errors (e.g. a Docker preflight
    # failure) and polls forever on a ticket nobody else will ever run. In slug
    # mode a fast non-zero failure is always an infra error -> abort.
    if not args.slug:
        # Re-check ticket counts: if another runner claimed the ticket,
        # there will be 0 executable — that's a race, not an infra error.
        recheck = get_ticket_counts(project_root)
        if recheck.get("executable", 0) == 0:
            logger.debug(
                "Harness failed in %.1fs but 0 executable -- likely race with another runner, resuming poll",
                elapsed,
            )
            status_indent(f"{yellow('[~] Ticket grabbed by another runner')} — back to polling")
            if not interruptible_sleep(args.wait):
                return "break"
            return "continue"
    logger.debug("Harness failed in %.1fs -- likely infrastructure error, aborting", elapsed)
    status_indent(
        f"{bold_red('[X] Harness failed')} in {elapsed:.1f}s -- likely infrastructure error, not retrying"
    )
    return "abort"


def _handle_limit_wait(limit_wait: int) -> str:
    """Sleep through a subscription limit cooldown; returns 'continue' or 'break'."""
    resume_time = datetime.fromtimestamp(time.time() + limit_wait).strftime("%H:%M")
    logger.debug(
        "Subscription limit detected -- sleeping %ds (until ~%s)", limit_wait, resume_time
    )
    print()
    status_indent(
        f"{yellow('Subscription limit')} -- sleeping {limit_wait}s {dim(f'(until ~{resume_time})')}"
    )
    if not interruptible_sleep(limit_wait):
        return "break"
    logger.debug("Limit wait complete, resuming")
    status_indent(f"{green('Limit wait complete, resuming...')}")
    return "continue"


def _handle_post_run(
    args: argparse.Namespace,
    project_root: Path,
    exit_code: int,
    elapsed: float,
) -> str:
    """Post-run cleanup: log, detect races/limits, orphan sweep; returns action string."""
    # --- Post-run cleanup: must complete even on repeated Ctrl+C ---
    # On Windows, CTRL_C_EVENT hits the whole console group, so a
    # second KeyboardInterrupt can fire during cleanup.  Suppress it
    # here so orphan handling always runs.
    try:
        logger.debug("Harness exited: code=%d, elapsed=%.1fs", exit_code, elapsed)
        print()
        if exit_code == EXIT_USER_QUIT:
            status_indent("User quit Console TUI — stopping")
            return "break"
        if exit_code == 0:
            status_indent(f"Harness exited {bold_green('OK')} ({elapsed:.0f}s)")
        else:
            status_indent(f"Harness exited {bold_red(f'code {exit_code}')} ({elapsed:.0f}s)")

        fast_action = _check_fast_failure(args, project_root, exit_code, elapsed)
        if fast_action == "abort":
            # Fast infra failure. In slug mode _run_harness pre-activated this
            # ticket itself, so it's now stranded in active/; the harness may
            # also have left a queue-mode ticket active. Sweep it back (fail it)
            # before aborting so it isn't orphaned. The race/shutdown paths
            # below intentionally skip this -- another runner owns the ticket.
            handle_post_run_orphans(project_root, exit_code, 0)
            return fast_action
        if fast_action is not None:
            return fast_action

        # Check for subscription limit (scans recently failed tickets)
        limit_wait = detect_subscription_limit(project_root)

        # Safety net: handle any tickets still in active/
        handle_post_run_orphans(project_root, exit_code, limit_wait)
    except KeyboardInterrupt:
        # Second Ctrl+C during cleanup -- still run orphan handling
        logger.warning("Interrupted during post-run cleanup, forcing orphan sweep")
        handle_post_run_orphans(project_root, exit_code if exit_code else 130, 0)
        return "break"

    if limit_wait > 0:
        return _handle_limit_wait(limit_wait)
    return "next"


def _log_attempt(args: argparse.Namespace, attempt: int, counts: dict[str, int]) -> None:
    """Log the current attempt with ticket counts.

    In console mode the visible status line is suppressed -- the TUI takes
    over within a moment, and the line would just flash before being
    cleared. The debug log still records the attempt for post-mortem.
    """
    ex = counts["executable"]
    a, w = counts.get("active", 0), counts.get("waiting", 0)
    b, r = counts.get("blocked", 0), counts.get("review", 0)
    logger.debug(
        "Attempt #%d -- %d executable, %d active, %d waiting, %d blocked, %d review",
        attempt,
        ex,
        a,
        w,
        b,
        r,
    )
    if _will_use_console(args):
        return
    print()
    status(
        f"{bold_accent(f'> Attempt #{attempt}')} {dim('--')} {bold_green(f'{ex} executable')}, {dim(f'{a} active, {w} waiting, {b} blocked, {r} review')}"
    )


def _sleep_until_next_ticket(args: argparse.Namespace, project_root: Path) -> bool:
    """Re-check queue and sleep if empty; returns False if shutdown interrupted."""
    counts = get_ticket_counts(project_root)
    if counts.get("executable", 0) > 0:
        logger.debug("%d executable -- starting next ticket immediately", counts["executable"])
        n_exec = counts["executable"]
        a = counts.get("active", 0)
        active_note = f", {a} active" if a else ""
        status(
            f"{green(f'{n_exec} executable')}{dim(active_note)} {dim('--')} starting next ticket immediately"
        )
        return True
    a, w = counts.get("active", 0), counts.get("waiting", 0)
    logger.debug("0 executable, %d active, %d waiting -- sleeping %ds", a, w, args.wait)
    return interruptible_sleep(args.wait)


def _run_automatic_doctor(project_root: Path) -> None:
    """Run and report the stale startup Doctor without hiding its progress."""
    # Once per sweep (not per ticket): a stale automatic Doctor runs before
    # unattended work begins. It is advisory and fail-soft; the normal
    # preflight remains the blocking gate for ticket execution.
    from booley.harness import auto_doctor

    auto_doctor.run_if_due(
        project_root,
        trigger="booley-run",
        progress=lambda message: logger.info("Automatic Doctor — %s", message),
    )
    health_summary = auto_doctor.consume_changed_summary(project_root, channel="booley-run")
    if health_summary:
        report = auto_doctor.load_report(project_root) or {}
        emit = logger.warning if any(auto_doctor.issue_counts(report)) else logger.info
        emit(health_summary)
    elif auto_doctor.load_report(project_root) is None:
        doctor_stamp.warn_if_stale(
            project_root,
            logger.warning,
            emphasize_action=bold_amber,
        )


def _ticket_loop(
    args: argparse.Namespace,
    project_root: Path,
    venv_py: str,
) -> int:
    """Run the main ticket-processing loop; returns exit code."""
    _run_automatic_doctor(project_root)
    handle_startup_orphans(project_root)
    os.chdir(str(project_root))
    attempt = 0
    tickets_run = 0
    idle = _IdleState()

    while not _shutdown_requested():
        attempt += 1
        counts = get_ticket_counts(project_root)

        if counts.get("executable", 0) == 0:
            if _handle_idle(args, counts, idle) == "break":
                break
            continue

        idle.reset()
        result = _execute_one_ticket(
            args,
            project_root,
            venv_py,
            attempt,
            counts,
        )
        if result == "break":
            break
        if result == "abort":
            return 1
        if result == "continue":
            continue

        tickets_run += 1
        if args.count and tickets_run >= args.count:
            logger.info("Completed %d/%d tickets, exiting", tickets_run, args.count)
            break

        if not _sleep_until_next_ticket(args, project_root):
            break

    if _shutdown_requested():
        handle_post_run_orphans(project_root, 130, 0)
    logger.info("=== Booley exiting ===")
    return 0


def _execute_one_ticket(
    args: argparse.Namespace,
    project_root: Path,
    venv_py: str,
    attempt: int,
    counts: dict[str, int],
) -> str:
    """Execute a single ticket iteration. Returns action: 'next', 'break', 'abort', 'continue'."""
    _log_attempt(args, attempt, counts)

    if args.dry_run:
        _show_dry_run(venv_py)
        return "break"

    exit_code, elapsed = _run_harness(args, project_root, venv_py)
    return _handle_post_run(args, project_root, exit_code, elapsed)


def _show_dry_run(venv_py: str) -> None:
    """Render the command an observational Ticket Mode preview would run."""
    logger.debug("[dry-run] Would run: %s -m booley.harness", venv_py)
    status_indent(f"{dim('[dry-run]')} Would run: {venv_py} -m booley.harness")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


# Runtime-location table (ADR 0028 Decision 2): where each command may run.
# Container-only commands ARE the Booley workflow — they act on the Session
# Runtime. Host-only commands create/serve the container boundary itself.
# `doctor` is dual (context-aware checks inside); `cheat` runs anywhere.
# `shell` needs host Docker too, but keeps its own tailored refusal in
# _cmd_shell ("you are already inside a sandbox — just use this shell").
_CONTAINER_ONLY_COMMANDS = frozenset(
    command
    for command, location in COMMAND_LOCATIONS.items()
    if location is CommandLocation.SESSION_RUNTIME
)
# `session` drives the Session Runtime from outside it: like `init` it needs host
# Docker, and the sandbox has none (ADR 0016).
_HOST_ONLY_COMMANDS = frozenset(
    command for command, location in COMMAND_LOCATIONS.items() if location is CommandLocation.HOST
)


def _effective_command(args: argparse.Namespace) -> str | None:
    """The subcommand being run, resolving hidden legacy flat flags."""
    if args.command:
        return str(args.command)
    if getattr(args, "board", False):
        return "board"
    if getattr(args, "doctor", False):
        return "doctor"
    if getattr(args, "cheat", False):
        return "cheat"
    return "chat"


def _enforce_runtime_location(command: str | None) -> None:
    """Refuse a command invoked on the wrong side of the container boundary.

    One chokepoint, right after argparse (ADR 0028): rejection happens before
    any runtime setup so the message — which names the fix — is the only
    output.
    """
    if command is None:
        return
    error: str | None = None
    if command in _CONTAINER_ONLY_COMMANDS:
        error = runtime_context.container_only_error(f"booley {command}")
    elif command in _HOST_ONLY_COMMANDS:
        error = runtime_context.host_only_error(f"booley {command}")
    if error is not None:
        print(error, file=sys.stderr)
        sys.exit(2)


def main() -> int:  # noqa: PLR0911 -- CLI coordinator; returns preserve each command's exit code
    """Entry point: parse CLI, handle early exits, set up runtime, run ticket loop."""
    args = _parse_cli()
    command = _effective_command(args)

    # Bootstrap has no Project and must not even discover one. Its host-only
    # venue guard still runs before configuration or reconciliation.
    _enforce_runtime_location(command)
    if command == "bootstrap":
        return run_bootstrap(args)
    if command == "projects":
        return project_inventory_cli.run(args)

    project_root = (
        Path(args.project_root).resolve()
        if hasattr(args, "project_root") and args.project_root
        else find_project_root()
    )
    source_rejection = _reject_source_project_command(command, project_root)
    if source_rejection is not None:
        return source_rejection

    # Runtime-location guard: one chokepoint after argparse, before anything
    # touches the filesystem or clears the screen.
    # docker-exec entry drops the spec's remoteEnv — self-heal the proxy env
    # here so agents spawned below inherit a working egress path.
    if runtime_context.ensure_proxy_env():
        logger.debug("proxy env was absent in-container — defaulted to booley-proxy")

    early = _handle_early_exits(args, project_root)
    if early is not None:
        return early

    # Only 'run' subcommand reaches here.
    if args.check_ready:
        return _check_ticket_readiness(args, project_root)
    if args.dry_run:
        _print_banner(args)
        return _preview_ticket_run(args, project_root)
    venv_py = _setup_runtime(args, project_root)
    _print_banner(args)
    return _ticket_loop(args, project_root, venv_py)


if __name__ == "__main__":
    sys.exit(main())
