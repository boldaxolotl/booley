"""BooleyFlow — deterministic end-to-end orchestration without an LLM.

Wraps subprocess execution with timeout, output capture, and sentinel-based
pass/fail detection. Used for lint, simulate, synthesize.

Commands run as local subprocesses inside the Sandbox. There is no
per-Flow execution-location selection or host command boundary.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar, cast

from booley.core.boundary import BoundaryError
from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows import execution, job_resources
from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.cli_help import SurfaceHelp
from booley.flows.cli_selection import CliInvocation
from booley.flows.display import format_flow_display_label
from booley.flows.endpoint_context import EndpointContext
from booley.flows.endpoint_state import EndpointState
from booley.flows.execution_persistence import FlowExecutionAdapter, StandaloneFlowExecution
from booley.flows.invocation import resolve_timeout_ms
from booley.flows.job_resources import JobResourceMonitor
from booley.flows.request import FlowRequest
from booley.runtime import runtime_context
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome, ExecutionResult
from booley.runtime.python_artifacts import relocate_python_artifacts
from booley.targets.catalog import PreparedTargetSelection
from booley.targets.domain import TARGET_AWARE_FLOWS, FuseSocError, TargetHandle

if TYPE_CHECKING:
    from booley.flows.terminal_progress import TerminalProgress

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


def _prepare_supervised_command(cmd: list[str], env: dict[str, str]):
    from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV
    from booley.runtime.supervised_execution import (
        current_supervised_execution,
    )

    scope = current_supervised_execution()
    if scope is not None and scope.execution_id is not None:
        env[RUNTIME_EXECUTION_ENV] = str(scope.execution_id)
    return cmd, scope


def _finish_supervised_process(scope, proc) -> None:
    if scope is not None and proc is not None:
        scope.processes.unregister(proc)


def _popen_kwargs_with_lease() -> dict[str, Any]:
    """Carry the active build lease into a supervised process group on POSIX."""
    from booley.core.file_lock import active_child_lease_fd
    from booley.runtime.platform_paths import popen_new_group_kwargs

    kwargs = popen_new_group_kwargs()
    lease_fd = active_child_lease_fd()
    if os.name != "nt" and lease_fd is not None:
        kwargs["pass_fds"] = (lease_fd,)
    return kwargs


def _start_local_process(cmd, cwd, env, scope):
    if scope is not None and scope.cancelled():
        return None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            **_popen_kwargs_with_lease(),
        )
        if scope is not None:
            scope.processes.register(proc)
        return proc
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return None
    except BaseException:
        raise


def _communicate_local_process(
    cmd: list[str],
    proc: subprocess.Popen,
    scope: Any,
    started: float,
    timeout: int,
) -> SubprocessResult:
    from booley.runtime.platform_paths import kill_process_tree

    with proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            logger.warning("Command timed out after %.1fs: %s", elapsed, " ".join(cmd))
            kill_process_tree(proc)
            stdout, stderr = _drain_after_kill(proc)
            _finish_supervised_process(scope, proc)
            return SubprocessResult(
                returncode=-1, stdout=stdout, stderr=stderr, timed_out=True, duration_s=elapsed
            )
        except BaseException:
            logger.warning("Interrupted; killing the process tree of: %s", " ".join(cmd))
            kill_process_tree(proc)
            _finish_supervised_process(scope, proc)
            raise
        elapsed = time.monotonic() - started
        _finish_supervised_process(scope, proc)
        return SubprocessResult(
            returncode=proc.returncode,
            stdout=_decode_output(stdout),
            stderr=_decode_output(stderr),
            duration_s=elapsed,
        )


def _observe_local_process(proc, observer, scope, started, timeout):
    from booley.flows.observed_process import communicate_observed

    result = SubprocessResult()

    def capture_finished() -> None:
        result.duration_s = time.monotonic() - started

    try:
        with proc:
            stdout, stderr, timed_out = communicate_observed(
                proc, observer, timeout=timeout, on_capture_complete=capture_finished
            )
        result.returncode = -1 if timed_out else proc.returncode
        result.stdout = stdout
        result.stderr = stderr
        result.timed_out = timed_out
    finally:
        _finish_supervised_process(scope, proc)
    return result


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
        resolved = self.execution_adapter.validate_and_resolve(self.args)
        if isinstance(resolved, EndpointOutcome):
            return resolved
        self.flow_acceptance = resolved
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

    def _execute_local(
        self, cmd: list[str], *, timeout: int | None = None, cwd: Path | None = None
    ) -> SubprocessResult:
        """Run locally and reap the whole process tree on timeout or interruption.

        *cwd* defaults to :meth:`_get_cwd`; a caller running another checkout
        (a Cycle Count baseline) names that checkout.
        """
        import sys as _sys

        # python3 is not on PATH on Windows; use the running interpreter.
        if cmd and cmd[0] == "python3" and _sys.platform == "win32":
            cmd = [_sys.executable, *cmd[1:]]

        timeout = self._get_timeout() if timeout is None else timeout
        cwd = self._get_cwd() if cwd is None else cwd
        env = {**os.environ, **self._extra_subprocess_env()}
        try:
            cache_root = resolve_checkout_booley_dir(cwd) / ".runtime" / "python-artifacts"
        except (OSError, RuntimeError, ValueError):
            cache_root = Path(tempfile.gettempdir()) / "booley-python-artifacts"
        env = relocate_python_artifacts(
            env,
            cache_root,
            pytest_scope=f"flow:{getattr(self, 'name', type(self).__name__)}:{cwd.resolve()}",
        )
        cmd, scope = _prepare_supervised_command(cmd, env)
        from booley.flows.terminal_progress import current_progress

        observer = current_progress()
        is_sim_module = cmd[:3] == ["python3", "-m", "booley.flows.sim"] or (
            len(cmd) > 2 and cmd[1] == "-m" and cmd[2].startswith("booley.flows.sim.")
        )
        if observer is not None and is_sim_module:
            env["PYTHONUNBUFFERED"] = "1"
        flow = getattr(self, "name", type(self).__name__)
        label = job_resources.current_label() or job_resources.ResourceLabel(
            getattr(self, "_selected_target", None) or "", flow
        )
        monitor = JobResourceMonitor(label.split)
        env.update(monitor.temp_env())
        try:
            result = self._run_measured(cmd, cwd, env, scope, observer, monitor, timeout)
        finally:
            measurement = monitor.finish()  # also when interrupted, so its thread stops
            shutil.rmtree(monitor.tmp_dir, ignore_errors=True)
        if result is None:
            return SubprocessResult(returncode=-1)
        result.peak_rss_mb = measurement.peak_rss_mb
        result.oom_kill_delta = measurement.oom_kill_delta or 0
        job_resources.record_command(flow, label, self._resource_flags(), measurement, result)
        return result

    def _run_measured(self, cmd, cwd, env, scope, observer, monitor, timeout):
        """Run one command under *monitor*; ``None`` when it could not start."""
        start = time.monotonic()
        proc = _start_local_process(cmd, cwd, env, scope)
        if proc is None:
            return None
        if observer is not None:
            # Only a spawned tool counts as started in the progress record.
            observer.begin_command()
        monitor.start(proc.pid)
        if observer is not None:
            return _observe_local_process(proc, observer, scope, start, timeout)
        return _communicate_local_process(cmd, proc, scope, start, timeout)

    def _resource_flags(self) -> tuple[str, ...]:
        """Instrumentation flags that key this Flow's resource history."""
        return ()

    def _execute(
        self, cmd: list[str], *, timeout: int | None = None, cwd: Path | None = None
    ) -> SubprocessResult:
        """Run a subprocess locally (inside the Sandbox, ADR 0028)."""
        logger.info(
            "Running: %s (timeout=%ds, cwd=%s)",
            " ".join(cmd),
            self._get_timeout() if timeout is None else timeout,
            self._get_cwd() if cwd is None else cwd,
        )
        return self._execute_local(cmd, timeout=timeout, cwd=cwd)

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
            from booley.flows.terminal_progress import current_progress

            observer = current_progress()
            if observer is not None:
                observer.log_path_known(
                    log_dir / "run.log",
                    target=target,
                    temporary=getattr(self, "_execution_role", "candidate") == "baseline",
                )
        except OSError:
            logger.debug("could not open a fresh run.log in %s", log_dir, exc_info=True)

    # ------------------------------------------------------------------
    # Sandbox boundary executor
    # ------------------------------------------------------------------

    def _execute_boundary(
        self,
        cmd: list[str],
        *,
        timeout: int | None = None,
        cwd: Path | None = None,
    ) -> SubprocessResult:
        """Run a boundary command locally inside the Sandbox, in *cwd* when given."""
        dispatched = time.time()
        # Pass only what was given: test doubles of _execute take just the command.
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        if cwd is not None:
            options["cwd"] = cwd
        result = self._execute(cmd, **options)
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


class BuiltinFlow(FlowMechanics, EndpointState, Generic[RequestT]):
    """Invocation seam shared only by Booley's four shipped Flows.

    The Flow is the single owner of its per-invocation state; every entry point
    starts an invocation through ``_start_invocation``.
    """

    request_type: ClassVar[type[FlowRequest]] = FlowRequest
    argument_adapter: ClassVar[type[BuiltinArguments]] = BuiltinArguments
    # The in-process caller's explicit CLI origin and outer Project; None reads the env marker.
    _cli_invocation: CliInvocation | None = None

    target_required: bool = False
    target_help: str = SurfaceHelp(
        "FuseSoC .core Target name(s) this run applies to. The CLI flag may be "
        "repeated or its value comma-separated; MCP accepts one comma-separated string. "
        "List available Targets with `booley targets`. When omitted, exactly one "
        "eligible Target is selected and announced; zero or multiple eligible Targets "
        "fail and list the eligible Targets.",
        cli=(
            "FuseSoC .core Target name(s) to run. Repeat the flag or separate names "
            "with commas. List available Targets with `booley targets`. When omitted, "
            "exactly one eligible Target is selected and announced; zero or multiple "
            "eligible Targets fail and list the eligible Targets."
        ),
    )

    def __init__(self) -> None:
        self._start_invocation(StandaloneFlowExecution())

    def _start_invocation(
        self, adapter: FlowExecutionAdapter, request: FlowRequest | None = None
    ) -> None:
        """Start one invocation: fresh EndpointState fields, adapter, request and Flow extras."""
        EndpointState.__init__(self)  # new invocation id, display identity, Criteria ledger, ...
        self.configure_flow_execution(adapter)
        self._args = request
        self.terminal_progress: TerminalProgress | None = None
        self.publication_resources = contextlib.ExitStack()
        # Baseline worktrees held open in `publication_resources`, by revision; each
        # revision is materialized once per invocation and shared (#1959).
        self.baseline_worktrees: dict[str, Path] = {}
        self._simulation_campaign_outcomes: tuple[Any, ...] = ()
        self._simulation_acceptance_outcomes: tuple[Any, ...] = ()
        self.reset_report_metadata()
        job_resources.clear_label()  # a stage label must not outlive its invocation

    @property
    def args(self) -> RequestT:
        return cast(RequestT, super().args)

    def reset_report_metadata(self) -> None:
        """Clear invocation-local metadata before admission gates."""
        super().reset_report_metadata()
        self.cycle_report_json: str | None = None
        self._automatic_target_notice = ""
        self._automatic_target_notice_published = False
        self._prepared_target_selection = None
        self.begin_invocation()

    def _finalize_result(self, result: EndpointOutcome) -> None:
        result.cycle_report_json = self.cycle_report_json or "{}"
        super()._finalize_result(result)

    def execute_prepared(self) -> ExecutionResult:
        """Keep invocation resources alive through final report publication."""
        with self.publication_resources:
            return super().execute_prepared()

    def prepare_target_endpoint(self) -> EndpointOutcome | None:
        """Resolve the complete Target selection before admission."""
        if not self.accepts_target or self.name not in TARGET_AWARE_FLOWS:
            return None
        if "_run" in self.__dict__:
            return None
        if self.name == "sim" and "prepare_simulation_endpoint" in self.__dict__:
            return None
        if self.name == "sim" and getattr(self.args, "resume_from", None) is not None:
            return None
        try:
            selection = PreparedTargetSelection.resolve(
                self.args.work_dir,
                self.args.target,
                for_flow=self.name,
                select_omitted=True,
            )
        except (FuseSocError, OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"{self.name} Target selection failed: {exc}",
            )
        self._prepared_target_selection = selection
        self.set_report_targets([handle.selector for handle in selection.handles])
        if selection.automatic:
            from booley.flows.endpoint_reporting import automatic_target_notice

            self.args.target = selection.target_arg
            self._automatic_target_notice = automatic_target_notice(
                self.name, selection.target_arg
            )
        return None

    def _selected_target_selection(self) -> PreparedTargetSelection:
        """Return the complete selection prepared for this invocation."""
        prepared = getattr(self, "_prepared_target_selection", None)
        if prepared is not None and prepared.matches(self.args.work_dir, self.args.target):
            return prepared
        return PreparedTargetSelection.resolve(
            self.args.work_dir,
            self.args.target,
            for_flow=self.name,
        )

    def _selected_target_handles(self) -> tuple[TargetHandle, ...]:
        """Return the immutable selection prepared for this invocation."""
        return self._selected_target_selection().handles

    @property
    def _paired_project_baseline(self) -> PairedProjectBaseline:
        return self.flow_acceptance.paired_project

    def parse_args(self, argv: list[str] | None = None) -> RequestT:
        from booley.flows.builtin_cli import parse_request

        self._start_invocation(self.execution_adapter, parse_request(self, argv))
        return self.args

    def begin_invocation(self) -> None:
        """Drop whatever this Flow object kept from an earlier invocation.

        Called wherever a new invocation starts: a new Flow, a newly
        parsed request, and the preparation of its execution. A Flow
        that keeps per-invocation state overrides this to start it afresh, so
        one Flow object never answers a request from another request's state.
        """

    def execute(
        self,
        request: RequestT,
        *,
        adapter: FlowExecutionAdapter | None = None,
    ) -> ExecutionResult:
        """Execute typed input through a fresh, transport-independent invocation."""
        from dataclasses import replace

        from booley.flows.endpoint_cli import apply_environment

        if not isinstance(request, self.request_type):
            raise TypeError(f"{self.name} requires {self.request_type.__name__}")
        self._start_invocation(adapter or StandaloneFlowExecution(), replace(request))
        apply_environment(self._args, self.endpoint_kind)
        return self.execute_prepared()

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
