"""Tests for the Developer Agent backends: Claude SDK stream, Codex runs, backend config."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from booley.config import agent as config_agent
from booley.config import agent_app
from booley.config.agent import AgentSettings, set_agent_settings
from booley.config.settings import BackendConfigError
from booley.core.models import AgentCallParams
from booley.runtime._retry import MAX_API_RETRIES
from booley.runtime.agent_backend import ClaudeSDKBackend, CodexBackend
from booley.runtime.agent_config import (
    BackendConfig,
    get_backend_config,
    load_backend_config,
    set_backend_config,
)
from booley.runtime.agent_errors import AgentProviderError, TransientAPIError, UsageLimitError
from booley.runtime.runtime_context import RuntimeLocation
from tests.claude_cli_fake import record_sdk_options, replay_sdk_stream, sdk_message, sdk_result
from tests.codex_cli_fake import (
    codex_report,
    install_fake_codex,
    replay_codex_run,
    write_codex_login,
)
from tests.conftest import require_symlinks
from tests.sandbox_venue_fake import place, point_mounts
from tests.time_budget import scaled

_MODEL = "claude-opus-4-8"


def _assistant(*blocks, usage=None):
    return sdk_message("AssistantMessage", content=list(blocks), model=_MODEL, usage=usage)


def _user(*blocks):
    return sdk_message("UserMessage", content=list(blocks))


def _tool_use(name, tool_input, tool_id="t"):
    return SimpleNamespace(id=tool_id, name=name, input=tool_input)


def _tool_result(tool_id, *, is_error=False):
    return SimpleNamespace(tool_use_id=tool_id, is_error=is_error)


def _raise_callback_error(_event):
    raise RuntimeError("console died")


async def _claude(monkeypatch, cwd, *steps, on_event=None, **params):
    """One ``ClaudeSDKBackend.call`` whose SDK stream is ``steps``."""
    replay_sdk_stream(monkeypatch, *steps)
    params = {"prompt": "p", "model": _MODEL, **params}
    return await ClaudeSDKBackend(auth_mode="visitor").call(
        AgentCallParams(cwd=cwd, **params), on_event=on_event
    )


def _refusal_error(detail):
    from claude_agent_sdk import ResultError

    failure = ResultError("Command failed with exit code 1")
    failure.result = detail
    failure.errors = []
    return failure


# ===========================================================================
# Claude SDK backend
# ===========================================================================


_NO_OPTIONS = {
    "allowed_tools": [],
    "resume": None,
    "continue_conversation": False,
    "nested_agent": None,
    "nested_tools": None,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({}, {}, id="defaults"),
        pytest.param(
            {"allowed_agent_capabilities": ["Read", "Grep"]},
            {"allowed_tools": ["Read", "Grep"]},
            id="allowed-capabilities",
        ),
        pytest.param(
            {"session_id": "abc-123", "resume_session": True},
            {"resume": "abc-123", "continue_conversation": True},
            id="resume",
        ),
        pytest.param(
            {"nested_mcp_tools": ["sim", "bwave_open"]},
            {"nested_agent": "1", "nested_tools": "sim,bwave_open"},
            id="nested-allowlist",
        ),
        pytest.param(
            {"nested_mcp_tools": []},
            {"nested_agent": "1", "nested_tools": ""},
            id="empty-nested-allowlist",
        ),
    ],
)
async def test_sdk_options_map_call_params(monkeypatch, tmp_path, params, expected):
    seen = record_sdk_options(monkeypatch)

    await ClaudeSDKBackend(auth_mode="visitor").call(
        AgentCallParams(prompt="p", model="sonnet", cwd=tmp_path, **params)
    )

    (options,) = seen
    server = options.mcp_servers["booley"]
    assert (server["command"], server["args"]) == ("python", ["-m", "booley.mcp.server"])
    assert options.strict_mcp_config is True
    assert {
        "allowed_tools": getattr(options, "allowed_tools", []),
        "resume": getattr(options, "resume", None),
        "continue_conversation": getattr(options, "continue_conversation", False),
        "nested_agent": server["env"].get("BOOLEY_NESTED_AGENT"),
        "nested_tools": server["env"].get("BOOLEY_NESTED_MCP_TOOLS"),
    } == {**_NO_OPTIONS, **expected}


@pytest.mark.asyncio
async def test_backend_swaps_keep_options_independent_and_parent_env_unchanged(
    monkeypatch, tmp_path
):
    from booley.runtime import auth_token

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_KEY")
    auth_token.store_token("SYNTHETIC_OAUTH", "claude")
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    captured_options = record_sdk_options(monkeypatch)
    params = AgentCallParams(prompt="p", model="sonnet", cwd=tmp_path)

    await ClaudeSDKBackend(auth_mode="subscription").call(params)
    await ClaudeSDKBackend(auth_mode="api_key").call(params)

    assert len(captured_options) == 2
    subscription, api_key = captured_options
    assert subscription is not api_key
    assert getattr(subscription, "cli_path", None) is None
    assert getattr(api_key, "cli_path", None) is None
    assert subscription.env["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_API_KEY" not in api_key.env
    assert "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK" not in os.environ
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in os.environ


@pytest.mark.asyncio
async def test_unclassified_stream_exception_logs_crash_context_and_keeps_traceback_at_debug(
    monkeypatch, tmp_path, caplog
):
    caplog.set_level("DEBUG")
    transcript = tmp_path / "t.jsonl"

    with pytest.raises(RuntimeError, match="provider exploded"):
        await _claude(
            monkeypatch,
            tmp_path,
            _assistant(_tool_use("Read", {"file_path": "x"})),
            lambda options: options.stderr("boom\n"),
            RuntimeError("provider exploded"),
            transcript_path=transcript,
        )

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert (
        "Crash context: 1 turns completed before exception: RuntimeError (attempt 1)" in warnings
    )
    assert "  [cli-stderr] boom" in warnings
    assert "  ToolUse:Read(x)" in warnings
    assert f"Transcript (may contain final activity): {transcript}" in warnings
    ordinary = [
        r
        for r in caplog.records
        if r.levelno >= 20 and r.getMessage() == "Agent call failed: provider exploded"
    ]
    assert len(ordinary) == 1
    assert ordinary[0].exc_info is None
    assert any(r.levelname == "DEBUG" and r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_result_error_classification_uses_terminal_text(monkeypatch, tmp_path, caplog):
    caplog.set_level("DEBUG")

    with pytest.raises(UsageLimitError) as raised:
        await _claude(monkeypatch, tmp_path, _refusal_error("You've hit your usage limit"))

    assert not [r for r in caplog.records if r.levelno >= 30]
    notes = "\n".join(raised.value.__notes__)
    assert "Crash context: 0 turns completed" in notes
    assert "SDK CLI stderr buffer empty on exception: ResultError (attempt 1)" in notes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "late_failure",
    [TransientAPIError("late"), anyio.ClosedResourceError("late")],
    ids=["transient", "closed-resource"],
)
async def test_post_result_teardown_exceptions_are_swallowed(
    monkeypatch, tmp_path, caplog, late_failure
):
    caplog.set_level("DEBUG")

    result = await _claude(monkeypatch, tmp_path, sdk_result("done"), late_failure)

    assert result.output == "done"
    assert not [r for r in caplog.records if r.levelno >= 30]


@pytest.mark.asyncio
async def test_completed_file_edits_emit_their_paths_once(monkeypatch, tmp_path):
    events = []

    await _claude(
        monkeypatch,
        tmp_path,
        _assistant(
            _tool_use("Edit", {"file_path": "/work/rtl/top.sv"}, "e1"),
            _tool_use("Write", {"file_path": "rtl/b.sv"}, "e2"),
        ),
        _user(_tool_result("e1"), _tool_result("e2", is_error=True)),
        _user(_tool_result("e1")),
        sdk_result("done"),
        on_event=events.append,
    )

    assert [e for e in events if e["type"] == "file_change"] == [
        {"type": "file_change", "paths": ["/work/rtl/top.sv"]}
    ]


@pytest.mark.asyncio
async def test_raising_event_callback_never_aborts_the_call(monkeypatch, tmp_path):
    result = await _claude(
        monkeypatch,
        tmp_path,
        _assistant(SimpleNamespace(text="aloud"), usage={"output_tokens": 5}),
        _assistant(_tool_use("Edit", {"file_path": "a.sv"}, "e1")),
        _user(_tool_result("e1")),
        sdk_result("done", total_cost_usd=0.01),
        on_event=_raise_callback_error,
    )

    assert result.output == "done"


@pytest.mark.asyncio
async def test_usage_events_report_deltas_context_and_billed_cost(monkeypatch, tmp_path):
    events = []

    result = await _claude(
        monkeypatch,
        tmp_path,
        _assistant(usage={"output_tokens": 42}),
        _assistant(
            usage={"input_tokens": 4_000, "cache_read_input_tokens": 86_000, "output_tokens": 200}
        ),
        _assistant(SimpleNamespace(text="aloud")),
        _assistant(
            usage={"input_tokens": 2_000, "cache_read_input_tokens": 140_000, "output_tokens": 300}
        ),
        sdk_result("done", total_cost_usd=0.01),
        on_event=events.append,
    )

    usage = [e for e in events if e["type"] == "usage"]
    assert usage[0] == {
        "type": "usage",
        "output_tokens": 42,
        "cost_usd": pytest.approx(42 * 25e-6),
        "context_tokens": 0,
        "context_limit": 1_000_000,
    }
    assert [e["output_tokens"] for e in usage] == [42, 200, 300, 0]
    assert [e["context_tokens"] for e in usage] == [0, 90_000, 142_000, 142_000]
    assert usage[-1]["cost_usd"] < 0  # the billed cost replaces the estimate
    assert sum(e["cost_usd"] for e in usage) == pytest.approx(0.01)
    # The text-only message is shown but reports no usage of its own.
    assert [e["type"] for e in events] == ["usage", "usage", "agent_text", "usage", "usage"]
    assert result.input_tokens == 232_000


@pytest.mark.asyncio
async def test_cache_writes_are_tracked_and_billed_at_their_own_rate(monkeypatch, tmp_path):
    result = await _claude(
        monkeypatch,
        tmp_path,
        _assistant(usage={"input_tokens": 0, "cache_creation_input_tokens": 100_000}),
        sdk_result("done"),
    )

    assert result.cache_create_tokens == 100_000
    # opus-4-8 input is $5/M, so a cache write is $6.25/M.
    assert result.cost_usd == pytest.approx(100_000 * 6.25 / 1e6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture", "expected"),
    [(["ReportFindings"], {"ReportFindings": [{"n": 1}, {"n": 2}]}), (None, {})],
    ids=["named", "nothing"],
)
async def test_capture_collects_only_named_tool_inputs_in_order(
    monkeypatch, tmp_path, capture, expected
):
    result = await _claude(
        monkeypatch,
        tmp_path,
        _assistant(
            _tool_use("ReportFindings", {"n": 1}),
            _tool_use("Read", {"file_path": "a.sv"}),
            _tool_use("ReportFindings", None),
        ),
        _assistant(_tool_use("ReportFindings", {"n": 2})),
        sdk_result("done"),
        capture_agent_capability_calls=capture,
    )

    assert result.captured_agent_capability_calls == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stall", "stall-after-result", "progress"])
async def test_idle_timeout_counts_from_the_last_stream_event(monkeypatch, tmp_path, case):
    # Total time of "progress" (about 0.5s) exceeds its timeout; every gap stays well under it.
    steps = {
        "stall": [scaled(0.4)],
        "stall-after-result": [sdk_result("partial"), scaled(0.4)],
        "progress": [s for _ in range(8) for s in (_assistant(), scaled(0.06))],
    }[case]
    timeout = scaled(0.3 if case == "progress" else 0.15)

    run = _claude(monkeypatch, tmp_path, *steps, sdk_result("done"), timeout_seconds=timeout)
    if case == "stall":
        with pytest.raises(TimeoutError):
            await run
    else:
        assert (await run).output == ("done" if case == "progress" else "partial")


@pytest.fixture
def pinned_models():
    """Pin the [models] reviewer and coverage_analyst entries of one Agent CLI to one model."""

    def pin(provider: str, model: str) -> None:
        set_agent_settings(
            AgentSettings(
                default_cli=provider,
                specialist_models={"reviewer": model, "coverage_analyst": model},
            )
        )

    yield pin
    set_agent_settings(None)


_REFUSED_MODEL = "gpt-refused-model"
_REFUSED_CLAUDE_MODEL = "claude-refused-model"


def _assert_actionable_refusal(error, *, provider: str, model: str, detail_fragment: str) -> None:
    assert type(error).__name__ == "ModelUnavailableError"
    assert error.provider == provider
    assert error.model == model
    message = str(error)
    assert model in message
    assert "[models] reviewer" in message
    assert "[models] coverage_analyst" in message
    assert "Protected Input" in message
    # The raw provider text is kept on the exception, not in the message.
    assert detail_fragment in error.detail
    assert detail_fragment not in message


@pytest.mark.asyncio
async def test_claude_model_refusal_is_terminal_and_actionable(
    tmp_path, monkeypatch, pinned_models
):
    from claude_agent_sdk import ResultError

    pinned_models("claude", _REFUSED_CLAUDE_MODEL)
    detail = (
        f"There's an issue with the selected model ({_REFUSED_CLAUDE_MODEL}). "
        "It may not exist or you may not have access to it."
    )
    attempts = replay_sdk_stream(monkeypatch, _refusal_error(detail))
    sleep = AsyncMock()
    monkeypatch.setattr(anyio, "sleep", sleep)

    with pytest.raises((AgentProviderError, ResultError)) as raised:
        await ClaudeSDKBackend(auth_mode="visitor").call(
            AgentCallParams(prompt="work", model=_REFUSED_CLAUDE_MODEL, cwd=tmp_path)
        )

    _assert_actionable_refusal(
        raised.value,
        provider="claude",
        model=_REFUSED_CLAUDE_MODEL,
        detail_fragment="selected model",
    )
    assert [options.model for options in attempts] == [_REFUSED_CLAUDE_MODEL]
    sleep.assert_not_awaited()


# ===========================================================================
# Codex backend: a replayed run per classification case
# ===========================================================================


def _codex_events(*, source: str, detail: str, partial_output: bool = False) -> list[dict]:
    events = []
    if partial_output:
        events.append(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Working…"}}
        )
    if source == "error":
        events.append({"type": "error", "message": detail})
    elif source == "turn.failed":
        events.append({"type": "turn.failed", "error": {"message": detail}})
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["developer", "reviewer"])
@pytest.mark.parametrize("partial_output", [False, True])
@pytest.mark.parametrize("source", ["error", "turn.failed", "stderr", "stderr-preamble"])
@pytest.mark.parametrize(
    "detail",
    [
        "You\u2019ve hit your usage limit. Try again later.",
        "You hit your spend cap set in your workspace. Increase your spend cap to continue.",
        "Your workspace is out of credits. Add credits to continue.",
    ],
)
async def test_codex_usage_limit_propagates_without_retry(
    tmp_path, monkeypatch, label, partial_output, source, detail
):
    from booley.runtime.agent_execution import configure_agent_call

    stderr = detail if source.startswith("stderr") else ""
    if source == "stderr-preamble":
        stderr = "startup warning\n" * 80 + stderr
    events = _codex_events(source=source, detail=detail, partial_output=partial_output)
    launches = replay_codex_run(monkeypatch, events=events, stderr=stderr, returncode=1)
    sleep = AsyncMock()
    monkeypatch.setattr(anyio, "sleep", sleep)
    params = configure_agent_call(
        AgentCallParams(prompt="work", model="test", cwd=tmp_path, label=label)
    )

    with pytest.raises(UsageLimitError) as raised:
        await CodexBackend().call(params)

    assert raised.value.provider == "codex"
    assert str(raised.value) == (stderr or detail)
    assert len(launches) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("provider_error", [None, "Provider process stopped"])
async def test_codex_agent_text_cannot_identify_a_usage_limit(
    tmp_path, monkeypatch, returncode, provider_error
):
    text = "I'll investigate usage limit, spend cap, and workspace out-of-credits handling."
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}}]
    if provider_error:
        events.append({"type": "turn.failed", "error": {"message": provider_error}})
    launches = replay_codex_run(monkeypatch, events=events, returncode=returncode)

    result = await CodexBackend().call(AgentCallParams(prompt="work", model="test", cwd=tmp_path))

    assert result.output == text
    assert len(launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["stderr", "stdout", "turn.failed"])
@pytest.mark.parametrize(
    ("detail", "retried"),
    [
        pytest.param("Codex could not start: configuration was rejected.", False, id="rejected"),
        pytest.param("Request too large: 1500 bytes", False, id="1500-bytes"),
        pytest.param("max_tokens 2500 exceeds the model limit", False, id="max-tokens-2500"),
        pytest.param("request req_015003 failed validation", False, id="request-id-500"),
        pytest.param("Invalid max_tokens: 500.5; expected an integer", False, id="decimal-500"),
        pytest.param("Invalid connection string in config", False, id="connection-string"),
        pytest.param('unexpected status 503: {"error":"upstream"}', True, id="status-only-503"),
        pytest.param("API Error: 500 internal server error", True, id="api-500"),
        pytest.param("502 Bad Gateway", True, id="bad-gateway"),
        pytest.param("503 Service Unavailable", True, id="service-unavailable"),
        pytest.param("529 overloaded", True, id="overloaded"),
        pytest.param("ECONNRESET", True, id="econnreset"),
        pytest.param("ETIMEDOUT", True, id="etimedout"),
        pytest.param("connection reset by peer", True, id="connection-reset"),
        pytest.param("stream disconnected before completion", True, id="stream-disconnected"),
    ],
)
async def test_codex_no_output_provider_failure_retries_only_transient_text(
    tmp_path, monkeypatch, source, detail, retried
):
    launches = replay_codex_run(
        monkeypatch,
        events=_codex_events(source=source, detail=detail),
        stdout=detail if source == "stdout" else "",
        stderr=detail if source == "stderr" else "",
        returncode=1,
    )
    sleep = AsyncMock()
    monkeypatch.setattr(anyio, "sleep", sleep)

    with pytest.raises(TransientAPIError if retried else AgentProviderError) as raised:
        await CodexBackend().call(AgentCallParams(prompt="work", model="test", cwd=tmp_path))

    assert detail in str(raised.value)
    assert len(launches) == (MAX_API_RETRIES if retried else 1)
    assert sleep.await_count == len(launches) - 1
    if not retried:
        assert raised.value.provider == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["error", "turn.failed", "stderr"])
@pytest.mark.parametrize(
    "detail",
    [
        # The issue's 0.4.0 run; the request id would match the transient "500" pattern.
        '{"status":400,"error":{"message":"The model is unavailable","code":"model_not_found",'
        '"request_id":"req_5001"}}',
        f"The '{_REFUSED_MODEL}' model is not supported when using Codex with a ChatGPT account.",
    ],
    ids=["issue-json", "chatgpt-text"],
)
async def test_codex_model_refusal_is_terminal_and_actionable(
    tmp_path, monkeypatch, pinned_models, source, detail
):
    pinned_models("codex", _REFUSED_MODEL)
    launches = replay_codex_run(
        monkeypatch,
        events=_codex_events(source=source, detail=detail),
        stderr=detail if source == "stderr" else "",
        returncode=1,
    )
    sleep = AsyncMock()
    monkeypatch.setattr(anyio, "sleep", sleep)

    with pytest.raises(AgentProviderError) as raised:
        await CodexBackend().call(
            AgentCallParams(prompt="work", model=_REFUSED_MODEL, cwd=tmp_path)
        )

    _assert_actionable_refusal(
        raised.value, provider="codex", model=_REFUSED_MODEL, detail_fragment=detail[:40]
    )
    assert len(launches) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_call_reports_parsed_output_usage_thread_and_transcript(tmp_path, monkeypatch):
    def message(text):
        return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}

    events = [
        {"type": "thread.started", "thread_id": "thread-7"},
        message("first"),
        message(""),
        message("second"),
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5000, "output_tokens": 200, "cached_input_tokens": 4000},
        },
    ]
    launches = replay_codex_run(monkeypatch, stdout="not json", events=events)
    transcript = tmp_path / "sub" / "transcript.jsonl"

    result = await CodexBackend().call(
        AgentCallParams(
            prompt="Do it",
            system_prompt="You are a reviewer",
            model="test",
            cwd=tmp_path,
            transcript_path=transcript,
        )
    )

    assert result.output == "first\n\nsecond"
    assert (result.input_tokens, result.output_tokens, result.cached_tokens) == (5000, 200, 4000)
    assert result.session_id == "thread-7"
    assert launches[0]["prompt"] == "You are a reviewer\n\n---\n\nDo it"
    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["type"] == "thread.started"


_BYPASS = ["--dangerously-bypass-approvals-and-sandbox", "-"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capabilities", "resume", "tail"),
    [
        pytest.param(None, False, _BYPASS, id="unrestricted"),
        pytest.param(
            ["Read", "Glob", "Grep", "Write", "Edit", "Bash"], False, _BYPASS, id="full-tools"
        ),
        pytest.param(["Read", "Glob", "Grep", "Edit", "Bash"], False, _BYPASS, id="edit-tools"),
        pytest.param(["Bash"], False, _BYPASS, id="bash-only"),
        pytest.param(["Read", "Write"], False, _BYPASS, id="write-without-bash"),
        pytest.param(["Read", "Glob", "Grep"], False, ["-s", "read-only", "-"], id="read-only"),
        pytest.param([], False, ["-s", "read-only", "-"], id="no-tools"),
        pytest.param(
            ["Read"],
            True,
            ["-c", 'sandbox_mode="read-only"', "thread-7", "-"],
            id="read-only-resume",
        ),
    ],
)
async def test_codex_command_maps_tool_tiers_to_sandbox_flags(
    tmp_path, monkeypatch, capabilities, resume, tail
):
    launches = replay_codex_run(monkeypatch)
    params = {"session_id": "thread-7", "resume_session": True} if resume else {}

    await CodexBackend().call(
        AgentCallParams(
            prompt="work",
            model="test",
            cwd=tmp_path,
            allowed_agent_capabilities=capabilities,
            **params,
        )
    )

    (launch,) = launches
    assert launch["cmd"][:3] == (
        ["codex", "exec", "resume"] if resume else ["codex", "exec", "--json"]
    )
    assert launch["cmd"][-len(tail) :] == tail
    assert launch["prompt"] == "work"


@pytest.mark.asyncio
async def test_codex_live_file_change_events_keep_paths(tmp_path, monkeypatch):
    install_fake_codex(tmp_path, monkeypatch)
    write_codex_login(tmp_path)
    change = {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [
                {"kind": "update", "path": "rtl/top.sv"},
                {"kind": "add", "path": "tb/top_tb.sv"},
            ],
        },
    }
    monkeypatch.setenv("FAKE_CODEX_EVENTS", json.dumps([change]))
    events = []

    await codex_report(tmp_path, on_event=events.append)

    assert [e for e in events if e["type"] == "file_change"] == [
        {"type": "file_change", "paths": ["rtl/top.sv", "tb/top_tb.sv"]}
    ]


@pytest.mark.asyncio
async def test_nested_codex_home_holds_the_selected_login_not_the_default_one(
    tmp_path, monkeypatch
):
    require_symlinks(tmp_path)
    install_fake_codex(tmp_path, monkeypatch)
    write_codex_login(tmp_path, '{"OPENAI_API_KEY":"SYNTHETIC_DEFAULT_KEY"}')
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    login = '{"tokens":{"access_token":"SYNTHETIC_SELECTED_LOGIN"}}'
    (alternate / "auth.json").write_bytes(login.encode())
    monkeypatch.setenv("CODEX_HOME", str(alternate))

    report = await codex_report(tmp_path, nested_mcp_tools=["lint"])

    home = Path(report["home"])
    assert home != alternate
    assert (home / "auth.json").read_text(encoding="utf-8") == login


@pytest.mark.asyncio
async def test_cached_nested_home_drops_a_removed_login(tmp_path, monkeypatch):
    require_symlinks(tmp_path)
    install_fake_codex(tmp_path, monkeypatch)
    login = write_codex_login(tmp_path)

    first = await codex_report(tmp_path, auth_mode="visitor", nested_mcp_tools=["lint"])
    login.unlink()
    second = await codex_report(tmp_path, auth_mode="visitor", nested_mcp_tools=["lint"])

    assert first["auth_json_exists"] is True
    assert second["home"] == first["home"]
    assert second["auth_json_exists"] is False
    # A link left dangling is not a login the fake can read, but it is still a leftover.
    assert not os.path.lexists(Path(second["home"]) / "auth.json")


@pytest.mark.asyncio
async def test_subscription_codex_pins_relative_cli_home_before_changing_cwd(
    tmp_path, monkeypatch
):
    # Kept on the private spawn: this host rule must hold on Windows, and the shared fake
    # `codex` is a POSIX script, so no public entry point reaches the spawn there.
    import asyncio

    from booley.runtime import _codex_backend

    parent = tmp_path / "parent"
    child = tmp_path / "child"
    for directory, content in (
        (parent, '{"tokens":{"access_token":"SYNTHETIC_LOGIN"}}'),
        (child, '{"OPENAI_API_KEY":"SYNTHETIC_KEY"}'),
    ):
        (directory / "alternate").mkdir(parents=True)
        (directory / "alternate" / "auth.json").write_text(content)
    monkeypatch.chdir(parent)
    monkeypatch.setenv("CODEX_HOME", "alternate")
    place(monkeypatch, RuntimeLocation.HOST)
    captured = {}

    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _codex_backend._codex_spawn(
        ["codex"], AgentCallParams(prompt="x", model="chosen", cwd=child), "subscription"
    )
    assert captured["env"]["CODEX_HOME"] == str(parent / "alternate")


# ===========================================================================
# BackendConfig and config loading
# ===========================================================================


@pytest.fixture
def sandbox(monkeypatch):
    """Choose the host or Sandbox side of the config, with no hand-off variables set."""
    for name in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_PRIMARY_AUTH", "BOOLEY_AGENT_APP"):
        monkeypatch.delenv(name, raising=False)
    set_backend_config(None)

    def choose(*, inside: bool) -> None:
        place(
            monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX if inside else RuntimeLocation.HOST
        )

    choose(inside=False)
    yield choose
    set_backend_config(None)


def _container_host(monkeypatch):
    place(monkeypatch, RuntimeLocation.HOST, container_host=True)


def _unmarked_sandbox(monkeypatch):
    """A Sandbox its spec declares, in an image that left no marker file."""
    place(monkeypatch, RuntimeLocation.HOST)
    point_mounts(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


_VENUES = [
    pytest.param(_container_host, False, id="container-host"),
    pytest.param(_unmarked_sandbox, True, id="sandbox-without-marker"),
]


@pytest.mark.parametrize(("venue", "in_sandbox"), _VENUES)
def test_agent_configuration_follows_the_sandbox_classification(
    sandbox, monkeypatch, venue, in_sandbox
):
    venue(monkeypatch)
    monkeypatch.delenv("BOOLEY_AGENT_CLIS", raising=False)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")

    assert config_agent.gateway_handoff_active() is in_sandbox
    assert config_agent.visitor_mode_honoured() is in_sandbox
    settings = config_agent.settings_from_data({"agent": {"default": "codex"}}, lazy=True)
    assert (settings.auth_for("codex") == "gateway") is in_sandbox

    monkeypatch.setenv("BOOLEY_AGENT_APP", "auto")
    if in_sandbox:
        assert agent_app.auto_requested() is True
    else:
        with pytest.raises(agent_app.AgentAppError, match="honored only inside a Sandbox"):
            agent_app.auto_requested()


@pytest.mark.asyncio
@pytest.mark.parametrize(("venue", "in_sandbox"), _VENUES)
async def test_codex_bypasses_its_os_sandbox_in_any_container(
    tmp_path, monkeypatch, venue, in_sandbox
):
    # Both venues are containers, so the expectation does not depend on ``in_sandbox``.
    launches = replay_codex_run(monkeypatch)
    venue(monkeypatch)

    await CodexBackend().call(
        AgentCallParams(
            prompt="work", model="test", cwd=tmp_path, allowed_agent_capabilities=["Read"]
        )
    )

    (launch,) = launches
    assert launch["cmd"][-len(_BYPASS) :] == _BYPASS


@pytest.mark.asyncio
@pytest.mark.parametrize(("venue", "in_sandbox"), _VENUES)
async def test_nested_codex_home_only_in_a_sandbox(tmp_path, monkeypatch, venue, in_sandbox):
    require_symlinks(tmp_path)
    install_fake_codex(tmp_path, monkeypatch)
    venue(monkeypatch)
    selected = write_codex_login(tmp_path).parent
    (selected / "config.toml").write_bytes(b"")
    monkeypatch.setenv("CODEX_HOME", str(selected))

    report = await codex_report(tmp_path, nested_mcp_tools=["lint"])

    assert (Path(report["home"]) != selected) is in_sandbox


def _write_toml(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(textwrap.dedent(text).encode("utf-8"))


def test_backend_for_builds_other_cli_once_with_its_auth():
    """The default CLI uses the active backend; another listed CLI gets its own, cached."""
    active = MagicMock()
    settings = AgentSettings(
        default_cli="claude", clis=("claude", "codex"), auth={"codex": "api_key"}
    )
    cfg = BackendConfig(settings=settings, active_backend=active)

    codex = cfg.backend_for("codex")

    assert cfg.backend_for("claude") is active
    assert isinstance(codex, CodexBackend)
    # The backend exposes its policy only through a credential check that needs a real login.
    assert codex._auth_mode == "api_key"
    assert cfg.backend_for("codex") is codex


_NO_TOML = ("claude", ("claude",), "subscription")


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param({}, _NO_TOML, id="no-file"),
        pytest.param(
            {"pipeline.toml": "this is not valid toml {{{"}, _NO_TOML, id="malformed-toml"
        ),
        pytest.param(
            {"booley.toml": '[agent]\nprovider = "claude"\n'}, _NO_TOML, id="claude-provider"
        ),
        pytest.param(
            {"booley.toml": '[agent]\nprimary = "codex"\nsecondary = false\n'},
            _NO_TOML,
            id="retired-primary-alias",
        ),
        pytest.param(
            {"booley.toml": '[agent]\nauth = "api_key"\n'},
            ("claude", ("claude",), "api_key"),
            id="auth",
        ),
        pytest.param(
            {"pipeline.toml": '[agent]\nprimary_auth = "api_key"\n'},
            _NO_TOML,
            id="retired-primary-auth-alias",
        ),
        pytest.param(
            {
                "pipeline.toml": '[agent]\nauth = "subscription"\n',
                "booley.toml": '[agent]\nauth = "api_key"\n',
            },
            ("claude", ("claude",), "api_key"),
            id="booley-toml-before-pipeline-toml",
        ),
    ],
)
def test_project_toml_selects_cli_and_auth(sandbox, tmp_path, files, expected):
    for name, text in files.items():
        _write_toml(tmp_path / ".booley", name, text)

    load_backend_config(tmp_path)

    cfg = get_backend_config()
    assert isinstance(cfg.active_backend, ClaudeSDKBackend)
    settings = cfg.settings
    assert (settings.default_cli, settings.clis, settings.auth_for("claude")) == expected


def test_invalid_provider_raises(sandbox, tmp_path):
    """An invalid provider is a hard error, never a silent fallback."""
    _write_toml(tmp_path / ".booley", "booley.toml", '[agent]\nprovider = "gpt4all"\n')

    with pytest.raises(BackendConfigError, match="gpt4all"):
        load_backend_config(tmp_path)


def test_models_pin_on_second_cli(sandbox, tmp_path):
    """A [models] pin on a listed non-default CLI selects that CLI for the Specialist."""
    _write_toml(
        tmp_path / ".booley",
        "booley.toml",
        """\
        [agent]
        clis = ["claude", "codex"]

        [agent.auth]
        codex = "api_key"

        [models]
        reviewer = "gpt-6-luna"
        """,
    )

    load_backend_config(tmp_path)

    settings = get_backend_config().settings
    assert settings.default_cli == "claude"
    assert settings.cli_for_specialist("reviewer") == "codex"
    assert settings.model_for_specialist("reviewer") == "gpt-6-luna"
    assert (settings.auth_for("claude"), settings.auth_for("codex")) == ("subscription", "api_key")


def test_direct_settings_reload_refreshes_runtime_backend(sandbox, tmp_path):
    """Runtime notices a Config reload performed by a config-only caller."""
    from booley.config.agent import load_agent_settings

    assert isinstance(get_backend_config().active_backend, ClaudeSDKBackend)
    _write_toml(tmp_path / ".booley", "booley.toml", '[agent]\nprovider = "codex"\n')

    load_agent_settings(tmp_path)

    cfg = get_backend_config()
    assert isinstance(cfg.active_backend, CodexBackend)
    assert cfg.settings.default_cli == "codex"


_CLAUDE = ("claude", None)
_FIX = "BOOLEY_PRIMARY_PROVIDER=claude"


@pytest.mark.parametrize(
    ("inside", "env", "toml", "expected"),
    [
        pytest.param(False, {}, None, _CLAUDE, id="host-nothing"),
        pytest.param(
            False,
            {},
            '[agent]\nprovider = "codex"\nauth = "api_key"\n',
            ("codex", "api_key"),
            id="project-toml",
        ),
        pytest.param(
            False,
            {"BOOLEY_PRIMARY_PROVIDER": "codex"},
            '[agent]\nprovider = "claude"\nauth = "api_key"\n',
            ("codex", "api_key"),
            id="handoff-keeps-project-auth",
        ),
        pytest.param(
            False,
            {"BOOLEY_PRIMARY_PROVIDER": "claude", "BOOLEY_AGENT_APP": "codex"},
            None,
            _CLAUDE,
            id="provider-env-beats-agent-app",
        ),
        pytest.param(True, {}, None, "unresolved inside container", id="container-nothing"),
        pytest.param(True, {"BOOLEY_AGENT_APP": "codex"}, None, ("codex", None), id="agent-app"),
        pytest.param(
            True, {"BOOLEY_AGENT_APP": "none"}, None, "unresolved inside container", id="app-none"
        ),
    ],
)
def test_lazy_backend_resolution(sandbox, monkeypatch, tmp_path, inside, env, toml, expected):
    sandbox(inside=inside)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if toml is not None:
        _write_toml(tmp_path, "booley.toml", toml)

    if isinstance(expected, str):
        with pytest.raises(BackendConfigError, match=expected) as raised:
            get_backend_config()
        # The message tells a standalone user how to pin a provider.
        assert _FIX in str(raised.value)
        assert "[agent] default" in str(raised.value)
        return
    cli, codex_auth = expected
    cfg = get_backend_config()
    assert isinstance(cfg.active_backend, {"claude": ClaudeSDKBackend, "codex": CodexBackend}[cli])
    assert cfg.settings.default_cli == cli
    if codex_auth is not None:
        assert cfg.settings.auth_for("codex") == codex_auth
