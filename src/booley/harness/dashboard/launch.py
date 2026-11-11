"""The process boundary of the Dashboard's Run wizard: list, check, quote and submit a Flow.

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
from typing import TYPE_CHECKING, Any, cast

from booley.mcp.registry import McpToolInfo, discover_mcp_tools, flow_blurb
from booley.runtime import detached_job

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.runtime.job_slots import AdmissionOutlook

# The child parses and builds a request only; a minute covers a cold import on a slow host.
CHECK_TIMEOUT_S = 60
_ERROR_MARKER = ": error: "
NO_FACTS = "argument check gave no run facts"
# Flows whose default per-work-unit timeout reaches this get a timeout line on the review screen.
_LONG_TIMEOUT_MS = 30 * 60_000


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


@dataclass(frozen=True)
class RunFacts:
    """What the child's parse says about the run, for the review screen.

    ``job_class`` is the admission class (``None``: no slot); ``timeout_ms`` is
    set only for Flows with a long default budget, and ``timeout_problem`` says
    why the configured value cannot be resolved.
    """

    job_class: str | None
    dry_run: bool
    timeout_ms: int | None = None
    timeout_problem: str = ""

    @property
    def takes_slot(self) -> bool:
        """True when admission would queue this run: a classed Flow that is not a dry run."""
        return self.job_class is not None and not self.dry_run


@dataclass(frozen=True)
class CheckResult:
    """The argument check's answer: the shell's rejection (``error``) or the run's facts."""

    error: str = ""
    facts: RunFacts | None = None


def _error_text(stderr: str, returncode: int) -> str:
    """argparse's message without its usage block, else the last line the child printed."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if _ERROR_MARKER in line:
            return "\n".join(lines[index:])
    return lines[-1] if lines else f"argument check failed (exit {returncode})"


def _parse_facts(stdout: str) -> RunFacts | None:
    """The facts line the child printed last, or ``None`` when there is none to read."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        payload: object = json.loads(lines[-1]) if lines else None
        if not isinstance(payload, dict):
            return None
        data = cast("dict[str, Any]", payload)
        timeout = cast("dict[str, Any]", data.get("timeout") or {})
        job_class = data["job_class"]
        timeout_ms = timeout.get("ms")
        timeout_problem = timeout.get("problem", "")
        if not (
            (job_class is None or isinstance(job_class, str))
            and isinstance(data["dry_run"], bool)
            and (timeout_ms is None or isinstance(timeout_ms, int))
            and isinstance(timeout_problem, str)
        ):
            return None
        return RunFacts(job_class, data["dry_run"], timeout_ms, timeout_problem)
    except (ValueError, KeyError, AttributeError):
        return None


def check(worktree: Path, flow: str, argv: tuple[str, ...]) -> CheckResult:
    """Why the shell would reject ``booley flow <flow> *argv`` in *worktree*, else the run's facts."""
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
        return CheckResult(f"argument check timed out after {CHECK_TIMEOUT_S}s")
    except OSError as exc:
        return CheckResult(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if result.returncode != 0:
        return CheckResult(_error_text(result.stderr, result.returncode))
    facts = _parse_facts(result.stdout)
    if facts is None:
        return CheckResult(NO_FACTS)
    return CheckResult(facts=facts)


class Launcher:
    """What the Run wizard needs from the outside world; one per Dashboard."""

    def __init__(self, booley_dir: Path) -> None:
        self._booley_dir = booley_dir

    def flows(self) -> tuple[FlowChoice, ...]:
        return builtin_flows()

    def check(self, worktree: Path, flow: str, argv: tuple[str, ...]) -> CheckResult:
        return check(worktree, flow, argv)

    def outlook(self, job_class: str) -> AdmissionOutlook:
        """What an interactive claim on *job_class* would meet; changes nothing. ``OSError`` if unreadable."""
        from booley.config.jobs import parse_caps
        from booley.runtime.job_slots import SlotStore, slots_dir
        from booley.runtime.shared_infra import load_job_budget_config

        root = slots_dir(self._booley_dir)
        assert root is not None  # a Booley Directory was given, so the store is resolvable
        caps = parse_caps(load_job_budget_config(booley_dir=self._booley_dir))
        return SlotStore(root, caps).outlook(job_class)

    def uncommitted(self, worktree: Path) -> int:
        """Uncommitted changes in *worktree*, counted as ``booley worktree list`` counts them.

        Raises ``WorktreeListError`` when Git fails and ``BooleyLayoutError`` for a retired layout.
        """
        from booley.core.booley_dir import own_booley_dir_repo_path
        from booley.harness.worktree_list import count_uncommitted

        return count_uncommitted(worktree, own_booley_dir_repo_path(worktree).as_posix())

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


def _timeout_facts(flow: BuiltinFlow, request: Any) -> dict[str, Any]:
    """The timeout the run will resolve, for Flows with a long default; empty for the others."""
    from booley.core.boundary import BoundaryError
    from booley.flows.invocation import default_timeout_ms, resolve_timeout_ms

    if default_timeout_ms(flow.name) < _LONG_TIMEOUT_MS:
        return {}
    try:
        return {"ms": resolve_timeout_ms(flow.name, request.work_dir, request.timeout_ms)}
    except BoundaryError as exc:
        return {"problem": str(exc)}


def main(argv: list[str]) -> int:
    """The child of :func:`check`: ``<flow> -- <arguments>``; 0 when the shell would accept them.

    Parses like ``booley flow`` (one leading ``--`` is dropped) but never applies the
    environment or runs the Flow. Argparse's exit code 2 stands for a rejection; on
    success the last stdout line is the run's facts as JSON (see :class:`RunFacts`).
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
        request, _quiet = prepare_request(flow, typed, CliInvocation())
    except SystemExit as exit_request:
        if exit_request.code == 0:  # argparse printed help and stopped
            parser.error("--help shows help; it is not a run")
        raise
    facts = {
        "job_class": flow._resolve_job_class(),
        "dry_run": bool(request.dry_run and flow.non_persisting_dry_run),
        "timeout": _timeout_facts(flow, request),
    }
    print(json.dumps(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
