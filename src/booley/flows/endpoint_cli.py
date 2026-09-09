"""CLI parsing and process entry points for endpoint adapters."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from booley.runtime.endpoint_execution import (
    EndpointOutcome,
    ExecutionResult,
    execute_endpoint,
)
from booley.ticket_board.paths import ticket_runtime_dir

logger = logging.getLogger(__name__)

# A leading `C:`-style component. On Windows pathlib parses this as the drive of
# an ABSOLUTE path; on POSIX it is just an ordinary relative directory name.
_DRIVE_COMPONENT_RE = re.compile(r"^[A-Za-z]:$")


def _report_dir_arg(value: str) -> Path:
    """``--report-dir`` argparse type — rejects an MSYS-mangled host path.

    Git Bash / MSYS2 rewrites POSIX-looking argv into Windows paths when it
    spawns a native exe, so

        booley session enter -- python3 -m booley.flows.lint --report-dir /tmp/rep

    reaches the endpoint inside the Linux container as
    ``C:/Users/<you>/AppData/Local/Temp/rep`` — which POSIX pathlib reads as a
    *relative* path whose first component is literally ``C:``. Every
    ``report_dir.mkdir(parents=True)`` downstream then happily created
    ``/work/C:/Users/...`` inside the workspace, i.e. a junk ``C:`` directory in
    the user's repo (the bind mount renders the illegal colon as U+F03A), while
    the "See <path>" summary echoed the Windows path back and looked plausible.

    A relative path whose first component is a drive letter is never something a
    caller means, so refuse it and name the cause. The check keys off pathlib's
    own flavour rather than the host OS: on Windows ``C:/x`` is absolute and
    passes untouched (a real, legitimate host report dir); only the POSIX
    reading — the broken one — is caught.
    """
    path = Path(value)
    if not path.is_absolute() and path.parts and _DRIVE_COMPONENT_RE.match(path.parts[0]):
        raise argparse.ArgumentTypeError(
            f"--report-dir {value!r} is a Windows host path, but this endpoint "
            f"writes inside the container, where {path.parts[0]!r} is just a "
            "directory name — the reports would land in a junk "
            f"'{path.parts[0]}' folder in your workspace.\n"
            "Git Bash/MSYS rewrites '/tmp/...' into a Windows path when it "
            "launches booley. Re-run with MSYS_NO_PATHCONV=1 (or MSYS2_ARG_CONV_EXCL='*'), "
            "double the leading slash ('//tmp/rep'), or pass a path under the "
            "workspace instead."
        )
    return path


def add_common_args(
    parser: argparse.ArgumentParser,
    *,
    target_required: bool,
    accepts_target: bool,
    target_help: str,
) -> None:
    """Add args shared by all endpoints.

    Ticket context (slug, state-file, report-dir) comes from env vars
    set by the developer.  Endpoints work without them (human mode).
    """
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path.cwd(),
        help="Working directory (worktree root)",
    )
    parser.add_argument(
        "--report-dir",
        type=_report_dir_arg,
        default=None,
        help="Directory for endpoint report output",
    )
    # Kept default="" (not argparse required) so each endpoint's validation can
    # produce specific guidance and discovery can represent no selection.
    if accepts_target:
        parser.add_argument(
            "--target",
            required=target_required,
            help=target_help,
        )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help=(
            "Run without satisfying Ticket criteria. In Ticket Mode this "
            "is required for a Flow/Target combination outside the Acceptance Basis."
        ),
    )


def parse_args(endpoint, argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments, filling ticket context from env vars."""
    endpoint._raw_argv = argv if argv is not None else sys.argv[1:]
    endpoint._args = endpoint._parser.parse_args(argv)
    if hasattr(endpoint._args, "steer") and isinstance(endpoint._args.steer, list):
        if len(endpoint._args.steer) == 0:
            endpoint._args.steer = ""
        elif len(endpoint._args.steer) == 1:
            endpoint._args.steer = endpoint._args.steer[0]
    apply_environment(endpoint._args, endpoint.endpoint_kind)
    return endpoint._args


def execute_cli(endpoint, argv: list[str] | None = None) -> ExecutionResult:
    """Adapt CLI arguments and run the transport-independent coordinator."""
    prepared = endpoint._prepare_cli_execution(argv)
    if isinstance(prepared, EndpointOutcome):
        return ExecutionResult(exit_code=prepared.exit_code, outcome=prepared)
    return execute_endpoint(endpoint, prepared)


def main(endpoint, argv: list[str] | None = None) -> int:
    """Run the CLI adapter and return its process exit code."""
    return endpoint.execute_cli(argv).exit_code


def cli(endpoint) -> None:
    """Entry point for ``if __name__ == '__main__'`` usage."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    sys.exit(endpoint.main())


def apply_environment(args, endpoint_kind: str) -> None:
    """Resolve Session/Ticket context equally for typed and CLI requests."""
    from booley.review.execution_context import validate_recording

    validate_recording(getattr(args, "work_dir", None))
    # Ticket context from env vars (set by developer or explicit review-exec)
    args.slug = os.environ.get("BOOLEY_SLUG", "")
    state_env = os.environ.get("BOOLEY_STATE_FILE", "")
    args.state_file = Path(state_env) if state_env else None
    # report-dir: CLI flag wins, then env var, then None
    if args.report_dir is None:
        logs_env = os.environ.get("BOOLEY_LOGS_DIR", "")
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
        report_leaf = "flow-reports" if endpoint_kind == "flow" else "mcp-tool-reports"
        if runtime_env:
            args.report_dir = Path(runtime_env) / report_leaf
        elif logs_env:
            args.report_dir = ticket_runtime_dir(logs_env) / report_leaf
        else:
            args.report_dir = None
