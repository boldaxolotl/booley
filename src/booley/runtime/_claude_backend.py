"""Claude Agent SDK backend implementation."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    UserMessage,
    query,
)

from booley.config.pricing import context_limit
from booley.core.models import AgentCallParams, AgentResult
from booley.runtime.timefmt import format_human_datetime, rfc3339_from_epoch, utc_now_rfc3339

from ._claude_transcript_md import (  # noqa: F401 — re-exported for backward compat
    _claude_md_block_lines,
    _claude_md_prompt_lines,
    _claude_md_usage_lines,
    _claude_write_markdown,
)
from ._cost import estimate_cost, format_usage_log
from ._retry import (
    MAX_API_RETRIES,
    RATE_LIMIT_FALLBACK_BACKOFF_S,
    RATE_LIMIT_SLEEP_BUFFER_S,
    _is_transient_error,
    compute_backoff,
    transcript_path_for_attempt,
    transcript_path_for_label,
)
from .agent_errors import (
    ContextExhaustedError,
    TransientAPIError,
    UsageLimitError,
    is_context_exhausted,
    is_usage_limit,
)
from .developer_budget import DeveloperBudget, run_with_developer_budget
from .prompt_artifacts import write_prompt_artifacts

logger = logging.getLogger(__name__)

_PROVIDER_WEB_MCP_TOOLS = ("WebFetch", "WebSearch")

_SDK_TEARDOWN_EXCEPTIONS: tuple[type[BaseException], ...] = (
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    anyio.BrokenResourceError,
    asyncio.CancelledError,
    ConnectionError,
)

STDERR_RING_SIZE = 200
_ACTIVITY_RING_SIZE = 8


@dataclass
class _StreamState:
    """Mutable per-attempt stream progress shared with exception handlers.

    ``_process_stream`` mutates this in place as messages arrive, so if it
    raises mid-stream the crash handlers still see the real turn count /
    activity ring, and ``_finalize_result`` still gets the session id from a
    ResultMessage that arrived before a teardown exception. All fields have
    safe defaults, so nothing is ever unbound even if the stream dies before
    the first message.
    """

    turn_count: int = 0
    session_id: str | None = None
    last_activity: deque[str] = field(default_factory=lambda: deque(maxlen=_ACTIVITY_RING_SIZE))
    pending_file_edits: dict[str, str] = field(default_factory=dict)


class ClaudeSDKBackend:
    """Agent backend using the Claude Agent SDK (claude-agent-sdk package).

    ``auth_mode`` is the resolved ``[agent] auth`` policy. Under
    ``subscription`` the agent env is scrubbed of ``ANTHROPIC_API_KEY`` (see
    ``_build_sdk_options``); under ``api_key`` the health check fails loud
    when the key is absent, because the CLI would otherwise silently fall
    back to — and bill — the subscription.
    """

    def __init__(self, auth_mode: str = "auto") -> None:
        self._auth_mode = auth_mode

    @property
    def name(self) -> str:
        return "Claude SDK"

    def health_check(self) -> str | None:
        from booley.runtime import auth_token

        if (
            self._auth_mode == "api_key"
            and not (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        ):
            return (
                "[agent] auth = 'api_key' but ANTHROPIC_API_KEY is not set — "
                "export it, or switch auth to 'subscription' or 'auto'"
            )
        if self._auth_mode == "subscription" and (
            auth_token.effective_credential(auth_token.APP_CLAUDE, policy="subscription") is None
        ):
            return (
                "[agent] auth = 'subscription' but no subscription credential found "
                "(no login at ~/.claude/.credentials.json and no CLAUDE_CODE_OAUTH_TOKEN) — "
                "run `claude login` or `booley auth`"
            )
        return None

    async def call(
        self,
        params: AgentCallParams,
        **kwargs: Any,
    ) -> AgentResult:
        """Call a Claude agent via the Agent SDK with retry."""
        on_event = kwargs.pop("on_event", None)
        budget: DeveloperBudget | None = kwargs.pop("developer_budget", None)

        options = _build_sdk_options(params, auth_mode=self._auth_mode)
        _log_call_start(params)

        # --- Retry loop ---
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                return await self._call_once(
                    params.prompt,
                    options,
                    params.timeout_seconds,
                    transcript_path=_transcript_path_for_attempt(params.transcript_path, attempt),
                    output_format=params.output_format,
                    capture_agent_capability_calls=params.capture_agent_capability_calls,
                    attempt=attempt,
                    label=params.label,
                    on_event=on_event,
                    budget=budget,
                )
            except (UsageLimitError, ContextExhaustedError):
                raise
            except TransientAPIError as e:
                if attempt >= MAX_API_RETRIES:
                    logger.error("All %d retries exhausted: %s", MAX_API_RETRIES, e)
                    raise
                backoff = compute_backoff(attempt, e.retry_after)
                logger.warning(
                    "Transient error (attempt %d/%d): %s. Retrying in %.0fs",
                    attempt,
                    MAX_API_RETRIES,
                    e,
                    backoff,
                )
                if backoff > 0:
                    if budget is not None:
                        budget.resume_prefix("claude-mcp:")
                        budget.pause("claude-retry-backoff", "transient retry")
                    try:
                        if budget is None:
                            await anyio.sleep(backoff)
                        else:
                            await run_with_developer_budget(anyio.sleep(backoff), budget)
                    finally:
                        if budget is not None:
                            budget.resume("claude-retry-backoff")

        raise RuntimeError("Agent call exhausted all retries without result")

    # --- Internal helpers ---

    async def _process_stream(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        timeout_seconds: int,
        counters: _UsageCounters,
        transcript_file: Any,
        on_event: Any,
        state: _StreamState,
        budget: DeveloperBudget | None = None,
    ) -> None:
        """Stream agent messages, recording progress into ``state``.

        Progress is mutated in place (rather than returned) so that partial
        values survive when this coroutine raises mid-stream — the caller's
        exception handlers read them for crash diagnostics.

        Without a Developer budget, the timeout is **idle/heartbeat-aware**:
        ``timeout_seconds`` bounds the gap between consecutive stream events,
        and the deadline is reset every time *any* message arrives. A stream
        that is merely slow — e.g. throttled to a crawl under heavy 7-day
        rate-limit pressure — keeps resetting the deadline and is never killed
        as long as it keeps making progress. Only a genuine stall (no event at
        all for ``timeout_seconds``) raises ``TimeoutError``. This is strictly
        more permissive than the old total-wall-clock timeout: anything that
        completed before still completes, and throttled-but-progressing turns
        that used to die mid-stream now survive.
        """
        if budget is not None:
            await run_with_developer_budget(
                _consume_claude_stream(
                    prompt,
                    options,
                    counters,
                    transcript_file,
                    on_event,
                    state,
                    budget=budget,
                ),
                budget,
            )
            return

        with anyio.fail_after(timeout_seconds) as idle_scope:
            await _consume_claude_stream(
                prompt,
                options,
                counters,
                transcript_file,
                on_event,
                state,
                on_message=lambda: setattr(
                    idle_scope, "deadline", anyio.current_time() + timeout_seconds
                ),
            )

    async def _call_once(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        timeout_seconds: int,
        *,
        transcript_path: Path | None,
        output_format: dict[str, Any] | None,
        capture_agent_capability_calls: list[str] | None = None,
        attempt: int = 1,
        label: str | None = None,
        on_event: Any = None,
        budget: DeveloperBudget | None = None,
    ) -> AgentResult:
        """Single attempt to call the agent."""
        transcript_path, counters, stderr_buffer = self._prepare_call(
            prompt,
            options,
            transcript_path=transcript_path,
            attempt=attempt,
            label=label,
            capture_agent_capability_calls=capture_agent_capability_calls,
        )

        # Stream progress lives outside the try so it is always bound: even if
        # _process_stream raises before yielding anything (and the handler
        # swallows a post-result teardown error), _finalize_result below still
        # reads valid defaults instead of hitting an unbound local.
        stream_state = _StreamState()

        try:
            with contextlib.ExitStack() as stack:
                transcript_file = _open_transcript(stack, transcript_path)
                if transcript_file is not None:
                    _write_prompt_header(
                        transcript_file,
                        getattr(options, "system_prompt", None),
                        prompt,
                    )
                try:
                    stream_kwargs = {"budget": budget} if budget is not None else {}
                    await self._process_stream(
                        prompt,
                        options,
                        timeout_seconds,
                        counters,
                        transcript_file,
                        on_event,
                        stream_state,
                        **stream_kwargs,
                    )
                except TimeoutError:
                    # A ResultMessage already landed before the stall, so the
                    # turn's work is complete — a stalled SDK teardown must not
                    # discard a finished result (that is the "30 min of spend
                    # for nothing" failure). Swallow and finalize the result.
                    if counters.got_result:
                        logger.debug(
                            "Swallowed post-result idle timeout (SDK teardown stalled "
                            "after %ds with a result already captured)",
                            timeout_seconds,
                        )
                    else:
                        logger.error(
                            "Agent stream idle for %ds (no stream event) — timing out",
                            timeout_seconds,
                        )
                        _dump_crash_context(
                            stderr_buffer,
                            stream_state.last_activity,
                            stream_state.turn_count,
                            reason="idle timeout",
                            attempt=attempt,
                            transcript_path=transcript_path,
                        )
                        raise
                except (
                    Exception  # noqa: BLE001 — funnel failures into unified crash handling
                ) as e:
                    _handle_stream_exception(
                        e,
                        counters.got_result,
                        stderr_buffer,
                        stream_state.last_activity,
                        stream_state.turn_count,
                        attempt,
                        transcript_path,
                    )
        finally:
            _claude_write_markdown(transcript_path)

        return _finalize_result(
            counters,
            options.model,
            output_format,
            label,
            session_id=stream_state.session_id,
        )

    def _prepare_call(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        *,
        transcript_path: Path | None,
        attempt: int,
        label: str | None,
        capture_agent_capability_calls: list[str] | None = None,
    ) -> tuple[Path | None, _UsageCounters, deque[str]]:
        """Set up prompt artifacts, usage counters, and stderr capture.

        Mutates ``options.stderr`` to install the buffering callback and returns
        (transcript_path, counters, stderr_buffer) for the try-body.
        """
        transcript_path = transcript_path_for_label(transcript_path, label)
        write_prompt_artifacts(
            transcript_path,
            system_prompt=getattr(options, "system_prompt", None),
            user_prompt=prompt,
            metadata={
                "backend": "claude-sdk",
                "label": label,
                "model": getattr(options, "model", None),
                "cwd": str(getattr(options, "cwd", "")),
                "attempt": attempt,
            },
        )

        counters = _UsageCounters(frozenset(capture_agent_capability_calls or ()))
        stderr_buffer: deque[str] = deque(maxlen=STDERR_RING_SIZE)

        def _stderr_callback(line: str) -> None:
            stderr_buffer.append(line)
            logger.debug("[cli-stderr] %s", line.rstrip())

        options.stderr = _stderr_callback

        logger.debug("Agent CLI: %s", getattr(options, "cli_path", None) or "<SDK-selected>")
        return transcript_path, counters, stderr_buffer

    # _call_once is defined above (after call), remaining helpers below.


# ---------------------------------------------------------------------------
# Helpers — call setup
# ---------------------------------------------------------------------------


def _log_call_start(params: AgentCallParams) -> None:
    """Log agent call start with model and prompt preview."""
    short_model = params.model.split("-")[1] if "-" in params.model else params.model
    tag = f"{params.label} " if params.label else ""
    logger.info("%sAgent started (%s)", tag, short_model)
    prompt_preview = params.prompt[:200].replace("\n", " ")
    if len(params.prompt) > 200:
        prompt_preview += "..."
    logger.debug(
        "Agent details: model=%s, max_turns=%s, cwd=%s",
        params.model,
        params.max_turns or "unlimited",
        Path(params.cwd).resolve(),
    )
    logger.debug("Agent prompt (%d chars): %s", len(params.prompt), prompt_preview)


def _claude_env_overrides(auth_mode: str) -> dict[str, str]:
    """Child-process env the Claude CLI must see.

    Nonessential traffic is disabled by default without mutating the parent
    process. An explicit ambient value is preserved. Authentication entries
    are keyed by the ``[agent] auth`` policy.

    Under ``subscription``, the CLI's own precedence would bill an exported
    ``ANTHROPIC_API_KEY`` over the subscription, and its prescribed remedy is
    to unset the variable. ``options.env`` can only merge OVER the inherited
    environment (the SDK builds ``{**os.environ, **options.env}``), so "unset"
    is expressed as an empty value — which the CLI treats as absent (empty
    string is falsy on every process.env read).

    Outside ``api_key``, the rotation-free ``booley auth`` credential is handed
    to the CLI via its env. The registrar's settings.json ``env`` route does
    NOT reach SDK runs: ``setting_sources`` excludes "user" settings, so
    without this the agent falls back to the seeded
    ``~/.claude/.credentials.json`` — whose refresh token the HOST rotates out
    from under long-lived containers, crashing every ticket at launch with
    "OAuth session expired and could not be refreshed". An ambient
    ``CLAUDE_CODE_OAUTH_TOKEN`` still wins (``resolve_token`` checks env
    first); under ``auto`` an exported ``ANTHROPIC_API_KEY`` still outranks it
    CLI-side, so precedence holds.
    """
    from booley.runtime import auth_token

    env_overrides = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": os.environ.get(
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"
        )
    }
    if auth_mode == "subscription":
        env_overrides["ANTHROPIC_API_KEY"] = ""
    if auth_mode != "api_key":
        stored = auth_token.resolve_token(auth_token.APP_CLAUDE)
        if stored and not (os.environ.get(auth_token.ENV_VAR) or "").strip():
            env_overrides[auth_token.ENV_VAR] = stored
    return env_overrides


def _build_sdk_options(
    params: AgentCallParams,
    auth_mode: str = "auto",
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions from AgentCallParams."""
    cwd_path = Path(params.cwd).resolve()
    options = ClaudeAgentOptions(
        model=params.model,
        cwd=cwd_path,
        permission_mode="bypassPermissions",
    )

    env_overrides = _claude_env_overrides(auth_mode)
    if env_overrides:
        options.env = env_overrides

    if params.max_turns is not None:
        options.max_turns = params.max_turns
    if params.allowed_agent_capabilities is not None:
        options.allowed_agent_capabilities = params.allowed_agent_capabilities
    # Provider-hosted web MCP tools bypass the container network namespace. Keep
    # every harness-launched agent offline even when it runs outside Booley's
    # managed image (the image also carries an immutable Claude policy).
    denied_mcp_tools = list(
        dict.fromkeys([*(params.disallowed_agent_capabilities or []), *_PROVIDER_WEB_MCP_TOOLS])
    )
    options.disallowed_agent_capabilities = denied_mcp_tools
    if params.system_prompt is not None:
        options.system_prompt = params.system_prompt
    if params.max_budget_usd is not None:
        options.max_budget_usd = params.max_budget_usd
    if params.output_format is not None:
        if params.output_format.get("type") != "json_schema":
            options.output_format = {"type": "json_schema", "schema": params.output_format}
        else:
            options.output_format = params.output_format

    if params.session_id is not None:
        options.resume = params.session_id
    if params.resume_session:
        options.continue_conversation = True

    _apply_mcp_servers(options, params)

    options.setting_sources = ["project"] if params.needs_skills else []
    options.debug_stderr = None
    return options


def _apply_mcp_servers(
    options: ClaudeAgentOptions,
    params: AgentCallParams,
) -> None:
    """Wire the Booley MCP server into the SDK options.

    Mirrors the Codex nested-agent contract: when ``nested_mcp_tools`` is set
    (a recursion-safe allowlist), the spawned MCP server receives
    ``BOOLEY_NESTED_AGENT=1`` + ``BOOLEY_NESTED_MCP_TOOLS=<csv>`` in its env,
    which ``mcp_server._nested_allowlist()`` reads to filter discovery. When
    ``nested_mcp_tools is None`` the call is developer-level — full Booley
    MCP, no filter env.

    The SDK inherits the parent env for stdio servers, so only the
    nested-specific vars need to be set here (unlike Codex, which replaces it).
    """
    # BOOLEY_MCP_NESTED=1 tells the spawned server it is a sub-agent's server
    # so it skips orphan-lock reconciliation of the parent's in-flight events.
    server_env: dict[str, str] = {"BOOLEY_MCP_NESTED": "1"}
    if params.nested_mcp_tools is not None:
        server_env["BOOLEY_NESTED_AGENT"] = "1"
        server_env["BOOLEY_NESTED_MCP_TOOLS"] = ",".join(params.nested_mcp_tools)

    options.mcp_servers = {
        "booley": {
            "type": "stdio",
            "command": "python",
            "args": ["-m", "booley.mcp.server"],
            "env": server_env,
        }
    }
    # Only Booley loads — the project's own .mcp.json IDE entry is ignored.
    options.strict_mcp_config = True


# ---------------------------------------------------------------------------
# Helpers — usage tracking
# ---------------------------------------------------------------------------


class _UsageCounters:
    """Mutable token/cost counters accumulated during streaming."""

    def __init__(self, capture_names: frozenset[str] = frozenset()) -> None:
        self.total_input = 0
        self.total_output = 0
        self.total_cached = 0
        self.total_cache_create = 0
        # Prompt size of the most recent assistant message — i.e. how full the
        # context window is *right now*. Unlike the cumulative totals this does
        # not grow with turn count, so it is the number worth showing live.
        self.context_tokens = 0
        self.final_text = ""
        self.structured: dict | None = None
        self.total_cost = 0.0
        self.got_result = False
        # MCP-tool-call inputs to collect (by MCP tool name) and the collected results.
        self._capture_names = capture_names
        self.captured_agent_capability_calls: dict[str, list[dict]] = {}
        # Totals already handed to the live-usage callback. Deltas are emitted
        # rather than absolutes so a consumer can simply accumulate, and so a
        # fresh counter for the next agent call starts from zero without the
        # display jumping backwards.
        self._emitted_output = 0
        self._emitted_cost = 0.0

    def update_from_assistant(self, message: AssistantMessage) -> None:
        """Accumulate usage from an AssistantMessage."""
        if message.usage:
            prompt = (
                message.usage.get("input_tokens", 0)
                + message.usage.get("cache_creation_input_tokens", 0)
                + message.usage.get("cache_read_input_tokens", 0)
            )
            self.total_input += prompt
            self.context_tokens = prompt
            self.total_cached += message.usage.get("cache_read_input_tokens", 0)
            self.total_cache_create += message.usage.get("cache_creation_input_tokens", 0)
            self.total_output += message.usage.get("output_tokens", 0)

    def capture_mcp_tool_uses(self, message: AssistantMessage) -> None:
        """Record ``input`` dicts of MCP-tool-use blocks named in ``_capture_names``.

        The agent may split its report across several calls, so inputs are
        appended in arrival order rather than overwriting. No-op unless the
        caller asked to capture at least one MCP tool name.
        """
        if not self._capture_names:
            return
        for block in message.content or []:
            name = getattr(block, "name", None)
            if name not in self._capture_names:
                continue
            inp = getattr(block, "input", None)
            if isinstance(inp, dict):
                self.captured_agent_capability_calls.setdefault(name, []).append(dict(inp))

    def apply_result(self, message: ResultMessage) -> None:
        """Finalize counters from the ResultMessage."""
        self.got_result = True
        if message.result:
            self.final_text = message.result
        if message.structured_output is not None:
            self.structured = message.structured_output
        if message.total_cost_usd:
            self.total_cost = message.total_cost_usd
        if message.usage:
            agg_in = (
                message.usage.get("input_tokens", 0)
                + message.usage.get("cache_creation_input_tokens", 0)
                + message.usage.get("cache_read_input_tokens", 0)
            )
            self.total_input = max(self.total_input, agg_in)
            self.total_cached = max(
                self.total_cached, message.usage.get("cache_read_input_tokens", 0)
            )
            self.total_cache_create = max(
                self.total_cache_create, message.usage.get("cache_creation_input_tokens", 0)
            )
            self.total_output = max(self.total_output, message.usage.get("output_tokens", 0))

    def estimated_cost(self, model: str) -> float:
        """Authoritative billed cost once known, else a price-table estimate."""
        if self.total_cost > 0:
            return self.total_cost
        return estimate_cost(
            model,
            self.total_input,
            self.total_cached,
            self.total_output,
            self.total_cache_create,
        )

    def usage_delta(self, model: str) -> tuple[int, float]:
        """Return (output_tokens, cost_usd) accrued since the previous call.

        Output tokens rather than a flat input+output sum: with prompt caching
        the cumulative input is dominated by re-reads of the same context and
        grows with turn count, so it tracks conversation length rather than
        work done. Output is monotone and is what the Console displays.

        Cost mirrors :meth:`estimated_cost` — the authoritative
        ``total_cost_usd`` from the ResultMessage once it lands, and an estimate
        before then. The switch from estimate to actual can yield a small
        negative delta, which is intentional: the running total then converges
        on the billed figure instead of staying stuck at an over- or
        under-estimate.
        """
        cost = self.estimated_cost(model)
        delta = (self.total_output - self._emitted_output, cost - self._emitted_cost)
        self._emitted_output = self.total_output
        self._emitted_cost = cost
        return delta


def _open_transcript(
    stack: contextlib.ExitStack,
    transcript_path: Path | None,
) -> io.TextIOBase | None:
    """Open a transcript file within an ExitStack, or return None."""
    if transcript_path is None:
        return None
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    return stack.enter_context(transcript_path.open("w", encoding="utf-8"))


def _dispatch_on_event(on_event: Any, message: AssistantMessage) -> None:
    """Forward text and thinking blocks from an AssistantMessage to the on_event callback."""
    if on_event is None:
        return
    for etype, attr in (("agent_thinking", "thinking"), ("agent_text", "text")):
        chunks = [
            getattr(block, attr)
            for block in (message.content or [])
            if hasattr(block, attr) and getattr(block, attr)
        ]
        if chunks:
            try:
                on_event({"type": etype, "text": "\n".join(chunks)})
            except (
                Exception  # noqa: BLE001
            ):  # display failure is best-effort; do not abort a paid turn
                logger.debug("on_event error (swallowed)", exc_info=True)


async def _consume_claude_stream(
    prompt: str,
    options: ClaudeAgentOptions,
    counters: _UsageCounters,
    transcript_file: Any,
    on_event: Any,
    state: _StreamState,
    *,
    budget: DeveloperBudget | None = None,
    on_message: Callable[[], None] | None = None,
) -> None:
    """Consume one Claude query stream and route every message."""
    async for message in query(prompt=prompt, options=options):
        if on_message is not None:
            on_message()
        if budget is not None:
            _update_budget_for_claude_message(message, budget)
        if isinstance(message, AssistantMessage):
            state.turn_count += 1
            _record_activity(state.last_activity, message)
            _dispatch_on_event(on_event, message)
            counters.update_from_assistant(message)
            counters.capture_mcp_tool_uses(message)
            _dispatch_usage(on_event, counters, options.model)
            if transcript_file is not None:
                _write_transcript_turn(transcript_file, message)
            _capture_pending_file_edits(message, state)
        elif isinstance(message, UserMessage):
            _dispatch_completed_file_edits(on_event, message, state)
        elif isinstance(message, ResultMessage):
            counters.apply_result(message)
            _dispatch_usage(on_event, counters, options.model)
            state.session_id = getattr(message, "session_id", None)
        elif isinstance(message, RateLimitEvent):
            await _handle_rate_limit_event(message, budget)


def _update_budget_for_claude_message(message: object, budget: DeveloperBudget) -> None:
    """Pause active time between Claude's Booley tool-use and result blocks."""
    content = getattr(message, "content", None)
    blocks = content if isinstance(content, list) else []
    if isinstance(message, AssistantMessage):
        for block in blocks:
            name = getattr(block, "name", None)
            call_id = getattr(block, "id", None)
            if not _is_booley_mcp_name(name) or not isinstance(call_id, str):
                continue
            budget.pause(f"claude-mcp:{call_id}", f"waiting for {_short_mcp_name(name)}")
    elif isinstance(message, UserMessage):
        for block in blocks:
            call_id = getattr(block, "tool_use_id", None)
            if isinstance(call_id, str):
                budget.resume(f"claude-mcp:{call_id}")


def _is_booley_mcp_name(name: object) -> bool:
    return isinstance(name, str) and name.startswith("mcp__booley__")


def _short_mcp_name(name: str) -> str:
    return name.removeprefix("mcp__booley__")


_EDIT_PATH_FIELDS = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Write": "file_path",
}


def _capture_pending_file_edits(message: AssistantMessage, state: _StreamState) -> None:
    """Remember Claude file-operation paths until their result messages arrive."""
    for block in message.content or []:
        field_name = _EDIT_PATH_FIELDS.get(getattr(block, "name", None))
        mcp_tool_id = getattr(block, "id", None)
        mcp_tool_input = getattr(block, "input", None)
        if (
            field_name is None
            or not isinstance(mcp_tool_id, str)
            or not isinstance(mcp_tool_input, dict)
        ):
            continue
        path = mcp_tool_input.get(field_name)
        if isinstance(path, str) and path:
            state.pending_file_edits[mcp_tool_id] = path


def _dispatch_completed_file_edits(
    on_event: Any,
    message: UserMessage,
    state: _StreamState,
) -> None:
    """Emit successful Claude file-operation results as backend-neutral events."""
    paths: list[str] = []
    content = message.content if isinstance(message.content, list) else []
    for block in content:
        mcp_tool_id = getattr(block, "tool_use_id", None)
        path = state.pending_file_edits.pop(mcp_tool_id, None)
        if path is not None and not getattr(block, "is_error", False):
            paths.append(path)
    if not paths or on_event is None:
        return
    try:
        on_event({"type": "file_change", "paths": paths})
    except Exception:  # display failure is best-effort; do not abort a paid turn  # noqa: BLE001
        logger.debug("on_event file change error (swallowed)", exc_info=True)


def _dispatch_usage(on_event: Any, counters: _UsageCounters, model: str) -> None:
    """Forward the usage accrued since the last dispatch to ``on_event``.

    Lets the Console tick its status bar mid-run instead of standing still
    until the agent's final result lands. ``output_tokens``/``cost_usd`` are
    deltas the consumer accumulates; ``context_tokens`` is an absolute
    snapshot of how full the window is right now.
    """
    if on_event is None:
        return
    output_tokens, cost = counters.usage_delta(model)
    if not output_tokens and not cost:
        return
    try:
        on_event(
            {
                "type": "usage",
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "context_tokens": counters.context_tokens,
                "context_limit": context_limit(model),
            }
        )
    except Exception:  # display failure is best-effort; do not abort a paid turn  # noqa: BLE001
        logger.debug("on_event usage error (swallowed)", exc_info=True)


def _handle_stream_exception(
    exc: Exception,
    got_result: bool,
    stderr_buffer: deque[str],
    last_activity: deque[str],
    turn_count: int,
    attempt: int,
    transcript_path: Path | None,
) -> None:
    """Handle exceptions from the SDK stream, re-raising as appropriate."""
    if isinstance(exc, TransientAPIError):
        if got_result:
            logger.debug("Swallowed post-result transient error (SDK teardown artifact)")
            return
        _dump_crash_context(
            stderr_buffer,
            last_activity,
            turn_count,
            reason="transient API error",
            attempt=attempt,
            transcript_path=transcript_path,
        )
        raise exc

    # intentionally broad: SDK can raise arbitrary exceptions
    if got_result and isinstance(exc, _SDK_TEARDOWN_EXCEPTIONS):
        logger.debug(
            "Swallowed post-result SDK-teardown exception: %s: %s",
            type(exc).__name__,
            exc,
        )
        return

    _dump_crash_context(
        stderr_buffer,
        last_activity,
        turn_count,
        reason=f"exception: {type(exc).__name__}",
        attempt=attempt,
        transcript_path=transcript_path,
    )
    if is_usage_limit(str(exc)):
        raise UsageLimitError(str(exc), provider="claude") from exc
    if is_context_exhausted(str(exc)):
        raise ContextExhaustedError(str(exc), provider="claude") from exc
    if _is_transient_error(exc):
        logger.warning("Transient API error: %s", exc)
        raise TransientAPIError(str(exc)) from exc
    logger.error("Agent call failed: %s", exc, exc_info=True)
    raise exc


def _try_structured_fallback(
    output_format: dict[str, Any] | None,
    structured: dict | None,
    final_text: str,
) -> tuple[dict | None, bool]:
    """Attempt structured output fallback via JSON extraction."""
    if output_format is None or structured:
        return structured, False
    from .agent import extract_json

    extracted = extract_json(final_text)
    if extracted is not None:
        logger.warning("SDK structured_output missing/empty -- recovered via JSON extraction")
        return extracted, True
    logger.warning("SDK structured_output missing/empty and JSON extraction failed")
    return None, True


def _finalize_result(
    c: _UsageCounters,
    model: str,
    output_format: dict[str, Any] | None,
    label: str | None,
    *,
    session_id: str | None = None,
) -> AgentResult:
    """Build AgentResult from counters, apply structured fallback, log summary."""
    structured, structured_fallback = _try_structured_fallback(
        output_format,
        c.structured,
        c.final_text,
    )
    cost = c.estimated_cost(model)

    result = AgentResult(
        output=c.final_text,
        structured=structured,
        input_tokens=c.total_input,
        output_tokens=c.total_output,
        cached_tokens=c.total_cached,
        cache_create_tokens=c.total_cache_create,
        cost_usd=cost,
        structured_fallback=structured_fallback,
        session_id=session_id,
        captured_agent_capability_calls=c.captured_agent_capability_calls,
    )

    tag = f"{label} " if label else ""
    logger.info(
        "%sAgent done (%s)",
        tag,
        format_usage_log(c.total_input, c.total_cached, c.total_output, cost),
    )
    _log_result_details(c, cost, structured)
    return result


def _log_result_details(
    c: _UsageCounters,
    cost: float,
    structured: dict | None,
) -> None:
    """Emit DEBUG-level result details (preview, token counts, structured keys)."""
    output_preview = c.final_text[:300].replace("\n", " ") if c.final_text else "(empty)"
    if len(c.final_text) > 300:
        output_preview += "..."
    logger.debug(
        "Agent tokens: %d input (%d cached) + %d output, $%.1f, response=%d chars",
        c.total_input,
        c.total_cached,
        c.total_output,
        cost,
        len(c.final_text),
    )
    logger.debug("Agent response preview: %s", output_preview)
    if structured is not None:
        logger.debug(
            "Agent structured keys: %s",
            list(structured.keys()) if isinstance(structured, dict) else type(structured).__name__,
        )


# ---------------------------------------------------------------------------
# Helpers — streaming and crash diagnostics
# ---------------------------------------------------------------------------


def _record_activity(ring: deque[str], msg: AssistantMessage) -> None:
    """Append a one-line summary of an AssistantMessage to the activity ring."""
    for block in msg.content or []:
        name = getattr(block, "name", None)
        if name:
            inp = getattr(block, "input", None)
            if isinstance(inp, dict):
                short = inp.get("command", inp.get("pattern", inp.get("file_path", "")))
                if isinstance(short, str) and len(short) > 120:
                    short = short[:117] + "..."
                ring.append(f"ToolUse:{name}({short})")
            else:
                ring.append(f"ToolUse:{name}")
        elif hasattr(block, "text"):
            text = (getattr(block, "text", "") or "")[:100].replace("\n", " ")
            ring.append(f"Text:{text}")


def _dump_crash_context(
    stderr_buf: deque[str],
    activity: deque[str],
    turn_count: int,
    *,
    reason: str,
    attempt: int | None = None,
    transcript_path: Path | None = None,
) -> None:
    """Emit crash diagnostics: stderr, last activity, transcript pointer."""
    attempt_tag = f" (attempt {attempt})" if attempt is not None else ""

    if stderr_buf:
        logger.warning(
            "SDK CLI stderr (last %d lines, on %s%s):",
            len(stderr_buf),
            reason,
            attempt_tag,
        )
        for line in list(stderr_buf):
            logger.warning("  [cli-stderr] %s", line.rstrip())
    else:
        logger.warning(
            "SDK CLI stderr buffer empty on %s%s -- child either died "
            "pre-stderr or SDK did not route it.",
            reason,
            attempt_tag,
        )

    logger.warning(
        "Crash context: %d turns completed before %s%s",
        turn_count,
        reason,
        attempt_tag,
    )
    if activity:
        logger.warning("Last %d agent actions:", len(activity))
        for entry in activity:
            logger.warning("  %s", entry)
    if transcript_path and transcript_path.exists():
        logger.warning("Transcript (may contain final activity): %s", transcript_path)


async def _handle_rate_limit_event(
    event: RateLimitEvent, budget: DeveloperBudget | None = None
) -> None:
    """Handle a rate limit event from the SDK stream."""
    info = event.rate_limit_info
    if info.status == "rejected":
        if info.resets_at:
            now = time.time()
            sleep_s = max(0, info.resets_at - now) + RATE_LIMIT_SLEEP_BUFFER_S
        else:
            sleep_s = RATE_LIMIT_FALLBACK_BACKOFF_S

        logger.warning(
            "Rate limited (%s). Sleeping %.0fs (resets_at=%s, +%ds buffer)",
            info.rate_limit_type,
            sleep_s,
            rfc3339_from_epoch(info.resets_at) if info.resets_at else "unknown",
            RATE_LIMIT_SLEEP_BUFFER_S,
        )
        _notify_rate_limit(info.rate_limit_type, sleep_s, info.resets_at)
        if budget is not None:
            budget.pause("claude-rate-limit", "provider rate limit")
        try:
            if budget is None:
                await anyio.sleep(sleep_s)
            else:
                await run_with_developer_budget(anyio.sleep(sleep_s), budget)
        finally:
            if budget is not None:
                budget.resume("claude-rate-limit")

        raise TransientAPIError(
            f"rate limited ({info.rate_limit_type}), slept {sleep_s:.0f}s",
            retry_after=0,
        )
    if info.status == "allowed_warning":
        logger.warning(
            "Approaching rate limit: %.0f%% used (%s)",
            (info.utilization or 0) * 100,
            info.rate_limit_type,
        )


def _notify_rate_limit(rate_limit_type: str | None, sleep_s: float, resets_at: int | None) -> None:
    """Fire-and-forget ntfy notification for rate limit sleep."""
    try:
        from booley.ticket_board.notifications import is_event_enabled, ntfy_send
    except ImportError:
        return
    if not is_event_enabled("rate_limit"):
        return
    reset_str = (
        format_human_datetime(datetime.fromtimestamp(resets_at, tz=UTC))
        if resets_at
        else "unknown"
    )
    ntfy_send(
        title=f"Harness rate-limited ({rate_limit_type or 'unknown'})",
        body=f"Sleeping {sleep_s / 60:.0f}min until {reset_str}",
        priority="3",
    )


_transcript_path_for_attempt = transcript_path_for_attempt


def _write_prompt_header(
    f: io.TextIOBase,
    system_prompt: str | None,
    user_prompt: str,
) -> None:
    """Write the system + user prompts as the first JSONL entry."""
    entry = {
        "type": "prompt",
        "timestamp": utc_now_rfc3339(),
        "system_prompt": system_prompt or "",
        "user_prompt": user_prompt,
    }
    f.write(json.dumps(entry, default=str) + "\n")
    f.flush()


def _write_transcript_turn(f: io.TextIOBase, msg: AssistantMessage) -> None:
    """Append one AssistantMessage to the transcript JSONL file."""
    blocks = []
    for block in msg.content or []:
        b: dict[str, Any] = {"type": type(block).__name__}
        if hasattr(block, "text"):
            b["text"] = block.text
        if hasattr(block, "name"):
            b["name"] = block.name
        if hasattr(block, "input"):
            b["input"] = block.input
        if hasattr(block, "content"):
            b["content"] = block.content
        if hasattr(block, "thinking"):
            b["thinking"] = block.thinking
        blocks.append(b)

    entry = {
        "timestamp": utc_now_rfc3339(),
        "usage": msg.usage,
        "content": blocks,
    }
    f.write(json.dumps(entry, default=str) + "\n")
    f.flush()
