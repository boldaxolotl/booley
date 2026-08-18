"""OpenAI Codex CLI backend implementation."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import anyio

from ._codex_live_usage import CodexLiveUsage
from ._codex_transcript_md import (  # noqa: F401 — re-exported for backward compat
    CodexParsedEvents,
    _codex_md_item_lines,
    _codex_md_usage_lines,
    _codex_parse_events,
    _codex_write_markdown,
    _is_structured_only_agent_text,
    _mcp_result_text,
    _strip_bash_wrapper,
    _truncate_transcript_block,
)
from ._cost import estimate_cost, format_usage_log
from ._retry import (
    MAX_API_RETRIES,
    _is_transient_error,
    compute_backoff,
    transcript_path_for_attempt,
    transcript_path_for_label,
)
from .blocking import (
    AgentTimeoutError,
    ContextExhaustedError,
    TransientAPIError,
    UsageLimitError,
    is_context_exhausted,
    is_usage_limit,
)
from .developer_budget import DeveloperBudget, run_with_developer_budget
from .models import AgentCallParams, AgentResult
from .prompt_artifacts import write_prompt_artifacts

logger = logging.getLogger(__name__)


def _inside_container() -> bool:
    """Detect if we're already running inside a Docker container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _codex_sandbox_mode(allowed_agent_capabilities: list[str] | None) -> str:
    """Map Booley MCP tool lists to Codex CLI sandbox modes.

    EDIT_TOOLS includes Bash but should NOT get full-auto — only FULL_TOOLS
    (which adds Write) warrants unrestricted access.

    NOTE: ``codex exec`` has a known bug where ``--full-auto`` and
    ``-s workspace-write`` are silently ignored, resolving to read-only.
    We use ``danger-full-access`` as workaround for write-capable tiers
    (safe — harness already runs in isolated worktrees).
    See https://github.com/openai/codex/issues/18113
    """
    if allowed_agent_capabilities is None:
        return "danger-full-access"
    has_write = "Write" in allowed_agent_capabilities
    has_bash = "Bash" in allowed_agent_capabilities
    if has_write and has_bash:
        return "danger-full-access"
    if "Edit" in allowed_agent_capabilities or has_write or has_bash:
        return "danger-full-access"
    return "read-only"


def _codex_ensure_additional_properties(schema: dict) -> dict:
    """Deep-clone schema and make it OpenAI strict-mode compliant.

    Injects ``additionalProperties: false`` and ``required: [all keys]``
    at every object level that declares ``properties``.
    """
    schema = copy.deepcopy(schema)

    def _patch(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                ap = node.get("additionalProperties")
                if isinstance(ap, dict):
                    raise ValueError(
                        f"Schema at '{path}' uses additionalProperties as a "
                        f"schema (dynamic-key map) — incompatible with OpenAI "
                        f"strict mode. Convert to array-of-objects instead."
                    )
                if "properties" in node:
                    node.setdefault("additionalProperties", False)
                    node["required"] = list(node["properties"].keys())
            for k, v in node.items():
                _patch(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _patch(item, f"{path}[{i}]")

    _patch(schema)
    return schema


def _deny_patterns_to_prompt(patterns: list[str]) -> str | None:
    """Convert ``Bash(*dir/*)`` deny patterns into a prompt-level prohibition.

    The Codex CLI has no ``disallowed_agent_capabilities`` mechanism, so this is the only
    way to enforce category boundaries — inject the restriction into the prompt.
    """
    import re

    dirs: set[str] = set()
    for p in patterns:
        m = re.match(r"Bash\(\*([^/\\]+)[/\\]", p)
        if m:
            dirs.add(m.group(1))
    if not dirs:
        return None
    dir_list = ", ".join(f"{d}/" for d in sorted(dirs))
    return (
        "**IMPORTANT — file-access restriction (enforced by harness):**\n"
        f"Do NOT read, write, list, grep, or access any files under: {dir_list}\n"
        "These directories are out of scope. Attempts to access them will be "
        "treated as a boundary violation."
    )


def _codex_build_prompt(
    prompt: str,
    system_prompt: str | None,
    disallowed_agent_capabilities: list[str] | None = None,
) -> str:
    """Assemble the full prompt with optional system prefix."""
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt)
        parts.append("---")
    if disallowed_agent_capabilities:
        deny_msg = _deny_patterns_to_prompt(disallowed_agent_capabilities)
        if deny_msg:
            parts.append(deny_msg)
    parts.append(prompt)
    return "\n\n".join(parts)


def _codex_extract_structured(output: str) -> tuple[dict | None, bool]:
    """Extract structured JSON from Codex output, preferring the last message.

    Multi-turn Codex sessions may emit intermediate agent_messages before the
    final corrected answer.  When multiple segments exist, try the last one
    first (the final answer), then fall back to earlier segments.

    Returns (structured_dict_or_None, is_fallback).
    """
    from .agent import extract_json

    try:
        return json.loads(output), False
    except (json.JSONDecodeError, ValueError):
        pass

    segments = [s.strip() for s in output.split("\n\n") if s.strip()]
    if len(segments) > 1:
        for segment in reversed(segments):
            try:
                return json.loads(segment), True
            except (json.JSONDecodeError, ValueError):
                pass
            extracted = extract_json(segment)
            if extracted is not None:
                return extracted, True

    extracted = extract_json(output)
    return extracted, True


def _codex_write_transcript(events: list[dict], transcript_path: Path | None) -> None:
    """Write raw JSONL events to transcript file."""
    if transcript_path is None or not events:
        return
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, default=str) + "\n")


_transcript_path_for_attempt = transcript_path_for_attempt


class CodexBackend:
    """Agent backend using the OpenAI Codex CLI (codex exec --json).

    Spawns `codex exec` as a subprocess, feeds the prompt via stdin,
    and parses JSONL events from stdout.  Auth is handled by the CLI
    itself (subscription login via `codex login`).
    """

    def __init__(self, auth_mode: str = "auto") -> None:
        # The resolved [agent] auth policy. Under "subscription" the spawn env
        # is scrubbed of OPENAI_API_KEY (Codex then authenticates via its
        # auth.json login); under "api_key" the health check fails loud when no
        # key is available.
        self._auth_mode = auth_mode

    @staticmethod
    def _log_start(params: AgentCallParams) -> str:
        """Log call start and return the tag prefix string."""
        short_model = (
            params.model.rsplit("/", maxsplit=1)[-1] if "/" in params.model else params.model
        )
        effort_tag = f", effort={params.reasoning_effort}" if params.reasoning_effort else ""
        tag = f"{params.label} " if params.label else ""
        logger.info("%sCodex agent started (%s%s)", tag, short_model, effort_tag)
        return tag

    @property
    def name(self) -> str:
        return "Codex"

    def health_check(self) -> str | None:
        from booley.harness import auth_token

        codex_bin = shutil.which("codex")
        if codex_bin is None:
            return "Codex CLI not found on PATH (npm i -g @openai/codex)"
        if self._auth_mode == "api_key" and auth_token.resolve_token(auth_token.APP_CODEX) is None:
            return (
                "[agent] auth = 'api_key' but no OPENAI_API_KEY is exported or stored — "
                "export it, run `booley auth --app codex`, or switch auth"
            )
        if (
            self._auth_mode == "subscription"
            and not auth_token.subscription_creds_path(auth_token.APP_CODEX).is_file()
        ):
            return (
                "[agent] auth = 'subscription' but no Codex login found at ~/.codex/auth.json — "
                "run `codex login` or switch auth"
            )
        return None

    async def call(
        self,
        params: AgentCallParams,
        **kwargs: Any,
    ) -> AgentResult:
        on_event = kwargs.pop("on_event", None)
        budget: DeveloperBudget | None = kwargs.pop("developer_budget", None)
        tag = self._log_start(params)
        full_prompt = _codex_build_prompt(
            params.prompt,
            params.system_prompt,
            params.disallowed_agent_capabilities,
        )

        last_exc: Exception | None = None
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                result = await self._call_once(
                    params,
                    full_prompt=full_prompt,
                    transcript_path=_transcript_path_for_attempt(
                        params.transcript_path,
                        attempt,
                    ),
                    on_event=on_event,
                    budget=budget,
                )
                logger.info(
                    "%sCodex agent done (%s)",
                    tag,
                    format_usage_log(
                        result.input_tokens,
                        result.cached_tokens,
                        result.output_tokens,
                        result.cost_usd,
                    ),
                )
                return result
            except (UsageLimitError, ContextExhaustedError):
                raise
            except TransientAPIError as exc:
                last_exc = exc
                if attempt >= MAX_API_RETRIES:
                    break
                backoff = compute_backoff(attempt)
                logger.warning(
                    "Codex transient error (attempt %d/%d): %s -- retrying in %.0fs",
                    attempt,
                    MAX_API_RETRIES,
                    exc,
                    backoff,
                )
                if budget is not None:
                    budget.resume_prefix("codex-mcp:")
                    budget.pause("codex-retry-backoff", "transient retry")
                try:
                    if budget is None:
                        await anyio.sleep(backoff)
                    else:
                        await run_with_developer_budget(anyio.sleep(backoff), budget)
                finally:
                    if budget is not None:
                        budget.resume("codex-retry-backoff")

        raise last_exc or RuntimeError("Codex agent exhausted retries")

    async def _call_once(
        self,
        params: AgentCallParams,
        *,
        full_prompt: str,
        transcript_path: Path | None,
        on_event: Any = None,
        budget: DeveloperBudget | None = None,
    ) -> AgentResult:
        """Single Codex CLI invocation."""
        transcript_path, cmd, schema_file = self._prepare_call(
            params,
            full_prompt=full_prompt,
            transcript_path=transcript_path,
        )
        try:
            raw_output, raw_stderr, returncode = await _codex_run_subprocess(
                cmd,
                params,
                full_prompt=full_prompt,
                on_event=on_event,
                auth_mode=self._auth_mode,
                budget=budget,
            )
            output, in_tok, out_tok, cached_tok, error_msg, events = _codex_parse_events(
                raw_output
            )
            _codex_write_transcript(events, transcript_path)
            _codex_write_markdown(
                events,
                transcript_path,
                system_prompt=params.system_prompt,
                user_prompt=params.prompt,
            )
            _codex_check_errors(error_msg, output, returncode, raw_stderr, raw_output)
            structured, structured_fallback = _codex_resolve_structured(
                params.output_format,
                output,
            )
            return AgentResult(
                output=output,
                structured=structured,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_tokens=cached_tok,
                cost_usd=estimate_cost(params.model, in_tok, cached_tok, out_tok),
                structured_fallback=structured_fallback,
                session_id=_codex_thread_id(events),
            )
        finally:
            if schema_file is not None:
                with contextlib.suppress(OSError):
                    Path(schema_file.name).unlink()

    def _prepare_call(
        self,
        params: AgentCallParams,
        *,
        full_prompt: str,
        transcript_path: Path | None,
    ) -> tuple[Path | None, list[str], Any]:
        """Resolve the transcript, write prompt artifacts, and build the cmd.

        Returns (transcript_path, cmd, schema_file) for the try-body.
        """
        transcript_path = transcript_path_for_label(transcript_path, params.label)
        write_prompt_artifacts(
            transcript_path,
            system_prompt=params.system_prompt,
            user_prompt=params.prompt or full_prompt,
            full_prompt=full_prompt,
            metadata={
                "backend": "codex",
                "label": params.label,
                "model": params.model,
                "cwd": str(params.cwd),
                "reasoning_effort": params.reasoning_effort,
                "allowed_agent_capabilities": params.allowed_agent_capabilities,
            },
        )
        cmd, schema_file = _codex_build_cmd(
            params.model,
            params.cwd,
            params.allowed_agent_capabilities,
            params.output_format,
            params.reasoning_effort,
            session_id=params.session_id if params.resume_session else None,
        )
        return transcript_path, cmd, schema_file


# ---------------------------------------------------------------------------
# Codex _call_once helpers
# ---------------------------------------------------------------------------


def _codex_build_cmd(
    model: str,
    cwd: str | Path,
    allowed_agent_capabilities: list[str] | None,
    output_format: dict[str, Any] | None,
    reasoning_effort: str | None,
    session_id: str | None = None,
) -> tuple[list[str], Any]:
    """Build the codex exec command and optional schema tempfile."""
    import tempfile

    cwd_path = str(Path(cwd).resolve())
    sandbox = _codex_sandbox_mode(allowed_agent_capabilities)
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        raise RuntimeError("Codex CLI not found on PATH")

    if session_id:
        cmd = [codex_bin, "exec", "resume", "--json", "-m", model]
    else:
        cmd = [codex_bin, "exec", "--json", "-m", model, "-C", cwd_path]
    # Search is provider-hosted and does not traverse the container network.
    # Pin it off for harness calls; /etc/codex/requirements.toml enforces the
    # same rule for interactive sessions and rejects conflicting overrides.
    cmd.extend(["-c", 'web_search="disabled"'])
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])

    # Inside a Docker container, Codex's bwrap sandbox fails (no user
    # namespaces). The container IS the sandbox — always bypass.
    if _inside_container() or sandbox == "danger-full-access":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    elif session_id:
        cmd.extend(["-c", f'sandbox_mode="{sandbox}"'])
    else:
        cmd.extend(["-s", sandbox])

    schema_file = None
    if output_format is not None:
        patched = _codex_ensure_additional_properties(output_format)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as sf:
            json.dump(patched, sf)
            schema_file = sf
        cmd.extend(["--output-schema", schema_file.name])

    if session_id:
        cmd.append(session_id)
    cmd.append("-")
    logger.debug("Codex cmd: %s", " ".join(cmd[:10]))
    return cmd, schema_file


def _codex_thread_id(events: list[dict[str, Any]]) -> str | None:
    """Return the resumable thread identifier emitted by ``codex exec``."""
    for event in events:
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


_NESTED_HOMES: dict[str, str] = {}


def _ticket_home_scope() -> str:
    """Return a filesystem-safe namespace unique to this ticket run."""
    import hashlib
    import os

    slug = os.environ.get("BOOLEY_SLUG", "").strip() or "ticket"
    identity = f"{slug}-pid-{os.getpid()}"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in identity)[:32]
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
    return f"{safe or 'ticket'}-{digest}"


def _ensure_nested_codex_home(
    parent_label: str,
    allowed_mcp_tools: list[str] | None,
) -> str:
    """Create a per-parent HOME dir for a nested Codex agent.

    Nested agents inherit ~/.codex/config.toml from the developer, which
    exposes all Booley MCP tools — including the very Specialist that spawned
    the agent. Letting them through caused infinite recursion (reviewer →
    codex → MCP reviewer → codex → …).

    The previous "empty config" fix broke specialists that genuinely need MCP
    primitives (e.g. debugger needs simulate + bwave_*; coverage_analyst
    Phase-2 same). Current design: each spawning specialist declares
    ``nested_mcp_tools`` (an allowlist of safe, non-recursive MCP tool
    names); we generate a per-parent config.toml with that list baked into
    ``BOOLEY_NESTED_MCP_TOOLS`` in [env]. The MCP server reads that env on
    startup and filters discovery to exactly those names. Empty list → no
    MCP at all (current behavior for most specialists).

    Cache key: the allowlist itself. Two callers with identical allowlists
    share the same nested home (cheap; the config is identical). A `None`
    allowlist is treated the same as empty.
    """
    import hashlib
    import os

    from .mcp_config import generate_codex_config

    allowlist = list(allowed_mcp_tools or [])
    # Deterministic cache key from the allowlist content. Empty -> "_none".
    allowlist_str = ",".join(sorted(allowlist))
    ticket_scope = _ticket_home_scope()
    cache_key = f"{ticket_scope}:{allowlist_str or '_none'}"
    cached = _NESTED_HOMES.get(cache_key)
    if cached is not None:
        return cached

    # Dir name: short hash of the cache key, plus a hint of the parent_label
    # for easier debugging when poking around /tmp.
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:8]
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in (parent_label or "anon"))[
        :40
    ]
    nested = Path(f"/tmp/codex-nested-home-{ticket_scope}-{safe_label}-{digest}")
    codex_dir = nested / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    # Forward all BOOLEY_* env so MCP-spawned MCP endpoint subprocesses (run_sim_batch,
    # bwave, etc.) can locate the project, logs dir, and ticket slug. Codex
    # *replaces* the MCP process env when [env] is present, so we have to
    # enumerate everything the child needs.
    booley_env = {k: v for k, v in os.environ.items() if k.startswith("BOOLEY_")}
    booley_env["BOOLEY_NESTED_AGENT"] = "1"
    booley_env["BOOLEY_NESTED_MCP_TOOLS"] = ",".join(allowlist)

    (codex_dir / "config.toml").write_text(generate_codex_config(extra_env=booley_env))

    original_auth = Path(os.environ.get("HOME", "/home/agent")) / ".codex" / "auth.json"
    if original_auth.exists():
        import shutil as _shutil

        _shutil.copy2(original_auth, codex_dir / "auth.json")

    _NESTED_HOMES[cache_key] = str(nested)
    return _NESTED_HOMES[cache_key]


def _ensure_developer_codex_home(
    slug_label: str,
    enabled_mcp_tools: list[str],
) -> str:
    """Create a per-ticket HOME dir for an DEVELOPER-level Codex agent.

    ADR 0028: the developer launches as a native in-container Codex
    session. Codex REPLACES the MCP server's environment when [env] is
    present, so the developer's BOOLEY_* env (slug, logs dir, state file,
    project dir, agent role — already exported into ``os.environ`` by
    ``_launch_developer_agent``) must be baked into a config.toml.

    Structure mirrors ``_ensure_nested_codex_home`` but with developer
    semantics: the MCP allowlist rides in ``BOOLEY_MCP_TOOLS`` (an
    MCP-exposure filter) and the nested-agent markers (BOOLEY_NESTED_AGENT /
    BOOLEY_NESTED_MCP_TOOLS) are deliberately absent — setting them would
    hide the Specialists from the very agent that drives them.

    No caching: the config bakes per-ticket env, and one Runner process
    serves exactly one ticket, so regenerating on each (retry) spawn is
    cheap and always current.
    """
    import os

    from .mcp_config import generate_codex_config

    booley_env = {k: v for k, v in os.environ.items() if k.startswith("BOOLEY_")}
    # Belt-and-braces: never leak nested markers into the developer server.
    booley_env.pop("BOOLEY_NESTED_AGENT", None)
    booley_env.pop("BOOLEY_NESTED_MCP_TOOLS", None)

    safe_label = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in (slug_label or "developer")
    )[:40]
    home = Path(f"/tmp/codex-developer-home-{_ticket_home_scope()}-{safe_label}")
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    (codex_dir / "config.toml").write_text(
        generate_codex_config(
            enabled_mcp_tools=enabled_mcp_tools,
            extra_env=booley_env,
        )
    )

    original_auth = Path(os.environ.get("HOME", "/home/agent")) / ".codex" / "auth.json"
    if original_auth.exists():
        shutil.copy2(original_auth, codex_dir / "auth.json")

    return str(home)


async def _codex_spawn(
    cmd: list[str],
    params: AgentCallParams,
    auth_mode: str = "auto",
) -> tuple[asyncio.subprocess.Process, Path]:
    """Spawn the codex subprocess with piped I/O.

    Inside a container the HOME redirect depends on the call level:

    * Developer Agent launch (``params.developer_mcp_tools`` set, ADR 0028):
      per-ticket HOME whose config.toml bakes the exported BOOLEY_* env and
      the MCP allowlist, no nested markers. See
      ``_ensure_developer_codex_home``.
    * Everything else is a nested agent: per-parent HOME whose config.toml
      exposes only the recursion-safe MCP tools in
      ``params.nested_mcp_tools``. See ``_ensure_nested_codex_home``.

    When ``params.label`` is None on the nested path the caller didn't
    identify itself — we fall back to "_anonymous" with an empty allowlist
    so recursion is still blocked (matches the pre-refactor empty-config
    behavior).
    """
    env = None
    if _inside_container():
        env = dict(os.environ)
        if params.developer_mcp_tools is not None:
            env["HOME"] = _ensure_developer_codex_home(
                params.label or "developer",
                params.developer_mcp_tools,
            )
        else:
            env["HOME"] = _ensure_nested_codex_home(
                params.label or "_anonymous",
                params.nested_mcp_tools,
            )

    if auth_mode == "subscription":
        # [agent] auth = "subscription": drop the API key so Codex bills the
        # auth.json login instead of the key. Host-side the env is otherwise
        # inherited, so the scrub forces an explicit copy.
        env = dict(os.environ) if env is None else env
        env.pop("OPENAI_API_KEY", None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        cwd=str(Path(params.cwd).resolve()),
        env=env,
    )
    codex_home = (env or os.environ).get("CODEX_HOME")
    if codex_home:
        session_root = Path(codex_home) / "sessions"
    else:
        home = (env or os.environ).get("HOME", str(Path.home()))
        session_root = Path(home) / ".codex" / "sessions"
    return proc, session_root


async def _codex_run_subprocess(
    cmd: list[str],
    params: AgentCallParams,
    *,
    full_prompt: str,
    on_event: Any,
    auth_mode: str = "auto",
    budget: DeveloperBudget | None = None,
) -> tuple[str, str, int]:
    """Spawn codex exec, stream I/O, return (stdout, stderr, returncode)."""
    proc, session_root = await _codex_spawn(cmd, params, auth_mode)
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    live_usage = CodexLiveUsage(params.model, on_event, session_root)

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            stderr_buf.extend(chunk)

    async def _stream_and_drain() -> None:
        assert proc.stdin is not None
        proc.stdin.write(full_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        stderr_task = asyncio.create_task(_drain_stderr())
        try:
            assert proc.stdout is not None
            await _read_stdout(proc.stdout, stdout_buf, on_event, live_usage, budget)
            await proc.wait()
        finally:
            await live_usage.close()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    await _run_codex_work(
        _stream_and_drain(),
        proc,
        timeout_seconds=params.timeout_seconds,
        budget=budget,
    )

    return (
        bytes(stdout_buf).decode("utf-8", errors="replace"),
        bytes(stderr_buf).decode("utf-8", errors="replace"),
        proc.returncode,
    )


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a backend process and wait for its resources to be reaped."""
    if proc.returncode is None:
        proc.kill()
    await proc.wait()


async def _run_codex_work(
    work: Coroutine[Any, Any, None],
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
    budget: DeveloperBudget | None,
) -> None:
    """Run streamed Codex work and always terminate its process on cancellation."""
    try:
        if budget is None:
            await asyncio.wait_for(work, timeout=timeout_seconds)
        else:
            await run_with_developer_budget(
                work,
                budget,
                on_exhausted=lambda _kind: _terminate_process(proc),
            )
    except TimeoutError as err:
        await _terminate_process(proc)
        raise AgentTimeoutError(f"Codex timed out after {timeout_seconds}s") from err
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise


async def _read_stdout(
    stdout: asyncio.StreamReader,
    buf: bytearray,
    on_event: Any,
    live_usage: CodexLiveUsage | None = None,
    budget: DeveloperBudget | None = None,
) -> None:
    """Read stdout, optionally dispatching on_event for agent messages."""
    if on_event is not None or budget is not None:
        async for raw_line in stdout:
            buf.extend(raw_line)
            decoded = raw_line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                ev = json.loads(decoded)
                if not isinstance(ev, dict):
                    continue
                _dispatch_stdout_event(ev, on_event, live_usage, budget)
            except Exception:  # noqa: BLE001 — tolerate malformed/partial JSON lines in the event stream
                pass
    else:
        while True:
            chunk = await stdout.read(8192)
            if not chunk:
                break
            buf.extend(chunk)


def _dispatch_stdout_event(
    event: dict,
    on_event: Any,
    live_usage: CodexLiveUsage | None,
    budget: DeveloperBudget | None = None,
) -> None:
    """Route one validated ``codex exec --json`` record."""
    etype = event.get("type")
    item = event.get("item", {})
    if budget is not None and isinstance(item, dict):
        _update_budget_for_mcp_event(etype, item, budget)
    if etype == "thread.started" and live_usage is not None:
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            live_usage.start(thread_id)
        return
    if etype == "turn.completed" and live_usage is not None:
        live_usage.completed(event.get("usage", {}))
        return
    if etype != "item.completed":
        return
    if on_event is None:
        return
    if item.get("type") == "agent_message" and item.get("text"):
        on_event({"type": "agent_text", "text": item["text"]})
    elif item.get("type") == "file_change":
        changes = item.get("changes", [])
        on_event(
            {
                "type": "file_change",
                "paths": [
                    change["path"]
                    for change in changes
                    if isinstance(change, dict) and isinstance(change.get("path"), str)
                ],
            }
        )


def _update_budget_for_mcp_event(
    event_type: object,
    item: dict[str, Any],
    budget: DeveloperBudget,
) -> None:
    """Pause active time across synchronous calls to the Booley MCP server."""
    if item.get("type") != "mcp_tool_call" or item.get("server") != "booley":
        return
    call_id = item.get("id")
    if not isinstance(call_id, str) or not call_id:
        return
    key = f"codex-mcp:{call_id}"
    if event_type == "item.started":
        tool = item.get("tool")
        budget.pause(key, f"waiting for {tool}" if isinstance(tool, str) else "Booley tool wait")
    elif event_type in {"item.completed", "item.failed"}:
        budget.resume(key)


def _codex_check_errors(
    error_msg: str | None,
    output: str,
    returncode: int,
    raw_stderr: str,
    raw_output: str,
) -> None:
    """Raise appropriate exceptions for Codex error conditions."""
    if error_msg and not output:
        _raise_for_codex_detail(error_msg, prefix="Codex error")

    if returncode != 0 and not output:
        detail = (raw_stderr or raw_output)[:500]
        _raise_for_codex_detail(detail, prefix=f"Codex exit code {returncode}")


def _raise_for_codex_detail(detail: str, *, prefix: str) -> None:
    """Classify an error string and raise the appropriate exception."""
    if is_usage_limit(detail):
        raise UsageLimitError(detail, provider="codex")
    if is_context_exhausted(detail):
        raise ContextExhaustedError(detail, provider="codex")
    if _is_transient_error(RuntimeError(detail)):
        raise TransientAPIError(f"{prefix}: {detail}", retry_after=10)
    raise RuntimeError(f"{prefix}: {detail}")


def _codex_resolve_structured(
    output_format: dict[str, Any] | None,
    output: str,
) -> tuple[dict | None, bool]:
    """Resolve structured output from Codex response."""
    if output_format is None or not output:
        return None, False
    structured, structured_fallback = _codex_extract_structured(output)
    if structured_fallback and structured is not None:
        logger.warning("Codex structured output recovered via last-segment extraction")
    elif structured is None:
        logger.warning("Codex structured output: JSON extraction failed")
    return structured, structured_fallback
