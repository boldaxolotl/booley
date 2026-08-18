"""Entry point: python -m booley.harness --ticket <slug-or-path>"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .blocking import EXIT_USER_QUIT, UserQuitError
from .developer import run_ticket
from .preflight import PreflightError


def main() -> int:
    _force_utf8()
    args = _parse_args()
    _stamp_developer_pid()
    # Detect console BEFORE setting up logging: the stdout handler would
    # otherwise print "Preflight OK" etc. to the terminal a beat before
    # the TUI takes over, exactly the chrome we want to suppress.
    use_console = _detect_console(args)
    _setup_logging(args.verbose, suppress_stdout=use_console)

    project_root = Path(args.project_root) if args.project_root else _find_project_root()
    if project_root is None:
        print("ERROR: Could not find project root (no .git directory found)", file=sys.stderr)
        return 1

    return _run_harness(args, project_root, use_console)


def _force_utf8() -> None:
    """Force UTF-8 for all text I/O on Windows."""
    os.environ.setdefault("PYTHONUTF8", "1")
    if sys.platform == "win32" and getattr(sys.stdout, "encoding", "").lower() not in (
        "utf-8",
        "utf8",
    ):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the harness entry point."""
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Booley -- execute tickets end-to-end",
    )
    parser.add_argument(
        "--ticket", "-t", help="Ticket slug or path to .md file. Omit for auto-select.", default=""
    )
    parser.add_argument(
        "--project-root",
        "-p",
        help="Project root directory (default: auto-detect from git)",
        default=None,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--no-transcripts",
        action="store_true",
        help="Disable per-agent transcript logging (saves disk space)",
    )
    parser.add_argument(
        "--no-console", action="store_true", help="Disable full-screen Console TUI (use log mode)"
    )
    return parser.parse_args()


def _stamp_developer_pid() -> None:
    """Publish developer PID for orphan detection by child processes."""
    try:
        from booley.config.project_config import ENV_PREFIX as _proj_env

        _orch_env = f"{_proj_env}_DEVELOPER_PID"
    except Exception:  # noqa: BLE001 — optional import may fail; fall back to the default env-var name
        _orch_env = "BOOLEY_DEVELOPER_PID"
    os.environ.setdefault(_orch_env, str(os.getpid()))


def _setup_logging(verbose: bool, *, suppress_stdout: bool = False) -> None:
    """Configure root logger with console handler.

    When ``suppress_stdout`` is set (console mode), no StreamHandler is
    attached -- the TUI will own the terminal, and INFO chatter from
    preflight/parse-validate would otherwise flash before it takes over.
    The file handler attached later by setup_file_logging still captures
    everything for post-mortem.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if not suppress_stdout:
        console = logging.StreamHandler()
        if verbose:
            console.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            console.setLevel(logging.DEBUG)
        else:
            from .logging_utils import TerseFormatter

            console.setFormatter(TerseFormatter(datefmt="%H:%M:%S"))
            console.setLevel(logging.INFO)
        root.addHandler(console)
    else:
        # Console mode hides INFO chatter, but WARN+/ERROR must still reach
        # the user (preflight failures, crashes during bring-up). Send those
        # to stderr -- the TUI redraws over stdout but errors that happen
        # before or after the TUI runs still need to surface somewhere.
        err = logging.StreamHandler(sys.stderr)
        err.setLevel(logging.WARNING)
        from .logging_utils import TerseFormatter

        err.setFormatter(TerseFormatter(datefmt="%H:%M:%S"))
        root.addHandler(err)

    for noisy in ("claude_agent_sdk._internal", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _detect_console(args: argparse.Namespace) -> bool:
    """Determine whether to use the Console TUI."""
    if args.no_console:
        return False
    if os.environ.get("BOOLEY_CONSOLE") == "0":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _run_harness(args: argparse.Namespace, project_root: Path, use_console: bool) -> int:
    """Run the developer, returning an exit code."""
    try:
        asyncio.run(
            run_ticket(
                args.ticket,
                project_root,
                save_transcripts=not args.no_transcripts,
                use_console=use_console,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except UserQuitError:
        return EXIT_USER_QUIT
    except PreflightError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        logging.getLogger(__name__).critical("Harness failed: %s", e, exc_info=True)
        return 1
    return 0


def _find_project_root() -> Path | None:
    """Walk up from cwd to find git repo root (skip .booley's own repo)."""
    p = Path.cwd().resolve()
    while p != p.parent:
        if (p / ".git").exists() and p.name != ".booley":
            return p
        p = p.parent
    return None


if __name__ == "__main__":
    sys.exit(main())
