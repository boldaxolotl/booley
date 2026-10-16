"""OpenAI Codex CLI backend implementation."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import json
import logging
import os
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import anyio

from booley.core.models import AgentArtifactPaths, AgentCallParams, AgentResult
from booley.core.temporary_workspace import (
    LeaseLaunch,
    launch_failed,
    launch_finished,
    launch_result,
    prepare_launch,
    registry_root,
    settle_launch,
    temporary_workspace,
    validate_native_command,
)

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
from .agent_errors import (
    AgentProviderError,
    AgentTimeoutError,
    ContextExhaustedError,
    TransientAPIError,
    UsageLimitError,
    is_context_exhausted,
    is_usage_limit,
)
from .prompt_artifacts import adjacent_artifact_paths, write_prompt_artifacts

_PRIVATE_HOME = contextvars.ContextVar("booley_private_codex_home", default=None)


@contextlib.contextmanager
def _invocation_home():
    current = _PRIVATE_HOME.get()
    if current is not None:
        yield current
        return
    with temporary_workspace(prefix="codex-home-", kind="codex-home") as raw:
        home = Path(raw)
        token = _PRIVATE_HOME.set(home)
        try:
            yield home
        finally:
            _PRIVATE_HOME.reset(token)


@contextlib.asynccontextmanager
async def _async_invocation_home():
    from booley.core.workspace_async import async_workspace

    if _PRIVATE_HOME.get() is not None:
        yield _PRIVATE_HOME.get()
        return
    async with async_workspace(prefix="codex-home-", kind="codex-home") as raw:
        token = _PRIVATE_HOME.set(Path(raw))
        try:
            yield Path(raw)
        finally:
            _PRIVATE_HOME.reset(token)


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
        from booley.runtime import auth_token

        plan = auth_token.credential_plan(auth_token.APP_CODEX, self._auth_mode)
        codex_bin = shutil.which("codex")
        if codex_bin is None:
            return "Codex CLI not found on PATH (npm i -g @openai/codex)"
        if self._auth_mode == "api_key" and plan.selected is None:
            return (
                "[agent] auth = 'api_key' but no OPENAI_API_KEY is exported or stored — "
                "export it, run `booley auth --app codex`, or switch auth"
            )
        if self._auth_mode == "subscription" and plan.selected is None:
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
                await anyio.sleep(backoff)

        raise last_exc or AgentProviderError("Codex agent exhausted retries", provider="codex")

    async def _call_once(
        self,
        params: AgentCallParams,
        *,
        full_prompt: str,
        transcript_path: Path | None,
        on_event: Any = None,
    ) -> AgentResult:
        """Single Codex CLI invocation, including preparation and result cleanup."""
        async with _async_invocation_home():
            return await self._call_in_home(
                params, full_prompt=full_prompt, transcript_path=transcript_path, on_event=on_event
            )

    async def _call_in_home(
        self,
        params: AgentCallParams,
        *,
        full_prompt: str,
        transcript_path: Path | None,
        on_event: Any = None,
    ) -> AgentResult:
        source = transcript_path_for_label(transcript_path, params.label)
        paths = (params.artifact_paths or adjacent_artifact_paths)(source)
        transcript_path, cmd, schema_file = self._prepare_call(
            params,
            full_prompt=full_prompt,
            transcript_path=transcript_path,
            paths=paths,
        )
        try:
            raw_output, raw_stderr, returncode = await _codex_run_subprocess(
                cmd,
                params,
                full_prompt=full_prompt,
                on_event=on_event,
                auth_mode=self._auth_mode,
            )
            from booley.core.workspace_async import filesystem_call

            return await filesystem_call(
                _decode_codex_result,
                params,
                (raw_output, raw_stderr, returncode),
                transcript_path,
                paths,
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
        paths: AgentArtifactPaths | None = None,
    ) -> tuple[Path | None, list[str], Any]:
        """Resolve the transcript, write prompt artifacts, and build the cmd.

        Returns (transcript_path, cmd, schema_file) for the try-body.
        """
        transcript_path = transcript_path_for_label(transcript_path, params.label)
        write_prompt_artifacts(
            paths or adjacent_artifact_paths(transcript_path),
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


def _decode_codex_result(params, captured, transcript_path, paths) -> AgentResult:
    raw_output, raw_stderr, returncode = captured
    output, in_tok, out_tok, cached_tok, error_msg, events = _codex_parse_events(raw_output)
    _codex_write_transcript(events, transcript_path)
    _codex_write_markdown(
        events,
        transcript_path,
        markdown_path=paths.transcript_markdown,
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
        raise AgentProviderError("Codex CLI not found on PATH", provider="codex")

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
            mode="w", suffix=".json", delete=False, encoding="utf-8", dir=_PRIVATE_HOME.get()
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


def _canonical_codex_home(environment: dict[str, str]) -> Path:
    return Path(
        environment.get("CODEX_HOME") or Path(environment.get("HOME", str(Path.home()))) / ".codex"
    ).absolute()


def _populate_private_codex_home(
    codex_dir: Path,
    booley_env: dict[str, str],
    enabled_mcp_tools: list[str] | None = None,
    auth_mode: str = "auto",
    *,
    canonical: Path | None = None,
) -> None:
    """Generate private capabilities and link only the canonical native auth file."""
    from .mcp_config import generate_codex_config

    original = canonical or _canonical_codex_home(dict(os.environ))
    codex_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = codex_dir / "config.toml"
    source_config = config_path if config_path.exists() else original / "config.toml"
    existing = source_config.read_text(encoding="utf-8") if source_config.exists() else None
    config = generate_codex_config(
        enabled_mcp_tools=enabled_mcp_tools, extra_env=booley_env, existing_config=existing
    )
    from .auth_token import link_codex_auth

    if link_codex_auth(original / "auth.json", codex_dir / "auth.json", auth_mode):
        config = 'cli_auth_credentials_store = "file"\n' + config
    config_path.write_text(config, encoding="utf-8")


def _prepare_private_home(
    params: AgentCallParams, home: Path, auth_mode: str = "auto"
) -> dict[str, str]:
    environment = dict(os.environ)
    canonical = _canonical_codex_home(environment)
    if auth_mode != "auto":
        from .auth_token import subscription_creds_path

        canonical = subscription_creds_path("codex").parent
    private = home / ".codex"
    allowlist = list(params.nested_mcp_tools or [])
    booley_env = {key: value for key, value in environment.items() if key.startswith("BOOLEY_")}
    booley_env.update(params.nested_mcp_env or {})
    booley_env.update(
        BOOLEY_NESTED_AGENT="1",
        BOOLEY_NESTED_MCP_TOOLS=",".join(allowlist),
        BOOLEY_SCRATCH_ROOT=str(registry_root()),
    )
    _populate_private_codex_home(
        private, booley_env, allowlist, canonical=canonical, auth_mode=auth_mode
    )
    for name in ("models_cache.json", "version.json"):
        if (canonical / name).is_file():
            shutil.copy2(canonical / name, private / name)
    if params.session_state_dir is not None:
        params.session_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (private / "sessions").symlink_to(
            params.session_state_dir.absolute(), target_is_directory=True
        )
    return {**environment, "HOME": str(home), "CODEX_HOME": str(private)}


async def _codex_spawn(
    cmd: list[str],
    params: AgentCallParams,
    auth_mode: str = "auto",
) -> tuple[asyncio.subprocess.Process, Path, LeaseLaunch]:
    """Spawn Codex with piped I/O, private configuration, and shared credentials."""
    home = _PRIVATE_HOME.get()
    assert home is not None
    from booley.core.workspace_async import acquire_task, filesystem_call

    env = await filesystem_call(_prepare_private_home, params, home, auth_mode)
    if params.text_only:
        from .text_only_agent import prepare_codex_text_only

        cmd, env = await filesystem_call(
            prepare_codex_text_only,
            cmd,
            params,
            dict(os.environ),
            private=home / ".codex",
            auth_mode=auth_mode,
        )
        env["HOME"] = str(home)
    if auth_mode == "subscription":
        env.pop("OPENAI_API_KEY", None)
    from .auth_token import credential_plan

    plan = credential_plan("codex", auth_mode)
    plan.require_credential()
    env = plan.child_environment(env)
    launch = await acquire_task(
        asyncio.create_task(asyncio.to_thread(prepare_launch, cmd, params.workspace_guard_fds)),
        settle_launch,
    )
    proc = await _spawn_prepared_codex(launch, cmd, params, env)
    return proc, Path(env["CODEX_HOME"]) / "sessions", launch


async def _spawn_prepared_codex(launch, cmd, params, env):
    from booley.core.workspace_async import acquire_task, filesystem_call

    try:
        if launch.roots:
            validate_native_command(cmd, {"cwd": params.cwd, "env": env})
        spawn = asyncio.create_subprocess_exec(
            *launch.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            cwd=str(Path(params.cwd).resolve()),
            env=env,
            **launch.kwargs,
        )
        return await acquire_task(asyncio.create_task(spawn), _terminate_process)
    except (OSError, asyncio.CancelledError):
        await filesystem_call(launch_failed, launch)
        raise


async def _codex_run_subprocess(
    cmd: list[str],
    params: AgentCallParams,
    *,
    full_prompt: str,
    on_event: Any,
    auth_mode: str = "auto",
) -> tuple[str, str, int]:
    """Spawn codex exec, stream I/O, and release each invocation's private home."""
    async with _async_invocation_home():
        return await _codex_run_in_home(
            cmd, params, full_prompt=full_prompt, on_event=on_event, auth_mode=auth_mode
        )


async def _codex_run_in_home(
    cmd: list[str],
    params: AgentCallParams,
    *,
    full_prompt: str,
    on_event: Any,
    auth_mode: str = "auto",
) -> tuple[str, str, int]:
    from booley.core.workspace_async import filesystem_call

    proc, session_root, launch = await _codex_spawn(cmd, params, auth_mode)
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    live_usage = CodexLiveUsage(params.model, on_event, session_root)

    async def _stream_and_drain() -> None:
        assert proc.stdin is not None
        proc.stdin.write(full_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        stderr_task = asyncio.create_task(_drain_codex_stderr(proc.stderr, stderr_buf))
        try:
            assert proc.stdout is not None
            await _read_stdout(proc.stdout, stdout_buf, on_event, live_usage)
            await proc.wait()
        finally:
            await live_usage.close()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    try:
        await _run_codex_work(
            _stream_and_drain(),
            proc,
            timeout_seconds=params.timeout_seconds,
            launch=launch,
        )
        native_result = await filesystem_call(launch_result, launch)
    finally:
        await filesystem_call(launch_finished, launch)

    return (
        bytes(stdout_buf).decode("utf-8", errors="replace"),
        bytes(stderr_buf).decode("utf-8", errors="replace"),
        proc.returncode if native_result is None else native_result,
    )


async def _drain_codex_stderr(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
    assert stream is not None
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        buffer.extend(chunk)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a backend process and wait for its resources to be reaped."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=16)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=1)


class _CleanupDeadlineError(RuntimeError):
    """Native work finished, but its descendant settlement exceeded the bound."""


async def _wait_native_settlement(task: asyncio.Task[None]) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=16)
    except TimeoutError as error:
        raise _CleanupDeadlineError from error


async def _native_work_deadline(work, launch: LeaseLaunch | None, timeout_seconds: int) -> None:
    from booley.core.workspace_async import filesystem_call

    if launch is None or not launch.roots:
        await asyncio.wait_for(work, timeout=timeout_seconds)
        return
    task = asyncio.create_task(work)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while not task.done():
            if await filesystem_call(launch_result, launch) is not None:
                await _wait_native_settlement(task)
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                # Publication may have raced the final poll. Native completion
                # freezes its result; descendant settlement has its own bound.
                if await filesystem_call(launch_result, launch) is None:
                    raise TimeoutError
                await _wait_native_settlement(task)
                return
            await asyncio.wait({task}, timeout=min(0.02, remaining))
        task.result()
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _run_codex_work(
    work: Coroutine[Any, Any, None],
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
    launch: LeaseLaunch | None = None,
) -> None:
    """Run streamed Codex work and always terminate its process on cancellation."""
    try:
        await _native_work_deadline(work, launch, timeout_seconds)
    except BaseException as error:
        from booley.core.workspace_async import joined

        cleanup = asyncio.create_task(_terminate_process(proc))
        try:
            await joined(cleanup)
        except (OSError, TimeoutError):
            logger.warning("Codex cleanup was interrupted or remains uncertain; retaining scratch")
        if isinstance(error, _CleanupDeadlineError):
            logger.warning("Native Codex result preserved; descendant cleanup remains uncertain")
            return
        if isinstance(error, TimeoutError):
            raise AgentTimeoutError(f"Codex timed out after {timeout_seconds}s") from error
        raise


async def _read_stdout(
    stdout: asyncio.StreamReader,
    buf: bytearray,
    on_event: Any,
    live_usage: CodexLiveUsage | None = None,
) -> None:
    """Read stdout, optionally dispatching on_event for agent messages."""
    if on_event is not None:
        async for raw_line in stdout:
            buf.extend(raw_line)
            decoded = raw_line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                ev = json.loads(decoded)
                if not isinstance(ev, dict):
                    continue
                _dispatch_stdout_event(ev, on_event, live_usage)
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
) -> None:
    """Route one validated ``codex exec --json`` record."""
    etype = event.get("type")
    item = event.get("item", {})
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


def _codex_check_errors(
    error_msg: str | None,
    output: str,
    returncode: int,
    raw_stderr: str,
    raw_output: str,
) -> None:
    """Raise appropriate exceptions for Codex error conditions."""
    # Progress messages are not a successful result when the usage cap ends the call.
    if error_msg and (not output or is_usage_limit(error_msg)):
        _raise_for_codex_detail(error_msg, prefix="Codex error")

    if returncode != 0:
        # Classify the full provider diagnostic, never agent text in JSONL stdout.
        if is_usage_limit(raw_stderr):
            _raise_for_codex_detail(raw_stderr, prefix=f"Codex exit code {returncode}")
        if not output:
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
    raise AgentProviderError(f"{prefix}: {detail}", provider="codex")


def _codex_resolve_structured(
    output_format: dict[str, Any] | None,
    output: str,
) -> tuple[dict | None, bool]:
    """Resolve structured output from Codex response."""
    if output_format is None or not output:
        return None, False
    structured, structured_fallback = _codex_extract_structured(output)
    if structured_fallback and structured is not None:
        logger.info("Codex structured output recovered via last-segment extraction")
    elif structured is None:
        logger.warning("Codex structured output: JSON extraction failed")
    return structured, structured_fallback
