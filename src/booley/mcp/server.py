"""Booley MCP server — exposes Flows and Specialists as structured MCP tools.

Two transports (``--transport``, or ``BOOLEY_MCP_TRANSPORT``):

- ``stdio`` (default) — spawned by the agent app / SDK as a child process
  inside the Sandbox. The agent client owns the child process lifetime.
- ``http`` — a streamable-HTTP server on ``127.0.0.1``. Used by
  Interactive Mode (ADR 0023): the devcontainer ``postStartCommand`` starts it
  on every container start (including resume), and the agent app *connects*
  to a URL instead of owning a child process — so a devcontainer stop→start
  no longer strands the client with a dead stdio pipe it never re-spawns.

Each MCP tool call dispatches to the endpoint's canonical Python module,
inheriting env vars (BOOLEY_SLUG, BOOLEY_LOGS_DIR, etc.) from the container.

B-Wave MCP tools are registered separately with hand-authored schemas since
bwave uses subparsers + REMAINDER, not cleanly auto-extractable.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import shlex
import socket
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import MCPError
from mcp.server import Server
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity
from mcp.server.stdio import stdio_server
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)
from mcp.types import Tool as McpSdkTool

if TYPE_CHECKING:
    from booley.flows.invocation import BudgetPlan

from booley import __version__
from booley.core.boundary import BoundaryError, require_finite_number
from booley.flows.endpoint_events import (
    _endpoint_end_event,
    _endpoint_start_event,
    _write_display_event,
)
from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.store import GoalStore, GoalStoreError
from booley.goals.warnings import goal_warnings
from booley.mcp import (
    call_context,
    catalog,
    endpoint_process,
    job_lifecycle,
    job_results,
)
from booley.mcp.application import (
    McpApplication,
    McpDispatchResult,
    McpInputRequired,
    McpRequestContext,
    McpTextBlock,
    McpToolDefinition,
    McpToolPayload,
    UnknownMcpToolError,
)
from booley.mcp.call_context import (
    CallContext,
    resolve_call_context,
    resolve_work_dir,
)
from booley.mcp.flow_execution_selection import select_flow_execution
from booley.mcp.goal_tools import (
    GOAL_TOOL_NAMES,
    dispatch_goal_tool,
)
from booley.mcp.job_lifecycle import JobLifecycle, JobPoll, JobResult, _poll_status
from booley.mcp.live_catalog import (
    LiveCatalog,
    custom_directory,
    ensure_unique_names,
    project_fingerprint,
    selected_directory,
)
from booley.mcp.report_encoding import ReportEncoder
from booley.mcp.session_observer import SessionMaintenanceApp, SessionObserver
from booley.mcp.session_peer import PeerBoundary
from booley.mcp.session_registry import Attribution
from booley.mcp.tool_changes import ChangingToolsServer, ToolChanges
from booley.runtime import job_records as jobrec
from booley.runtime import pid, runtime_context
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.build_metadata import format_status_line
from booley.runtime.display_identity import DisplayIdentity
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.git import is_linked_worktree
from booley.runtime.heartbeat import REAPER_HEARTBEAT_PATH, touch_reaper_heartbeat
from booley.runtime.mcp_config import (
    DEFAULT_HTTP_PORT,
    HTTP_ENDPOINT_PATH,
    HTTP_PORT_ENV,
    http_port,
)
from booley.runtime.session_paths import logs_runtime_dir
from booley.runtime.timefmt import format_human_datetime

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext


is_pid_alive = pid.is_pid_alive


_REPORT_ENCODER = ReportEncoder()
_structured_from_report = _REPORT_ENCODER.structured_report
_format_mcp_tool_result = _REPORT_ENCODER.text_result

logger = logging.getLogger(__name__)

# Route logging to stderr — stdout is the MCP JSON-RPC pipe.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[mcp] %(levelname)s %(message)s",
)

# ADR 0027: endpoints heavy/long enough to outlive the MCP client's call cap run as
# detached background jobs (submit → poll) instead of holding the call open.
# Everything else stays synchronous — it finishes inside the inline wait.
# The LLM specialists (reviewer / mutation_tester / coverage_analyst) joined
# 2026-07-06 for the same reason: each drives a full sub-agent loop with a
# 20-30 min default_timeout, so an interactive call reliably outran the ~60s
# client cap and came back as a bare timeout — no run_id, no poll, recoverable
# only via booley_report. They are lightweight on memory (Read/Grep/Glob +
# API calls), so single-flight admission is stricter than they need, but the
# poll affordance is exactly what was missing.
_ASYNC_JOB_MCP_TOOLS = frozenset(
    {
        "sim",
        "fpga",
        "synth",
        "reviewer",
        "mutation_tester",
        "coverage_analyst",
    }
)

# How long a submit (and each poll) blocks inline before handing back a run_id.
# Must sit safely under the generic MCP client's ~60-90s call cap so the caller
# never times out mid-wait. Short runs finish inside this window and return
# their full result inline — indistinguishable from the old synchronous path.
# The historical 50s ceiling came from the clients' default MCP-tool-call caps
# (Claude Code kills at 60s out of the box — measured on 2.1.205, ADR 0027
# amendment 2026-07-09). Both sandbox clients now get a 2h cap from the same
# image this server ships in (Dockerfile ENV MCP_TOOL_TIMEOUT + the
# registrar's per-server timeout / tool_timeout_sec), so the waits are sized
# for token economics instead: 240s keeps the polling agent inside the
# Anthropic prompt-cache TTL (~5 min), so every RUNNING round-trip is a cheap
# cache read. Do not raise past ~270s: besides the per-poll cache miss, the
# client's HTTP layer kills a header-less (json_response=True) request at
# ~300s no matter what the MCP-tool timeout is set to — see
# _POLL_WAIT_SECONDS_MAX.
_DEFAULT_JOB_INLINE_WAIT_SECONDS = 240.0

# Interactive MCP servers are Docker containers spawned by Codex/Claude tabs.
# Clients do not always tear them down promptly, so the server exits itself
# after sitting idle. Set BOOLEY_MCP_IDLE_TIMEOUT_SECONDS=0 to disable.
_DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS = 2 * 60 * 60
_DEFAULT_INTERACTIVE_MAX_AGE_SECONDS = 12 * 60 * 60
_MCP_WATCHDOG_POLL_SECONDS = 30.0

# HTTP transport (Interactive Mode, ADR 0023). Loopback only — the URL is
# reachable from agent apps inside the same container, never from the host.
_HTTP_HOST = "127.0.0.1"


def _exposure() -> catalog.Exposure:
    return catalog.Exposure.from_mapping(os.environ)


def _env_timeout_seconds(name: str, default: float | None) -> float | None:
    """Read a timeout env var; non-positive values disable that timeout."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    if value <= 0:
        return None
    return value


# The reaper (booley.docker.reaper) reads this epoch-seconds heartbeat via
# ``docker exec`` to decide if a Sandbox is idle (ADR 0018 WS2/WS4).
# Canonical path + touch helper live in booley.runtime.heartbeat; the module-level
# alias stays monkeypatchable for tests.
_MCP_HEARTBEAT_PATH = REAPER_HEARTBEAT_PATH


class _McpLifetime:
    """Track activity and decide when an interactive MCP server is stale."""

    def __init__(
        self,
        idle_timeout_seconds: float | None,
        max_age_seconds: float | None,
        *,
        now: Callable[[], float] = time.monotonic,
        heartbeat_path: str | None = None,
    ) -> None:
        self.sessions = SessionObserver()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_age_seconds = max_age_seconds
        self._now = now
        self._started_at = now()
        self._last_activity_at = self._started_at
        self._in_flight = 0
        # Wall-clock heartbeat for the external reaper (None disables it).
        self._heartbeat_path = heartbeat_path
        self._write_heartbeat()

    @classmethod
    def from_env(cls, *, self_exit: bool = True) -> _McpLifetime:
        """Build the lifetime policy for this process from the environment.

        ``self_exit=False`` (HTTP transport) keeps the reaper heartbeat but
        disables the idle/max-age self-exit: the HTTP server must stay up for
        the whole container lifetime — clients reconnect to its URL on demand,
        and a self-exited server would recreate the dead-endpoint bug the HTTP
        transport exists to fix. Container-level idle reaping (ADR 0018 WS2)
        remains the resource backstop.
        """
        if os.environ.get("BOOLEY_MCP_MODE") != "interactive":
            # Client-owned server lifetime: no self-exit, but MCP activity feeds
            # the Sandbox reaper heartbeat so active work does not read as idle
            # (ADR 0028 Decision 11).
            return cls(None, None, heartbeat_path=_MCP_HEARTBEAT_PATH)
        if not self_exit:
            return cls(None, None, heartbeat_path=_MCP_HEARTBEAT_PATH)
        return cls(
            _env_timeout_seconds(
                "BOOLEY_MCP_IDLE_TIMEOUT_SECONDS",
                _DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS,
            ),
            _env_timeout_seconds(
                "BOOLEY_MCP_MAX_AGE_SECONDS",
                _DEFAULT_INTERACTIVE_MAX_AGE_SECONDS,
            ),
            heartbeat_path=_MCP_HEARTBEAT_PATH,
        )

    def _write_heartbeat(self) -> None:
        """Record wall-clock activity for the reaper (best-effort)."""
        touch_reaper_heartbeat(self._heartbeat_path)

    def mark_activity(self) -> None:
        self._last_activity_at = self._now()
        self._write_heartbeat()

    def mark_mcp_endpoint_start(self) -> None:
        self._in_flight += 1
        self.mark_activity()

    def mark_mcp_endpoint_end(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        self.mark_activity()

    def should_exit(self) -> tuple[bool, str]:
        """Return whether the server should exit and the reason."""
        if self._in_flight > 0:
            return False, ""
        now = self._now()
        if (
            self.idle_timeout_seconds is not None
            and now - self._last_activity_at >= self.idle_timeout_seconds
        ):
            return True, f"idle for {int(now - self._last_activity_at)}s"
        if self.max_age_seconds is not None and now - self._started_at >= self.max_age_seconds:
            return True, f"older than {int(now - self._started_at)}s"
        return False, ""

    async def wait_until_stale(self) -> str:
        """Sleep until the configured lifetime policy says to exit."""
        while self.idle_timeout_seconds is not None or self.max_age_seconds is not None:
            should_exit, reason = self.should_exit()
            if should_exit:
                return reason
            await asyncio.sleep(_MCP_WATCHDOG_POLL_SECONDS)
        await asyncio.Event().wait()
        return ""


def _reconcile_orphaned_jobs() -> None:
    """Delegate startup reconciliation to the Job lifecycle owner."""
    _JobManager().reconcile_orphaned()


# ---------------------------------------------------------------------------
# MCP tool discovery
# ---------------------------------------------------------------------------


def _discover_booley_mcp_tools() -> tuple[list[dict[str, Any]], list[str]]:
    """Import endpoint classes and extract MCP tool definitions.

    Uses ``discover_mcp_tools()`` for filtering (respects booley.toml allowlists),
    then imports + extracts schemas only for surviving endpoints.
    Handles built-ins and custom endpoints from ``.booley/mcp_tools/``.

    Returns (MCP tools, errors) where:
      - MCP tools: list of dicts {name, module, description, schema}
      - errors: list of human-readable failure messages (empty on success)
    """
    from booley.mcp.registry import discover_mcp_tools

    project_mcp_tools_dir = get_project_mcp_tools_dir()
    specialist_config, flow_config = _get_endpoint_config()
    filtered_endpoints = discover_mcp_tools(
        project_mcp_tools_dir=project_mcp_tools_dir,
        specialist_config=specialist_config,
        flow_config=flow_config,
    )
    ensure_unique_names(filtered_endpoints)
    allowed_names = {t.name for t in filtered_endpoints}

    custom_mcp_tool_paths = _index_custom_mcp_tools(project_mcp_tools_dir, allowed_names)
    builtin_results, builtin_errors = catalog.discover_builtin_definitions(
        filtered_endpoints, _exposure()
    )
    custom_results, custom_errors = _discover_custom_mcp_tools(custom_mcp_tool_paths)
    results = builtin_results + custom_results
    errors = builtin_errors + custom_errors
    return results, errors


def _index_custom_mcp_tools(
    project_mcp_tools_dir: Path | None,
    allowed_names: set[str],
) -> dict[str, Path]:
    """Build path index for custom MCP tools that passed the allowlist filter."""
    custom_mcp_tool_paths: dict[str, Path] = {}
    if not project_mcp_tools_dir or not project_mcp_tools_dir.is_dir():
        return custom_mcp_tool_paths
    for py_file in project_mcp_tools_dir.glob("*.py"):
        if py_file.stem.startswith("_"):
            continue
        from booley.mcp.registry import extract_mcp_tool_info

        info = extract_mcp_tool_info(py_file, builtin=False)
        if info and info.name in allowed_names:
            custom_mcp_tool_paths[info.name] = py_file
    return custom_mcp_tool_paths


def _discover_custom_mcp_tools(
    custom_mcp_tool_paths: dict[str, Path],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Import custom MCP tool files and extract schemas.

    Returns (results, errors).
    """
    from booley.mcp.catalog_worker import run_schema_worker

    definitions, errors = run_schema_worker(custom_mcp_tool_paths)
    return [
        item for item in definitions if catalog.tool_visible(item["name"], _exposure())
    ], errors


def get_project_mcp_tools_dir() -> Path | None:
    """Resolve custom endpoints through the runtime-selected Booley Directory."""
    return custom_directory(selected_directory())


def _get_endpoint_config() -> tuple[dict[str, Any], dict[str, Any]]:
    from booley.mcp.endpoint_config import get_endpoint_config

    return get_endpoint_config()


# ---------------------------------------------------------------------------
# B-Wave MCP tool definitions (hand-authored)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------


def _resolve_transcript_dir(
    mcp_tool_name: str,
    call_counts: dict[str, int],
    context: CallContext | None = None,
) -> Path:
    """Build a per-invocation transcript directory for an an agent capability.

    Raw path: $BOOLEY_RUNTIME_DIR/transcripts/<mcp_tool_name>/<N>/
    Rendered Markdown sidecars are written under human-logs/transcripts.
    where N is the 1-based invocation counter for that Specialist.
    Falls back to a tempdir if BOOLEY_LOGS_DIR is not set (with a warning).
    """
    runtime_env = (
        str(context.runtime_dir)
        if context is not None and context.runtime_dir
        else os.environ.get("BOOLEY_RUNTIME_DIR", "")
    )
    logs_dir = (
        str(context.logs_dir)
        if context is not None and context.logs_dir
        else os.environ.get("BOOLEY_LOGS_DIR", "")
    )
    if runtime_env:
        runtime_dir = Path(runtime_env)
    elif logs_dir:
        runtime_dir = logs_runtime_dir(logs_dir)
    else:
        # Interactive Mode sets neither env var. Writing transcripts to an
        # ephemeral OS tempdir made a failed specialist impossible to
        # post-mortem — the raw agent output vanished with the session and was
        # never where `booley cheat` advertises (QA_REPORT C1: an ~$0.85
        # reviewer failure could not be inspected at all). Persist under the
        # project's own .runtime tree so the transcript survives and is
        # discoverable. Fall back to a tempdir only when no project exists.
        from booley.runtime.checkout_role import SourceCheckoutProjectError

        try:
            from booley.runtime.booley_dir import runtime_dir as _project_runtime_dir

            runtime_dir = _project_runtime_dir()
            logger.info(
                "BOOLEY_LOGS_DIR not set — persisting agent transcripts under "
                "the project runtime dir %s",
                runtime_dir,
            )
        except (FileNotFoundError, ImportError, SourceCheckoutProjectError):
            import tempfile

            runtime_dir = Path(tempfile.mkdtemp(prefix="booley_logs_"))
            logger.warning(
                "BOOLEY_LOGS_DIR not set and no project found — agent "
                "transcripts go to %s (may be lost after the session)",
                runtime_dir,
            )

    call_counts[mcp_tool_name] += 1
    seq = call_counts[mcp_tool_name]
    transcript_dir = runtime_dir / "transcripts" / mcp_tool_name / str(seq)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir


def _params_to_argv(params: dict[str, Any]) -> list[str]:
    """Convert MCP params dict to CLI argv list.

    Option-like values (a ``--test`` selector such as ``--meminit=...``) are
    rendered in the one-token ``--flag=value`` form: as the *next* argv item,
    argparse would read them as a new option and drop the selector — the F-12
    bug class, fixed here at the top of the forwarding chain.
    """
    argv: list[str] = []

    def emit(flag: str, value: str) -> None:
        argv.extend([f"{flag}={value}"] if value.startswith("-") else [flag, value])

    for key, value in params.items():
        flag = f"--{key.replace('_', '-')}"

        if isinstance(value, bool):
            if value:
                argv.append(flag)
            continue

        if isinstance(value, list):
            for item in value:
                if isinstance(item, list):
                    argv.extend([flag, *map(str, item)])
                else:
                    emit(flag, str(item))
            continue

        emit(flag, str(value))

    return argv


# Watchdog sampling interval while a child may be queued for its class slot.
# Queue-state reads are a handful of small file stats/reads per tick; 5s keeps
# the budget accounting within one tick of the truth without measurable IO.


async def _wait_with_queue_credit(
    proc: asyncio.subprocess.Process,
    timeout: float,
    is_queued: Callable[[], bool],
    on_first_active: Callable[[], None] | None,
) -> bool:
    """Compatibility delegate to the queue-aware supervisor."""
    return await endpoint_process._wait_with_queue_credit(
        proc,
        timeout,
        is_queued,
        on_first_active,
        tick_seconds=endpoint_process._WATCHDOG_TICK_SECONDS,
    )


async def _run_subprocess(
    cmd: list[str],
    *,
    timeout: int = 600,
    on_spawn: Callable[[int], None] | None = None,
    env: dict[str, str] | None = None,
    is_queued: Callable[[], bool] | None = None,
    on_first_active: Callable[[], None] | None = None,
) -> tuple[int, str, str, bool]:
    """Delegate process supervision with adapter-configured diagnostic timings."""
    return await endpoint_process._run_subprocess(
        cmd,
        timeout=timeout,
        on_spawn=on_spawn,
        env=env,
        is_queued=is_queued,
        on_first_active=on_first_active,
        watchdog_tick=endpoint_process._WATCHDOG_TICK_SECONDS,
        cancel_grace=endpoint_process.CANCEL_GRACE_SECONDS,
    )


# One MCP tool call's MCP payload: plain text blocks, or the SDK's combination
# form ``(blocks, structured_dict)``, which attaches ``structuredContent`` to
# the CallToolResult while keeping the text blocks exactly as they were
# (mcp 1.28 ``CombinationContent``). No MCP tool declares an ``outputSchema``,
# so the SDK performs no output validation on the attached dict — a tuple
# return without a schema is explicitly supported.
McpRawContent = list[TextContent] | tuple[list[TextContent], dict[str, Any]]
McpToolContent = McpRawContent | McpDispatchResult


def _dispatch_result(content: McpRawContent, *, is_error: bool) -> McpToolContent:
    """Attach an explicit MCP error disposition to existing result content."""
    if not is_error:
        return content
    return McpDispatchResult(value=content, is_error=True)


def _error_result(message: str) -> McpDispatchResult:
    """Return one caller-visible MCP error without raising a protocol error."""
    return McpDispatchResult(
        value=[TextContent(type="text", text=message)],
        is_error=True,
    )


def _with_structured_report(
    content: list[TextContent],
    report: dict[str, Any] | None,
) -> McpRawContent:
    """Attach *report* as structuredContent when possible, else pass through.

    The text blocks are returned untouched either way — structured output is
    additive, never a substitute for the card the agent already parses.
    """
    structured = _structured_from_report(report)
    if structured is None:
        return content
    return content, structured


def _latest_report(
    endpoint: str | None = None,
    *,
    located: job_results.LocatedJob | None = None,
    context: CallContext | None = None,
) -> dict[str, Any] | None:
    return _MCP_JOB_REPOSITORY.latest_report(endpoint, located=located, context=context)


def _try_read_report() -> dict[str, Any] | None:
    return _latest_report()


def _available_report_endpoints(*, context: CallContext | None = None) -> list[str]:
    return job_results._available_report_endpoints(context=context)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def _log_discovery_errors(
    mcp_tools: list[dict[str, Any]],
    discovery_errors: list[str],
) -> None:
    """Log and print MCP tool discovery failures to stderr."""
    logger.error(
        "MCP TOOL DISCOVERY FAILURES (%d endpoints failed to load):\n  %s",
        len(discovery_errors),
        "\n  ".join(discovery_errors),
    )
    print(
        f"[mcp] ERROR: {len(discovery_errors)} endpoint(s) failed to load. "
        f"Only {len(mcp_tools)} MCP tools available. Failures:",
        file=sys.stderr,
        flush=True,
    )
    for err in discovery_errors:
        print(f"[mcp]   - {err}", file=sys.stderr, flush=True)


def _build_mcp_tool_index(
    mcp_tools: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build name -> mcp_tool_def lookup for Booley + B-Wave MCP tools."""
    index: dict[str, dict[str, Any]] = {}
    for t in mcp_tools:
        index[t["name"]] = t
    for t in [
        tool
        for tool in catalog._BWAVE_MCP_TOOLS
        if catalog.tool_visible(tool["name"], _exposure())
    ]:
        index[t["name"]] = t
    return index


def _format_status_card(mcp_tool_names: list[str]) -> str:
    """Return the concise user-facing Interactive Mode status card."""
    session_id = _interactive_session_id()
    mcp_tools_text = ", ".join(mcp_tool_names) if mcp_tool_names else "none"
    try:
        from booley.runtime.health_presentation import current_summary

        health = current_summary(Path.cwd())
    except Exception:  # noqa: BLE001 — status must survive corrupt/missing advisory state
        health = "Automatic Doctor status unavailable."
    return (
        "```text\n"
        f"Booley ready. Sandbox container {session_id} is running.\n"
        f"{format_status_line()}\n"
        f"Available MCP tools: {mcp_tools_text}.\n"
        f"Health: {health}\n"
        "```"
    )


def _dispatch_status(mcp_tool_names: list[str]) -> list[TextContent]:
    """Handle the synthetic status MCP tool without spawning a subprocess."""
    return [
        TextContent(
            type="text",
            text=_format_status_card(mcp_tool_names),
        ),
    ]


def _prepend_changed_health_alert(content: McpToolContent) -> McpToolContent:
    """Prepend a changed automatic-health issue to the first interactive result."""
    if not catalog.status_visible(_exposure()):
        return content
    try:
        from booley.runtime.health_presentation import consume_changed_summary

        alert = consume_changed_summary(Path.cwd(), channel="mcp-tool", issues_only=True)
    except Exception:  # noqa: BLE001 — health reporting must never break a Booley Flow result
        return content
    if alert is None:
        return content
    block = TextContent(type="text", text=f"HEALTH WARNING: {alert}")
    if isinstance(content, McpDispatchResult):
        return McpDispatchResult(
            value=_prepend_health_block(content.value, block),
            is_error=content.is_error,
            goal_aware=content.goal_aware,
        )
    return _prepend_health_block(content, block)


def _prepend_health_block(content: object, block: TextContent) -> McpRawContent:
    """Prepend one health block while retaining optional structured content."""
    if isinstance(content, tuple):
        blocks, structured = content
        return [block, *blocks], structured
    if not isinstance(content, list):
        raise TypeError("MCP tool returned content that is not a list")
    return [block, *content]


def _dispatch_targets(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the synthetic targets-listing MCP tool in-process (no subprocess).

    The same cheap ``.core``-YAML surface as ``booley targets --json``
    (:mod:`booley.targets.target_surface`); the expensive resolved detail view stays
    CLI-only — an agent that needs resolved files runs the Booley Flow itself.
    """
    import json as _json

    from booley.fusesoc import fusesoc_registry
    from booley.targets import target_surface

    work_dir_error = _validate_work_dir(arguments.get("work_dir"))
    if work_dir_error is not None:
        return [TextContent(type="text", text=work_dir_error)]
    project_root = resolve_work_dir(arguments)

    try:
        surface = target_surface.collect_surface(project_root)
        surface = target_surface.filter_surface(
            surface,
            for_flow=arguments.get("for_flow"),
            glob=arguments.get("glob"),
        )
    except (fusesoc_registry.FuseSocError, ValueError) as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]

    payload = target_surface.surface_payload(surface, project_root)
    return [TextContent(type="text", text=_json.dumps(payload, indent=2))]


def _format_report_card(
    report: dict[str, Any] | None, endpoint: str | None, *, context: CallContext | None = None
) -> str:
    """Render a fetched run report (or a helpful not-found message)."""
    if report is None:
        which = f" for endpoint {endpoint!r}" if endpoint else ""
        available = _available_report_endpoints(context=context)
        hint = (
            f" Reports on disk: {', '.join(available)}."
            if available
            else " No endpoint reports have been written in this run yet."
        )
        return f"No completed run report found{which}.{hint}"

    header_fields = (
        "flow",
        "mcp_tool",
        "target",
        "passed",
        "exit_code",
        "timestamp",
        "elapsed_s",
    )
    lines = [
        f"{field}: {report[field]}"
        for field in header_fields
        if report.get(field) not in (None, "")
    ]
    detail = report.get("detail")
    if isinstance(detail, dict) and detail:
        lines.append(f"detail: {json.dumps(detail)}")
    card = "```text\n" + "\n".join(lines) + "\n```"
    report_text = report.get("report_text")
    if report_text:
        card += f"\n--- report ---\n{report_text}"
    return card


def _dispatch_report(arguments: dict[str, Any]) -> McpToolContent:
    """Handle the synthetic report-fetch MCP tool without spawning a subprocess."""
    raw = arguments.get("endpoint")
    endpoint = raw.strip() if isinstance(raw, str) and raw.strip() else None
    work_dir_error = _validate_work_dir(arguments.get("work_dir"))
    if work_dir_error is not None:
        return _error_result(work_dir_error)
    try:
        context = resolve_call_context(arguments)
    except (GoalBindingError, GoalStoreError) as exc:
        return _error_result(str(exc))
    report = _latest_report(endpoint, context=context)
    content = [
        TextContent(type="text", text=_format_report_card(report, endpoint, context=context))
    ]
    return _with_structured_report(content, report)


async def _dispatch_bwave(
    name: str,
    arguments: dict[str, Any],
) -> list[TextContent] | None:
    """Handle B-Wave MCP tool calls. Returns None if *name* is not a B-Wave MCP tool."""
    bwave_subcmds = {
        "bwave": lambda a: [
            sys.executable,
            "-m",
            "booley.bwave.cli",
            *a.get("extra_args", []),
        ],
    }
    builder = bwave_subcmds.get(name)
    if builder is None:
        return None
    if not catalog.tool_visible(name, _exposure()):
        return None
    cmd = builder(arguments)
    extra_args = arguments.get("extra_args", [])
    display_label = shlex.join(extra_args) if extra_args else None
    identity = DisplayIdentity.current(uuid.uuid4().hex)
    _write_display_event(
        _endpoint_start_event(
            name,
            None,
            display_label=display_label,
            identity=identity,
        ),
    )
    started = time.monotonic()
    exit_code = 2
    try:
        exit_code, stdout, stderr, _timed_out = await _run_subprocess(cmd)
        return [
            TextContent(
                type="text",
                text=_format_mcp_tool_result(exit_code, stdout, stderr),
            )
        ]
    finally:
        _write_display_event(
            _endpoint_end_event(
                name,
                None,
                EndpointOutcome(exit_code=exit_code),
                time.monotonic() - started,
                display_label=display_label,
                identity=identity,
            ),
        )


def _job_inline_wait_seconds() -> float:
    """Inline wait before a submit hands back a run_id (0 → detach immediately)."""
    val = _env_timeout_seconds(
        "BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS",
        _DEFAULT_JOB_INLINE_WAIT_SECONDS,
    )
    return val if val is not None else 0.0


def _job_poll_wait_seconds() -> float:
    """How long a single poll blocks waiting for the job (0 → return at once)."""
    val = _env_timeout_seconds(
        "BOOLEY_MCP_JOB_POLL_WAIT_SECONDS",
        catalog._DEFAULT_JOB_POLL_WAIT_SECONDS,
    )
    return val if val is not None else 0.0


# Ceiling for the caller-supplied booley_poll 'wait_seconds'. The binding
# limit is NOT the configured 2h client cap but the HTTP layer beneath it:
# With json_response=True the response carries no headers until the MCP tool
# returns, and Node's undici kills a header-less request at ~300s (measured
# 295s, ADR 0027 amendment 2026-07-09) regardless of MCP_TOOL_TIMEOUT. 270s
# stays under that with margin — and also inside the Anthropic prompt-cache
# TTL, so even a max-length poll costs the caller only a cheap cache read.
_POLL_WAIT_SECONDS_MAX = 270


def _requested_poll_wait_seconds(arguments: dict[str, Any]) -> float:
    """Resolve how long this poll call should block waiting for the job.

    A caller-supplied 'wait_seconds' (long-poll, ADR 0027 amendment 2026-07-06)
    is clamped to [0, _POLL_WAIT_SECONDS_MAX]. When absent — or not a number,
    the same tolerant stance as the env knobs — the server default applies.
    ``require_finite_number`` rejects both the bool trap (``True``/``False``
    here is always a caller bug, not a duration) and NaN/inf, which would
    otherwise poison the ``min``/``max`` clamp below.
    """
    raw = arguments.get("wait_seconds")
    try:
        seconds = require_finite_number(raw, field="wait_seconds")
    except BoundaryError:
        return _job_poll_wait_seconds()
    return float(min(max(seconds, 0), _POLL_WAIT_SECONDS_MAX))


def job_roots() -> tuple[Path, ...]:
    """Retained roots resolved by the durable Job owner."""
    return job_results.job_roots(container_root=call_context.container_jobs_root)


def _locate_job(run_id: str) -> job_results.LocatedJob | None:
    return job_results._locate_job(run_id, roots=job_roots())


def _job_record(run_id: str) -> jobrec.JobRecord | None:
    located = _locate_job(run_id)
    return None if located is None else located.record


def _job_phase(run_id: str) -> str:
    return _MCP_JOB_REPOSITORY.phase(run_id)


def _format_job_attached(name: str, run_id: str) -> str:
    """Message for a duplicate submit attached to the identical in-flight run."""
    return (
        f"{_job_phase(run_id)}: an identical '{name}' run is already in "
        f"flight — attached to it instead of starting a duplicate "
        f"(run_id={run_id}). This is NOT the result. Call the "
        f"'{catalog.POLL_MCP_TOOL_NAME}' MCP tool with run_id={run_id} and keep polling "
        f"until it returns an EXIT_CODE."
    )


def _format_job_running(name: str, run_id: str) -> str:
    """Message returned when a heavy endpoint detaches past the inline wait."""
    phase = _job_phase(run_id)
    waiting = (
        "is waiting for its concurrency slot"
        if phase.startswith("QUEUED")
        else "is taking longer than the inline wait"
    )
    return (
        f"{phase}: '{name}' {waiting} and is now "
        f"running in the background (run_id={run_id}). This is NOT the result "
        f"— the run has not finished. Call the '{catalog.POLL_MCP_TOOL_NAME}' MCP tool with "
        f"run_id={run_id} to keep waiting, and keep polling until it returns "
        f"an EXIT_CODE. Pass/fail criteria are recorded only when the run "
        f"actually completes, so do not conclude the step until then."
    )


def _format_job_running_poll(run_id: str) -> str:
    """Message for a poll whose job is still running (or queued for a slot)."""
    message = (
        f"{_job_phase(run_id)}: job {run_id} is still in progress. Call "
        f"'{catalog.POLL_MCP_TOOL_NAME}' again with the same run_id to keep waiting for "
        f"the result."
    )
    progress = _running_progress(run_id)
    if progress is None:
        return message
    completed = progress.get("completed_targets", [])
    pending = progress.get("pending_targets", [])
    phase = progress.get("phase", "running")
    return (
        f"{message} Nonterminal checkpoint ({phase}): completed targets "
        f"{completed}; pending targets {pending}. This partial checkpoint is "
        "not a final Flow verdict."
    )


def _running_progress(run_id: str) -> dict[str, Any] | None:
    return _MCP_JOB_REPOSITORY.progress(run_id)


def _running_poll_content(run_id: str) -> McpToolContent:
    """Running poll card enriched with the latest durable matrix checkpoint."""
    content = [TextContent(type="text", text=_format_job_running_poll(run_id))]
    rec = _job_record(run_id)
    return _goal_warning_result(
        _with_structured_report(content, _running_progress(run_id)),
        None if rec is None else job_results.job_binding(rec),
        None if rec is None else rec.session_key,
    )


def _job_report(rec: jobrec.JobRecord | None) -> tuple[dict[str, Any] | None, bool]:
    return job_results._job_report(rec, repository=_MCP_JOB_REPOSITORY)


class _McpJobRepository(job_results.JobRepository):
    """Wire the adapter's existing private substitution points to shared lookup."""

    def roots(self) -> tuple[Path, ...]:
        return job_roots()

    def locate(self, run_id: str) -> job_results.LocatedJob | None:
        return _locate_job(run_id)

    def record(self, run_id: str) -> jobrec.JobRecord | None:
        return _job_record(run_id)

    def report_dirs(
        self, located: job_results.LocatedJob | None = None, *, context: CallContext | None = None
    ) -> tuple[Path, ...]:
        return job_results._endpoint_report_dirs(located, context=context)


_MCP_JOB_REPOSITORY = _McpJobRepository()


class _JobManager(JobLifecycle):
    """MCP composition adapter; all lifecycle decisions belong to JobLifecycle."""

    _repository = _MCP_JOB_REPOSITORY

    @staticmethod
    def _is_alive(process_id: int) -> bool:
        return is_pid_alive(process_id)

    @staticmethod
    async def _run_process(cmd: list[str], **kwargs: Any) -> tuple[int, str, str, bool]:
        return await _run_subprocess(cmd, **kwargs)

    @staticmethod
    def _write_end(name: str, duration_s: int, **kwargs: Any) -> None:
        endpoint_process.write_synthetic_endpoint_end(name, duration_s, **kwargs)

    @staticmethod
    async def _cancel_process(pid: int) -> None:
        await _cancel_adopted_process_group(pid)

    def _report(self, rec: jobrec.JobRecord | None) -> tuple[dict[str, Any] | None, bool]:
        return _job_report(rec)

    def result_text(self, run_id: str) -> str:
        """Render a finished job's result exactly like the synchronous path."""
        result = self.terminal_result(run_id)
        return _format_mcp_tool_result(
            result.exit_code, result.stdout, result.stderr, result.report
        )

    def result_content(self, run_id: str, *, result: JobResult | None = None) -> McpToolContent:
        """``result_text`` as MCP content, with structuredContent attached.

        Only a *fresh* report (one that provably belongs to this run, see
        ``_job_report``) is attached — the structured verdict must vouch for
        the run's outcome, unlike the text card, which may show a non-fresh
        report with its caveats spelled out.
        """
        result = result or self.terminal_result(run_id)
        exit_code, stdout, stderr, report, report_fresh = (
            result.exit_code,
            result.stdout,
            result.stderr,
            result.report,
            result.report_fresh,
        )
        content = [
            TextContent(
                type="text",
                text=_format_mcp_tool_result(exit_code, stdout, stderr, report),
            )
        ]
        rendered = _with_structured_report(content, report if report_fresh else None)
        rec = self._read_run(run_id)
        binding = None if rec is None else job_results.job_binding(rec)
        return _goal_warning_result(
            _dispatch_result(rendered, is_error=exit_code != 0),
            binding,
            None if rec is None else rec.session_key,
        )


# Re-check cadence while long-polling a disk-only (adopted) job. Each check
# is a couple of small file reads; 5s keeps the answer prompt without churn.


async def _poll_from_disk(
    run_id: str,
    jobs: _JobManager,
    wait_seconds: float = 0.0,
) -> McpToolContent:
    """Compose an adopted Job's SDK-independent poll outcome."""
    state = await jobs.poll_from_disk(
        run_id, wait_seconds, disk_tick=job_lifecycle._DISK_POLL_TICK_SECONDS
    )
    return _render_job_poll(state, jobs)


def _render_job_poll(state: JobPoll, jobs: _JobManager) -> McpToolContent:
    if state.status == "unknown":
        return _error_result(
            f"Unknown run_id {state.run_id!r}. It may belong to a different project "
            f"run, or the job record has been cleaned up."
        )
    if state.status == "finished":
        return jobs.result_content(state.run_id, result=state.result)
    return _running_poll_content(state.run_id)


async def _dispatch_poll_impl(arguments: dict[str, Any], jobs: _JobManager) -> McpToolContent:
    """Validate wire inputs and compose the lifecycle's bounded poll outcome."""
    raw = arguments.get("run_id")
    run_id = raw.strip() if isinstance(raw, str) else ""
    if not run_id:
        return _error_result("Provide a 'run_id' (returned by an endpoint that reported RUNNING).")
    state = await _poll_status(
        run_id,
        _requested_poll_wait_seconds(arguments),
        jobs.wait,
        _MCP_JOB_REPOSITORY,
        is_pid_alive,
        disk_tick=job_lifecycle._DISK_POLL_TICK_SECONDS,
    )
    return _render_job_poll(state, jobs)


async def _dispatch_sleep(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the diagnostic sleep MCP tool: hold the call open, then report timing.

    Sleeps in short ticks and logs each one, so when the client kills the call
    before it returns, the server log still shows exactly how far the call got
    — that log line IS the measurement. A cancelled handler (client disconnect
    on the streamable-HTTP transport) is logged with its elapsed time and
    re-raised.
    """
    raw = arguments.get("seconds")
    try:
        parsed = require_finite_number(raw, field="seconds")
    except BoundaryError:
        parsed = -1.0  # any negative sentinel trips the "non-negative" check below
    if parsed < 0:
        return [
            TextContent(
                type="text",
                text="Provide 'seconds' as a non-negative number.",
            )
        ]
    requested = min(parsed, 86400.0)
    started_wall = datetime.now(UTC)
    started = time.monotonic()
    deadline = started + requested
    logger.info(
        "booley_sleep: holding call open for %.1fs (started %s, pid %d)",
        requested,
        format_human_datetime(started_wall, seconds=True),
        os.getpid(),
    )
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(10.0, remaining))
            logger.info(
                "booley_sleep: still holding, %.1fs elapsed of %.1fs",
                time.monotonic() - started,
                requested,
            )
    except asyncio.CancelledError:
        logger.warning(
            "booley_sleep: CANCELLED after %.1fs of %.1fs — the client "
            "killed the call at this point",
            time.monotonic() - started,
            requested,
        )
        raise
    elapsed = time.monotonic() - started
    logger.info("booley_sleep: completed after %.1fs", elapsed)
    return [
        TextContent(
            type="text",
            text=(
                f"SLEEP_COMPLETE: requested={requested:.1f}s "
                f"elapsed={elapsed:.1f}s "
                f"started={format_human_datetime(started_wall, seconds=True)} "
                f"finished={format_human_datetime(datetime.now(UTC), seconds=True)} "
                f"pid={os.getpid()}. If you are reading this, the call survived "
                f"the client's MCP-tool-call cap at this duration."
            ),
        )
    ]


async def _cancel_adopted_process_group(pid: int) -> None:
    """Delegate disk-only process-group cancellation."""
    await endpoint_process._cancel_adopted_process_group(
        pid, grace_seconds=endpoint_process.CANCEL_GRACE_SECONDS
    )


async def _dispatch_cancel_impl(
    arguments: dict[str, Any],
    jobs: _JobManager,
) -> list[TextContent]:
    """Handle the synthetic cancel MCP tool for queued and running jobs."""
    raw = arguments.get("run_id")
    run_id = raw.strip() if isinstance(raw, str) else ""
    if not run_id:
        return [TextContent(type="text", text="Provide the 'run_id' of a queued job.")]
    phase = await jobs.cancel(run_id)
    if phase is None:
        return [TextContent(type="text", text=f"Unknown run_id {run_id!r}.")]
    if phase == "already_finished":
        return [
            TextContent(
                type="text",
                text=f"Job {run_id} already finished — nothing to cancel. "
                f"Fetch its result with '{catalog.POLL_MCP_TOOL_NAME}'.",
            )
        ]
    if phase == "finished":
        return [
            TextContent(
                type="text",
                text=f"Job {run_id} finished before cancellation took effect. "
                f"Fetch its result with '{catalog.POLL_MCP_TOOL_NAME}'.",
            )
        ]
    return [
        TextContent(
            type="text",
            text=(
                f"CANCELLED: {phase} job {run_id} was withdrawn before it started."
                if phase == "queued"
                else f"CANCELLED: running job {run_id} received SIGTERM; any process "
                f"still alive after {endpoint_process.CANCEL_GRACE_SECONDS:.0f}s was force-killed."
            ),
        )
    ]


async def _dispatch_async_job(
    name: str,
    cmd: list[str],
    mcp_tool_timeout: int,
    jobs: _JobManager,
    *,
    context: CallContext,
) -> McpToolContent:
    """Compose inline completion or a background handoff from the lifecycle."""
    try:
        submission = await jobs.submit_and_wait(
            name, cmd, mcp_tool_timeout, context=context, wait_seconds=_job_inline_wait_seconds()
        )
    except jobrec.JobRecordError as exc:
        return _error_result(str(exc))
    state = submission.poll
    if state.status != "running" or (submission.attached and not state.tracked):
        return _render_job_poll(state, jobs)
    text = (
        _format_job_attached(name, state.run_id)
        if submission.attached
        else _format_job_running(name, state.run_id)
    )
    return _with_structured_report(
        [TextContent(type="text", text=text)], _running_progress(state.run_id)
    )


def _find_attachable_job(
    name: str, cmd: list[str], *, context: CallContext | None = None
) -> str | None:
    return job_results._find_attachable_job(name, cmd, context=context, roots=job_roots())


async def _attach_to_job(name: str, run_id: str, jobs: _JobManager) -> McpToolContent:
    """Compose an attached live Job after the lifecycle's inline wait."""
    state = await jobs.poll(
        run_id, _job_inline_wait_seconds(), disk_tick=job_lifecycle._DISK_POLL_TICK_SECONDS
    )
    if state.status != "running" or not state.tracked:
        return _render_job_poll(state, jobs)
    return _with_structured_report(
        [TextContent(type="text", text=_format_job_attached(name, run_id))],
        _running_progress(run_id),
    )


def _validate_work_dir(value: Any) -> str | None:
    """Reject a ``work_dir`` argument that is not a usable checkout root.

    The agent-facing ``work_dir`` retargets an endpoint at another checkout (a
    linked git worktree, e.g. one made by ``booley worktree new``). Fail-closed
    validation: an arbitrary directory would silently scan zero cores or —
    worse — a stale copy of the RTL, so anything that is not the session
    workspace itself or the root of a linked worktree (``.git`` is a *file*
    pointing at the parent repo's ``.git/worktrees/``) is refused up front
    with guidance, before a subprocess is ever spawned.

    Returns an error message, or None when the argument is absent or valid.
    """
    if value is None:
        return None
    work_dir = Path(str(value))
    if not work_dir.is_absolute():
        work_dir = Path.cwd() / work_dir
    try:
        resolved = work_dir.resolve()
    except OSError as exc:
        return f"ERROR: work_dir {value!r} cannot be resolved: {exc}"
    if resolved == Path.cwd().resolve():
        return None  # explicit spelling of the default
    if not resolved.is_dir():
        return (
            f"ERROR: work_dir {value!r} does not exist. Create a worktree "
            "first: `booley worktree new <name>` puts it under "
            ".booley/worktrees/<name>."
        )
    if not is_linked_worktree(resolved):
        return (
            f"ERROR: work_dir {value!r} is not the root of a linked git "
            "worktree (no .git pointer file). Pass the worktree root created "
            "by `booley worktree new <name>` under .booley/worktrees/, "
            "or omit work_dir to run against the session workspace."
        )
    return None


def _endpoint_command(
    name: str,
    arguments: dict[str, Any],
    definition: dict[str, Any],
    call_counts: dict[str, int],
    context: CallContext,
) -> list[str]:
    """Build the canonical subprocess command for one endpoint definition."""
    arguments = dict(arguments)
    argv = _params_to_argv(arguments)
    if definition.get("is_specialist"):
        transcript_dir = (
            _resolve_transcript_dir(name, call_counts, context=context)
            if context.binding
            else _resolve_transcript_dir(name, call_counts)
        )
        argv.extend(["--transcript-dir", str(transcript_dir)])
    module = definition["module"]
    runner = select_flow_execution(binding=context.binding).flow_runner_module
    if definition.get("is_flow") and runner is not None:
        cmd = ["python", "-m", runner]
        if definition.get("is_custom") and definition.get("custom_path"):
            cmd.extend(["--custom-path", definition["custom_path"]])
        return [*cmd, name, *argv]
    if definition.get("is_custom") and definition.get("custom_path"):
        return ["python", definition["custom_path"], *argv]
    module_path = definition.get("module_path") or f"booley.mcp.{module}"
    return ["python", "-m", module_path, *argv]


async def _run_inline_endpoint(
    name: str,
    cmd: list[str],
    timeout: int,
    jobs: _JobManager,
    *,
    context: CallContext,
    publish_activity: bool,
    invocation_id: str | None = None,
) -> tuple[int, str, str, bool]:
    """Run inline work, recording only user-visible endpoint activity."""
    if publish_activity:
        return await jobs.run_synchronous(name, cmd, timeout, context=context)
    identity = DisplayIdentity.current(invocation_id or uuid.uuid4().hex)
    try:
        result = await _run_subprocess(
            cmd,
            timeout=timeout,
            env=endpoint_process._endpoint_subprocess_env(
                context,
                pytest_scope=name,
                BOOLEY_DISPLAY_INVOCATION_ID=identity.invocation_id,
            ),
        )
    except asyncio.CancelledError:
        endpoint_process.write_synthetic_endpoint_end(
            name, 0, identity=identity, outcome="aborted"
        )
        raise
    if result[3]:
        endpoint_process.write_synthetic_endpoint_end(name, timeout, identity=identity)
    return result


async def _dispatch_booley_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    mcp_tool_def: dict[str, Any],
    mcp_tool_call_counts: dict[str, int],
    jobs: _JobManager,
    request_context: McpRequestContext | None = None,
) -> McpToolContent:
    """Run a Booley Flow subprocess and return an MCP result.

    Heavy, long-running endpoints (``_ASYNC_JOB_MCP_TOOLS``) go through the submit/poll
    job model (ADR 0027): they can outlive the MCP client's call cap, so holding
    the call open orphans the subprocess and BLOCKs the next call. Everything
    else stays synchronous and returns its full result inline.
    """
    work_dir_error = _validate_work_dir(arguments.get("work_dir"))
    if work_dir_error is not None:
        return _error_result(work_dir_error)
    try:
        context = resolve_call_context(arguments)
        if request_context is not None and request_context.session_key() is not None:
            context = replace(context, session_key=request_context.session_key())
    except (GoalBindingError, GoalStoreError) as exc:
        return _error_result(f"ERROR: {exc}")

    try:
        cmd = _endpoint_command(name, arguments, mcp_tool_def, mcp_tool_call_counts, context)
    except (GoalStoreError, GoalBindingError) as exc:
        return _error_result(f"ERROR: {exc}")

    try:
        mcp_tool_timeout = _mcp_tool_timeout_seconds(
            name, arguments, mcp_tool_def, context=context
        )
    except ValueError as exc:
        return _error_result(f"ERROR: invalid Flow timeout: {exc}")
    logger.info("Dispatching %s (timeout=%ds): %s", name, mcp_tool_timeout, " ".join(cmd))

    if name in _ASYNC_JOB_MCP_TOOLS:
        result = await _dispatch_async_job(name, cmd, mcp_tool_timeout, jobs, context=context)
        return _goal_warning_result(result, context.binding, context.session_key)

    exit_code, stdout, stderr, _timed_out = await _run_inline_endpoint(
        name,
        cmd,
        mcp_tool_timeout,
        jobs,
        context=context,
        publish_activity=bool(mcp_tool_def.get("is_flow") or mcp_tool_def.get("is_specialist")),
    )
    skip_report = bool(arguments.get("dry_run")) and bool(
        mcp_tool_def.get("non_persisting_dry_run")
    )
    report = (
        None
        if skip_report
        else (_latest_report(context=context) if context.binding else _try_read_report())
    )
    content = [
        TextContent(type="text", text=_format_mcp_tool_result(exit_code, stdout, stderr, report))
    ]
    rendered = _with_structured_report(content, report)
    return _goal_warning_result(
        _dispatch_result(rendered, is_error=exit_code != 0), context.binding, context.session_key
    )


def _sim_mcp_tool_timeout_seconds(
    arguments: dict[str, Any],
    default: int,
    context: CallContext,
) -> int:
    """Whole-invocation Simulation watchdog with mode-aware stage budgets."""
    from booley.flows.invocation import requested_timeout_ms, resolve_timeout_ms
    from booley.flows.sim.config import resolve_sim_build_timeout_ms
    from booley.flows.sim.flow import _resolve_sim_campaign_work_units, _resolve_sim_timeout_ms
    from booley.flows.sim.mode import SimulationMode, normalize_simulation_mode

    work_dir = context.work_dir
    requested = requested_timeout_ms(arguments)
    if requested is None:
        timeout_ms = _resolve_sim_timeout_ms(work_dir)
    else:
        timeout_ms = resolve_timeout_ms("sim", None, requested)
    build_timeout_ms = resolve_sim_build_timeout_ms(work_dir)
    run_s = max(1, timeout_ms // 1000)
    build_s = max(1, build_timeout_ms // 1000)
    raw_target = str(arguments.get("target") or "").strip()
    target_count = _target_count(arguments)
    mode = normalize_simulation_mode(str(arguments.get("mode") or SimulationMode.SIMULATE.value))
    try:
        work_units = (
            1
            if arguments.get("resume_from") is not None
            else _resolve_sim_campaign_work_units(
                work_dir,
                raw_target,
                arguments.get("test"),
                None,
                mode,
                seed=arguments.get("seed"),
            )
        )
    except Exception:  # noqa: BLE001 — malformed project input is graded by the child
        work_units = target_count
    planned_s = _sim_planned_timeout_seconds(
        arguments,
        mode=mode,
        target_count=target_count,
        work_units=work_units,
        run_s=run_s,
        build_s=build_s,
        state_path=context.state_path,
    )
    return max(default, planned_s)


def _sim_planned_timeout_seconds(
    arguments: dict[str, Any],
    *,
    mode: Any,
    target_count: int,
    work_units: int,
    run_s: int,
    build_s: int,
    state_path: Path | None,
) -> int:
    """Sum child stage limits plus finalization slack for one Simulation call."""
    from booley.flows.base import DEFAULT_TIMEOUT_S
    from booley.flows.sim.flow import _TRACE_CLEANUP_MARGIN_S
    from booley.flows.sim.mode import SimulationMode

    if mode.elaborates_only:
        sweep_s = run_s if mode is SimulationMode.ELAB_ONLY_STANDALONE else 0
        return target_count * build_s + sweep_s + 30
    if arguments.get("coverage"):
        runs_s = work_units * (run_s + DEFAULT_TIMEOUT_S + _TRACE_CLEANUP_MARGIN_S)
        targets_s = target_count * (build_s + 30 + DEFAULT_TIMEOUT_S)
        return runs_s + targets_s + 30
    baseline_scale = 2 if _baseline_required("cycle_count_", state_path) else 1
    run_units = work_units * baseline_scale
    build_units = target_count * baseline_scale
    planned_s = build_units * build_s + run_units * (DEFAULT_TIMEOUT_S + run_s)
    if arguments.get("trace"):
        planned_s += _TRACE_CLEANUP_MARGIN_S * run_units
    return planned_s + 30


def _target_count(arguments: dict[str, Any]) -> int:
    """Count comma-separated selectors without resolving project state."""
    raw_target = str(arguments.get("target") or "").strip()
    return max(1, len([token for token in raw_target.split(",") if token.strip()]))


def _implementation_budget_plan(
    name: str,
    arguments: dict[str, Any],
    default: int,
    context: CallContext,
) -> BudgetPlan:
    """Plan the pre-spawn watchdog for synthesis or FPGA implementation."""
    from booley.flows.invocation import BudgetPlan, requested_timeout_ms, resolve_timeout_ms

    timeout_ms = resolve_timeout_ms(
        name,
        context.explicit_work_dir,
        requested_timeout_ms(arguments),
    )
    criterion_prefix = "synthesis_ok_" if name == "synth" else "fpga_impl_ok_"
    has_baseline = bool(arguments.get("baseline")) or _baseline_required(
        criterion_prefix, context.state_path
    )
    work_units = _target_count(arguments) * (2 if has_baseline else 1)
    return BudgetPlan(
        timeout_ms=timeout_ms,
        work_units=work_units,
        setup_grace_per_unit_s=60,
        finalize_grace_s=120,
        outer_floor_s=default,
    )


def _lint_budget_plan(
    arguments: dict[str, Any],
    default: int,
    context: CallContext,
) -> BudgetPlan:
    """Plan one active timeout budget for every selected lint Target."""
    from booley.flows.invocation import BudgetPlan, requested_timeout_ms, resolve_timeout_ms

    timeout_ms = resolve_timeout_ms(
        "lint",
        context.explicit_work_dir,
        requested_timeout_ms(arguments),
    )
    work_units = _target_count(arguments)
    return BudgetPlan(
        timeout_ms=timeout_ms,
        work_units=work_units,
        setup_grace_per_unit_s=0,
        finalize_grace_s=30,
        outer_floor_s=default,
    )


def _mcp_tool_timeout_seconds(
    name: str,
    arguments: dict[str, Any],
    mcp_tool_def: dict[str, Any],
    *,
    context: CallContext | None = None,
) -> int:
    """Outer MCP kill budget for a endpoint subprocess.

    For simulate, the user-facing ``timeout`` argument is the simulator
    budget in milliseconds. The Flow still needs time after that to
    close FIFO trace writers, reap bwave, and write reports, so the MCP
    watchdog must be larger than the sim budget. Dispatch passes the call's
    resolved *context*; without one it is resolved from *arguments*.
    """
    from booley.targets.flow_names import canonical

    if context is None:
        context = resolve_call_context(arguments)
    name = canonical(name)
    default = int(mcp_tool_def.get("default_timeout") or 600)
    if name in {"synth", "fpga"}:
        return _implementation_budget_plan(name, arguments, default, context).outer_timeout_s

    if name == "lint":
        return _lint_budget_plan(arguments, default, context).outer_timeout_s

    if name != "sim":
        return default
    return _sim_mcp_tool_timeout_seconds(arguments, default, context)


def _baseline_required(criterion_prefix: str, state_path: Path | None) -> bool:
    """Whether criteria persisted at *state_path* will auto-enable an implementation baseline."""
    from booley.evidence.fields import BASELINE_REF_PARAM

    if state_path is None:
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    criteria = state.get("criteria") if isinstance(state, dict) else None
    if not isinstance(criteria, dict):
        return False
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("params"), dict)
        and bool(entry["params"].get(BASELINE_REF_PARAM))
        for name, entry in criteria.items()
        if name.startswith(criterion_prefix)
    )


def _dispatch_coverage_evidence(arguments: dict[str, Any]) -> McpToolContent:
    """Serve one bounded query from the Campaign-scoped evidence module."""
    from booley.mcp.coverage_evidence import query_active_coverage_evidence

    try:
        result = query_active_coverage_evidence(arguments)
    except (OSError, ValueError) as exc:
        result = {"error": "coverage_evidence_rejected", "message": str(exc)}
    return [TextContent(type="text", text=json.dumps(result, sort_keys=True))]


async def _dispatch_special_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    jobs: _JobManager,
    status_mcp_tool_names: list[str],
    request_context: McpRequestContext | None = None,
) -> McpToolContent | McpInputRequired | None:
    """Route the fixed, always-nameable meta MCP tools (status/report/poll/cancel/sleep).

    Returns ``None`` when *name* isn't one of these — the caller falls through
    to Booley/B-Wave MCP-tool dispatch.
    """
    # The synchronous meta MCP tools share one shape (visibility gate + handler);
    # a hidden-but-named MCP tool falls through to None like an unknown name.
    sync_meta: dict[str, tuple[Callable[[], bool], Callable[[], McpToolContent]]] = {
        catalog._STATUS_MCP_TOOL_NAME: (
            lambda: catalog.status_visible(_exposure()),
            lambda: _dispatch_status(status_mcp_tool_names),
        ),
        catalog._REPORT_MCP_TOOL_NAME: (
            lambda: catalog.report_visible(_exposure()),
            lambda: _dispatch_report(arguments),
        ),
        catalog._TARGETS_MCP_TOOL_NAME: (
            lambda: catalog.utilities_visible(_exposure()),
            lambda: _dispatch_targets(arguments),
        ),
        catalog._COVERAGE_EVIDENCE_TOOL_NAME: (
            lambda: catalog.coverage_visible(_exposure()),
            lambda: _dispatch_coverage_evidence(arguments),
        ),
    }
    entry = sync_meta.get(name)
    if entry is not None:
        visible, handler = entry
        return handler() if visible() else None
    if name == catalog.POLL_MCP_TOOL_NAME and catalog.utilities_visible(_exposure()):
        return await _dispatch_poll(arguments, jobs)
    if name == catalog._CANCEL_MCP_TOOL_NAME and catalog.utilities_visible(_exposure()):
        return await _dispatch_cancel(arguments, jobs)
    if name == catalog._SLEEP_MCP_TOOL_NAME and _exposure().debug:
        return await _dispatch_sleep(arguments)
    if name in GOAL_TOOL_NAMES and catalog.goals_visible(_exposure()):
        return await dispatch_goal_tool(
            name, arguments, booley_dir=resolve_booley_dir(), request_context=request_context
        )
    return None


async def _dispatch_application_tool(
    name: str,
    arguments: dict[str, Any],
    mcp_tool_def: Mapping[str, Any],
    *,
    jobs: _JobManager,
    status_mcp_tool_names: list[str],
    mcp_tool_call_counts: dict[str, int],
    request_context: McpRequestContext | None = None,
) -> McpToolContent | McpInputRequired:
    """Dispatch one validated application call to its concrete endpoint."""
    if name in GOAL_TOOL_NAMES and request_context is not None:
        special_result = await _dispatch_special_mcp_tool(
            name, arguments, jobs, status_mcp_tool_names, request_context
        )
    else:
        special_result = await _dispatch_special_mcp_tool(
            name, arguments, jobs, status_mcp_tool_names
        )
    if special_result is not None:
        return special_result
    bwave_result = await _dispatch_bwave(name, arguments)
    if bwave_result is not None:
        return _prepend_changed_health_alert(bwave_result)
    args = (name, arguments, dict(mcp_tool_def), mcp_tool_call_counts, jobs)
    result = (
        await _dispatch_booley_mcp_tool(*args)
        if request_context is None
        else await _dispatch_booley_mcp_tool(*args, request_context=request_context)
    )
    return _prepend_changed_health_alert(result)


def _build_mcp_application(
    mcp_tools: list[dict[str, Any]],
    discovery_errors: list[str],
    lifetime: _McpLifetime,
    *,
    live: bool = False,
) -> McpApplication:
    """Build the SDK-independent catalog and endpoint dispatcher."""
    from booley.targets.flow_names import canonical

    mcp_tool_index = _build_mcp_tool_index(mcp_tools)
    status_mcp_tool_names: list[str] = []
    jobs = _JobManager(lifetime)
    mcp_tool_call_counts: dict[str, int] = collections.defaultdict(int)

    async def dispatch(
        name: str,
        arguments: dict[str, Any],
        mcp_tool_def: Mapping[str, Any],
    ) -> McpToolContent | McpInputRequired:
        return await _dispatch_application_tool(
            name,
            arguments,
            mcp_tool_def,
            jobs=jobs,
            status_mcp_tool_names=status_mcp_tool_names,
            mcp_tool_call_counts=mcp_tool_call_counts,
        )

    async def request_dispatch(
        name: str,
        arguments: dict[str, Any],
        mcp_tool_def: Mapping[str, Any],
        context: McpRequestContext,
    ) -> McpToolContent | McpInputRequired:
        return await _dispatch_application_tool(
            name,
            arguments,
            mcp_tool_def,
            jobs=jobs,
            status_mcp_tool_names=status_mcp_tool_names,
            mcp_tool_call_counts=mcp_tool_call_counts,
            request_context=context,
        )

    application = McpApplication(
        catalog.visible_definitions(_exposure(), mcp_tools),
        dispatch=dispatch,
        canonicalize=canonical,
        on_discovery_error=discovery_errors.append,
        request_dispatch=request_dispatch,
    )
    status_mcp_tool_names.extend(
        tool.name for tool in application.list_tools() if tool.name in mcp_tool_index
    )

    def publish_names(endpoints: list[dict[str, Any]]) -> None:
        advertised = {tool.name for tool in application.list_tools()}
        endpoint_index = _build_mcp_tool_index(endpoints)
        status_mcp_tool_names[:] = sorted(advertised.intersection(endpoint_index))

    if live:
        from booley.core.checkout_role import SourceCheckoutProjectError

        # Schema-only adapters may supply definitions without selecting a Project.
        # Real discovery still enforces the source-checkout guard.
        with contextlib.suppress(SourceCheckoutProjectError):
            application.live_catalog = LiveCatalog(
                application,
                mcp_tools,
                definitions_provider=lambda endpoints: catalog.visible_definitions(
                    _exposure(), endpoints
                ),
                builtin_provider=lambda discovered: catalog.discover_builtin_definitions(
                    discovered, _exposure()
                ),
                visible=lambda name: catalog.tool_visible(name, _exposure()),
                on_publish=publish_names,
            )

    return application


def _build_mcp_catalog(
    lifetime: _McpLifetime,
) -> tuple[McpApplication, list[dict[str, Any]], list[str]]:
    """Discover and validate the catalog shared by live startup and probes."""
    from booley.core.checkout_role import SourceCheckoutProjectError

    directory = None
    with contextlib.suppress(SourceCheckoutProjectError):
        directory = selected_directory()
    initial_fingerprint = project_fingerprint(directory)
    mcp_tools, discovery_errors = _discover_booley_mcp_tools()
    logger.info("Discovered %d Booley MCP tools", len(mcp_tools))
    application = _build_mcp_application(mcp_tools, discovery_errors, lifetime, live=True)
    if (
        application.live_catalog is not None
        and application.live_catalog.fingerprint() != initial_fingerprint
    ):
        raise RuntimeError("MCP catalog inputs changed during startup; retry startup")
    return application, mcp_tools, discovery_errors


def build_mcp_probe_payload() -> dict[str, Any]:
    """Build the real Interactive Mode catalog and return Doctor's payload."""
    _maybe_configure_interactive_logs_dir()
    _load_agent_settings_from_toml()
    application, _mcp_tools, discovery_errors = _build_mcp_catalog(_McpLifetime(None, None))
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    return {
        "tools": [tool.name for tool in application.list_tools()],
        "errors": discovery_errors,
        "logs_dir": logs_dir,
        "logs_dir_ok": "/.booley/.interactive_logs/" in logs_dir.replace("\\", "/"),
    }


def _sdk_tool_result(
    payload: McpToolContent | McpInputRequired,
) -> CallToolResult | InputRequiredResult:
    """Project transport-neutral results at the SDK boundary."""
    if isinstance(payload, McpInputRequired):
        return InputRequiredResult(
            request_state=payload.request_state,
            input_requests={
                payload.response_key: ElicitRequest(
                    method="elicitation/create",
                    params=ElicitRequestFormParams(
                        mode="form",
                        message=payload.message,
                        requested_schema=payload.form_schema,
                    ),
                )
            },
        )
    return CallToolResult(
        content=[TextContent(type="text", text=block.text) for block in payload.content],
        structuredContent=payload.structured_content,
        isError=payload.is_error,
    )


async def _call_application_tool(
    application: McpApplication,
    params: CallToolRequestParams,
    context: ServerRequestContext | None = None,
    observer: SessionObserver | None = None,
) -> CallToolResult | InputRequiredResult:
    """Translate one application result or failure into MCP SDK types."""
    request = await _observed_request_context(context, params, observer)
    try:
        if context is None and observer is None:
            payload = await application.call_tool(params.name, params.arguments or {})
        else:
            payload = await application.call_tool(
                params.name, params.arguments or {}, request_context=request
            )
        payload = await _observe_tool_completion(payload, request, params.name, observer)
        return _sdk_tool_result(payload)
    except UnknownMcpToolError as exc:
        if observer is not None:
            await _record_presence(observer, request.attribution, params.name, "unknown-tool")
        raise MCPError(
            INVALID_PARAMS,
            f"Unknown MCP tool: {exc.name}",
        ) from None
    except MCPError:
        raise
    except Exception as exc:
        if observer is not None:
            await _record_presence(observer, request.attribution, params.name, "error")
        logger.exception("Unexpected MCP tool failure for %s", params.name)
        message = "Internal server error"
        raise MCPError(INTERNAL_ERROR, message) from exc


async def _record_presence(
    observer: SessionObserver,
    facts: Attribution | None,
    tool: str,
    outcome: str,
) -> None:
    """Presence is advisory: storage defects never replace an application result."""
    try:
        await observer.record(facts, tool, outcome)
    except Exception:
        logger.debug("Session completion observation unavailable", exc_info=True)


async def _observe_tool_completion(
    payload: McpToolPayload | McpInputRequired,
    request: McpRequestContext,
    tool: str,
    observer: SessionObserver | None,
) -> McpToolPayload | McpInputRequired:
    """Add at most one shared warning without changing successful tool payloads."""
    if observer is not None:
        outcome = (
            "input-required"
            if isinstance(payload, McpInputRequired)
            else ("error" if payload.is_error else "completed")
        )
        await _record_presence(observer, request.attribution, tool, outcome)
    if not payload.goal_aware:
        return payload
    warning = request.presentation_warning
    if observer is not None:
        try:
            _, warning = await observer.shared(request.attribution)
        except Exception:
            logger.debug("Session completion presentation unavailable", exc_info=True)
    text = (
        payload.message
        if isinstance(payload, McpInputRequired)
        else "\n".join(block.text for block in payload.content)
    )
    if not warning or warning in text.splitlines():
        return payload
    if isinstance(payload, McpInputRequired):
        return replace(payload, message=payload.message + "\n" + warning)
    return replace(payload, content=(*payload.content, McpTextBlock(warning)))


async def _observed_request_context(
    context: ServerRequestContext | None,
    params: CallToolRequestParams,
    observer: SessionObserver | None,
) -> McpRequestContext:
    """Unexpected advisory failures cannot prevent application dispatch."""
    try:
        return await _resolve_observed_request_context(context, params, observer)
    except Exception:
        logger.debug("Session request observation unavailable", exc_info=True)
        return (
            McpRequestContext()
            if context is None
            else _application_request_context(context, params)
        )


async def _resolve_observed_request_context(
    context: ServerRequestContext | None,
    params: CallToolRequestParams,
    observer: SessionObserver | None,
) -> McpRequestContext:
    request = (
        McpRequestContext() if context is None else _application_request_context(context, params)
    )
    if observer is None:
        return request
    client = context.session.client_params if context is not None else None
    name = client.client_info.name if client is not None else ""
    meta = dict(params.meta) if params.meta is not None else {}
    facts = await observer.attribution(
        params.arguments or {},
        meta,
        name,
        request=context.request if context is not None else None,
        tool=params.name,
    )
    if params.name != "tools/list":
        facts = await observer.resolved(facts)
    keys, warning = await observer.shared(facts)
    return replace(
        request, attribution=facts, other_session_keys=keys, presentation_warning=warning
    )


def _application_request_context(
    context: ServerRequestContext, params: CallToolRequestParams
) -> McpRequestContext:
    """Only SDK-boundary-verified request state enters application dispatch."""
    capabilities = context.session.client_capabilities
    form = (
        capabilities is not None
        and capabilities.elicitation is not None
        and capabilities.elicitation.form is not None
    )
    responses = (
        None
        if params.input_responses is None
        else {
            key: value.model_dump(mode="json", by_alias=False)
            for key, value in params.input_responses.items()
        }
    )
    return McpRequestContext(context.protocol_version, form, params.request_state, responses)


def _build_sdk_server(application: McpApplication, lifetime: _McpLifetime) -> Server:
    """Adapt the Booley MCP application to the MCP SDK server callbacks."""

    async def handle_list_tools(
        _ctx: ServerRequestContext,
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        lifetime.mark_activity()
        if application.live_catalog is not None:
            await application.live_catalog.refresh()
        request = await _observed_request_context(
            _ctx, CallToolRequestParams(name="tools/list", meta=_ctx.meta), lifetime.sessions
        )
        await lifetime.sessions.record(request.attribution, "tools/list", "completed")
        return ListToolsResult(tools=[_sdk_tool(tool) for tool in application.list_tools()])

    async def handle_call_tool(
        _ctx: ServerRequestContext,
        params: CallToolRequestParams,
    ) -> CallToolResult | InputRequiredResult:
        lifetime.mark_mcp_endpoint_start()
        try:
            from booley.targets.flow_names import canonical

            catalog = application.live_catalog
            if catalog is not None and catalog.needs_refresh(canonical(params.name)):
                await catalog.refresh()
            return await _call_application_tool(application, params, _ctx, lifetime.sessions)
        finally:
            lifetime.mark_mcp_endpoint_end()

    changes = (
        ToolChanges(application.live_catalog) if application.live_catalog is not None else None
    )
    server_type = ChangingToolsServer if changes is not None else Server
    server = server_type(
        "booley",
        version=__version__,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
        **(
            {"lifespan": changes.lifespan, "on_subscriptions_listen": changes.listen}
            if changes is not None
            else {}
        ),
    )
    if changes is not None:
        server.middleware.append(changes.remember_legacy_peer)
    server.middleware.append(
        RequestStateBoundary(
            RequestStateSecurity.ephemeral(audience="booley"), default_audience="booley"
        )
    )
    return server


def _build_server(
    lifetime: _McpLifetime | None = None,
) -> tuple[Server, list[dict[str, Any]]]:
    """Build the MCP v2 adapter around the Booley MCP application."""
    _reconcile_orphaned_jobs()
    lifetime = lifetime or _McpLifetime(None, None)
    application, mcp_tools, discovery_errors = _build_mcp_catalog(lifetime)
    if discovery_errors:
        _log_discovery_errors(mcp_tools, discovery_errors)
    server = _build_sdk_server(application, lifetime)
    return server, mcp_tools


def _sdk_tool(tool: McpToolDefinition) -> McpSdkTool:
    """Convert one Booley-owned definition at the MCP SDK seam."""
    return McpSdkTool(
        name=tool.name,
        description=tool.description,
        inputSchema=tool.input_schema,
        outputSchema=tool.output_schema,
    )


def _interactive_session_id() -> str:
    """Stable per-tab identifier for the Interactive Mode logs directory.

    The container hostname is Docker's short container ID by default — so
    this also identifies which tab the logs belong to.  Falls back to the
    MCP server's PID when the hostname is missing or degenerate (e.g.
    running directly on the host instead of in the sandbox container).
    """
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""
    if hostname and hostname != "localhost":
        return hostname
    return f"pid{os.getpid()}"


def _maybe_configure_interactive_logs_dir() -> None:
    """In Interactive Mode, point ``BOOLEY_LOGS_DIR`` at the project tree.

    Without this, transcripts and ``display.jsonl`` from MCP-driven endpoint
    calls land in an OS tempdir (see ``_resolve_transcript_dir``), which
    disappears on container exit. An explicitly configured ``BOOLEY_LOGS_DIR``
    takes precedence over this default.

    Interactive Mode is detected by:
      - ``BOOLEY_LOGS_DIR`` not already set;
      - ``.booley/`` present in CWD (the Project Root the outer agent
        bind-mounted into the container at ``/work``).

    Logs accumulate at ``.booley/.interactive_logs/<session-id>/``
    (gitignored — see the template emitted by ``booley init``).
    """
    if os.environ.get("BOOLEY_LOGS_DIR"):
        return
    booley_dir = Path.cwd() / ".booley"
    if not booley_dir.is_dir():
        return
    session_id = _interactive_session_id()
    logs_dir = booley_dir / ".interactive_logs" / session_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BOOLEY_LOGS_DIR"] = str(logs_dir)
    logger.info(
        "Interactive Mode: BOOLEY_LOGS_DIR=%s (session %s)",
        logs_dir,
        session_id,
    )


def _load_agent_settings_from_toml() -> None:
    """Honor ``[agent]``/``[sandbox]``/``[models]`` from the project's booley.toml.

    Loading configuration here makes the server honor booley.toml consistently
    with ``booley doctor``. (Specialist endpoint
    subprocesses resolve the provider independently — ``get_backend_config()``
    reads ``BOOLEY_DIR/booley.toml`` when the env hand-off is absent —
    so they no longer silently fall back to a default provider either.)

    ``BOOLEY_DIR`` points at the ``.booley`` dir when set, but
    ``load_agent_settings`` wants the Project Root that *contains* it, so use the
    parent (else CWD). A missing/unparseable booley.toml is handled inside
    ``load_agent_settings`` (defaults preserved); the broad guard only covers
    import-time surprises so a config hiccup never blocks server startup.
    """
    try:
        from booley.config.settings import load_agent_settings

        booley_dir = os.environ.get("BOOLEY_DIR", "")
        project_root = Path(booley_dir).parent if booley_dir else Path.cwd()
        load_agent_settings(project_root)
    except Exception:
        logger.debug("Failed to load agent settings from booley.toml", exc_info=True)


async def _main() -> None:
    _maybe_configure_interactive_logs_dir()
    _load_agent_settings_from_toml()
    lifetime = _McpLifetime.from_env()
    server, _ = _build_server(lifetime)
    async with stdio_server() as (read_stream, write_stream):
        run_task = asyncio.create_task(
            server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            ),
        )
        maintenance = asyncio.create_task(lifetime.sessions.maintenance_loop())
        watchdog_task = asyncio.create_task(lifetime.wait_until_stale())
        done, pending = await asyncio.wait(
            {run_task, watchdog_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        lifetime.sessions.close()
        maintenance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance
        if run_task in done:
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
            await run_task
            return

        reason = watchdog_task.result()
        logger.info("Interactive MCP server exiting: %s", reason)
        run_task.cancel()
        for task in pending:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


def _run_http(port: int) -> None:
    """Serve the MCP tools over protected streamable HTTP on loopback.

    Interactive Mode transport (ADR 0023): started by the devcontainer's
    ``postStartCommand`` (via ``incontainer_register.ensure_http_server``), so
    a container stop→start transparently brings the endpoint back — the agent
    apps hold only a URL, not a child process.
    """
    import uvicorn

    _maybe_configure_interactive_logs_dir()
    _load_agent_settings_from_toml()
    lifetime = _McpLifetime.from_env(self_exit=False)
    server, _ = _build_server(lifetime)
    app = _streamable_http_app(server)
    app = SessionMaintenanceApp(app, lifetime.sessions)
    logger.info("Interactive MCP server (HTTP) on %s:%d", _HTTP_HOST, port)
    uvicorn.run(app, host=_HTTP_HOST, port=port, log_level="warning")


def _streamable_http_app(server: Server):
    """Build Booley's JSON-response HTTP adapter with loopback-only headers."""
    from mcp.server.transport_security import TransportSecuritySettings

    security = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    return PeerBoundary(
        server.streamable_http_app(
            streamable_http_path=HTTP_ENDPOINT_PATH,
            json_response=True,
            max_request_body_size=4 * 1024 * 1024,
            transport_security=security,
            host=_HTTP_HOST,
        )
    )


def main() -> None:
    import argparse

    from booley.mcp.endpoint_config import EndpointConfigError

    # Runtime-location guard (ADR 0028): the Booley MCP server serves the Session
    # Runtime's MCP-tool stack — it has no meaning host-side.
    location_error = runtime_context.container_only_error("booley-mcp")
    if location_error is not None:
        print(location_error, file=sys.stderr)
        raise SystemExit(2)

    # Codex REPLACES the MCP child env per config.toml [env] (see
    # runtime.mcp_config), so a config that doesn't forward the proxy vars
    # leaves this server — and every endpoint subprocess under it — with no
    # egress path. Self-heal here so the whole endpoint subprocess tree inherits it.
    try:
        if runtime_context.ensure_proxy_env():
            logger.debug("proxy env was absent in-container — defaulted to booley-proxy")
    except runtime_context.EgressModeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("BOOLEY_MCP_TRANSPORT", "stdio"),
        help="stdio: client-spawned child; http: standalone loopback server (Interactive Mode)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"HTTP port (default: ${HTTP_PORT_ENV} or {DEFAULT_HTTP_PORT})",
    )
    args = parser.parse_args()
    try:
        if args.transport == "http":
            _run_http(args.port if args.port is not None else http_port())
        else:
            asyncio.run(_main())
    except EndpointConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _goal_warning_result(
    result: McpToolContent, binding: GoalRunBinding | None, session_key: str | None
) -> McpToolContent:
    if binding is None:
        return result
    try:
        prefix = goal_warnings(
            GoalStore(binding.booley_dir).load(binding.record_id),
            binding.booley_dir,
            work_dir=binding.worktree_root,
            session_key=session_key,
        )
    except GoalStoreError as exc:
        prefix = f"WARNING: Goal Record cannot be read: {exc}"
    payload = result.value if isinstance(result, McpDispatchResult) else result
    if prefix:
        payload = _prepend_health_block(payload, TextContent(type="text", text=prefix))
    return McpDispatchResult(
        value=payload,
        is_error=result.is_error if isinstance(result, McpDispatchResult) else False,
        goal_aware=True,
    )


async def _dispatch_poll(arguments: dict[str, Any], jobs: _JobManager) -> McpToolContent:
    try:
        return await _dispatch_poll_impl(arguments, jobs)
    except jobrec.JobRecordError as exc:
        return _error_result(str(exc))


async def _dispatch_cancel(arguments: dict[str, Any], jobs: _JobManager) -> McpToolContent:
    try:
        return await _dispatch_cancel_impl(arguments, jobs)
    except jobrec.JobRecordError as exc:
        return _error_result(str(exc))


if __name__ == "__main__":
    main()
