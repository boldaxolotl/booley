"""The process boundary of the Dashboard's Run wizard: list, form, check, quote and submit a Flow.

No Textual here. The wizard calls a :class:`Launcher`; the argument check runs
the shell's own parse of a built-in Flow in a child process (``main`` below), so
nothing it prints or sets reaches the Dashboard, and relative paths resolve in
the chosen worktree.
"""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from booley.mcp.registry import McpToolInfo, discover_mcp_tools, flow_blurb
from booley.runtime import detached_job

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.harness.dashboard.run_form import RunForm

# The child parses and builds a request only; a minute covers a cold import on a slow host.
CHECK_TIMEOUT_S = 60
_ERROR_MARKER = ": error: "


class ArgumentsError(ValueError):
    """The arguments text cannot be split into shell words."""


@dataclass(frozen=True)
class FlowChoice:
    """One built-in Flow the wizard offers, with the first sentence of its description."""

    name: str
    blurb: str


def _builtin_flow_infos() -> list[McpToolInfo]:
    """Built-in Flows only: no Project tools directory is passed to discovery."""
    return sorted(
        (info for info in discover_mcp_tools() if info.kind == "flow"), key=lambda info: info.name
    )


def builtin_flows() -> tuple[FlowChoice, ...]:
    """Every built-in Flow by name, as ``booley flow`` runs them whatever ``[flows]`` enables."""
    return tuple(
        FlowChoice(info.name, flow_blurb(info.description)) for info in _builtin_flow_infos()
    )


def split_arguments(text: str) -> tuple[str, ...]:
    """Split the arguments text the way a shell would."""
    try:
        return tuple(shlex.split(text))
    except ValueError as exc:
        raise ArgumentsError(f"unbalanced quotes: {exc}") from exc


def shell_command(worktree: Path, flow: str, argv: tuple[str, ...]) -> str:
    """The shell command equal to the submitted Job: type it in *worktree*."""
    return f"cd {shlex.quote(str(worktree))} && {shlex.join(['booley', 'flow', flow, *argv])}"


def form(flow: str) -> RunForm:
    """The Run form of the built-in Flow *flow*, read from its human parser.

    Built in this process: the code is the tree the Job's worker runs, and building
    a parser prints nothing and sets nothing. Parsing a typed command stays in the
    child of :func:`check`.
    """
    from booley.flows.builtin_cli import build_cli_parser
    from booley.flows.cli_selection import CliInvocation
    from booley.harness.dashboard.run_form import describe

    return describe(build_cli_parser(_load_flow(flow), CliInvocation()))


def _error_text(stderr: str, returncode: int) -> str:
    """argparse's message without its usage block, else the last line the child printed."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if _ERROR_MARKER in line:
            return "\n".join(lines[index:])
    return lines[-1] if lines else f"argument check failed (exit {returncode})"


def check(worktree: Path, flow: str, argv: tuple[str, ...]) -> str:
    """Why the shell would reject ``booley flow <flow> *argv`` in *worktree*; empty when valid."""
    # -P: the Project's own modules never shadow what the check imports, as in the shell's script.
    command = [sys.executable, "-B", "-P", "-m", __name__, flow, "--", *argv]
    # The worker's environment, so the check sees the code and settings the Job will run with.
    environment = {**detached_job.worker_environment(), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run(
            command,
            cwd=worktree,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CHECK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"argument check timed out after {CHECK_TIMEOUT_S}s"
    except OSError as exc:
        return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return "" if result.returncode == 0 else _error_text(result.stderr, result.returncode)


class Launcher:
    """What the Run wizard needs from the outside world; one per Dashboard."""

    def __init__(self, booley_dir: Path) -> None:
        self._booley_dir = booley_dir

    def flows(self) -> tuple[FlowChoice, ...]:
        return builtin_flows()

    def form(self, flow: str) -> RunForm:
        return form(flow)

    def check(self, worktree: Path, flow: str, argv: tuple[str, ...]) -> str:
        return check(worktree, flow, argv)

    def submit(self, worktree: Path, flow: str, argv: tuple[str, ...]) -> str:
        """Start the Standalone Job; return its Jobs window row key. ``OSError`` if it cannot be recorded."""
        from booley.harness.standalone_job import standalone_jobs_dir
        from booley.harness.standalone_worker import StandaloneRequest, submit

        run_id = submit(self._booley_dir, StandaloneRequest(worktree, "flow", flow, argv))
        return json.dumps([str(standalone_jobs_dir(self._booley_dir)), run_id])


def _load_flow(name: str) -> BuiltinFlow:
    """The built-in Flow *name*, imported from its own module."""
    from booley.flows.base import BuiltinFlow

    info = next(info for info in _builtin_flow_infos() if info.name == name)
    module = importlib.import_module(
        "booley." + Path(info.path).with_suffix("").as_posix().replace("/", ".")
    )
    flow_class = next(
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, BuiltinFlow) and obj.name == name
    )
    return flow_class()


def main(argv: list[str]) -> int:
    """The child of :func:`check`: ``<flow> -- <arguments>``; 0 when the shell would accept them.

    Parses like ``booley flow`` (one leading ``--`` is dropped) but never applies the
    environment or runs the Flow. Argparse's exit code 2 stands for a rejection.
    """
    from booley.flows.builtin_cli import build_cli_parser, prepare_request
    from booley.flows.cli_selection import CliInvocation
    from booley.harness.endpoint_help import tail_project_selections

    name, _separator, *typed = argv
    if typed[:1] == ["--"]:
        typed = typed[1:]
    flow = _load_flow(name)
    parser = build_cli_parser(flow, CliInvocation())
    # The worktree chooses the Project: a selector would send the Job elsewhere.
    if tail_project_selections(parser, typed):
        parser.error("the worktree chooses the Project; remove -C/--project/--work-dir")
    try:
        prepare_request(flow, typed, CliInvocation())
    except SystemExit as exit_request:
        if exit_request.code == 0:  # argparse printed help and stopped
            parser.error("--help shows help; it is not a run")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
