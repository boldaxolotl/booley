"""Specialist — McpTool subclass for LLM-powered operations.

Handles Agent CLI and model selection from booley.toml ([models] pins), prompt
construction, and transcript capture. Reuses the existing agent.py / _claude_backend.py /
_codex_backend.py infrastructure.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import threading
from abc import abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from booley.agent_workspace.isolation import remove_shadow_package
from booley.core.boundary import parse_positive_int_arg
from booley.core.models import AgentCallParams
from booley.flows import job_resources
from booley.flows.cli_arguments import HumanParser, add_timeout_args
from booley.flows.cli_help import SurfaceHelp
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import MemoryAdmissionPlan, measure_specialist_claim
from booley.flows.prepared_invocation import OrdinaryRunInvocation, PreparedInvocation
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS, McpTool, McpToolResult
from booley.runtime import job_slots
from booley.runtime.agent_execution import configure_agent_call
from booley.runtime.exception_diagnostics import (
    exception_report_text,
    log_exception,
    write_exception_diagnostic,
)
from booley.runtime.model_fallback import ModelRun, call_with_fallback
from booley.runtime.nested_mcp_capabilities import nested_mcp_tools_for
from booley.runtime.owned_scratch import sweep_stale_scratch
from booley.runtime.process_tree import descendant_pids as _descendant_pids

from .specialist_workspace import (
    WorkspaceAccess,
    isolated_agent_workspace,
    restore_result_paths,
)

logger = logging.getLogger(__name__)


class Specialist(McpTool):
    """Base for Specialists that invoke an LLM agent.

    Subclasses implement ``_build_prompt`` and ``_interpret_output``.
    The base handles model selection, agent invocation, and transcript capture.

    Token/cost tracking: each call via _invoke_agent() accumulates into
    _total_* counters. Subclasses that override _run() and make multiple
    agent calls get automatic aggregation — use _invoke_agent() instead
    of calling _call_agent_sync() directly.
    """

    endpoint_kind = "specialist"
    report_schema = "booley.specialist-report/v1"

    # A Specialist is admitted by memory (ADR 0087): a companion claim that
    # reserves its own history's peak, or a default.
    JOB_CLASS = job_slots.CLASS_MEMORY

    def _memory_admission_plan(
        self, project: Path | None, requests: Sequence[JobRequest] | None
    ) -> MemoryAdmissionPlan:
        return measure_specialist_claim(project, self.name)

    # Default max conversation turns
    default_max_turns: int | None = None
    # Default timeout in seconds
    default_timeout: int = 1800
    # Minimum timeout — developer can't go below this (prevents premature exit-2)
    min_timeout: int = 600
    # Agent capability set (Claude Code capability names)
    agent_capabilities: list[str] | None = None
    # Provider-independent write boundary for the nested agent. Read-only
    # calls run against a disposable snapshot of the current worktree.
    workspace_access: WorkspaceAccess = "read_write"

    # Per-specialist nested-MCP allowlists live in
    # ``booley.runtime.nested_mcp_capabilities.NESTED_MCP_CAPABILITIES`` and are
    # injected by ``_invoke_agent`` — do not redeclare here per subclass.

    def __init__(self) -> None:
        super().__init__()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cached_tokens = 0
        self._total_cache_create_tokens = 0
        self._total_cost_usd = 0.0
        self._last_session_id: str | None = None
        # The model the latest agent call ran on (and any [models] list fallback).
        self._model_run: ModelRun | None = None
        # Concurrent agent calls (one per Reviewer guide kind) share the totals above.
        self._usage_lock = threading.Lock()
        self._thread_model_run = threading.local()
        # The agent CLI call windows of this run, measured for the resource history.
        self._agent_calls: list[job_resources.AgentCallUsage] = []

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        """Add agent-specific arguments."""
        parser.add_argument(
            "--instruction",
            default="",
            help="Freeform instruction for the Specialist (e.g. distilled plan)",
        )
        parser.add_argument(
            "--transcript-dir",
            type=Path,
            default=None,
            help="Directory for Specialist transcript output",
        )
        parser.add_argument(
            "--max-turns",
            type=parse_positive_int_arg,
            default=self.default_max_turns,
            help="Maximum Specialist conversation turns",
        )
        add_timeout_args(
            parser,
            human=isinstance(parser, HumanParser),
            default=self.default_timeout * 1000,
            help_text="Specialist model-call timeout in positive milliseconds",
        )
        self._add_agent_args(parser)

    def _pre_state_gate(self) -> McpToolResult | None:
        """Refuse host or disabled execution before loading state or admission."""
        from booley.mcp.endpoint_config import (
            EndpointConfigError,
            endpoint_is_enabled,
            get_endpoint_config,
        )
        from booley.runtime import runtime_context

        error = runtime_context.container_only_error(f"booley specialist {self.name}")
        if error is not None:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=error)
        try:
            config, flows = get_endpoint_config(Path(self.args.work_dir))
        except EndpointConfigError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        if not endpoint_is_enabled(self.name, "specialist", config, flows):
            return McpToolResult(
                exit_code=EXIT_ERROR, report_text=f"Specialist {self.name!r} is disabled."
            )
        return None

    def model_call_timeout_ms(self, asked_ms: int) -> int:
        """The timeout a model call really gets: *asked_ms*, raised to this Specialist's minimum."""
        return max(asked_ms, self.min_timeout * 1000)

    def timeout_seconds(self, timeout_ms: int | None = None) -> int:
        """Convert model-call milliseconds to the provider's whole seconds, never below the minimum."""
        milliseconds = self.model_call_timeout_ms(
            self.args.timeout_ms if timeout_ms is None else timeout_ms
        )
        return (milliseconds + 999) // 1000

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        """Hook for subclasses to add additional arguments."""

    @staticmethod
    def _add_steer_arg(parser: argparse.ArgumentParser, *, help_text: str) -> None:
        """Register repeatable caller context with one MCP array shape."""
        parser.add_argument(
            "--steer",
            action="append",
            default=None,
            help=SurfaceHelp(
                f"{help_text} Repeatable; over MCP pass an array of strings, not a bare string.",
                cli=f"{help_text} Repeat the flag to add more.",
            ),
        )

    @staticmethod
    def _add_dry_run_arg(parser: argparse.ArgumentParser) -> None:
        """Register the common non-executing specialist preview mode."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate inputs and preview planned work without invoking Specialists or EDA tools",
        )

    def _dry_run_result(self, *, summary: str, detail: dict[str, Any]) -> McpToolResult:
        """Build the common successful, non-acceptance dry-run result."""
        payload = {"mode": "dry_run", **detail}
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text=summary,
            display_lines=[summary],
            detail=payload,
        )

    def _resolve_model(self) -> str:
        """The model this Specialist runs on: its ``[models]`` pin, else the built-in default."""
        from booley.config.settings import get_agent_settings

        return get_agent_settings().model_for_specialist(self.name)

    def _resolve_cli(self) -> str:
        """The Agent CLI this Specialist runs on: the one its model implies."""
        from booley.config.settings import get_agent_settings

        return get_agent_settings().cli_for_specialist(self.name)

    def _resolve_effort(self) -> str | None:
        """Reasoning effort for the Agent CLI this Specialist runs on."""
        return self._reasoning_effort(self._resolve_cli())

    def _reasoning_effort(self, cli: str) -> str | None:
        """Reasoning effort for a call on *cli*; Specialists with their own rule override."""
        from booley.config.settings import get_agent_settings

        return get_agent_settings().effort_for(cli)

    def transcript_path(self) -> Path | None:
        """Resolve transcript file path."""
        transcript_dir = self.args.transcript_dir
        if transcript_dir is None:
            return None
        transcript_dir.mkdir(parents=True, exist_ok=True)
        return transcript_dir / f"{self.name}.jsonl"

    def _write_provider_diagnostic(
        self, exc: Exception, transcript_path: Path | None = None
    ) -> Path | None:
        """Persist a provider traceback without changing the primary outcome."""
        return write_exception_diagnostic(
            exc,
            endpoint_name=self.name,
            invocation_id=self._invocation_id,
            report_dir=self.args.report_dir,
            transcript_path=transcript_path,
        )

    # --- Overridable agent parameters ---

    def _system_prompt(self) -> str | None:
        """Return system prompt override. None = use default."""
        return None

    def _output_format(self) -> dict[str, Any] | None:
        """Return JSON schema for structured output. None = free-form."""
        return None

    def _needs_skills(self) -> bool:
        """Whether the agent needs project-level runtime settings loaded."""
        return False

    def _disallowed_agent_capabilities(self) -> list[str] | None:
        """Agent-capability deny patterns for category boundary enforcement. Override in subclasses."""
        return None

    def _workspace_isolation_category(self) -> str | None:
        """Category whose opposite sources must be absent from a read-only snapshot."""
        return None

    def _workspace_isolation_scope(self) -> tuple[str, ...]:
        """Exact authorized sources; subclasses may already have resolved scope."""
        return tuple(
            part.strip()
            for part in (getattr(self.args, "scope", "") or "").split(",")
            if part.strip()
        )

    @abstractmethod
    def _build_prompt(self) -> str:
        """Build the agent prompt. Implemented by subclasses."""

    @abstractmethod
    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        """Interpret agent output into a McpToolResult. Implemented by subclasses."""

    def _invoke_agent(
        self,
        params: AgentCallParams,
        on_event: Any = None,
    ) -> Any:
        """Call an isolated agent on the model this Specialist's ``[models]`` value selects.

        A string pin (or none) calls once with *params* untouched, so a provider refusal
        raises the model-refusal error and nothing else is tried. A hidden fallback list
        (ADR 0084 amendment, #2836) is walked by ``call_with_fallback``: each attempt
        replaces the model, Agent CLI and reasoning effort; a session resumes only on the
        model that made it. The model that ran is kept in ``self._model_run``.
        """
        from booley.config.settings import get_agent_settings

        settings = get_agent_settings()
        is_list = settings.model_candidates(self.name) is not None
        first_cli = params.agent_cli or self._resolve_cli()
        # A resumed session belongs to the model that made it: the one an earlier call of
        # this Specialist ran on, else the first entry the caller built *params* for.
        previous = self._model_run
        session_pair = (previous.model, previous.cli) if previous else (params.model, first_cli)

        def attempt(model: str, cli: str) -> Any:
            if not is_list:
                return self._invoke_agent_once(params, on_event)
            call_params = dataclasses.replace(
                params,
                model=model,
                agent_cli=cli,
                reasoning_effort=self._reasoning_effort(cli),
            )
            if (model, cli) != session_pair:
                # A session never transfers across models or Agent CLIs.
                call_params = dataclasses.replace(
                    call_params, session_id=None, resume_session=False
                )
                self.emit_progress(f"trying [models] entry {model} on {cli}")
            return self._invoke_agent_once(call_params, on_event)

        result, model_run = call_with_fallback(settings, self.name, attempt)
        with self._usage_lock:
            self._model_run = model_run
        # Concurrent calls share ``_model_run``; each thread also keeps the model its own call ran on.
        self._thread_model_run.value = model_run
        return result

    def last_call_model_run(self) -> ModelRun | None:
        """The model the calling thread's latest ``_invoke_agent`` ran on."""
        return getattr(self._thread_model_run, "value", None)

    def _invoke_agent_once(
        self,
        params: AgentCallParams,
        on_event: Any = None,
    ) -> Any:
        """One isolated agent call with *params*, accumulating token/cost usage.

        Missing callbacks stream specialist_thinking events to display.jsonl.
        Missing per-Specialist nested MCP-tool allowlists come from
        booley.runtime.nested_mcp_capabilities, the shared authority.
        A parent-death watchdog bounds this expensive call to the client's
        lifetime (SETUP-F-36).
        """
        if on_event is None:
            on_event = self._make_streaming_callback()
        if params.agent_cli is None:
            params = dataclasses.replace(params, agent_cli=self._resolve_cli())
        if params.nested_mcp_tools is None:
            params = dataclasses.replace(
                params,
                nested_mcp_tools=nested_mcp_tools_for(self.name),
            )
        # Reclaim scratch left by runs that were killed before they could clean up.
        sweep_stale_scratch()
        with isolated_agent_workspace(
            params,
            self.workspace_access,
            self._workspace_isolation_category(),
            scope=self._workspace_isolation_scope(),
            excluded_roots=(
                (Path(self.args.report_dir).resolve(),)
                if getattr(self.args, "report_dir", None)
                else ()
            ),
        ) as (
            call_params,
            snapshot,
        ):
            with (
                parent_death_watchdog(self.name),
                job_resources.measure_agent_call(self._agent_calls),
            ):
                result = _call_agent_sync(call_params, on_event=on_event)
            if snapshot is not None:
                result = restore_result_paths(result, snapshot)
        # A session id is a string or nothing. Backends have handed back other
        # shapes (and test doubles hand back mocks), and since F-42 gave
        # standalone runs a real persistence path, anything non-str would now
        # reach `Path.write_text` and raise instead of being quietly ignored.
        session_id = getattr(result, "session_id", None)
        with self._usage_lock:
            self._total_input_tokens += getattr(result, "input_tokens", 0)
            self._total_output_tokens += getattr(result, "output_tokens", 0)
            self._total_cached_tokens += getattr(result, "cached_tokens", 0)
            self._total_cache_create_tokens += getattr(result, "cache_create_tokens", 0)
            self._total_cost_usd += getattr(result, "cost_usd", 0.0)
            self._last_session_id = session_id if isinstance(session_id, str) else None
        return result

    # --- Session persistence helpers ---

    def _session_file_path(self, key: str) -> Path | None:
        """Return the path for a session_id persistence file, or ``None``.

        ``BOOLEY_LOGS_DIR`` is the first choice when it names an existing
        directory. Otherwise, use the Project's persistent runtime sessions
        directory, shared with mutation-lock session tracking. Return ``None``
        when no Project is discoverable.
        """
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
        if logs_dir and Path(logs_dir).is_dir():
            return Path(logs_dir) / f"{key}.session_id"
        try:
            from booley.runtime.booley_dir import runtime_dir

            return runtime_dir() / "sessions" / f"{key}.session_id"
        except (FileNotFoundError, ImportError, OSError):
            logger.debug("No project runtime dir for session persistence (key=%s)", key)
            return None

    def _persist_session_id(self, key: str) -> None:
        """Write self._last_session_id to disk for cross-invocation resume."""
        if not self._last_session_id:
            return
        path = self._session_file_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._last_session_id, encoding="utf-8")
        except OSError:
            logger.warning("Failed to persist session_id to %s", path)

    def _make_streaming_callback(self) -> Any:
        """Create an on_event callback that streams agent text to display.jsonl."""
        from booley.flows.endpoint_events import _specialist_thinking_event, _write_display_event

        def _on_event(event: dict) -> None:
            if event.get("type") == "agent_thinking":
                text = event.get("text", "")
                if text:
                    _write_display_event(_specialist_thinking_event(text))

        return _on_event

    def _validate_call_args(self) -> McpToolResult | None:
        """Developer Agent session argument validation hook.

        Default: no-op. Specialists override this to catch missing arguments
        with a useful message before prompt construction or command assembly.
        """
        return None

    def _agent_call_params(self) -> AgentCallParams:
        return AgentCallParams(
            prompt=self._build_prompt(),
            model=self._resolve_model(),
            cwd=self.args.work_dir,
            allowed_agent_capabilities=self.agent_capabilities,
            disallowed_agent_capabilities=self._disallowed_agent_capabilities(),
            system_prompt=self._system_prompt(),
            output_format=self._output_format(),
            max_turns=self.args.max_turns,
            timeout_seconds=self.timeout_seconds(),
            transcript_path=self.transcript_path(),
            label=self.name,
            needs_skills=self._needs_skills(),
            reasoning_effort=self._resolve_effort(),
        )

    def _agent_failure_result(self, exc: Exception, transcript: Path | None) -> McpToolResult:
        log_exception(logger, exc, summary=f"Specialist invocation failed for {self.name}")
        diagnostic_path = self._write_provider_diagnostic(exc, transcript)
        self.emit_progress("Specialist invocation failed")
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=exception_report_text(self.name, exc, diagnostic_path),
        )

    def prepare_endpoint_invocation(self) -> PreparedInvocation | McpToolResult:
        """The ordinary run, wrapped to record its agent calls in the resource history."""
        return OrdinaryRunInvocation(self._measured_run)

    def _measured_run(self) -> McpToolResult:
        """Run the Specialist, then append its agent calls' record; the result is unchanged.

        A run that made no agent call (a dry run, a refused argument) records
        nothing. A run that raises is recorded as an error and raises again.
        """
        self._agent_calls.clear()
        try:
            result = self._run()
        except Exception:
            self._record_agent_calls(None)
            raise
        self._record_agent_calls(result.exit_code)
        return result

    def _record_agent_calls(self, returncode: int | None) -> None:
        if self._agent_calls:
            job_resources.record_specialist(self.name, self._agent_calls, returncode)

    def _run(self) -> McpToolResult:
        """Invoke the agent and interpret results."""
        validation_err = self._validate_call_args()
        if validation_err is not None:
            return validation_err

        if self.args.timeout_ms < self.min_timeout * 1000:
            logger.warning(
                "%s: --timeout-ms %dms below minimum %ds, clamping",
                self.name,
                self.args.timeout_ms,
                self.min_timeout,
            )
            self.args.timeout_ms = self.min_timeout * 1000

        params = self._agent_call_params()
        logger.info(
            "Invoking Specialist %s (cli=%s, model=%s, effort=%s, max_turns=%s, timeout=%dms)",
            self.name,
            self._resolve_cli(),
            params.model,
            params.reasoning_effort or "default",
            self.args.max_turns,
            self.args.timeout_ms,
        )
        self.emit_progress(
            f"invoking Specialist ({self._resolve_cli()}, {params.model}, "
            f"timeout={self.args.timeout_ms}ms)"
        )

        try:
            result = self._invoke_agent(params)
        except Exception as exc:  # noqa: BLE001 — normalize the agent-provider boundary
            return self._agent_failure_result(exc, params.transcript_path)
        finally:
            remove_shadow_package(self.args.work_dir)

        self.emit_progress("Specialist completed, interpreting output")
        return self._interpret_output(result.output, result.structured)

    def _stamp_model_note(self, detail: dict[str, Any]) -> None:
        """Record the model that ran (and any ``[models]`` fallback) in *detail*.

        Without a model call in this invocation the key is dropped, so a detail copied
        from an earlier run never claims a model this one did not use.
        """
        if self._model_run is None:
            detail.pop("specialist_model", None)
        else:
            detail["specialist_model"] = self._model_run.note()

    def _finalize_result(self, result: McpToolResult) -> None:
        """Stamp accumulated agent token/cost data onto the McpToolResult."""
        if self._model_run is not None:
            self._stamp_model_note(result.detail)
            if self._model_run.fell_back:
                result.report_text = f"{result.report_text}\n{self._model_run.summary()}".strip()
        result.input_tokens = self._total_input_tokens
        result.output_tokens = self._total_output_tokens
        result.cached_tokens = self._total_cached_tokens
        result.cache_create_tokens = self._total_cache_create_tokens
        result.cost_usd = self._total_cost_usd
        super()._finalize_result(result)


# ---------------------------------------------------------------------------
# Parent-death watchdog (SETUP-F-36)
# ---------------------------------------------------------------------------
#
# Killing the client of a `docker exec`/`booley session enter` run does NOT
# kill what it started: the in-container Specialist and its agent kept running
# detached, burning tokens with nobody reading the answer. Nothing else in
# the codebase watches this process relationship; run_guard watches disk.
# The Specialist watches its own parent: when the launching process dies, it is
# reparented (PID 1 in a container), which is cheap to poll and unambiguous.

_PARENT_WATCHDOG_ENV = "BOOLEY_PARENT_WATCHDOG"
_PARENT_WATCHDOG_INTERVAL_S = 5.0

# Shared rather than re-implemented, and imported under the private name so it
# can be substituted in tests.


def _abort_orphaned_run(label: str, initial_ppid: int) -> None:
    """Tear down this run after its launching process died.

    SIGTERM allows installed handlers to restore projected state before exit.
    Fatal termination may leave a disposable workspace for system temp cleanup;
    source isolation never moves files out of the live worktree.

    The agent CLI is the expensive half and it is spawned as a plain child
    (``_codex_backend._codex_spawn`` passes no ``start_new_session``), so it
    only shares our process group when we happen to lead one. That is true
    under MCP dispatch and potentially false for a direct shell launch, where
    the fallback used to signal *only ourselves* and leave the agent
    burning tokens — the exact thing this watchdog exists to stop. So the
    descendants are followed by parent link and signalled first (deepest
    first), and the process group only after, making the teardown independent
    of who happens to lead the group.
    """
    logger.error(
        "%s: launching process %d is gone — aborting this run (Specialist detached, "
        "tokens still burning). Set %s=0 to disable this watchdog.",
        label,
        initial_ppid,
        _PARENT_WATCHDOG_ENV,
    )
    import signal

    for pid in _descendant_pids(os.getpid()):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)

    with contextlib.suppress(OSError):
        if os.getpgid(0) == os.getpid():
            # Also covers grandchildren that re-exec'd but stayed in the group.
            os.killpg(os.getpgid(0), signal.SIGTERM)
            return
    with contextlib.suppress(OSError):
        os.kill(os.getpid(), signal.SIGTERM)


@contextlib.contextmanager
def parent_death_watchdog(
    label: str,
    interval: float = _PARENT_WATCHDOG_INTERVAL_S,
) -> Any:
    """Abort the run if the process that launched it disappears.

    No-op when disabled by env, on Windows (no reparenting to observe), or
    when already parented to init (nothing left to lose).
    """
    import threading

    if (os.environ.get(_PARENT_WATCHDOG_ENV, "") or "").strip() in {"0", "false", "no"}:
        yield
        return
    if os.name != "posix":
        yield
        return
    initial_ppid = os.getppid()
    if initial_ppid <= 1:
        yield
        return

    stop = threading.Event()

    def _poll() -> None:
        while not stop.wait(interval):
            if os.getppid() != initial_ppid:
                _abort_orphaned_run(label, initial_ppid)
                return

    thread = threading.Thread(target=_poll, name=f"parent-watchdog-{label}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()


def _call_agent_sync(
    params: AgentCallParams,
    on_event: Any = None,
) -> Any:
    """Synchronous wrapper around runtime.agent.call_agent().

    Runs the async call_agent in an event loop. Returns AgentResult.
    """
    import asyncio

    from booley.runtime.agent import call_agent

    async def _invoke() -> Any:
        return await call_agent(configure_agent_call(params), on_event=on_event)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _invoke())
            return future.result(timeout=params.timeout_seconds + 30)
    return asyncio.run(_invoke())
