"""BooleyFlow — deterministic end-to-end orchestration without an LLM.

Wraps subprocess execution with timeout, output capture, and sentinel-based
pass/fail detection. Used for lint, simulate, synthesize.

Commands run as local subprocesses inside the Session Runtime. There is no
per-Flow execution-location selection or host command boundary.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, cast

from booley.core.boundary import BoundaryError
from booley.criteria.state import DevelopmentState
from booley.evidence.acceptance import PairedProjectBaseline, ResolvedFlowAcceptance
from booley.flows import execution
from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.display import format_flow_display_label
from booley.flows.endpoint_context import EndpointContext
from booley.flows.execution_persistence import FlowExecutionAdapter
from booley.flows.invocation import resolve_timeout_ms
from booley.flows.request import FlowRequest
from booley.runtime import runtime_context
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome, ExecutionResult

logger = logging.getLogger(__name__)

# Default timeout for subprocess commands (10 minutes)
DEFAULT_TIMEOUT_S = 600


@dataclass
class SubprocessResult:
    """Captured output from a subprocess run."""

    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    # Wall-clock (unix) time the command was dispatched. File-based results
    # (ADR 0037 contract clause d) are age-gated against this so a leftover
    # artifact from an earlier run is never parsed as a fresh result.
    dispatched_unix: float = 0.0
    # Peak resident memory of the locally-executed process tree.  ``None``
    # means the platform could not measure it (not zero usage).
    peak_rss_mb: float | None = None
    # cgroup v2 OOM-kill counter delta observed across this command.  This
    # corroborates an otherwise-ambiguous SIGKILL/rc137 as an actual OOM.
    oom_kill_delta: int = 0


_CGROUP_MEMORY_EVENT_PATHS = (Path("/sys/fs/cgroup/memory.events"),)


def _cgroup_oom_kill_count() -> int | None:
    """Return this cgroup's cumulative OOM-kill count when available."""
    for path in _CGROUP_MEMORY_EVENT_PATHS:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, _, raw = line.partition(" ")
            if key != "oom_kill":
                continue
            try:
                return int(raw)
            except ValueError:
                break
    return None


def _linux_process_tree_rss_bytes(root_pid: int) -> int | None:
    """Best-effort aggregate RSS for *root_pid* and its descendants.

    The synthesis boundary's direct child is ``make`` while the memory lives
    in yosys/ABC/OpenROAD grandchildren, so sampling only the root process
    produces a dangerously reassuring number.  Linux exposes the descendant
    PIDs through ``task/<pid>/children`` without an optional psutil dependency.
    """
    if not Path("/proc").is_dir():
        return None
    pending = [root_pid]
    seen: set[int] = set()
    total_kib = 0
    measured = False
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
            pending.extend(int(tok) for tok in children.split())
        except (OSError, ValueError):
            pass
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    total_kib += int(line.split()[1])
                    measured = True
                except (IndexError, ValueError):
                    pass
                break
    return total_kib * 1024 if measured else None


class _ProcessTreeMemoryMonitor:
    """Small sampler used around long-running local mechanical commands."""

    def __init__(self, pid: int, *, interval_s: float = 0.25) -> None:
        self._pid = pid
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._peak_bytes: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._sample()
        self._thread.start()

    def finish(self) -> float | None:
        self._sample()
        self._stop.set()
        self._thread.join(timeout=2)
        return self._peak_bytes / (1024 * 1024) if self._peak_bytes is not None else None

    def _sample(self) -> None:
        rss = _linux_process_tree_rss_bytes(self._pid)
        if rss is not None and (self._peak_bytes is None or rss > self._peak_bytes):
            self._peak_bytes = rss

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            self._sample()


class FlowMechanics:
    """Base for deterministic Booley Flows that run subprocesses.

    Subclasses implement ``_build_command`` and ``_interpret_result``.
    The base handles subprocess execution, timeout, and output capture.
    """

    endpoint_kind = "flow"
    target_required = True

    def _resolve_display_label(self) -> str | None:
        """Describe the requested Target scope without resolving it."""
        return format_flow_display_label(self._requested_targets())

    def _pre_state_gate(self) -> EndpointOutcome | None:
        """Reject a changed Target/control-plane surface before any Flow runs."""
        location_error = runtime_context.container_only_error(f"booley flow {self.name}")
        if location_error is not None:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=location_error)
        try:
            execution.flow_enabled(self.name, Path(self.args.work_dir))
        except execution.FlowConfigError as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=str(exc))
        execution_state = getattr(self, "context", self)
        resolved = execution_state.execution_adapter.validate_and_resolve(self.args)
        if isinstance(resolved, EndpointOutcome):
            return resolved
        execution_state.flow_acceptance = resolved
        return None

    def _run(self) -> EndpointOutcome:
        """Execute subprocess and interpret results."""
        cmd = self._build_command()
        if not cmd:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="No command to execute",
            )
        proc_result = self._execute(cmd)
        return self._interpret_result(proc_result)

    def _build_command(self) -> list[str]:
        """Build a subprocess command for command-backed Flows.

        A direct Custom Flow may instead override :meth:`_run`.
        """
        raise NotImplementedError

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:
        """Interpret output for command-backed Flows."""
        raise NotImplementedError

    def _get_timeout(self) -> int:
        """Return timeout in seconds. Override for Flow-specific timeouts."""
        return DEFAULT_TIMEOUT_S

    def _get_cwd(self) -> Path:
        """Return working directory for subprocess."""
        return self.args.work_dir

    def _execute_local(self, cmd: list[str], *, timeout: int | None = None) -> SubprocessResult:
        """Run a command locally, killing the WHOLE tree on timeout.

        ``subprocess.run(..., timeout=...)`` only kills the direct child — for
        every mechanical Flow that child is a ``make``/``sh`` shim whose real
        work is a grandchild (``python -m booley.flows.sim.backends.verilator`` and the
        ``V<top>`` binary it supervises, yosys+abc, sv2v, ...). Those get
        reparented to init on a timeout and keep burning a core forever
        (observed: 99.9% CPU for 38+ minutes after a simulate timeout, F-13).
        So the child is spawned in its own process group
        (``popen_new_group_kwargs``) and the timeout path reaps the group via
        ``kill_process_tree`` BEFORE draining the pipes — draining first would
        block on a pipe the still-live grandchild holds open.

        The same group split makes Ctrl-C the *other* orphan path: ``setsid``
        takes the child out of the terminal's foreground process group, so a
        SIGINT from the tty reaches only Booley. ``subprocess.run`` handles that
        with an ``except: process.kill(); raise`` around ``communicate()``; this
        does the same with the tree-wide kill, catching ``BaseException`` so a
        ``KeyboardInterrupt`` cannot walk out of here leaving yosys+abc running
        under init.
        """
        import sys as _sys
        import time

        from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs

        # python3 is not on PATH on Windows; use the running interpreter.
        if cmd and cmd[0] == "python3" and _sys.platform == "win32":
            cmd = [_sys.executable, *cmd[1:]]

        timeout = self._get_timeout() if timeout is None else timeout
        cwd = self._get_cwd()
        env = os.environ.copy()
        env.update(self._extra_subprocess_env())
        start = time.monotonic()
        oom_before = _cgroup_oom_kill_count()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                **popen_new_group_kwargs(),
            )
        except FileNotFoundError:
            logger.error("Command not found: %s", cmd[0])
            return SubprocessResult(returncode=-1)
        memory_monitor = _ProcessTreeMemoryMonitor(proc.pid)
        memory_monitor.start()

        def resource_evidence() -> tuple[float | None, int]:
            peak_rss_mb = memory_monitor.finish()
            oom_after = _cgroup_oom_kill_count()
            oom_delta = (
                max(0, oom_after - oom_before)
                if oom_before is not None and oom_after is not None
                else 0
            )
            return peak_rss_mb, oom_delta

        # `with proc` closes the pipes and reaps on every exit path, exactly as
        # subprocess.run does.
        with proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                logger.warning("Command timed out after %.1fs: %s", elapsed, " ".join(cmd))
                kill_process_tree(proc)
                stdout, stderr = _drain_after_kill(proc)
                peak_rss_mb, oom_kill_delta = resource_evidence()
                return SubprocessResult(
                    returncode=-1,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                    duration_s=elapsed,
                    peak_rss_mb=peak_rss_mb,
                    oom_kill_delta=oom_kill_delta,
                )
            except BaseException:
                # KeyboardInterrupt above all: the child is in its own session,
                # so the tty's SIGINT never reached it. Without this the whole
                # toolchain (yosys+abc, the run-half + its V<top>) survives
                # Booley's exit reparented to init — the exact orphan class the
                # process-group spawn was added to prevent (F-13).
                logger.warning("Interrupted; killing the process tree of: %s", " ".join(cmd))
                kill_process_tree(proc)
                memory_monitor.finish()
                raise
            elapsed = time.monotonic() - start
            peak_rss_mb, oom_kill_delta = resource_evidence()
            return SubprocessResult(
                returncode=proc.returncode,
                stdout=_decode_output(stdout),
                stderr=_decode_output(stderr),
                duration_s=elapsed,
                peak_rss_mb=peak_rss_mb,
                oom_kill_delta=oom_kill_delta,
            )

    def _execute(self, cmd: list[str], *, timeout: int | None = None) -> SubprocessResult:
        """Run a subprocess locally (inside the Session Runtime, ADR 0028)."""
        logger.info(
            "Running: %s (timeout=%ds, cwd=%s)",
            " ".join(cmd),
            self._get_timeout() if timeout is None else timeout,
            self._get_cwd(),
        )
        return self._execute_local(cmd, timeout=timeout)

    def _extra_subprocess_env(self) -> dict[str, str]:
        """Return environment overrides for subprocess-backed EDA tools."""
        return {}

    def _open_run_log(self, target: str, log_dir: Path) -> None:
        """Open *target*'s ``run.log`` fresh, for a run about to start (F-26).

        Every mechanical Flow persists its full raw output as ``run.log`` at
        the END of a run, so for the whole duration of one the file still
        holds the PREVIOUS run's bytes — anyone tailing it while waiting on an
        async job reads that old verdict as live progress. Truncating it here,
        to a header naming this run, makes that impossible.

        Best-effort: a work dir we cannot write is the run's own problem to
        report, never a reason to fail the prepare half.
        """
        from booley.flows.run_log import begin_run_log

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            begin_run_log(log_dir, flow=self.name, target=target)
        except OSError:
            logger.debug("could not open a fresh run.log in %s", log_dir, exc_info=True)

    # ------------------------------------------------------------------
    # Session Runtime boundary executor
    # ------------------------------------------------------------------

    def _execute_boundary(
        self,
        cmd: list[str],
        *,
        timeout: int | None = None,
    ) -> SubprocessResult:
        """Run a boundary command locally inside the Session Runtime."""
        dispatched = time.time()
        result = self._execute(cmd) if timeout is None else self._execute(cmd, timeout=timeout)
        result.dispatched_unix = dispatched
        return result

    @staticmethod
    def _is_stale_artifact(path: Path, min_mtime: float | None) -> bool:
        """True if *path* predates the dispatched command and must be skipped.

        File-based boundary results are age-gated
        against ``SubprocessResult.dispatched_unix`` so a leftover artifact
        from an earlier run is never parsed as fresh. A file whose mtime cannot
        be read is treated as stale rather than risk a wrong parse.
        """
        if min_mtime is None:
            return False
        try:
            fresh = path.stat().st_mtime >= min_mtime
        except OSError:
            logger.debug("boundary: could not stat %s", path, exc_info=True)
            return True
        if not fresh:
            logger.debug("boundary: skipping stale artifact %s (predates dispatch)", path)
        return not fresh


class BooleyFlow(FlowMechanics, EndpointContext):
    """Source-compatible Custom Flow extension with its historical CLI hooks."""


RequestT = TypeVar("RequestT", bound=FlowRequest)


class BuiltinFlow(FlowMechanics, Generic[RequestT]):
    """Invocation seam shared only by Booley's four shipped Flows."""

    request_type: ClassVar[type[FlowRequest]] = FlowRequest
    argument_adapter: ClassVar[type[BuiltinArguments]] = BuiltinArguments

    name: str = ""
    description: str = ""
    code_modifying: bool = False
    modifies_category: str | None = None
    config_aware: bool = True
    accepts_target: bool = True
    announce_success_report: bool = False
    satisfies: ClassVar[list[str]] = []
    satisfies_args: ClassVar[dict[str, str]] = {}
    target_help: str = (
        "FuseSoC .core Target name(s) this run applies to, comma-separated. "
        "Run with --target <name> (list them with `booley targets`)."
    )

    def __init__(self) -> None:
        from booley.flows.execution_persistence import StandaloneFlowExecution
        from booley.flows.flow_session import FlowSession

        self.context = FlowSession(self, StandaloneFlowExecution())

    @property
    def args(self) -> RequestT:
        return cast(RequestT, self.context.args)

    @property
    def state(self) -> DevelopmentState:
        return self.context.state

    def set_criterion(
        self,
        key: str,
        met: bool,
        *,
        detail: dict[str, Any] | None = None,
        source_target: str | None = None,
    ) -> None:
        self.context.set_criterion(key, met, detail=detail, source_target=source_target)

    def reserve_invocation_dir(self) -> Path | None:
        return self.context.reserve_invocation_dir()

    def emit_progress(self, line: str) -> None:
        self.context.emit_progress(line)

    def emit_completion(self, line: str, *, repeats_at_end: bool = False) -> None:
        self.context.emit_completion(line, repeats_at_end=repeats_at_end)

    def _requested_targets(self) -> list[str]:
        return self.context._requested_targets()

    def _flow_enabled(self) -> bool:
        return self.context._flow_enabled()

    def _resolve_job_class(self) -> str | None:
        return None

    @property
    def _eda_tool(self) -> str | None:
        return self.context._eda_tool

    @_eda_tool.setter
    def _eda_tool(self, value: str | None) -> None:
        self.context._eda_tool = value

    @property
    def _args(self) -> FlowRequest | None:
        return self.context._args

    @_args.setter
    def _args(self, value: FlowRequest) -> None:
        self.context._args = value

    @property
    def _state(self) -> DevelopmentState | None:
        return self.context._state

    @_state.setter
    def _state(self, value: DevelopmentState) -> None:
        self.context._state = value

    @property
    def _flow_acceptance(self) -> ResolvedFlowAcceptance:
        return self.context.flow_acceptance

    @property
    def _paired_project_baseline(self) -> PairedProjectBaseline:
        return self.context.flow_acceptance.paired_project

    def read_state(self) -> DevelopmentState:
        return self.context.read_state()

    def parse_args(self, argv: list[str] | None = None) -> RequestT:
        from booley.flows.builtin_cli import parse_request

        self.context._args = parse_request(self, argv)
        return self.args

    def execute(
        self,
        request: RequestT,
        *,
        adapter: FlowExecutionAdapter | None = None,
    ) -> ExecutionResult:
        """Execute typed input through a fresh, transport-independent session."""
        from dataclasses import replace

        from booley.flows.endpoint_cli import apply_environment
        from booley.flows.execution_persistence import StandaloneFlowExecution
        from booley.flows.flow_session import FlowSession

        if not isinstance(request, self.request_type):
            raise TypeError(f"{self.name} requires {self.request_type.__name__}")
        self.context = FlowSession(self, adapter or StandaloneFlowExecution())
        self.context._args = replace(request)
        apply_environment(self.context._args, self.endpoint_kind)
        return self.context.execute_prepared()

    def execute_cli(
        self,
        argv: list[str] | None = None,
        *,
        adapter: FlowExecutionAdapter | None = None,
    ) -> ExecutionResult:
        from booley.flows.builtin_cli import execute_cli

        return execute_cli(self, argv, adapter=adapter)

    def main(
        self,
        argv: list[str] | None = None,
        *,
        adapter: FlowExecutionAdapter | None = None,
    ) -> int:
        return self.execute_cli(argv, adapter=adapter).exit_code

    def cli(self) -> None:
        from booley.flows.endpoint_cli import cli

        cli(self)

    default_timeout: ClassVar[int] = 600
    non_persisting_dry_run = True

    def _dry_run_result(self, plan: object) -> EndpointOutcome:
        """Render and optionally persist one normalized built-in Flow plan."""
        from booley.flows.plan import FlowPlan

        if not isinstance(plan, FlowPlan):
            raise TypeError("built-in dry-run requires a FlowPlan")
        payload = plan.as_dict()
        report_dir = self.args.report_dir
        if report_dir is not None:
            report_path = Path(report_dir) / self.name / "flow_plan.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(payload, indent=2) + "\n"
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=report_path.parent,
                    prefix=f".{report_path.name}.",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(serialized)
                temporary_path.replace(report_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        print(json.dumps(payload, indent=2))
        if plan.aggregate_errors:
            summary = "Dry-run planning failed: " + "; ".join(plan.aggregate_errors)
            exit_code = EXIT_ERROR
        else:
            summary = f"Dry run: {len(plan.work_units)} work unit(s)"
            exit_code = 0
        return EndpointOutcome(exit_code=exit_code, report_text=summary, detail=payload)

    def _timeout_ms(self) -> int:
        """Resolve the strict shared timeout precedence for this built-in Flow."""
        return resolve_timeout_ms(
            self.name,
            Path(self.args.work_dir),
            self.args.timeout_ms,
        )

    def _pre_state_gate(self) -> EndpointOutcome | None:
        """Validate the shared invocation contract before dry-run can return."""
        if (rejection := super()._pre_state_gate()) is not None:
            return rejection
        try:
            self._timeout_ms()
        except BoundaryError as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"ERROR: {exc}")
        return None

    def _get_timeout(self) -> int:
        """Return the active work-unit budget in whole seconds."""
        return max(1, self._timeout_ms() // 1000)


def _drain_after_kill(proc: subprocess.Popen) -> tuple[str, str]:
    """Read whatever the killed tree already wrote, without hanging on it.

    The group has been signalled, so the pipes should close promptly; a short
    second budget guards the pathological case (a descendant that survived
    SIGKILL, e.g. stuck in uninterruptible I/O, still holding the write end).
    Partial output beats no output — the tail is usually where the hang is.
    """
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(OSError):
            proc.kill()
        stdout, stderr = exc.stdout, exc.stderr
    except (OSError, ValueError):
        # Pipes already closed/invalidated by the kill — nothing to salvage.
        return "", ""
    return _decode_output(stdout), _decode_output(stderr)


def _decode_output(data: str | bytes | None) -> str:
    """Safely decode subprocess output that may be str, bytes, or None."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
