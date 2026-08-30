"""Booley MCP server — exposes Flows and Specialists as structured MCP tools.

Two transports (``--transport``, or ``BOOLEY_MCP_TRANSPORT``):

- ``stdio`` (default) — spawned by the agent app / SDK as a child process
  inside Docker. Used by Ticket Mode, where the harness owns the child and
  restarts it together with the run.
- ``http`` — a stateless streamable-HTTP server on ``127.0.0.1``. Used by
  Interactive Mode (ADR 0023): the devcontainer ``postStartCommand`` starts it
  on every container start (including resume), and the agent app *connects*
  to a URL instead of owning a child process — so a devcontainer stop→start
  no longer strands the client with a dead stdio pipe it never re-spawns.
  Stateless matters: Claude Code caches its ``Mcp-Session-Id`` across a server
  restart, and a stateless server accepts such requests instead of 404-ing.

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
import socket
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ServerCapabilities, TextContent, ToolsCapability
from mcp.types import Tool as McpSdkTool

from booley import __version__
from booley.core.boundary import BoundaryError, require_finite_number
from booley.runtime import job_records as jobrec
from booley.runtime import job_slots, runtime_context
from booley.runtime.build_metadata import format_status_line
from booley.runtime.heartbeat import REAPER_HEARTBEAT_PATH, touch_reaper_heartbeat
from booley.runtime.mcp_config import (
    DEFAULT_HTTP_PORT,
    HTTP_ENDPOINT_PATH,
    HTTP_PORT_ENV,
    http_port,
)
from booley.runtime.pid import is_pid_alive
from booley.runtime.process_group import (
    ProcessGroup,
    capture_process_group,
    force_async_process_group,
    force_async_process_group_now,
    new_group_kwargs,
    terminate_adopted_process_group,
    terminate_async_process_group,
)
from booley.runtime.timefmt import compact_utc_now, format_human_datetime, utc_now_rfc3339
from booley.ticket_board.paths import existing_ticket_runtime_file, ticket_runtime_dir

logger = logging.getLogger(__name__)

# Route logging to stderr — stdout is the MCP JSON-RPC pipe.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[mcp] %(levelname)s %(message)s",
)

# Max stdout/stderr bytes to include in MCP result (avoid blowing up context).
# Env-overridable via BOOLEY_MCP_MAX_STDOUT_BYTES / BOOLEY_MCP_MAX_STDERR_BYTES
# — see _max_stdout_bytes() / _max_stderr_bytes().
_DEFAULT_MAX_STDOUT_BYTES = 12_000
_DEFAULT_MAX_STDERR_BYTES = 4_000

# MCP tools used only by autonomous development. Interactive Mode is driven by
# the outer client, so exposing these just adds noise and invites the wrong
# workflow. Keeping the reason next to the name is not decoration: these MCP tools
# are discovered for Ticket Mode but intentionally absent from Interactive
# Mode, so a human diffing the two surfaces deserves an answer instead of
# "Unknown MCP tool".
_INTERACTIVE_HIDDEN_REASONS = {
    "tb_coder": (
        "it writes testbench code as a step of an autonomous ticket run — "
        "interactively, edit the testbench directly"
    ),
    "submit_run_report": (
        "it finalizes an autonomous ticket run by writing REPORT.md for the "
        "human reviewer — only the Developer Agent in Ticket Mode calls it"
    ),
}
_INTERACTIVE_MCP_EXCLUDED = frozenset(_INTERACTIVE_HIDDEN_REASONS)
_STATUS_MCP_TOOL_NAME = "booley_status"
_STATUS_MCP_TOOL_DESCRIPTION = "Show whether Booley Interactive Mode is ready in this tab."
_REPORT_MCP_TOOL_NAME = "booley_report"
_REPORT_MCP_TOOL_DESCRIPTION = (
    "Fetch the most recent completed run report for a Booley endpoint (e.g. "
    "sim) from disk — including after a client-side timeout, when the "
    "inline MCP result was lost but the run finished server-side. Pass the "
    "optional 'endpoint' argument to target a specific endpoint's latest report; "
    "omit it for the most recent report across all endpoints."
)
_POLL_MCP_TOOL_NAME = "booley_poll"
_POLL_MCP_TOOL_DESCRIPTION = (
    "Check on a long-running endpoint (sim / fpga / synth / reviewer / "
    "mutation_tester / coverage_analyst) that was "
    "started in the background. When such an endpoint takes longer than "
    "the inline wait, its call returns a 'run_id' instead of the result; pass "
    "that run_id here to get the current status. Pass 'wait_seconds' (0-270) to "
    "long-poll: the call blocks up to that long for the run to finish, turning "
    "a poll-loop into one blocking call; if the run is still going afterwards "
    "it returns 'RUNNING' — call again to keep waiting. Prefer few long polls "
    "over many short ones. Claude and Codex: omit 'wait_seconds' to use the "
    "Session Runtime's configured poll window. Codex: if its programmatic exec "
    "yields while this MCP call is still running, keep waiting on the same "
    "running cell in short slices; do not start another booley_poll call. When the "
    "run finishes it returns the full result (EXIT_CODE + report), exactly as "
    "the original call would have. Safe to call across a server restart: the "
    "job is tracked on disk, so a poll always returns a definite answer."
)
_CANCEL_MCP_TOOL_NAME = "booley_cancel"
_CANCEL_MCP_TOOL_DESCRIPTION = (
    "Cancel a QUEUED or RUNNING background job (pass the run_id a submit or "
    "poll reported). Running process groups receive SIGTERM, then SIGKILL after "
    "a bounded grace period. Polling the run_id afterwards reports the distinct "
    "CANCELLED terminal outcome."
)
_TARGETS_MCP_TOOL_NAME = "booley_targets"
_TARGETS_MCP_TOOL_DESCRIPTION = (
    "List the project's FuseSoC .core Targets — the values a Booley Flow's "
    "'target' argument accepts (ADR 0030). Cheap .core-YAML enumeration, no "
    "fusesoc run: per Target it reports the copy-pasteable selector "
    "(vlnv#name when the bare name is ambiguous), flow (sim/lint/generic), "
    "declared EDA tool, cocotb module, declared toplevel, which Doctor Flows the "
    "Target selects via flow_options.booley.doctor, and which Booley Flows could drive it. "
    "Optional filters: 'for_flow' (one of synth, fpga, sim, lint) "
    "keeps only Targets that Flow could drive; "
    "'glob' matches the bare name or vendor:library:name#target (e.g. "
    "'soc*', '*#lint'). Returns JSON."
)
_SLEEP_MCP_TOOL_NAME = "booley_sleep"
_SLEEP_MCP_TOOL_DESCRIPTION = (
    "Diagnostic MCP tool (exposed only when BOOLEY_MCP_DEBUG_TOOLS is set): hold "
    "this MCP tool call open for 'seconds' server-side, then return timing "
    "details. Exists to measure the MCP client's MCP-tool-call kill ceiling "
    "(ADR 0027) — it does no RTL work and is never useful for a ticket."
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
_DEFAULT_JOB_POLL_WAIT_SECONDS = 240.0

# Interactive MCP servers are Docker containers spawned by Codex/Claude tabs.
# Clients do not always tear them down promptly, so the server exits itself
# after sitting idle. Set BOOLEY_MCP_IDLE_TIMEOUT_SECONDS=0 to disable.
_DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS = 2 * 60 * 60
_DEFAULT_INTERACTIVE_MAX_AGE_SECONDS = 12 * 60 * 60
_MCP_WATCHDOG_POLL_SECONDS = 30.0

# HTTP transport (Interactive Mode, ADR 0023). Loopback only — the URL is
# reachable from agent apps inside the same container, never from the host.
_HTTP_HOST = "127.0.0.1"


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


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive-integer env var; invalid or non-positive falls back.

    Same tolerant style as ``_env_timeout_seconds`` (warn + default on garbage),
    except non-positive values also fall back: a zero/negative byte cap would
    render every MCP tool result empty, which is never what an operator wants.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r", name, raw)
        return default
    return value


def _max_stdout_bytes() -> int:
    """Byte cap for the stdout section of an MCP tool result."""
    return _env_positive_int("BOOLEY_MCP_MAX_STDOUT_BYTES", _DEFAULT_MAX_STDOUT_BYTES)


def _max_stderr_bytes() -> int:
    """Byte cap for the stderr section of an MCP tool result."""
    return _env_positive_int("BOOLEY_MCP_MAX_STDERR_BYTES", _DEFAULT_MAX_STDERR_BYTES)


# The reaper (booley.docker.reaper) reads this epoch-seconds heartbeat via
# ``docker exec`` to decide if a session container is idle (ADR 0018 WS2/WS4).
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
            # Ticket Mode stdio server: the client owns the process lifetime
            # (no self-exit), but MCP tool activity still feeds the container
            # reaper heartbeat so an active ticket never reads as idle
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


def _count_orphaned_starts(display_path: Path) -> dict[str, int]:
    """Count unmatched endpoint_start events in display.jsonl."""
    try:
        lines = display_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    unmatched: dict[str, int] = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        endpoint = ev.get("endpoint", "")
        if not endpoint:
            continue
        if ev.get("type") == "endpoint_start":
            unmatched[endpoint] = unmatched.get(endpoint, 0) + 1
        elif ev.get("type") == "endpoint_end" and unmatched.get(endpoint, 0) > 0:
            unmatched[endpoint] -= 1
    return unmatched


def _reconcile_orphaned_locks() -> None:
    """Write endpoint_end for any orphaned endpoint_start from a prior session.

    Only safe to run from an outer (TOP_LEVEL) bootstrap context. Nested
    servers spawned mid-session (e.g. a sub-agent's MCP server inside the
    sandbox container) would see the parent's legitimately in-flight
    endpoint_start as "unmatched" and emit spurious endpoint_end events. See
    ``booley.runtime.bootstrap_mode`` for the chokepoint that gates this.
    """
    from booley.runtime.bootstrap_mode import should_run_outer_bookkeeping

    if not should_run_outer_bookkeeping():
        return
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_dir:
        return
    display_path = existing_ticket_runtime_file(logs_dir, "display.jsonl")
    if not display_path.exists():
        return

    unmatched = _count_orphaned_starts(display_path)
    now_ts = utc_now_rfc3339()
    reconciled = [
        json.dumps(
            {
                "type": "endpoint_end",
                "endpoint": endpoint,
                "exit_code": 2,
                "duration_s": 0,
                "report_text": "Reconciled: orphaned lock from prior session",
                "timestamp": now_ts,
            }
        )
        for endpoint, count in unmatched.items()
        for _ in range(count)
    ]
    if not reconciled:
        return
    try:
        with display_path.open("a", encoding="utf-8") as f:
            for line in reconciled:
                f.write(line + "\n")
        logger.info(
            "Reconciled %d orphaned endpoint event(s) from prior session",
            len(reconciled),
        )
    except OSError:
        logger.warning("Failed to reconcile orphaned locks", exc_info=True)


def _reconcile_orphaned_jobs() -> None:
    """Make any 'running' job record with nothing behind it terminal (ADR 0027).

    A detached job's background task dies with the server, and its subprocess is
    SIGKILLed on shutdown — so after a restart a record left at ``running`` no
    longer has anything running behind it. Marking it terminal here means a poll
    (or status query) gets a definite answer instead of forever reporting a
    ghost as in-flight. Liveness is judged by ``derive_status`` (PID + deadline
    + argv identity, so a recycled PID cannot keep a ghost alive), and a dead
    job whose *fresh* report proves the run actually completed adopts the run's
    real exit code rather than a blanket failure. Same TOP_LEVEL gate as the
    lock reconcile: nested specialist servers share the runtime tree and must
    not adjudicate each other's records.
    """
    from booley.runtime.bootstrap_mode import should_run_outer_bookkeeping

    if not should_run_outer_bookkeeping():
        return
    for rec in jobrec.list_records():
        if rec.status != jobrec.STATUS_RUNNING:
            continue
        if jobrec.derive_status(rec, is_pid_alive) == jobrec.STATUS_RUNNING:
            continue  # genuinely survived the restart — leave it be
        report, report_fresh = _job_report(rec)
        report_exit = report.get("exit_code") if report_fresh and report else None
        if isinstance(report_exit, int):
            # The run finished and wrote this job's report; only the server
            # died before recording. Adopt the run's real outcome instead of
            # branding a completed (possibly passing) run as failed.
            rec.exit_code = report_exit
            rec.status = jobrec.terminal_status(report_exit, timed_out=False)
        else:
            rec.status = jobrec.STATUS_FAILED
            if rec.exit_code is None:
                rec.exit_code = 2
        jobrec.write_record(rec)
        logger.info("Reconciled orphaned job %s from prior session", rec.run_id)


# ---------------------------------------------------------------------------
# MCP tool discovery
# ---------------------------------------------------------------------------


def _discover_booley_mcp_tools() -> tuple[list[dict[str, Any]], list[str]]:
    """Import endpoint classes and extract MCP tool definitions.

    Uses ``discover_mcp_tools()`` for filtering (respects booley.toml allowlists),
    then imports + extracts schemas only for surviving endpoints.
    Handles built-ins and custom endpoints from ``.booley_project/mcp_tools/``.

    Returns (MCP tools, errors) where:
      - MCP tools: list of dicts {name, module, description, schema}
      - errors: list of human-readable failure messages (empty on success)
    """
    from booley.mcp.registry import discover_mcp_tools

    project_mcp_tools_dir = get_project_mcp_tools_dir()
    mcp_tool_config, flow_config = _get_endpoint_config()
    filtered_endpoints = discover_mcp_tools(
        project_mcp_tools_dir=project_mcp_tools_dir,
        mcp_tool_config=mcp_tool_config,
        flow_config=flow_config,
    )
    allowed_names = {t.name for t in filtered_endpoints}

    custom_mcp_tool_paths = _index_custom_mcp_tools(project_mcp_tools_dir, allowed_names)
    builtin_results, builtin_errors = _discover_builtin_mcp_tools(filtered_endpoints)
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


def _nested_allowlist() -> set[str] | None:
    """Return the nested-mode MCP tool allowlist, or None if not nested.

    Set by ``_ensure_nested_codex_home`` via the BOOLEY_NESTED_MCP_TOOLS env
    var (comma-separated). When BOOLEY_NESTED_AGENT=1, return a (possibly
    empty) set of allowed MCP tool names — discovery filters strictly to these,
    so Specialist MCP tools never appear in nested agents (recursion-safe).
    When BOOLEY_NESTED_AGENT is unset, return None (= developer mode,
    no filtering).
    """
    if os.environ.get("BOOLEY_NESTED_AGENT") != "1":
        return None
    raw = os.environ.get("BOOLEY_NESTED_MCP_TOOLS", "")
    return _split_mcp_tool_csv(raw)


def _split_mcp_tool_csv(raw: str) -> set[str]:
    """Parse a comma-separated MCP tool list from an env var."""
    from booley.targets.flow_names import canonical_set

    return canonical_set(t.strip() for t in raw.split(",") if t.strip())


def _explicit_mcp_allowlist() -> set[str] | None:
    """Return a first-class MCP exposure allowlist, if configured.

    ``BOOLEY_MCP_TOOLS`` filters *MCP exposure only*. It does not affect the
    Booley endpoint registry or autonomous execution.
    """
    raw = os.environ.get("BOOLEY_MCP_TOOLS")
    if raw is None:
        return None
    return _split_mcp_tool_csv(raw)


def _mcp_tool_visible(mcp_tool_name: str) -> bool:
    """Return whether *mcp_tool_name* should be exposed by this MCP server.

    Precedence:
    1. Nested runs use their recursion-safe specialist allowlist.
    2. ``BOOLEY_MCP_TOOLS`` is an explicit MCP-only allowlist.
    3. ``BOOLEY_MCP_MODE=interactive`` hides autonomous-only workflow MCP tools.
    4. Default/autonomous mode exposes the normal discovered registry.
    """
    from booley.targets.flow_names import canonical

    mcp_tool_name = canonical(mcp_tool_name)
    nested_allow = _nested_allowlist()
    if nested_allow is not None:
        return mcp_tool_name in nested_allow

    explicit_allow = _explicit_mcp_allowlist()
    if explicit_allow is not None:
        return mcp_tool_name in explicit_allow

    if os.environ.get("BOOLEY_MCP_MODE", "").strip().lower() == "interactive":
        return mcp_tool_name not in _INTERACTIVE_MCP_EXCLUDED

    return True


def _interactive_mcp_mode() -> bool:
    """Return whether this server is serving a human interactive tab."""
    return os.environ.get("BOOLEY_MCP_MODE", "").strip().lower() == "interactive"


def _interactive_hidden_note(mcp_tool_name: str) -> str | None:
    """Explain an MCP tool this tab hides on purpose, or None if it is not one.

    Explains intentional Interactive Mode hiding when somebody calls the
    missing MCP tool.
    """
    if not _interactive_mcp_mode() or mcp_tool_name not in _INTERACTIVE_MCP_EXCLUDED:
        return None
    return (
        f"{mcp_tool_name} is hidden in Interactive Mode: "
        f"{_INTERACTIVE_HIDDEN_REASONS[mcp_tool_name]}. Ticket Mode runs still get it."
    )


def _status_mcp_tool_visible() -> bool:
    """Return whether the synthetic status MCP tool should be exposed."""
    nested_allow = _nested_allowlist()
    if nested_allow is not None:
        return _STATUS_MCP_TOOL_NAME in nested_allow
    if _interactive_mcp_mode():
        return True
    explicit_allow = _explicit_mcp_allowlist()
    return explicit_allow is not None and _STATUS_MCP_TOOL_NAME in explicit_allow


def _report_mcp_tool_visible() -> bool:
    """Return whether the synthetic report-fetch MCP tool should be exposed.

    Same gating as the status MCP tool: always on for a human/agent interactive
    tab (its whole point is recovering a report after a client-side timeout),
    opt-in via nested/explicit allowlists otherwise.
    """
    nested_allow = _nested_allowlist()
    if nested_allow is not None:
        return _REPORT_MCP_TOOL_NAME in nested_allow
    if _interactive_mcp_mode():
        return True
    explicit_allow = _explicit_mcp_allowlist()
    return explicit_allow is not None and _REPORT_MCP_TOOL_NAME in explicit_allow


def _poll_mcp_tool_visible() -> bool:
    """Return whether the synthetic poll MCP tool should be exposed.

    Always on. The poll MCP tool is the transport-level completion of async
    dispatch (ADR 0027): any server that can dispatch a heavy endpoint can hand back
    a run_id, so the caller must always be able to poll it — in EVERY mode,
    including the Ticket-Mode developer (Q1: modes must not differ, unlike
    ``booley_report`` which is Interactive/opt-in). It reads job status only —
    it cannot invoke an endpoint or recurse — so it is exempt from the
    recursion-safety allowlists that gate real endpoints.
    """
    return True


def _targets_mcp_tool_visible() -> bool:
    """Return whether the synthetic targets-listing MCP tool should be exposed.

    Always on, like poll: it only reads ``.core`` YAML and booley.toml — no
    subprocess, no recursion — so the recursion-safety allowlists that gate
    real endpoints do not apply, and every mode's agent (interactive tab, ticket
    developer, nested specialist) needs to know what ``target`` values exist.
    """
    return True


def _sleep_mcp_tool_visible() -> bool:
    """Return whether the diagnostic sleep MCP tool should be exposed.

    Off unless ``BOOLEY_MCP_DEBUG_TOOLS`` is truthy. When on it is visible in
    EVERY mode (like poll): the whole point is measuring the client-side call
    cap in the exact tab/session shape under suspicion, so allowlists must not
    filter it. It only sleeps — no subprocess, no recursion — so the
    recursion-safety exemption that covers poll/cancel applies.
    """
    raw = os.environ.get("BOOLEY_MCP_DEBUG_TOOLS", "").strip().lower()
    return raw not in {"", "0", "false", "no"}


def _discover_builtin_mcp_tools(
    discovered: list[Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Import built-in endpoint modules and extract schemas.

    Returns (results, errors).
    """
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for info in discovered:
        path = Path(info.path)
        if path.is_absolute():
            continue
        package = path.parent.as_posix().replace("/", ".")
        module_name = path.stem
        mcp_tool_def, err = _import_and_extract(module_name, import_prefix=f"booley.{package}")
        if err is not None:
            errors.append(err)
        if mcp_tool_def is not None:
            if not _mcp_tool_visible(mcp_tool_def["name"]):
                continue
            results.append(mcp_tool_def)
    return results, errors


def _discover_custom_mcp_tools(
    custom_mcp_tool_paths: dict[str, Path],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Import custom MCP tool files and extract schemas.

    Returns (results, errors).
    """
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for _mcp_tool_name, py_path in custom_mcp_tool_paths.items():
        mcp_tool_def, err = _import_and_extract_file(py_path)
        if err is not None:
            errors.append(err)
        if mcp_tool_def is not None:
            if not _mcp_tool_visible(mcp_tool_def["name"]):
                continue
            results.append(mcp_tool_def)
    return results, errors


def get_project_mcp_tools_dir() -> Path | None:
    """Resolve the project-defined MCP endpoint directory.

    Public API: the harness CLI (booley.py) depends on this name rather than
    reaching into a peer module's private helper (principle 9).
    """
    project_dir = os.environ.get("BOOLEY_PROJECT_DIR", "")
    if project_dir:
        d = Path(project_dir) / "mcp_tools"
        legacy = Path(project_dir) / "tools"
        if legacy.is_dir() and not d.is_dir():
            raise RuntimeError(
                f"{legacy} is retired; rename it to {d} and migrate legacy `Tool` classes "
                "to McpTool, Specialist, or BooleyFlow"
            )
        return d if d.is_dir() else None
    cwd = Path.cwd()
    d = cwd / ".booley_project" / "mcp_tools"
    legacy = cwd / ".booley_project" / "tools"
    if legacy.is_dir() and not d.is_dir():
        raise RuntimeError(
            f"{legacy} is retired; rename it to {d} and migrate legacy `Tool` classes "
            "to McpTool, Specialist, or BooleyFlow"
        )
    return d if d.is_dir() else None


def _get_endpoint_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``[mcp_tools]`` and ``[flows]`` from booley.toml."""
    try:
        import tomllib

        project_dir = os.environ.get("BOOLEY_PROJECT_DIR", "")
        toml_path = (
            Path(project_dir) / "booley.toml"
            if project_dir
            else Path.cwd() / ".booley_project" / "booley.toml"
        )
        if toml_path.exists():
            with toml_path.open("rb") as f:
                cfg = tomllib.load(f)
            legacy = cfg.get("tools")
            if legacy is not None:
                raise ValueError(
                    "booley.toml [tools] is retired; move deterministic settings "
                    "to [flows.*] and Specialist settings to [mcp_tools.*]"
                )
            mcp_tools = cfg.get("mcp_tools", {})
            flows = cfg.get("flows", {})
            return (
                mcp_tools if isinstance(mcp_tools, dict) else {},
                flows if isinstance(flows, dict) else {},
            )
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — unreadable config falls back to empty
        logger.debug("Failed to load endpoint config from booley.toml", exc_info=True)
    return {}, {}


def _find_mcp_tool_class_in_module(
    mod: object,
    source_label: str,
) -> tuple[type, object, dict] | None:
    """Find an McpTool/BooleyFlow/Specialist subclass in *mod*, instantiate it, and extract its schema.

    Returns (cls, instance, schema) or None if nothing found.
    Logs and skips classes whose schema extraction fails.
    """
    from booley.mcp.base import McpTool
    from booley.mcp.schema_extractor import extract_schema

    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if not (
            isinstance(obj, type)
            and issubclass(obj, McpTool)
            and obj is not McpTool
            and hasattr(obj, "name")
            and obj.name
        ):
            continue
        try:
            instance = obj()
            schema_hook = getattr(instance, "mcp_schema", None)
            schema = schema_hook() if callable(schema_hook) else extract_schema(instance._parser)
        except Exception as exc:
            msg = f"SCHEMA EXTRACTION FAILED: {obj.name} in {source_label}: {exc}"
            logger.error(msg, exc_info=True)
            print(f"[mcp] ERROR {msg}", file=sys.stderr, flush=True)
            continue
        return obj, instance, schema
    return None


def _mcp_tool_def_from_class(
    cls: type,
    schema: dict,
    module_name: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build an MCP tool definition dict from an endpoint class and extracted schema."""
    from booley.specialists.specialist import Specialist

    result = {
        "name": cls.name,
        "module": module_name,
        "description": getattr(cls, "description", "") or "",
        "schema": schema,
        "default_timeout": getattr(cls, "default_timeout", 0),
        "is_specialist": issubclass(cls, Specialist),
    }
    result.update(extra)
    return result


def _import_module_safe(
    fqn: str,
) -> tuple[object | None, str | None]:
    """Import *fqn* and return (module, None) or (None, error_msg)."""
    import importlib
    import traceback

    try:
        return importlib.import_module(fqn), None
    except Exception as exc:
        msg = f"IMPORT FAILED: {fqn}: {exc}"
        logger.error(msg, exc_info=True)
        print(f"[mcp] ERROR {msg}", file=sys.stderr, flush=True)
        tb = traceback.format_exc()
        print(f"[mcp] TRACEBACK:\n{tb}", file=sys.stderr, flush=True)
        return None, msg


def _import_and_extract(
    module_name: str,
    *,
    import_prefix: str = "booley.mcp",
) -> tuple[dict[str, Any] | None, str | None]:
    """Import a single endpoint module and extract its schema.

    Returns (mcp_tool_def, error_msg). error_msg is None on success.
    """
    fqn = f"{import_prefix}.{module_name}"
    mod, err = _import_module_safe(fqn)
    if err is not None:
        return None, err

    found = _find_mcp_tool_class_in_module(mod, module_name)
    if found is None:
        msg = f"NO MCP ENDPOINT CLASS FOUND: {fqn} has no recognized MCP endpoint subclass"
        logger.warning(msg)
        return None, msg

    cls, _instance, schema = found
    is_custom = not import_prefix.startswith("booley.")
    return (
        _mcp_tool_def_from_class(
            cls,
            schema,
            module_name,
            is_custom=is_custom,
            module_path=f"{import_prefix}.{module_name}",
        ),
        None,
    )


def _import_file_safe(
    py_path: Path,
) -> tuple[object | None, str | None]:
    """Import a Python file as a module. Returns (module, None) or (None, error_msg)."""
    import importlib.util
    import traceback

    module_name = py_path.stem
    try:
        spec = importlib.util.spec_from_file_location(
            f"booley_custom_mcp_tools.{module_name}",
            py_path,
        )
        if spec is None or spec.loader is None:
            return None, f"Could not create module spec for {py_path}"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod.__name__] = mod
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as exc:
        msg = f"IMPORT FAILED: custom MCP tool {py_path}: {exc}"
        logger.error(msg, exc_info=True)
        print(f"[mcp] ERROR {msg}", file=sys.stderr, flush=True)
        tb = traceback.format_exc()
        print(f"[mcp] TRACEBACK:\n{tb}", file=sys.stderr, flush=True)
        return None, msg


def _import_and_extract_file(
    py_path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Import a custom MCP tool from a file path and extract its schema."""
    mod, err = _import_file_safe(py_path)
    if err is not None:
        return None, err

    found = _find_mcp_tool_class_in_module(mod, str(py_path))
    if found is None:
        msg = f"NO MCP ENDPOINT CLASS FOUND: custom MCP tool {py_path} has no recognized MCP endpoint subclass"
        logger.warning(msg)
        return None, msg

    cls, _instance, schema = found
    return _mcp_tool_def_from_class(
        cls,
        schema,
        py_path.stem,
        is_custom=True,
        custom_path=str(py_path),
    ), None


# ---------------------------------------------------------------------------
# B-Wave MCP tool definitions (hand-authored)
# ---------------------------------------------------------------------------

_BWAVE_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bwave",
        "description": (
            "RTL debug helper for simulation traces. Works with .fst "
            "waveform stores and raw .vcd traces. Registering a sim "
            "directory auto-builds an .fst store; a .vcd passed directly "
            "is recorded as-is and must be "
            "converted with `bwave build` before it can be queried. Before "
            'constructing commands, call with extra_args=["skill"] for '
            'agent workflow guidance, then extra_args=["--help"] for '
            "current syntax. Pass arguments exactly as they appear after "
            "`bwave` in the CLI help. To show the human a waveform (only "
            "for seeing — reading values needs no viewer), first locate the "
            "signals and time window with query commands, then call "
            '["gui", "@ALIAS", "--signals", "tb.dut.fifo.*", "--time", '
            '"1200c:1400c"] — this drives the VaporView viewer in the '
            "user's VS Code window. A new view gets the trace's clock as "
            "row 1 automatically (--no-clock opts out); --time brackets the "
            "range with the viewer's START/END markers so the human reads "
            "the span off the screen; --cursor moves START; --append adds "
            "signals to the current view. The signal list it prints is read "
            "back from the viewer — anything dropped is named in a stderr "
            "WARNING; relay it."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Arguments after the `bwave` CLI name. For agent "
                        'guidance, use ["skill"]. For help, use '
                        '["--help"] or ["COMMAND", "--help"]. Examples: '
                        '["register", "sim/work", "--as", "dut"], '
                        '["@dut", "wave", "-s", "*state*", "-t", '
                        '"100:200"], ["markers", "@dut", "set", '
                        '"start", "100"].'
                    ),
                },
            },
            "required": ["extra_args"],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------


def _resolve_transcript_dir(
    mcp_tool_name: str,
    call_counts: dict[str, int],
) -> Path:
    """Build a per-invocation transcript directory for an an agent capability.

    Raw path: $BOOLEY_RUNTIME_DIR/transcripts/<mcp_tool_name>/<N>/
    Rendered Markdown sidecars are written under human-logs/transcripts.
    where N is the 1-based invocation counter for that Specialist.
    Falls back to a tempdir if BOOLEY_LOGS_DIR is not set (with a warning).
    """
    runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if runtime_env:
        runtime_dir = Path(runtime_env)
    elif logs_dir:
        runtime_dir = ticket_runtime_dir(logs_dir)
    else:
        # Interactive Mode sets neither env var. Writing transcripts to an
        # ephemeral OS tempdir made a failed specialist impossible to
        # post-mortem — the raw agent output vanished with the session and was
        # never where `booley cheat` advertises (QA_REPORT C1: an ~$0.85
        # reviewer failure could not be inspected at all). Persist under the
        # project's own .runtime tree so the transcript survives and is
        # discoverable. Fall back to a tempdir only when no project exists.
        try:
            from booley.runtime.project_dir import runtime_dir as _project_runtime_dir

            runtime_dir = _project_runtime_dir()
            logger.info(
                "BOOLEY_LOGS_DIR not set — persisting agent transcripts under "
                "the project runtime dir %s",
                runtime_dir,
            )
        except (FileNotFoundError, ImportError):
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
                emit(flag, str(item))
            continue

        emit(flag, str(value))

    return argv


# Per-stream peak-memory caps for buffered subprocess output.
# 4x the result-formatter caps so the existing "truncated" indicator still
# fires for moderate overflow while catastrophic overflow (nested-agent JSONL
# streams 10s-100s of MB) stays bounded. Derived from the *resolved* formatter
# caps, so raising BOOLEY_MCP_MAX_STDOUT_BYTES scales the ring buffer with it.
_STREAM_CAP_MULTIPLIER = 4


def _stdout_cap_bytes() -> int:
    """Peak-memory cap for the buffered stdout of a endpoint subprocess."""
    return _max_stdout_bytes() * _STREAM_CAP_MULTIPLIER


def _stderr_cap_bytes() -> int:
    """Peak-memory cap for the buffered stderr of a endpoint subprocess."""
    return _max_stderr_bytes() * _STREAM_CAP_MULTIPLIER


async def _read_tail(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Drain *stream* to EOF, keeping only the last *cap* bytes.

    Trims periodically so peak memory stays under ~2x cap regardless of
    total output volume.
    """
    buf = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap * 2:
            del buf[:-cap]
    if len(buf) > cap:
        del buf[:-cap]
    return bytes(buf)


# Watchdog sampling interval while a child may be queued for its class slot.
# Queue-state reads are a handful of small file stats/reads per tick; 5s keeps
# the budget accounting within one tick of the truth without measurable IO.
_WATCHDOG_TICK_SECONDS = 5.0


async def _wait_with_queue_credit(
    proc: asyncio.subprocess.Process,
    timeout: float,
    is_queued: Callable[[], bool],
    on_first_active: Callable[[], None] | None,
) -> bool:
    """Wait for *proc*, charging only non-queued time against *timeout*.

    The child claims its concurrency slot INSIDE its own process (ADR 0028),
    so wall-clock since spawn includes any queue wait — which can exceed the
    endpoint's whole budget when a long run holds the class slot. Killing a job
    for time it spent waiting in line defeats the queue, so the watchdog
    samples the child's admission state each tick and only ticks the budget
    down while the child is not QUEUED. ``on_first_active`` fires once, on
    the first non-queued observation — the run-start stamp for the durable
    record. Returns True when the process exited, False on (active-time)
    timeout. State is sampled at tick granularity; a queued→active flip
    mid-tick miscounts at most one tick.
    """
    active_used = 0.0
    seen_active = False
    while True:
        # This probe performs only a handful of bounded local file reads.  A
        # thread hop is more dangerous than those reads: repeatedly submitting
        # through the default asyncio executor has hung on some runtimes during
        # executor wake-up, wedging the watchdog before it can enforce its own
        # timeout.  Keep the small admission-state sample on the event loop.
        queued = is_queued()
        if not queued and not seen_active:
            seen_active = True
            if on_first_active is not None:
                with contextlib.suppress(Exception):
                    on_first_active()
        remaining = timeout - active_used
        if not queued and remaining <= 0:
            return False
        tick = _WATCHDOG_TICK_SECONDS if queued else min(_WATCHDOG_TICK_SECONDS, remaining)
        started = time.monotonic()
        try:
            await asyncio.wait_for(proc.wait(), timeout=tick)
            return True
        except TimeoutError:
            if not queued:
                active_used += time.monotonic() - started


async def _run_subprocess(
    cmd: list[str],
    *,
    timeout: int = 600,
    on_spawn: Callable[[int], None] | None = None,
    env: dict[str, str] | None = None,
    is_queued: Callable[[], bool] | None = None,
    on_first_active: Callable[[], None] | None = None,
) -> tuple[int, str, str, bool]:
    """Run a subprocess with bounded output buffering, off the event loop.

    Uses asyncio subprocess so the MCP server stays protocol-responsive
    during long MCP tool calls (otherwise a blocking subprocess.run pegs the
    loop for the entire endpoint duration). Stdout/stderr are streamed through
    a tail-only ring buffer (see ``_read_tail``) so a 30-min nested-agent
    run can't blow the MCP server's RSS.

    With ``is_queued`` set (async-job dispatch), *timeout* bounds ACTIVE time
    only — see ``_wait_with_queue_credit``. Without it, the plain
    wall-clock-since-spawn watchdog applies (synchronous endpoints are unclassed and
    never queue).

    Returns (exit_code, stdout, stderr, timed_out).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **_async_process_group_kwargs(),
        )
    except OSError as e:
        return 2, "", f"Subprocess launch failed for {cmd[0]!r}: {e}", False
    group = capture_process_group(proc)

    if on_spawn is not None:
        # Hand the child PID to the caller (the JobManager stamps it into the
        # durable job record) so a restarted server can judge liveness by PID.
        with contextlib.suppress(Exception):
            on_spawn(proc.pid)

    assert proc.stdout is not None and proc.stderr is not None
    stdout_task = asyncio.create_task(_read_tail(proc.stdout, _stdout_cap_bytes()))
    stderr_task = asyncio.create_task(_read_tail(proc.stderr, _stderr_cap_bytes()))

    try:
        try:
            await _await_supervised_process(
                proc,
                timeout,
                is_queued=is_queued,
                on_first_active=on_first_active,
            )
        except TimeoutError:
            return await _timeout_subprocess_result(proc, group, timeout, stdout_task, stderr_task)
        except asyncio.CancelledError:
            # User cancellation is cooperative first: give the endpoint process
            # group a bounded opportunity to release locks and clean up, then
            # force down descendants that ignored SIGTERM.
            await _cancel_async_process_tree(proc, group)
            await _cancel_stream_tasks(stdout_task, stderr_task)
            raise

        stdout_bytes = await stdout_task
        stderr_bytes = await stderr_task
        return (
            proc.returncode if proc.returncode is not None else 2,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
            False,
        )
    finally:
        # If we're unwinding while the child is still alive, reap it. The common
        # case is the MCP client interrupting the MCP tool call, which cancels this
        # coroutine (CancelledError) at the ``await`` above. Without this, an
        # interrupted `simulate` would leave an orphaned simulator running under
        # its own session — still holding the sim lock and able to write a late
        # report long after Claude has moved on. Kill is signal-only/synchronous
        # because awaiting is unreliable during cancellation unwinding.
        if proc.returncode is None:
            _signal_kill_process_group(proc, group)
            for t in (stdout_task, stderr_task):
                if not t.done():
                    t.cancel()


async def _await_supervised_process(
    proc: asyncio.subprocess.Process,
    timeout: int,
    *,
    is_queued: Callable[[], bool] | None,
    on_first_active: Callable[[], None] | None,
) -> None:
    """Wait for a child, charging either wall-clock or active-only time."""
    if is_queued is None:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    if not await _wait_with_queue_credit(proc, timeout, is_queued, on_first_active):
        raise TimeoutError


async def _cancel_stream_tasks(*tasks: asyncio.Task[bytes]) -> None:
    """Cancel and drain subprocess output readers."""
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _timeout_subprocess_result(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
    timeout: int,
    stdout_task: asyncio.Task[bytes],
    stderr_task: asyncio.Task[bytes],
) -> tuple[int, str, str, bool]:
    """Kill a timed-out subprocess and construct its bounded diagnostic."""
    snapshot = await _async_process_snapshot(proc.pid)
    await _kill_async_process_tree(proc, group)
    await _cancel_stream_tasks(stdout_task, stderr_task)
    stderr = f"Subprocess timed out after {timeout}s"
    if snapshot:
        stderr += "\n\nProcess snapshot before kill:\n" + snapshot
    return 2, "", stderr, True


def _async_process_group_kwargs() -> dict[str, Any]:
    """Popen kwargs for an asyncio child process group/session."""
    return new_group_kwargs()


async def _async_process_snapshot(pid: int) -> str:
    """Best-effort process tree snapshot for timeout diagnostics."""
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            "tasklist",
            "/FI",
            f"PID eq {pid}",
            "/FO",
            "LIST",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
        except TimeoutError:
            return ""
        return stdout.decode("utf-8", errors="replace").strip()

    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-eo",
        "pid,ppid,stat,etime,cmd",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
    except TimeoutError:
        return ""
    rows = stdout.decode("utf-8", errors="replace").splitlines()
    if not rows:
        return ""
    wanted = {str(pid)}
    selected = [rows[0]]
    changed = True
    while changed:
        changed = False
        for row in rows[1:]:
            parts = row.split(None, 4)
            if len(parts) < 2:
                continue
            row_pid, row_ppid = parts[0], parts[1]
            if row_pid in wanted or row_ppid in wanted:
                if row not in selected:
                    selected.append(row)
                if row_pid not in wanted:
                    wanted.add(row_pid)
                    changed = True
    return "\n".join(selected[:80])


def _signal_kill_process_group(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Best-effort SIGKILL of the child's process group, no awaits.

    Safe to call while this coroutine is being cancelled (unlike
    :func:`_kill_async_process_tree`, which awaits and would immediately
    re-raise the pending cancellation). The child is started with
    ``start_new_session=True`` so it leads its own process group; killing the
    group reaps the whole endpoint subprocess tree — e.g. the Python runner *and* the
    simulator binary it spawned.
    """
    force_async_process_group_now(proc, group)


async def _kill_async_process_tree(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Terminate an asyncio subprocess and its process group."""
    await force_async_process_group(proc, group)


_CANCEL_GRACE_SECONDS = 5.0


async def _cancel_async_process_tree(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """SIGTERM a endpoint process group, then SIGKILL it after a bounded grace."""
    await terminate_async_process_group(proc, group, grace_seconds=_CANCEL_GRACE_SECONDS)


def _write_synthetic_endpoint_end(mcp_tool_name: str, timeout_s: int) -> None:
    """Write a synthetic endpoint_end after the MCP server kills a timed-out endpoint.

    The endpoint subprocess writes endpoint_start on entry and endpoint_end on exit, but a
    timeout kill prevents endpoint_end from being written — leaving an orphaned
    endpoint_start that blocks subsequent calls via the concurrency guard.
    """

    logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_dir:
        return
    event = {
        "type": "endpoint_end",
        "endpoint": mcp_tool_name,
        "exit_code": 2,
        "duration_s": timeout_s,
        "report_text": f"Killed by MCP server after {timeout_s}s timeout",
        "timestamp": utc_now_rfc3339(),
    }
    try:
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
        runtime_dir = Path(runtime_env) if runtime_env else ticket_runtime_dir(logs_dir)
        path = runtime_dir / "display.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        logger.debug("Failed to write synthetic endpoint_end", exc_info=True)


# One MCP tool call's MCP payload: plain text blocks, or the SDK's combination
# form ``(blocks, structured_dict)``, which attaches ``structuredContent`` to
# the CallToolResult while keeping the text blocks exactly as they were
# (mcp 1.28 ``CombinationContent``). No MCP tool declares an ``outputSchema``,
# so the SDK performs no output validation on the attached dict — a tuple
# return without a schema is explicitly supported.
McpToolContent = list[TextContent] | tuple[list[TextContent], dict[str, Any]]

# A report whose serialized ``reports`` payload exceeds this is not attached
# in full as structuredContent — the agent still gets the text card, and a
# compact scalar verdict replaces the heavy body (huge test lists / embedded
# logs would otherwise bloat every conversation turn).
_MAX_STRUCTURED_REPORT_BYTES = 64 * 1024


def _report_artifacts(report: dict[str, Any]) -> dict[str, Any]:
    """Pull the artifact pointers out of a run report, wherever an endpoint put them.

    Endpoints emit ``artifacts`` at the top level of their per-target report and
    inside ``detail`` (the copy that rides in the flat report this function
    usually sees). Both are checked; the top-level one wins on a key clash
    because it is the more specific of the two.

    ``detail`` is also scanned ONE level deep, because a multi-target Flow
    keys its detail by target and hangs the block off each entry
    (``detail["sim_fast"]["artifacts"]`` — asic_synthesize's aggregate shape).
    Without this the rescue silently found nothing for exactly the Flows whose
    reports grow large enough to be truncated.

    Values are paths, or (for a multi-target Flow such as ``simulate``) a
    per-target mapping of them — either way a few hundred bytes, which is the
    only property the truncation fallback depends on. Returns ``{}`` rather
    than raising on a malformed report: this runs on an already-oversized
    payload, where an exception would cost the agent the text card too.
    """
    found: dict[str, Any] = {}
    detail = report.get("detail")
    if isinstance(detail, dict):
        for key, value in detail.items():
            # One level down: a per-target entry carrying its own block.
            if key != "artifacts" and isinstance(value, dict):
                nested = value.get("artifacts")
                if isinstance(nested, dict) and nested:
                    found[key] = nested
        if isinstance(detail.get("artifacts"), dict):
            found.update(detail["artifacts"])
    if isinstance(report.get("artifacts"), dict):
        found.update(report["artifacts"])
    return found


def _structured_from_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bounded ``structuredContent`` payload for a run report, or None.

    Never raises: structured attachment is a best-effort enrichment of the
    text card, so any surprise (unserializable value, odd report shape)
    degrades to text-only rather than failing the MCP tool call.
    """
    try:
        if not isinstance(report, dict) or not report:
            return None
        payload: dict[str, Any] = {"reports": [report]}
        if len(json.dumps(payload).encode("utf-8")) > _MAX_STRUCTURED_REPORT_BYTES:
            # Oversized: keep the cheap scalar verdict, drop the heavy body.
            payload = {"reports": [], "truncated": True}
            for key in ("flow", "mcp_tool", "target", "exit_code"):
                if report.get(key) is not None:
                    payload[key] = report[key]
            # ...but never drop the artifact pointers. They are a few hundred
            # bytes and they are the whole recovery path: truncation happens on
            # exactly the big, noisy runs where the agent most needs to open
            # the log, and stripping the paths while keeping the verdict left
            # it with a bare "FAIL" and nowhere to go.
            artifacts = _report_artifacts(report)
            if artifacts:
                payload["artifacts"] = artifacts
        if isinstance(report.get("passed"), bool):
            payload["passed"] = report["passed"]
        return payload
    except Exception:  # noqa: BLE001 — enrichment falls back to text-only
        logger.debug("structuredContent attach failed; returning text-only", exc_info=True)
        return None


def _with_structured_report(
    content: list[TextContent],
    report: dict[str, Any] | None,
) -> McpToolContent:
    """Attach *report* as structuredContent when possible, else pass through.

    The text blocks are returned untouched either way — structured output is
    additive, never a substitute for the card the agent already parses.
    """
    structured = _structured_from_report(report)
    if structured is None:
        return content
    return content, structured


def _format_mcp_tool_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    report: dict[str, Any] | None = None,
) -> str:
    """Format hybrid MCP tool result (stdout + report fields)."""
    parts = [f"EXIT_CODE: {exit_code}"]

    max_stdout = _max_stdout_bytes()
    max_stderr = _max_stderr_bytes()

    # The stdout text as actually shown (post-truncation) — the dedupe check
    # below must run against this, not the full stdout.
    shown_stdout = ""
    if stdout:
        shown_stdout = stdout[-max_stdout:] if len(stdout) > max_stdout else stdout
        rendered = shown_stdout
        if len(stdout) > max_stdout:
            rendered = f"... (truncated, showing last {max_stdout} bytes)\n" + rendered
        parts.append(f"\n--- stdout ---\n{rendered}")

    if stderr:
        truncated = stderr[-max_stderr:] if len(stderr) > max_stderr else stderr
        parts.append(f"\n--- stderr ---\n{truncated}")

    if report:
        report_lines = []
        for field in ("status", "summary", "errors", "report_text"):
            if field not in report:
                continue
            if field == "report_text":
                # Heavy endpoints print their report_text to stdout AND return it
                # in report.json, so it would appear verbatim in both sections.
                # Skip the report copy when it already survives in the stdout
                # we actually show. Deliberate subtlety: containment is checked
                # against the TRUNCATED stdout — if truncation cut the summary
                # out of stdout, this check fails and the report section keeps
                # it, so the summary survives truncation exactly when needed.
                # Strip both sides so a trailing newline can't defeat the match.
                report_text = str(report[field]).strip()
                if report_text and report_text in shown_stdout.strip():
                    continue
            report_lines.append(f"{field}: {report[field]}")
        detail = report.get("detail")
        if isinstance(detail, dict):
            for field in ("reason", "error"):
                if detail.get(field):
                    report_lines.append(f"detail.{field}: {detail[field]}")
        if report_lines:
            parts.append("\n--- report ---\n" + "\n".join(report_lines))

    return "\n".join(parts)


def _endpoint_report_dirs() -> tuple[Path, ...]:
    """Return existing Flow and MCP endpoint report directories."""
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if not logs_dir:
        return ()
    runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
    runtime_dir = Path(runtime_env) if runtime_env else ticket_runtime_dir(logs_dir)
    candidates = (runtime_dir / "flow-reports", runtime_dir / "mcp-tool-reports")
    return tuple(path for path in candidates if path.is_dir())


def _read_report_json(path: Path) -> dict[str, Any] | None:
    """Parse a report.json, logging (not raising) on malformed/unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("Malformed report.json at %s: %s", path, e)
        return None
    except OSError as e:
        logger.warning("Failed to read report.json at %s: %s", path, e)
        return None


def _latest_report(endpoint: str | None = None) -> dict[str, Any] | None:
    """Read the most recent run report from disk.

    With *endpoint* set, prefer that endpoint's flat ``<endpoint>.json`` (the latest copy
    ``base.write_report`` refreshes each run), falling back to its newest
    numbered ``<endpoint>/<N>/report.json``. Without *endpoint*, return the most recent
    ``report.json`` across every endpoint (the inline dispatch behaviour).
    """
    report_dirs = _endpoint_report_dirs()
    if not report_dirs:
        return None

    if endpoint:
        flats = [reports / f"{endpoint}.json" for reports in report_dirs]
        existing_flats = [path for path in flats if path.is_file()]
        if existing_flats:
            return _read_report_json(max(existing_flats, key=lambda path: path.stat().st_mtime))
        numbered = sorted(
            (
                path
                for reports in report_dirs
                for path in reports.glob(f"{endpoint}/*/report.json")
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return _read_report_json(numbered[0]) if numbered else None

    latest: Path | None = None
    latest_mtime = 0.0
    for report_file in (path for reports in report_dirs for path in reports.rglob("report.json")):
        mtime = report_file.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = report_file
    return _read_report_json(latest) if latest else None


def _try_read_report() -> dict[str, Any] | None:
    """Read the most recent report.json across all endpoints (inline dispatch)."""
    return _latest_report(None)


def _available_report_endpoints() -> list[str]:
    """Names of endpoints that have a flat report on disk."""
    return sorted({p.stem for reports in _endpoint_report_dirs() for p in reports.glob("*.json")})


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


def _bwave_mcp_tools_for_mode() -> list[dict[str, Any]]:
    """Return the B-Wave MCP tool definitions visible in this server's mode.

    Uses the same exposure filter as Python-backed MCP tools so nested,
    explicit-allowlist, and Interactive Mode behavior stays coherent.
    """
    return [t for t in _BWAVE_MCP_TOOLS if _mcp_tool_visible(t["name"])]


def _status_mcp_tool_def() -> dict[str, Any] | None:
    """Return the synthetic Interactive Mode status MCP tool definition."""
    if not _status_mcp_tool_visible():
        return None
    return {
        "name": _STATUS_MCP_TOOL_NAME,
        "description": _STATUS_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def _report_mcp_tool_def() -> dict[str, Any] | None:
    """Return the synthetic report-fetch MCP tool definition (or None if hidden)."""
    if not _report_mcp_tool_visible():
        return None
    return {
        "name": _REPORT_MCP_TOOL_NAME,
        "description": _REPORT_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "endpoint": {
                    "type": "string",
                    "description": (
                        "Endpoint name to fetch the latest report for "
                        "(e.g. 'sim'). Omit for the most recent report "
                        "across all endpoints."
                    ),
                },
            },
            "additionalProperties": False,
        },
    }


def _poll_mcp_tool_def() -> dict[str, Any] | None:
    """Return the synthetic poll MCP tool definition (or None if hidden)."""
    if not _poll_mcp_tool_visible():
        return None
    return {
        "name": _POLL_MCP_TOOL_NAME,
        "description": _POLL_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": ("The run_id returned by an endpoint that reported RUNNING."),
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 270,
                    "description": (
                        "Long-poll: block up to this many seconds for the run "
                        "to finish before answering (clamped to 0-270; longer "
                        "would die at the HTTP layer's ~300s cap). 0 answers "
                        "with the current status immediately. Omit to use the "
                        "server default wait "
                        f"(BOOLEY_MCP_JOB_POLL_WAIT_SECONDS, "
                        f"{_DEFAULT_JOB_POLL_WAIT_SECONDS:g}s)."
                    ),
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    }


def _build_mcp_tool_index(
    mcp_tools: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build name -> mcp_tool_def lookup for Booley + B-Wave MCP tools."""
    index: dict[str, dict[str, Any]] = {}
    for t in mcp_tools:
        index[t["name"]] = t
    for t in _bwave_mcp_tools_for_mode():
        index[t["name"]] = t
    return index


def _cancel_mcp_tool_def() -> dict[str, Any] | None:
    """Return the synthetic cancel MCP tool definition (or None if hidden).

    Visible wherever poll is (always): any server that can hand back a
    run_id for a queued or running job must let the caller stop it. Like poll it
    only manages job state — it cannot invoke an endpoint or recurse — so it is
    exempt from the recursion-safety allowlists.
    """
    if not _poll_mcp_tool_visible():
        return None
    return {
        "name": _CANCEL_MCP_TOOL_NAME,
        "description": _CANCEL_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run_id of the queued or running job to cancel.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    }


def _targets_mcp_tool_def() -> dict[str, Any] | None:
    """Return the synthetic targets-listing MCP tool definition (or None if hidden)."""
    if not _targets_mcp_tool_visible():
        return None
    from booley.targets.target_surface import TARGET_AWARE_FLOWS

    return {
        "name": _TARGETS_MCP_TOOL_NAME,
        "description": _TARGETS_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "for_flow": {
                    "type": "string",
                    "enum": list(TARGET_AWARE_FLOWS),
                    "description": (
                        "Only Targets this Booley Flow could drive (compatibility, not wiring)."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Case-sensitive glob over the bare Target name or "
                        "vendor:library:name#target, e.g. 'soc*' or '*#lint'."
                    ),
                },
                "work_dir": {
                    "type": "string",
                    "description": (
                        "Optional worktree root to enumerate instead of the "
                        "session workspace (same validation as the Booley Flows' "
                        "work_dir)."
                    ),
                },
            },
            "additionalProperties": False,
        },
    }


def _sleep_mcp_tool_def() -> dict[str, Any] | None:
    """Return the diagnostic sleep MCP tool definition (or None if hidden)."""
    if not _sleep_mcp_tool_visible():
        return None
    return {
        "name": _SLEEP_MCP_TOOL_NAME,
        "description": _SLEEP_MCP_TOOL_DESCRIPTION,
        "schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 86400,
                    "description": ("How long to hold the call open server-side."),
                },
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    }


def _build_mcp_tool_list(
    mcp_tools: list[dict[str, Any]],
) -> list[McpSdkTool]:
    """Convert all MCP tool definitions to MCP SDK tool objects for list_tools."""
    mcp_tool_defs = [*mcp_tools, *_bwave_mcp_tools_for_mode()]
    status_def = _status_mcp_tool_def()
    if status_def is not None:
        mcp_tool_defs.append(status_def)
    report_def = _report_mcp_tool_def()
    if report_def is not None:
        mcp_tool_defs.append(report_def)
    poll_def = _poll_mcp_tool_def()
    if poll_def is not None:
        mcp_tool_defs.append(poll_def)
    cancel_def = _cancel_mcp_tool_def()
    if cancel_def is not None:
        mcp_tool_defs.append(cancel_def)
    sleep_def = _sleep_mcp_tool_def()
    if sleep_def is not None:
        mcp_tool_defs.append(sleep_def)
    targets_def = _targets_mcp_tool_def()
    if targets_def is not None:
        mcp_tool_defs.append(targets_def)
    return [
        McpSdkTool(name=t["name"], description=t["description"], inputSchema=t["schema"])
        for t in mcp_tool_defs
    ]


def _format_status_card(mcp_tool_names: list[str]) -> str:
    """Return the concise user-facing Interactive Mode status card."""
    session_id = _interactive_session_id()
    mcp_tools_text = ", ".join(mcp_tool_names) if mcp_tool_names else "none"
    try:
        from booley.harness.auto_doctor import current_summary

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
    if not _status_mcp_tool_visible():
        return content
    try:
        from booley.harness.auto_doctor import consume_changed_summary

        alert = consume_changed_summary(Path.cwd(), channel="mcp-tool", issues_only=True)
    except Exception:  # noqa: BLE001 — health reporting must never break a Booley Flow result
        return content
    if alert is None:
        return content
    block = TextContent(type="text", text=f"HEALTH WARNING: {alert}")
    if isinstance(content, tuple):
        blocks, structured = content
        return [block, *blocks], structured
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
    project_root = Path(str(arguments.get("work_dir") or Path.cwd()))

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


def _format_report_card(report: dict[str, Any] | None, endpoint: str | None) -> str:
    """Render a fetched run report (or a helpful not-found message)."""
    if report is None:
        which = f" for endpoint {endpoint!r}" if endpoint else ""
        available = _available_report_endpoints()
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
    report = _latest_report(endpoint)
    content = [TextContent(type="text", text=_format_report_card(report, endpoint))]
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
    if not _mcp_tool_visible(name):
        return None
    cmd = builder(arguments)
    exit_code, stdout, stderr, _timed_out = await _run_subprocess(cmd)
    return [TextContent(type="text", text=_format_mcp_tool_result(exit_code, stdout, stderr))]


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
        _DEFAULT_JOB_POLL_WAIT_SECONDS,
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


def _job_phase(run_id: str) -> str:
    """Admission phase of a detached job: "RUNNING" or "QUEUED (position N)".

    Derived from the child's own slot-store claim (ADR 0028): a waiter entry
    means the child process is alive but idling for its class slot. Position
    is 1-based for humans. Falls back to RUNNING when the store or claim is
    unreadable — over-claiming "queued" would stall agents that should poll.
    """
    rec = jobrec.read_record(run_id)
    root = job_slots.slots_dir()
    if rec is None or rec.pid is None or root is None:
        return "RUNNING"
    try:
        state = job_slots.SlotStore(root).state_for_pid(rec.pid)
    except OSError:
        return "RUNNING"
    if state is not None and state.state == job_slots.QUEUED:
        return f"QUEUED (position {(state.position or 0) + 1})"
    return "RUNNING"


def _format_job_attached(name: str, run_id: str) -> str:
    """Message for a duplicate submit attached to the identical in-flight run."""
    return (
        f"{_job_phase(run_id)}: an identical '{name}' run is already in "
        f"flight — attached to it instead of starting a duplicate "
        f"(run_id={run_id}). This is NOT the result. Call the "
        f"'{_POLL_MCP_TOOL_NAME}' MCP tool with run_id={run_id} and keep polling "
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
        f"— the run has not finished. Call the '{_POLL_MCP_TOOL_NAME}' MCP tool with "
        f"run_id={run_id} to keep waiting, and keep polling until it returns "
        f"an EXIT_CODE. Pass/fail criteria are recorded only when the run "
        f"actually completes, so do not conclude the step until then."
    )


def _format_job_running_poll(run_id: str) -> str:
    """Message for a poll whose job is still running (or queued for a slot)."""
    message = (
        f"{_job_phase(run_id)}: job {run_id} is still in progress. Call "
        f"'{_POLL_MCP_TOOL_NAME}' again with the same run_id to keep waiting for "
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
        "not a final synthesis verdict."
    )


def _running_progress(run_id: str) -> dict[str, Any] | None:
    """Return only this live job's run-scoped nonterminal checkpoint."""
    rec = jobrec.read_record(run_id)
    if rec is None:
        return None
    progress = _progress_for_run_id(rec.endpoint, rec.run_id)
    if progress is None or progress.get("complete") is True:
        return None
    return progress


def _running_poll_content(run_id: str) -> McpToolContent:
    """Running poll card enriched with the latest durable matrix checkpoint."""
    content = [TextContent(type="text", text=_format_job_running_poll(run_id))]
    return _with_structured_report(content, _running_progress(run_id))


def _report_for_run_id(endpoint: str, run_id: str) -> dict[str, Any] | None:
    """Scan an endpoint's numbered report dirs for *run_id*'s report.

    Fallback when the flat ``<endpoint>.json`` belongs to a different run.
    """
    reports = _endpoint_report_dirs()
    if not reports:
        return None
    numbered = sorted(
        (path for root in reports for path in root.glob(f"{endpoint}/*/report.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in numbered:
        report = _read_report_json(path)
        if report is not None and report.get("run_id") == run_id:
            return report
    return None


def _progress_for_run_id(endpoint: str, run_id: str) -> dict[str, Any] | None:
    """Newest run-scoped checkpoint when a killed matrix has no final report."""
    reports = _endpoint_report_dirs()
    if not reports:
        return None
    checkpoints = sorted(
        (path for root in reports for path in root.glob(f"{endpoint}/*/progress.json")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in checkpoints:
        report = _read_report_json(path)
        if report is not None and report.get("run_id") == run_id:
            report["partial"] = not bool(report.get("complete"))
            return report
    return None


def _job_report(rec: jobrec.JobRecord | None) -> tuple[dict[str, Any] | None, bool]:
    """The report belonging to *rec*'s run, plus whether it provably is.

    Returns ``(report, fresh)``. Identity first: a report stamped with a
    ``run_id`` (written by the child from ``BOOLEY_RUN_ID``) either matches
    the job — proof, ``fresh=True`` — or names a different concurrent run of
    the same endpoint, in which case the numbered invocation dirs are scanned for
    ours and the mismatched flat copy is never shown under this job's
    EXIT_CODE. Time-window gating survives only for legacy reports with no
    ``run_id``: the report's ``timestamp`` must fall inside
    ``[started_at, run_start + timeout_s + slack]``, where ``run_start`` is
    the observed ``run_started_at`` when the record has one — a job that
    queued past its own budget (ADR 0028) still writes its report inside the
    window. When a stamp is missing/unparseable we cannot judge: the report
    is kept (matching the pre-ADR-0027 attach behaviour) but never counts as
    fresh, so it can be shown yet never *vouches* for the run's outcome.
    """
    if rec is None:
        report = _latest_report()
        if report is None:
            return None, False
        return report, False

    # Prefer identity-stamped invocation artifacts before consulting the
    # last-writer-wins flat copy.  In particular, an old legacy flat report
    # must not hide this run's durable progress checkpoint after a worker dies.
    report = _report_for_run_id(rec.endpoint, rec.run_id)
    if report is None:
        report = _progress_for_run_id(rec.endpoint, rec.run_id)
    if report is not None:
        return report, True

    report = _latest_report(rec.endpoint)
    if report is None:
        return None, False

    report_run_id = report.get("run_id")
    if isinstance(report_run_id, str) and report_run_id:
        return (report, True) if report_run_id == rec.run_id else (None, False)
    return _gate_report_by_window(rec, report)


def _gate_report_by_window(
    rec: jobrec.JobRecord,
    report: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """Legacy freshness gate for reports with no ``run_id`` (see _job_report)."""
    report_ts = jobrec.parse_stamp(report.get("timestamp"))
    started_ts = jobrec.parse_stamp(rec.started_at)
    if report_ts is None or started_ts is None:
        return report, False
    if report_ts < started_ts:
        return None, False
    run_start_ts = jobrec.parse_stamp(rec.run_started_at) or started_ts
    if isinstance(rec.timeout_s, (int, float)) and (
        report_ts > run_start_ts + rec.timeout_s + jobrec.DEADLINE_SLACK_SECONDS
    ):
        return None, False
    return report, True


class _JobManager:
    """In-process registry for detached endpoint jobs. Narrates; never enforces.

    Admission moved out of this class (ADR 0028): the spawned endpoint child
    claims its own slot in the shared on-disk store (``booley.runtime.job_slots``)
    from inside ``McpTool.main``, so concurrency is governed identically for MCP
    dispatch, Developer Agents, Specialists, and bare CLI invocations — this
    server included. A submit therefore always spawns the child immediately;
    a child over its class cap idles in the queue (a live process holding its
    claim — the cost ADR 0028 accepts for PID-native claim identity), and
    submit/poll render QUEUED/RUNNING/result from the claim + record state.
    """

    def __init__(self, lifetime: _McpLifetime) -> None:
        self._lifetime = lifetime
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        # run_id -> (exit_code, stdout, stderr, timed_out) once finished.
        self._results: dict[str, tuple[int, str, str, bool]] = {}
        self._counter = 0

    def _next_run_id(self, endpoint: str) -> str:
        self._counter += 1
        stamp = compact_utc_now()
        return jobrec.make_run_id(endpoint, stamp, self._counter)

    def submit(self, name: str, cmd: list[str], timeout: int) -> str:
        """Spawn *cmd* as a detached background job and return its run_id.

        Always spawns: admission (queue or run) is the child's own slot-store
        claim, not this server's decision.
        """
        run_id = self._next_run_id(name)
        rec = jobrec.JobRecord(
            run_id=run_id,
            endpoint=name,
            started_at=utc_now_rfc3339(),
            timeout_s=timeout,
            argv=cmd,
        )
        jobrec.write_record(rec)
        # The submit CALL returns in seconds (mark_mcp_endpoint_end fires then), so hold
        # the lifetime busy independently or the server idle-exits mid-run.
        self._lifetime.mark_mcp_endpoint_start()
        self._tasks[run_id] = asyncio.create_task(
            self._run_and_record(run_id, name, cmd, timeout, rec),
        )
        return run_id

    async def _run_and_record(
        self,
        run_id: str,
        name: str,
        cmd: list[str],
        timeout: int,
        rec: jobrec.JobRecord,
    ) -> None:
        """Run the subprocess to completion and persist the terminal outcome."""
        try:

            def _stamp_pid(pid: int) -> None:
                rec.pid = pid
                jobrec.write_record(rec)

            def _child_is_queued() -> bool:
                # The child's own slot-store claim is the admission truth
                # (ADR 0028). Unreadable store / no claim yet counts as
                # active — over-claiming "queued" would let a wedged child
                # dodge its watchdog forever.
                if rec.pid is None:
                    return False
                root = job_slots.slots_dir()
                if root is None:
                    return False
                try:
                    state = job_slots.SlotStore(root).state_for_pid(rec.pid)
                except OSError:
                    return False
                return state is not None and state.state == job_slots.QUEUED

            def _stamp_run_started() -> None:
                # First non-queued observation == run start. The server is
                # the record's only writer, so no read-modify-write races
                # with the child.
                rec.run_started_at = utc_now_rfc3339()
                jobrec.write_record(rec)

            exit_code, stdout, stderr, timed_out = await _run_subprocess(
                cmd,
                timeout=timeout,
                on_spawn=_stamp_pid,
                # BOOLEY_RUN_ID lets the child stamp its report with the job
                # identity (poll-side attribution); BOOLEY_SLOT_TIMEOUT_S
                # carries the real watchdog budget to the child's slot claim
                # so the holder-deadline reap has a sound anchor.
                env={
                    **os.environ,
                    "BOOLEY_RUN_ID": run_id,
                    "BOOLEY_SLOT_TIMEOUT_S": str(timeout),
                },
                is_queued=_child_is_queued,
                on_first_active=_stamp_run_started,
            )
            if timed_out:
                _write_synthetic_endpoint_end(name, timeout)
            self._results[run_id] = (exit_code, stdout, stderr, timed_out)
            rec.status = jobrec.terminal_status(exit_code, timed_out)
            rec.exit_code = exit_code
            jobrec.write_record(rec)
        except asyncio.CancelledError:
            # Only booley_cancel owns this terminal state. Server shutdown also
            # cancels supervisor tasks, but those records intentionally remain
            # running for the next server's dead-PID/report reconciliation.
            if run_id not in self._cancel_requested:
                raise
            rec.status = jobrec.STATUS_CANCELLED
            rec.exit_code = 130
            self._results[run_id] = (
                130,
                "",
                f"CANCELLED: job {run_id} was stopped by request.",
                False,
            )
            jobrec.write_record(rec)
        finally:
            # Release the lifetime hold. On server shutdown this runs during
            # cancellation; the record stays non-terminal and the next
            # server's reconcile fails it via dead-PID.
            self._lifetime.mark_mcp_endpoint_end()

    async def cancel(self, run_id: str) -> str | None:
        """Cancel a queued or running job; return its former phase.

        The on-disk record is stamped before signalling so a concurrent poll,
        server restart, or cancellation race always sees a terminal outcome.
        In-process jobs cancel their supervising task (which performs the
        TERM/grace/KILL process-tree shutdown); adopted jobs use the durable
        PID and the same policy directly.
        """
        rec = jobrec.read_record(run_id)
        if rec is None:
            return None

        if jobrec.derive_status(rec, is_pid_alive) != jobrec.STATUS_RUNNING:
            return "finished"

        root = job_slots.slots_dir()
        was_queued = False
        if rec.pid is not None and root is not None:
            with contextlib.suppress(OSError):
                # Atomic against waiter promotion. If this loses the race, the
                # job is now running and is still cancellable below.
                was_queued = job_slots.SlotStore(root).cancel_waiter(rec.pid)

        task = self._tasks.get(run_id)
        if task is not None:
            if task.done():
                return "finished"
            self._cancel_requested.add(run_id)
            if not task.cancel():
                return "finished"
            with contextlib.suppress(asyncio.CancelledError):
                await task
        else:
            rec.status = jobrec.STATUS_CANCELLED
            rec.exit_code = 130
            jobrec.write_record(rec)
        if task is None and rec.pid is not None:
            await _cancel_adopted_process_group(rec.pid)
        return "queued" if was_queued else "running"

    async def wait(self, run_id: str, timeout: float) -> bool | None:
        """Wait up to *timeout* seconds for a tracked job.

        Returns True (finished), False (still running), or None when *run_id* is
        not tracked in this server process (caller falls back to the durable
        disk record). ``asyncio.wait`` never cancels the task on timeout, so a
        poll that gives up leaves the job running.
        """
        task = self._tasks.get(run_id)
        if task is None:
            return None
        if not task.done() and timeout > 0:
            await asyncio.wait({task}, timeout=timeout)
        return task.done()

    def result_text(self, run_id: str) -> str:
        """Render a finished job's result exactly like the synchronous path."""
        rec = jobrec.read_record(run_id)
        report, report_fresh = _job_report(rec)
        finished = self._results.get(run_id)
        if finished is not None:
            exit_code, stdout, stderr, _timed_out = finished
            return _format_mcp_tool_result(exit_code, stdout, stderr, report)
        if rec is not None and rec.status == jobrec.STATUS_CANCELLED:
            return _format_mcp_tool_result(
                130,
                "",
                f"CANCELLED: job {run_id} was stopped by request.",
                report,
            )
        # Terminal-from-disk (server restarted): no captured stdout/stderr.
        exit_code = rec.exit_code if rec and rec.exit_code is not None else None
        if exit_code is None and report_fresh and isinstance(report.get("exit_code"), int):
            # Orphan rescue: the run itself finished and wrote this job's
            # report — only the server died before recording the outcome.
            # Trust the report's exit code over a blanket failure, so a
            # passing sim that outlived a SIGKILLed server still reads as a
            # pass instead of "EXIT_CODE: 2" with a passing report below it.
            exit_code = report["exit_code"]
        if exit_code is None:
            exit_code = 2
        return _format_mcp_tool_result(exit_code, "", "", report)

    def result_content(self, run_id: str) -> McpToolContent:
        """``result_text`` as MCP content, with structuredContent attached.

        Only a *fresh* report (one that provably belongs to this run, see
        ``_job_report``) is attached — the structured verdict must vouch for
        the run's outcome, unlike the text card, which may show a non-fresh
        report with its caveats spelled out.
        """
        content = [TextContent(type="text", text=self.result_text(run_id))]
        rec = jobrec.read_record(run_id)
        report, report_fresh = _job_report(rec)
        return _with_structured_report(content, report if report_fresh else None)


# Re-check cadence while long-polling a disk-only (adopted) job. Each check
# is a couple of small file reads; 5s keeps the answer prompt without churn.
_DISK_POLL_TICK_SECONDS = 5.0


async def _poll_from_disk(run_id: str, jobs: _JobManager, wait_seconds: float = 0.0) -> str:
    """Resolve a poll for a job not tracked in this server, from the disk record.

    Honors the long-poll budget: an adopted job (submitted by a previous
    server generation) has no in-memory task for ``jobs.wait`` to block on,
    so without this loop every poll after a server restart returned RUNNING
    instantly — degrading the agent's one blocking call back into the chatty
    poll-loop the long-poll exists to kill. Re-derives status from the record
    each tick until terminal or out of budget.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        rec = jobrec.read_record(run_id)
        if rec is None:
            return (
                f"Unknown run_id {run_id!r}. It may belong to a different project "
                f"run, or the job record has been cleaned up."
            )
        status = jobrec.derive_status(rec, is_pid_alive)
        if status != jobrec.STATUS_RUNNING:
            return jobs.result_text(run_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _format_job_running_poll(run_id)
        await asyncio.sleep(min(_DISK_POLL_TICK_SECONDS, remaining))


async def _dispatch_poll(
    arguments: dict[str, Any],
    jobs: _JobManager,
) -> McpToolContent:
    """Handle the synthetic poll MCP tool: wait briefly, return status or result.

    Long-poll: 'wait_seconds' bounds how long this call blocks for a running
    job — the same ``jobs.wait`` mechanism the submit-side inline wait uses,
    just with a caller-chosen budget (see _requested_poll_wait_seconds).
    """
    raw = arguments.get("run_id")
    run_id = raw.strip() if isinstance(raw, str) else ""
    if not run_id:
        return [
            TextContent(
                type="text",
                text="Provide a 'run_id' (returned by an endpoint that reported RUNNING).",
            )
        ]
    wait_budget = _requested_poll_wait_seconds(arguments)
    finished = await jobs.wait(run_id, wait_budget)
    if finished is None:
        # Not tracked here (previous server generation): long-poll the durable
        # record with the same budget instead of answering instantly.
        return [TextContent(type="text", text=await _poll_from_disk(run_id, jobs, wait_budget))]
    if finished:
        return jobs.result_content(run_id)
    return _running_poll_content(run_id)


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
    """Cancel a disk-only job by its session-leader PID."""
    await terminate_adopted_process_group(
        ProcessGroup(pid),
        grace_seconds=_CANCEL_GRACE_SECONDS,
    )


async def _dispatch_cancel(
    arguments: dict[str, Any],
    jobs: _JobManager,
) -> list[TextContent]:
    """Handle the synthetic cancel MCP tool for queued and running jobs."""
    raw = arguments.get("run_id")
    run_id = raw.strip() if isinstance(raw, str) else ""
    if not run_id:
        return [TextContent(type="text", text="Provide the 'run_id' of a queued job.")]
    rec = jobrec.read_record(run_id)
    if rec is None:
        return [TextContent(type="text", text=f"Unknown run_id {run_id!r}.")]

    if jobrec.derive_status(rec, is_pid_alive) != jobrec.STATUS_RUNNING:
        return [
            TextContent(
                type="text",
                text=f"Job {run_id} already finished — nothing to cancel. "
                f"Fetch its result with '{_POLL_MCP_TOOL_NAME}'.",
            )
        ]

    phase = await jobs.cancel(run_id)
    if phase is None:
        return [TextContent(type="text", text=f"Unknown run_id {run_id!r}.")]
    if phase == "finished":
        return [
            TextContent(
                type="text",
                text=f"Job {run_id} finished before cancellation took effect. "
                f"Fetch its result with '{_POLL_MCP_TOOL_NAME}'.",
            )
        ]
    return [
        TextContent(
            type="text",
            text=(
                f"CANCELLED: {phase} job {run_id} was withdrawn before it started."
                if phase == "queued"
                else f"CANCELLED: running job {run_id} received SIGTERM; any process "
                f"still alive after {_CANCEL_GRACE_SECONDS:.0f}s was force-killed."
            ),
        )
    ]


async def _dispatch_async_job(
    name: str,
    cmd: list[str],
    mcp_tool_timeout: int,
    jobs: _JobManager,
) -> McpToolContent:
    """Submit a heavy endpoint as a background job; wait inline, else hand back a run_id.

    Idempotent attach: a re-submit whose endpoint + argv exactly match a live
    job — running **or queued** for its slot (ADR 0028) — joins that job
    instead of spawning a duplicate. A duplicate submit is almost always an
    agent that lost the first call's handle (client-side timeout, truncated
    context) retrying; spawning again would relaunch a multi-minute EDA run
    (or double-queue it). Live attach only; a *finished* identical job is
    never replayed, since the workspace (RTL under edit) may have changed.

    No admission here: the spawned child claims its own slot (McpTool.main) and
    idles in queue when its class is at cap. BLOCKED survives only as the
    child's queue-full refusal, which comes back through the normal result
    path.
    """
    attach = _find_attachable_job(name, cmd)
    if attach is not None:
        return await _attach_to_job(name, attach, jobs)
    run_id = jobs.submit(name, cmd, mcp_tool_timeout)
    finished = await jobs.wait(run_id, _job_inline_wait_seconds())
    if finished:
        return jobs.result_content(run_id)
    return [TextContent(type="text", text=_format_job_running(name, run_id))]


def _strip_transcript_dir(argv: list[str]) -> list[str]:
    """*argv* minus any ``--transcript-dir <path>`` pair.

    The dispatch layer appends a per-call numbered transcript dir to every
    specialist invocation, so two submits of the SAME logical call never have
    equal raw argv — comparing without it is what makes idempotent attach
    reachable for specialists at all (the endpoints whose duplicate submits
    each burn a full LLM sub-agent run). Comparison only: records keep the
    full argv, which the /proc identity guards need verbatim.
    """
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--transcript-dir":
            skip_next = True
            continue
        out.append(arg)
    return out


def _find_attachable_job(name: str, cmd: list[str]) -> str | None:
    """run_id of a live (running or queued) job matching endpoint + argv, or None.

    Scans the durable records rather than any in-process slot: the identical
    job may have been submitted by a previous server generation. Liveness is
    judged by ``derive_status`` (PID + deadline + argv identity), so a stale
    record cannot capture the new submit. Argv is compared modulo the
    per-call ``--transcript-dir`` (see ``_strip_transcript_dir``).
    """
    wanted = _strip_transcript_dir(cmd)
    newest: jobrec.JobRecord | None = None
    for rec in jobrec.list_records():
        if rec.endpoint != name or _strip_transcript_dir(rec.argv) != wanted:
            continue
        if rec.status != jobrec.STATUS_RUNNING:
            continue
        if not jobrec.is_active(rec, is_pid_alive):
            continue
        # started_at is a fixed-width ISO stamp: string order == time order.
        if newest is None or rec.started_at > newest.started_at:
            newest = rec
    return newest.run_id if newest is not None else None


async def _attach_to_job(
    name: str,
    run_id: str,
    jobs: _JobManager,
) -> McpToolContent:
    """Resolve a duplicate submit against the identical live job."""
    inline_wait = _job_inline_wait_seconds()
    finished = await jobs.wait(run_id, inline_wait)
    if finished is None:
        # Adopted job (no in-memory task): long-poll the durable record with
        # the same inline budget a fresh submit would have waited.
        return [TextContent(type="text", text=await _poll_from_disk(run_id, jobs, inline_wait))]
    if finished:
        return jobs.result_content(run_id)
    return [TextContent(type="text", text=_format_job_attached(name, run_id))]


def _validate_work_dir(value: Any) -> str | None:
    """Reject a ``work_dir`` argument that is not a usable checkout root.

    The agent-facing ``work_dir`` retargets an endpoint at another checkout (a
    linked git worktree, e.g. one made by ``worktree_create.sh``). Fail-closed
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
            "first (worktree_create.sh puts it under .booley_project/worktrees/)."
        )
    if not (resolved / ".git").is_file():
        return (
            f"ERROR: work_dir {value!r} is not the root of a linked git "
            "worktree (no .git pointer file). Pass the worktree root created "
            "by worktree_create.sh under .booley_project/worktrees/, or omit "
            "work_dir to run against the session workspace."
        )
    return None


async def _dispatch_booley_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    mcp_tool_def: dict[str, Any],
    mcp_tool_call_counts: dict[str, int],
    jobs: _JobManager,
) -> McpToolContent:
    """Run a Booley Flow subprocess and return an MCP result.

    Heavy, long-running endpoints (``_ASYNC_JOB_MCP_TOOLS``) go through the submit/poll
    job model (ADR 0027): they can outlive the MCP client's call cap, so holding
    the call open orphans the subprocess and BLOCKs the next call. Everything
    else stays synchronous and returns its full result inline.
    """
    work_dir_error = _validate_work_dir(arguments.get("work_dir"))
    if work_dir_error is not None:
        return [TextContent(type="text", text=work_dir_error)]

    argv = _params_to_argv(arguments)

    # Inject --transcript-dir for Specialists
    if mcp_tool_def.get("is_specialist"):
        transcript_dir = _resolve_transcript_dir(name, mcp_tool_call_counts)
        argv.extend(["--transcript-dir", str(transcript_dir)])

    # Custom MCP tools run via file path; builtins via python -m
    module = mcp_tool_def["module"]
    if mcp_tool_def.get("is_custom") and mcp_tool_def.get("custom_path"):
        cmd = ["python", mcp_tool_def["custom_path"], *argv]
    else:
        module_path = mcp_tool_def.get("module_path") or f"booley.mcp.{module}"
        cmd = ["python", "-m", module_path, *argv]

    mcp_tool_timeout = _mcp_tool_timeout_seconds(name, arguments, mcp_tool_def)
    logger.info("Dispatching %s (timeout=%ds): %s", name, mcp_tool_timeout, " ".join(cmd))

    if name in _ASYNC_JOB_MCP_TOOLS:
        return await _dispatch_async_job(name, cmd, mcp_tool_timeout, jobs)

    exit_code, stdout, stderr, timed_out = await _run_subprocess(cmd, timeout=mcp_tool_timeout)
    if timed_out:
        _write_synthetic_endpoint_end(name, mcp_tool_timeout)
    report = _try_read_report()
    content = [
        TextContent(type="text", text=_format_mcp_tool_result(exit_code, stdout, stderr, report))
    ]
    return _with_structured_report(content, report)


def _sim_mcp_tool_timeout_seconds(arguments: dict[str, Any], default: int) -> int:
    """Whole-campaign sim watchdog derived from its sequential work units."""
    from booley.flows.sim.flow import (
        _TRACE_CLEANUP_MARGIN_S,
        _resolve_sim_campaign_work_units,
        _resolve_sim_timeout_ms,
    )

    work_dir_raw = arguments.get("work_dir")
    work_dir = Path(work_dir_raw) if work_dir_raw else Path.cwd()
    requested = arguments.get("timeout")
    if requested is None:
        sim_seconds = max(1, _resolve_sim_timeout_ms(work_dir) // 1000)
    else:
        try:
            sim_seconds = max(1, int(requested) // 1000)
        except (TypeError, ValueError):
            return default

    raw_target = str(arguments.get("target") or "").strip()
    target_count = max(1, len([tok for tok in raw_target.split(",") if tok.strip()]))
    try:
        work_units = _resolve_sim_campaign_work_units(
            work_dir,
            raw_target,
            arguments.get("test"),
            arguments.get("skip"),
        )
    except Exception:  # noqa: BLE001 — malformed project input is graded by the child
        work_units = target_count

    campaign_budget_s = sim_seconds * work_units
    if _ticket_baseline_required("cycle_count_"):
        campaign_budget_s *= 2
    trace_margin_s = _TRACE_CLEANUP_MARGIN_S * work_units if arguments.get("trace") else 0
    call_margin_s = 0 if arguments.get("trace") else 30
    return max(default, campaign_budget_s) + trace_margin_s + call_margin_s


def _mcp_tool_timeout_seconds(
    name: str,
    arguments: dict[str, Any],
    mcp_tool_def: dict[str, Any],
) -> int:
    """Outer MCP kill budget for a endpoint subprocess.

    For simulate, the user-facing ``timeout`` argument is the simulator
    budget in milliseconds. The Flow still needs time after that to
    close FIFO trace writers, reap bwave, and write reports, so the MCP
    watchdog must be larger than the sim budget.
    """
    from booley.targets.flow_names import canonical

    name = canonical(name)
    default = int(mcp_tool_def.get("default_timeout") or 600)
    if name in {"synth", "fpga"}:
        # Implementation-flow public timeouts are PER TARGET, while this
        # watchdog owns the whole sequential matrix. Budget every selected
        # target and both baseline/current passes, plus orchestration headroom.
        if name == "synth":
            from booley.flows.synth.flow import _resolve_synth_timeout_ms

            timeout_resolver = _resolve_synth_timeout_ms
            criterion_prefix = "synthesis_ok_"
        else:
            from booley.flows.fpga.flow import _resolve_fpga_timeout_ms

            timeout_resolver = _resolve_fpga_timeout_ms
            criterion_prefix = "fpga_impl_ok_"

        work_dir_raw = arguments.get("work_dir")
        work_dir = Path(work_dir_raw) if work_dir_raw else None
        try:
            per_target_s = max(
                1,
                timeout_resolver(work_dir, arguments.get("timeout")) // 1000,
            )
        except Exception:  # noqa: BLE001 — malformed config is graded by the child
            return default
        raw_target = str(arguments.get("target") or "").strip()
        target_count = max(1, len([tok for tok in raw_target.split(",") if tok.strip()]))
        has_baseline = bool(arguments.get("baseline")) or _ticket_baseline_required(
            criterion_prefix
        )
        pass_count = target_count * (2 if has_baseline else 1)
        setup_margin_s = 60 * pass_count
        finalize_margin_s = 120
        return max(
            default,
            per_target_s * pass_count + setup_margin_s + finalize_margin_s,
        )

    if name != "sim":
        return default
    return _sim_mcp_tool_timeout_seconds(arguments, default)


def _ticket_baseline_required(criterion_prefix: str) -> bool:
    """Whether persisted criteria will auto-enable an implementation baseline."""
    from booley.flows.recipe_evidence import BASELINE_REF_PARAM

    state_path = os.environ.get("BOOLEY_STATE_FILE")
    if not state_path:
        return False
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
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


async def _dispatch_special_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    jobs: _JobManager,
    status_mcp_tool_names: list[str],
) -> McpToolContent | None:
    """Route the fixed, always-nameable meta MCP tools (status/report/poll/cancel/sleep).

    Returns ``None`` when *name* isn't one of these — the caller falls through
    to Booley/B-Wave MCP-tool dispatch.
    """
    # The synchronous meta MCP tools share one shape (visibility gate + handler);
    # a hidden-but-named MCP tool falls through to None like an unknown name.
    sync_meta: dict[str, tuple[Callable[[], bool], Callable[[], McpToolContent]]] = {
        _STATUS_MCP_TOOL_NAME: (
            _status_mcp_tool_visible,
            lambda: _dispatch_status(status_mcp_tool_names),
        ),
        _REPORT_MCP_TOOL_NAME: (_report_mcp_tool_visible, lambda: _dispatch_report(arguments)),
        _TARGETS_MCP_TOOL_NAME: (_targets_mcp_tool_visible, lambda: _dispatch_targets(arguments)),
    }
    entry = sync_meta.get(name)
    if entry is not None:
        visible, handler = entry
        return handler() if visible() else None
    if name == _POLL_MCP_TOOL_NAME and _poll_mcp_tool_visible():
        return await _dispatch_poll(arguments, jobs)
    if name == _CANCEL_MCP_TOOL_NAME and _poll_mcp_tool_visible():
        return await _dispatch_cancel(arguments, jobs)
    if name == _SLEEP_MCP_TOOL_NAME and _sleep_mcp_tool_visible():
        return await _dispatch_sleep(arguments)
    return None


def _build_server(
    lifetime: _McpLifetime | None = None,
) -> tuple[Server, list[dict[str, Any]]]:
    """Build the MCP server with all MCP tool registrations."""
    _reconcile_orphaned_locks()
    _reconcile_orphaned_jobs()
    lifetime = lifetime or _McpLifetime(None, None)
    server = Server("booley")

    mcp_tools, discovery_errors = _discover_booley_mcp_tools()
    logger.info("Discovered %d Booley MCP tools", len(mcp_tools))
    if discovery_errors:
        _log_discovery_errors(mcp_tools, discovery_errors)

    mcp_tool_index = _build_mcp_tool_index(mcp_tools)
    status_mcp_tool_names = list(mcp_tool_index)
    all_mcp_tools = _build_mcp_tool_list(mcp_tools)
    jobs = _JobManager(lifetime)

    @server.list_tools()
    async def handle_list_tools() -> list[McpSdkTool]:
        lifetime.mark_activity()
        return all_mcp_tools

    mcp_tool_call_counts: dict[str, int] = collections.defaultdict(int)

    @server.call_tool()
    async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None,
    ) -> McpToolContent:
        arguments = arguments or {}
        lifetime.mark_mcp_endpoint_start()
        try:
            return await _handle_call_tool(name, arguments)
        finally:
            lifetime.mark_mcp_endpoint_end()

    async def _handle_call_tool(
        name: str,
        arguments: dict[str, Any],
    ) -> McpToolContent:
        from booley.targets.flow_names import canonical

        name = canonical(name)
        special_result = await _dispatch_special_mcp_tool(
            name, arguments, jobs, status_mcp_tool_names
        )
        if special_result is not None:
            return special_result

        mcp_tool_def = mcp_tool_index.get(name)
        if mcp_tool_def is None:
            hidden = _interactive_hidden_note(name)
            return [TextContent(type="text", text=hidden or f"Unknown MCP tool: {name}")]

        bwave_result = await _dispatch_bwave(name, arguments)
        if bwave_result is not None:
            return _prepend_changed_health_alert(bwave_result)

        return _prepend_changed_health_alert(
            await _dispatch_booley_mcp_tool(
                name,
                arguments,
                mcp_tool_def,
                mcp_tool_call_counts,
                jobs,
            )
        )

    return server, mcp_tools


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
    disappears on container exit.  In Ticket Mode the harness sets
    ``BOOLEY_LOGS_DIR`` explicitly, so this is a no-op.

    Interactive Mode is detected by:
      - ``BOOLEY_LOGS_DIR`` not already set (harness sets it in Ticket Mode);
      - ``.booley_project/`` present in CWD (the project root the outer agent
        bind-mounted into the container at ``/work``).

    Logs accumulate at ``.booley_project/.interactive_logs/<session-id>/``
    (gitignored — see the template emitted by ``booley init``).
    """
    if os.environ.get("BOOLEY_LOGS_DIR"):
        return
    project_dir = Path.cwd() / ".booley_project"
    if not project_dir.is_dir():
        return
    session_id = _interactive_session_id()
    logs_dir = project_dir / ".interactive_logs" / session_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BOOLEY_LOGS_DIR"] = str(logs_dir)
    logger.info(
        "Interactive Mode: BOOLEY_LOGS_DIR=%s (session %s)",
        logs_dir,
        session_id,
    )


def _load_backend_config_from_toml() -> None:
    """Honor ``[agent]``/``[sandbox]``/``[models]`` from the project's booley.toml.

    Interactive Mode has no developer to configure backends, so without this
    the module-global ``BackendConfig`` stays unset in *this* server process.
    Loading here makes the server honor booley.toml exactly like Ticket Mode
    (the developer) and ``booley doctor`` already do. (Specialist endpoint
    subprocesses resolve the provider independently — ``get_backend_config()``
    reads ``BOOLEY_PROJECT_DIR/booley.toml`` when the env hand-off is absent —
    so they no longer silently fall back to a default provider either.)

    ``BOOLEY_PROJECT_DIR`` points at the ``.booley_project`` dir when set, but
    ``load_models_config`` wants the repo root that *contains* it, so use the
    parent (else CWD). A missing/unparseable booley.toml is handled inside
    ``load_models_config`` (defaults preserved); the broad guard only covers
    import-time surprises so a config hiccup never blocks server startup.
    """
    try:
        from booley.config.settings import load_models_config

        project_dir = os.environ.get("BOOLEY_PROJECT_DIR", "")
        project_root = Path(project_dir).parent if project_dir else Path.cwd()
        load_models_config(project_root)
    except Exception:  # noqa: BLE001 — config preload must not block startup
        logger.debug("Failed to load backend config from booley.toml", exc_info=True)


async def _main() -> None:
    _maybe_configure_interactive_logs_dir()
    _load_backend_config_from_toml()
    lifetime = _McpLifetime.from_env()
    server, _ = _build_server(lifetime)
    async with stdio_server() as (read_stream, write_stream):
        run_task = asyncio.create_task(
            server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="booley",
                    server_version=__version__,
                    capabilities=ServerCapabilities(
                        tools=ToolsCapability(),
                    ),
                ),
            ),
        )
        watchdog_task = asyncio.create_task(lifetime.wait_until_stale())
        done, pending = await asyncio.wait(
            {run_task, watchdog_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
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
    """Serve the MCP tools over stateless streamable HTTP on loopback.

    Interactive Mode transport (ADR 0023): started by the devcontainer's
    ``postStartCommand`` (via ``incontainer_register.ensure_http_server``), so
    a container stop→start transparently brings the endpoint back — the agent
    apps hold only a URL, not a child process. ``stateless=True`` is load-
    bearing: Claude Code keeps using its cached ``Mcp-Session-Id`` after a
    server restart, and a stateful server would reject it with a 404 the
    client never recovers from.
    """
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    _maybe_configure_interactive_logs_dir()
    _load_backend_config_from_toml()
    lifetime = _McpLifetime.from_env(self_exit=False)
    server, _ = _build_server(lifetime)
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,  # plain JSON responses; no SSE stream to strand
        stateless=True,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with manager.run():
            yield

    app = Starlette(
        routes=[Mount(HTTP_ENDPOINT_PATH, app=manager.handle_request)],
        lifespan=lifespan,
    )
    logger.info("Interactive MCP server (HTTP) on %s:%d", _HTTP_HOST, port)
    uvicorn.run(app, host=_HTTP_HOST, port=port, log_level="warning")


def main() -> None:
    import argparse

    # Runtime-location guard (ADR 0028): the Booley MCP server serves the Session
    # Runtime's MCP-tool stack — it has no meaning host-side.
    location_error = runtime_context.container_only_error("booley-mcp")
    if location_error is not None:
        print(location_error, file=sys.stderr)
        raise SystemExit(2)

    # Codex REPLACES the MCP child env per config.toml [env] (see
    # harness.mcp_config), so a config that doesn't forward the proxy vars
    # leaves this server — and every endpoint subprocess under it — with no
    # egress path. Self-heal here so the whole endpoint subprocess tree inherits it.
    if runtime_context.ensure_proxy_env():
        logger.debug("proxy env was absent in-container — defaulted to booley-proxy")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("BOOLEY_MCP_TRANSPORT", "stdio"),
        help="stdio: client-spawned child (Ticket Mode); "
        "http: standalone loopback server (Interactive Mode)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"HTTP port (default: ${HTTP_PORT_ENV} or {DEFAULT_HTTP_PORT})",
    )
    args = parser.parse_args()
    if args.transport == "http":
        _run_http(args.port if args.port is not None else http_port())
    else:
        asyncio.run(_main())


if __name__ == "__main__":
    main()
