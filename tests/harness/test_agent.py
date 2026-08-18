"""Tests for agent.py -- JSON extraction, CodexBackend, transient detection, transcripts."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from booley.harness.blocking import AgentTimeoutError, TransientAPIError
from booley.harness.models import AgentCallParams
from booley.runtime._codex_backend import _codex_run_subprocess, _codex_write_markdown
from booley.runtime.agent import (
    _is_transient_error,
    _transcript_path_for_attempt,
    _write_transcript_turn,
    call_agent,
    extract_json,
)
from booley.runtime.agent_backend import (
    CodexBackend,
    _codex_ensure_additional_properties,
    _codex_extract_structured,
    _codex_parse_events,
    _codex_sandbox_mode,
)


def _assistant_message():
    """Build an SDK message without coupling every test to its constructor."""
    from claude_agent_sdk import AssistantMessage

    try:
        return AssistantMessage(content=[], model="test-model")
    except TypeError:
        # claude-agent-sdk 0.1.x exposed mutable, no-argument message classes.
        message = AssistantMessage()
        message.content = []
        message.model = "test-model"
        return message


def _result_message():
    """Build an SDK result with the protocol fields required by current SDKs."""
    from claude_agent_sdk import ResultMessage

    try:
        return ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="test-session",
        )
    except TypeError:
        # claude-agent-sdk 0.1.x exposed mutable, no-argument message classes.
        message = ResultMessage()
        message.subtype = "success"
        message.duration_ms = 0
        message.duration_api_ms = 0
        message.is_error = False
        message.num_turns = 1
        message.session_id = "test-session"
        return message


# ===========================================================================
# extract_json
# ===========================================================================


class TestExtractJson:
    def test_fenced_json_block(self):
        text = 'Here is the result:\n```json\n{"issues_found": 2, "critical_found": 1}\n```\nDone.'
        result = extract_json(text)
        assert result == {"issues_found": 2, "critical_found": 1}

    def test_fenced_without_json_tag(self):
        text = '```\n{"key": "value"}\n```\n'
        result = extract_json(text)
        assert result == {"key": "value"}

    def test_bare_json_object(self):
        text = 'Some preamble. {"found": true, "count": 5} and some trailing text.'
        result = extract_json(text)
        assert result == {"found": True, "count": 5}

    def test_nested_json(self):
        obj = {"outer": {"inner": [1, 2, 3]}, "flag": True}
        text = f"Result: {json.dumps(obj)} done."
        result = extract_json(text)
        assert result == obj

    def test_no_json_returns_none(self):
        assert extract_json("No JSON here at all.") is None

    def test_empty_string_returns_none(self):
        assert extract_json("") is None

    def test_invalid_json_in_fence_falls_through(self):
        """Invalid JSON in fence -> tries bare JSON extraction."""
        text = '```json\n{bad json}\n```\n{"fallback": true}'
        result = extract_json(text)
        assert result == {"fallback": True}

    def test_multiple_json_objects_returns_first_valid(self):
        text = '{"first": 1} {"second": 2}'
        result = extract_json(text)
        assert result == {"first": 1}

    def test_json_with_braces_in_strings(self):
        obj = {"message": "use { and } carefully"}
        text = f"Output: {json.dumps(obj)}"
        result = extract_json(text)
        assert result == obj

    def test_malformed_braces_skipped(self):
        """Unbalanced braces before valid JSON should be skipped."""
        text = 'prefix { invalid {"valid": true}'
        result = extract_json(text)
        assert result == {"valid": True}

    def test_multiline_fenced_json(self):
        text = (
            "```json\n"
            "{\n"
            '  "issues_found": 3,\n'
            '  "issues_fixed": 2,\n'
            '  "issues": [\n'
            '    {"severity": "CRITICAL", "summary": "Bug"}\n'
            "  ]\n"
            "}\n"
            "```"
        )
        result = extract_json(text)
        assert result["issues_found"] == 3
        assert len(result["issues"]) == 1


# ===========================================================================
# CodexBackend helpers
# ===========================================================================


class TestCodexHelpers:
    def test_sandbox_mode_full_tools(self):
        assert (
            _codex_sandbox_mode(["Read", "Glob", "Grep", "Write", "Edit", "Bash"])
            == "danger-full-access"
        )

    def test_sandbox_mode_edit_tools(self):
        assert _codex_sandbox_mode(["Read", "Glob", "Grep", "Edit"]) == "danger-full-access"

    def test_sandbox_mode_read_only(self):
        assert _codex_sandbox_mode(["Read", "Glob", "Grep"]) == "read-only"

    def test_sandbox_mode_none(self):
        assert _codex_sandbox_mode(None) == "danger-full-access"

    def test_ensure_additional_properties(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        result = _codex_ensure_additional_properties(schema)
        assert result["additionalProperties"] is False
        assert result["required"] == ["x"]
        assert "additionalProperties" not in schema  # original unchanged
        assert "required" not in schema

    def test_ensure_additional_properties_nested(self):
        schema = {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {"y": {"type": "integer"}}}},
        }
        result = _codex_ensure_additional_properties(schema)
        assert result["additionalProperties"] is False
        assert result["required"] == ["inner"]
        assert result["properties"]["inner"]["additionalProperties"] is False
        assert result["properties"]["inner"]["required"] == ["y"]

    def test_ensure_additional_properties_completes_partial_required(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
        }
        result = _codex_ensure_additional_properties(schema)
        assert set(result["required"]) == {"a", "b"}, (
            "must include all properties for OpenAI strict mode"
        )

    def test_ensure_additional_properties_array_items(self):
        schema = {
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                    },
                },
            },
        }
        result = _codex_ensure_additional_properties(schema)
        items = result["properties"]["commands"]["items"]
        assert "cmd" in items["required"]
        assert "purpose" in items["required"]
        assert items["additionalProperties"] is False

    def test_parse_events_success(self):
        events_jsonl = "\n".join(
            [
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}
                ),
                json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}
                ),
            ]
        )
        text, in_tok, out_tok, _, err, events = _codex_parse_events(events_jsonl)
        assert text == "hello"
        assert in_tok == 100
        assert out_tok == 50
        assert err is None
        assert len(events) == 2

    def test_parse_events_error(self):
        events_jsonl = json.dumps({"type": "error", "message": "something broke"})
        text, _in_tok, _out_tok, _, err, _events = _codex_parse_events(events_jsonl)
        assert text == ""
        assert err == "something broke"

    def test_parse_events_turn_failed(self):
        events_jsonl = json.dumps({"type": "turn.failed", "error": {"message": "rate limit"}})
        _, _, _, _, err, _ = _codex_parse_events(events_jsonl)
        assert err == "rate limit"


class TestCodexExtractStructured:
    """Tests for _codex_extract_structured — prefer last segment."""

    def test_single_json(self):
        result, fallback = _codex_extract_structured('{"configs": ["config_a_prot"]}')
        assert result == {"configs": ["config_a_prot"]}
        assert fallback is False

    def test_multi_segment_picks_last(self):
        first = json.dumps({"configs": []})
        second = json.dumps({"configs": ["config_a_prot"]})
        output = first + "\n\n" + second
        result, fallback = _codex_extract_structured(output)
        assert result == {"configs": ["config_a_prot"]}
        assert fallback is True

    def test_multi_segment_real_world(self):
        first = json.dumps({"execution_context": {"configs": [], "defines": []}})
        second = json.dumps(
            {"execution_context": {"configs": ["config_a_prot"], "defines": ["X"]}}
        )
        output = first + "\n\n" + second
        result, fallback = _codex_extract_structured(output)
        assert result["execution_context"]["configs"] == ["config_a_prot"]
        assert fallback is True

    def test_embedded_json(self):
        result, fallback = _codex_extract_structured('Answer: {"x": 1} done')
        assert result == {"x": 1}
        assert fallback is True

    def test_empty_string(self):
        result, fallback = _codex_extract_structured("")
        assert result is None
        assert fallback is True

    def test_no_json(self):
        result, fallback = _codex_extract_structured("no json here at all")
        assert result is None
        assert fallback is True


class TestCodexBackend:
    def test_health_check_not_on_path(self):
        backend = CodexBackend()
        with patch("booley.runtime._codex_backend.shutil.which", return_value=None):
            result = backend.health_check()
        assert "not found" in result

    def test_health_check_ok(self):
        backend = CodexBackend()
        with patch("booley.runtime._codex_backend.shutil.which", return_value="/usr/bin/codex"):
            result = backend.health_check()
        assert result is None

    @pytest.mark.asyncio
    async def test_agent_timeout_is_not_retried(self, tmp_path: Path):
        backend = CodexBackend()
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path)
        timeout = AgentTimeoutError("Codex timed out after 7200s")

        with patch.object(backend, "_call_once", new_callable=AsyncMock) as call_once:
            call_once.side_effect = timeout
            with pytest.raises(AgentTimeoutError, match="7200s"):
                await backend.call(params)

        call_once.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wall_clock_deadline_raises_agent_timeout(self, tmp_path: Path):
        params = AgentCallParams(
            prompt="task",
            model="gpt-5",
            cwd=tmp_path,
            timeout_seconds=7200,
        )
        process = MagicMock()
        process.returncode = None
        process.wait = AsyncMock(return_value=-9)

        async def force_timeout(awaitable, timeout):
            assert timeout == 7200
            awaitable.close()
            raise TimeoutError

        with (
            patch(
                "booley.runtime._codex_backend._codex_spawn",
                new_callable=AsyncMock,
                return_value=(process, tmp_path),
            ),
            patch("booley.runtime._codex_backend.asyncio.wait_for", new=force_timeout),
            pytest.raises(AgentTimeoutError, match="7200s"),
        ):
            await _codex_run_subprocess([], params, full_prompt="task", on_event=None)

        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_external_cancellation_kills_codex_process(self, tmp_path: Path):
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path)
        process = MagicMock()
        process.returncode = None
        process.stdin = MagicMock()
        process.stdin.drain = AsyncMock()
        process.stdin.wait_closed = AsyncMock()
        process.stdout = asyncio.StreamReader()
        process.stderr = asyncio.StreamReader()
        process.wait = AsyncMock(return_value=-9)

        with patch(
            "booley.runtime._codex_backend._codex_spawn",
            new_callable=AsyncMock,
            return_value=(process, tmp_path),
        ):
            task = asyncio.create_task(
                _codex_run_subprocess([], params, full_prompt="task", on_event=None)
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        process.kill.assert_called_once_with()
        process.wait.assert_awaited()

    @pytest.mark.asyncio
    async def test_transient_failure_is_still_retried(self, tmp_path: Path):
        from booley.harness.models import AgentResult

        backend = CodexBackend()
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path)
        recovered = AgentResult(output="done")

        with (
            patch.object(backend, "_call_once", new_callable=AsyncMock) as call_once,
            patch("booley.runtime._codex_backend.anyio.sleep", new_callable=AsyncMock) as sleep,
        ):
            call_once.side_effect = [TransientAPIError("server unavailable"), recovered]
            result = await backend.call(params)

        assert result is recovered
        assert call_once.await_count == 2
        sleep.assert_awaited_once()


class TestCodexMarkdownTranscript:
    def test_renders_mcp_tool_results(self, tmp_path: Path):
        transcript = tmp_path / "coder.jsonl"
        events = [
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "booley",
                    "tool": "elab",
                    "arguments": {"config": "default"},
                    "status": "completed",
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "EXIT_CODE: 0\n\nRESULT: PASS (1/1)",
                            },
                        ],
                    },
                },
            },
        ]

        _codex_write_markdown(events, transcript, user_prompt="implement rtl")
        rendered = transcript.with_suffix(".md").read_text(encoding="utf-8")

        assert "# Actual Prompt Sent" in rendered
        assert "# User Prompt" not in rendered
        assert "**MCP Tool:** `booley.elab`" in rendered
        assert '"config": "default"' in rendered
        assert "RESULT: PASS (1/1)" in rendered

    def test_runtime_markdown_goes_to_human_logs(self, tmp_path: Path):
        transcript = (
            tmp_path / ".runtime" / "transcripts" / "reviewer" / "1" / "review-tb-quality.jsonl"
        )
        events = [
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "review",
                    "exit_code": 0,
                    "aggregated_output": "review complete",
                },
            },
        ]

        _codex_write_markdown(events, transcript, user_prompt="review tb")

        assert not transcript.with_suffix(".md").exists()
        rendered = (
            tmp_path / "human-logs" / "transcripts" / "reviewer" / "1" / "review-tb-quality.md"
        )
        assert rendered.read_text(encoding="utf-8")
        assert "review complete" in rendered.read_text(encoding="utf-8")

    def test_suppresses_structured_only_commit_messages(self, tmp_path: Path):
        transcript = tmp_path / "coder.jsonl"
        events = [
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": '{"commit_message":"feat(rtl): add mux"}',
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Implemented the mux.",
                },
            },
        ]

        _codex_write_markdown(events, transcript, user_prompt="implement rtl")
        rendered = transcript.with_suffix(".md").read_text(encoding="utf-8")

        assert '{"commit_message"' not in rendered
        assert "Implemented the mux." in rendered


# ===========================================================================
# _is_transient_error
# ===========================================================================


class TestIsTransientError:
    def test_connection_error_is_transient(self):
        assert _is_transient_error(ConnectionError("reset")) is True

    def test_os_error_is_transient(self):
        assert _is_transient_error(OSError("network down")) is True

    def test_generic_exception_is_not_transient(self):
        assert _is_transient_error(ValueError("bad value")) is False

    def test_runtime_error_is_not_transient(self):
        assert _is_transient_error(RuntimeError("unexpected")) is False

    def test_process_error_with_500(self):
        from claude_agent_sdk import ProcessError

        exc = ProcessError("500 internal server error")
        exc.stderr = "500 internal server error"
        assert _is_transient_error(exc) is True

    def test_process_error_with_529(self):
        from claude_agent_sdk import ProcessError

        exc = ProcessError("529 overloaded")
        exc.stderr = "529 overloaded"
        assert _is_transient_error(exc) is True

    def test_process_error_non_transient(self):
        from claude_agent_sdk import ProcessError

        exc = ProcessError("permission denied")
        exc.stderr = "permission denied"
        assert _is_transient_error(exc) is False


# ===========================================================================
# _transcript_path_for_attempt
# ===========================================================================


class TestTranscriptPathForAttempt:
    def test_none_returns_none(self):
        assert _transcript_path_for_attempt(None, 1) is None
        assert _transcript_path_for_attempt(None, 3) is None

    def test_first_attempt_returns_original(self):
        base = Path("/logs/02-planning-draft.jsonl")
        assert _transcript_path_for_attempt(base, 1) == base

    def test_retry_appends_suffix(self):
        base = Path("/logs/02-planning-draft.jsonl")
        result = _transcript_path_for_attempt(base, 2)
        assert result == Path("/logs/02-planning-draft-retry2.jsonl")

    def test_retry_3(self):
        base = Path("/logs/transcript.jsonl")
        result = _transcript_path_for_attempt(base, 3)
        assert result == Path("/logs/transcript-retry3.jsonl")


# ===========================================================================
# _write_transcript_turn
# ===========================================================================


class TestWriteTranscriptTurn:
    def test_writes_jsonl_line(self):
        """AssistantMessage with text block -> valid JSONL line."""
        # Create a mock message with content blocks
        msg = _assistant_message()
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "Found the bug"
        del text_block.name  # TextBlock has no name attr
        del text_block.input
        del text_block.content
        del text_block.thinking
        msg.content = [text_block]
        msg.usage = {"input_tokens": 100, "output_tokens": 50}

        buf = io.StringIO()
        _write_transcript_turn(buf, msg)

        line = buf.getvalue().strip()
        data = json.loads(line)
        assert "timestamp" in data
        assert data["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert len(data["content"]) == 1
        assert data["content"][0]["type"] == "TextBlock"
        assert data["content"][0]["text"] == "Found the bug"

    def test_empty_content(self):
        msg = _assistant_message()
        msg.content = []
        msg.usage = None

        buf = io.StringIO()
        _write_transcript_turn(buf, msg)

        data = json.loads(buf.getvalue().strip())
        assert data["content"] == []

    def test_tool_use_block(self):
        msg = _assistant_message()
        tool_block = MagicMock()
        type(tool_block).__name__ = "ToolUseBlock"
        tool_block.name = "Bash"
        tool_block.input = {"command": "ls"}
        del tool_block.text
        del tool_block.content
        del tool_block.thinking
        msg.content = [tool_block]
        msg.usage = {}

        buf = io.StringIO()
        _write_transcript_turn(buf, msg)

        data = json.loads(buf.getvalue().strip())
        block = data["content"][0]
        assert block["type"] == "ToolUseBlock"
        assert block["name"] == "Bash"
        assert block["input"] == {"command": "ls"}

    def test_thinking_block(self):
        msg = _assistant_message()
        think_block = MagicMock()
        type(think_block).__name__ = "ThinkingBlock"
        think_block.thinking = "Let me analyze..."
        del think_block.text
        del think_block.name
        del think_block.input
        del think_block.content
        msg.content = [think_block]
        msg.usage = {}

        buf = io.StringIO()
        _write_transcript_turn(buf, msg)

        data = json.loads(buf.getvalue().strip())
        block = data["content"][0]
        assert block["type"] == "ThinkingBlock"
        assert block["thinking"] == "Let me analyze..."


# ===========================================================================
# call_agent (integration with retry logic)
# ===========================================================================


class _AsyncIter:
    """Helper: wraps a list of messages into an async iterator for mocking query()."""

    def __init__(self, items):
        self._items = items
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


class TestCallAgent:
    @pytest.fixture(autouse=True)
    def _disable_docker_sandbox(self):
        """Disable Docker sandbox so call_agent routes through ClaudeSDKBackend."""
        from booley.config.agent import (
            BackendConfig,
            SandboxConfig,
            set_backend_config,
        )
        from booley.runtime.agent_backend import ClaudeSDKBackend

        cfg = BackendConfig(
            active_backend=ClaudeSDKBackend(),
            sandbox=SandboxConfig(),
        )
        set_backend_config(cfg)
        yield
        set_backend_config(None)

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_successful_call(self, mock_query):
        """Happy path: agent returns result message."""
        result_msg = _result_message()
        result_msg.result = "Fixed the bug"
        result_msg.structured_output = {"fixed": True}
        result_msg.total_cost_usd = 0.05
        result_msg.usage = {"input_tokens": 1000, "output_tokens": 500}

        mock_query.side_effect = lambda **kw: _AsyncIter([result_msg])

        result = await call_agent(
            AgentCallParams(
                prompt="Fix this",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
            )
        )
        assert result.output == "Fixed the bug"
        assert result.structured == {"fixed": True}
        assert result.input_tokens == 1000
        assert result.output_tokens == 500

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_assistant_message_accumulates_tokens(self, mock_query):
        """AssistantMessage usage tokens are accumulated."""
        msg1 = _assistant_message()
        msg1.content = []
        msg1.usage = {"input_tokens": 500, "output_tokens": 200}

        msg2 = _assistant_message()
        msg2.content = []
        msg2.usage = {"input_tokens": 600, "output_tokens": 300}

        result_msg = _result_message()
        result_msg.result = "done"
        result_msg.structured_output = None
        result_msg.total_cost_usd = 0.01
        result_msg.usage = None

        mock_query.side_effect = lambda **kw: _AsyncIter([msg1, msg2, result_msg])

        result = await call_agent(
            AgentCallParams(
                prompt="task",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
            )
        )
        assert result.input_tokens == 1100
        assert result.output_tokens == 500

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.anyio.sleep", new_callable=AsyncMock)
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_transient_error_retried(self, mock_query, mock_sleep):
        """TransientAPIError triggers retry with backoff."""
        call_count = 0

        def fake_query(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("ECONNRESET")
            result_msg = _result_message()
            result_msg.result = "recovered"
            result_msg.structured_output = None
            result_msg.total_cost_usd = 0.01
            result_msg.usage = {"input_tokens": 100, "output_tokens": 50}
            return _AsyncIter([result_msg])

        mock_query.side_effect = fake_query

        result = await call_agent(
            AgentCallParams(
                prompt="task",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
            )
        )
        assert result.output == "recovered"
        assert call_count == 2
        mock_sleep.assert_called_once()  # backoff sleep

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_extracts_json_from_text_when_no_structured(self, mock_query):
        """When output_format set but SDK returns no structured_output, extract from text."""
        result_msg = _result_message()
        result_msg.result = 'Here is the result:\n```json\n{"count": 5}\n```'
        result_msg.structured_output = None
        result_msg.total_cost_usd = 0.01
        result_msg.usage = {"input_tokens": 100, "output_tokens": 50}

        mock_query.side_effect = lambda **kw: _AsyncIter([result_msg])

        result = await call_agent(
            AgentCallParams(
                prompt="task",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
                output_format={"type": "object"},
            )
        )
        assert result.structured == {"count": 5}

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_structured_output_empty_dict_fallback(self, mock_query):
        """Empty dict structured_output triggers text fallback extraction."""
        result_msg = _result_message()
        result_msg.result = '{"stage_context": {"impl": "create file"}}'
        result_msg.structured_output = {}
        result_msg.total_cost_usd = 0.01
        result_msg.usage = {"input_tokens": 100, "output_tokens": 50}

        mock_query.side_effect = lambda **kw: _AsyncIter([result_msg])

        result = await call_agent(
            AgentCallParams(
                prompt="task",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
                output_format={"type": "object"},
            )
        )
        assert result.structured == {"stage_context": {"impl": "create file"}}
        assert result.structured_fallback is True

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_successful_call_with_output_format_wrapping(self, mock_query):
        """output_format without type=json_schema gets auto-wrapped."""
        result_msg = _result_message()
        result_msg.result = "done"
        result_msg.structured_output = {"key": "val"}
        result_msg.total_cost_usd = 0.01
        result_msg.usage = {"input_tokens": 100, "output_tokens": 50}

        mock_query.side_effect = lambda **kw: _AsyncIter([result_msg])

        result = await call_agent(
            AgentCallParams(
                prompt="task",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
                output_format={"type": "object", "properties": {}},
            )
        )
        assert result.structured == {"key": "val"}
        # Verify the SDK got the wrapped format
        opts = mock_query.call_args.kwargs["options"]
        assert opts.output_format["type"] == "json_schema"

    @pytest.mark.asyncio
    @patch("booley.runtime._claude_backend.query", new_callable=MagicMock)
    async def test_writes_transcript(self, mock_query, tmp_path: Path):
        """Transcript JSONL written when transcript_path provided."""
        msg = _assistant_message()
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "working on it"
        del text_block.name
        del text_block.input
        del text_block.content
        del text_block.thinking
        msg.content = [text_block]
        msg.usage = {"input_tokens": 100, "output_tokens": 50}

        result_msg = _result_message()
        result_msg.result = "done"
        result_msg.structured_output = None
        result_msg.total_cost_usd = 0.01
        result_msg.usage = None

        mock_query.side_effect = lambda **kw: _AsyncIter([msg, result_msg])

        transcript = tmp_path / "transcripts" / "test.jsonl"
        _result = await call_agent(
            AgentCallParams(
                prompt="task",
                model="claude-sonnet-4-6",
                cwd="/tmp",
                timeout_seconds=60,
                transcript_path=transcript,
            )
        )
        assert transcript.exists()
        lines = transcript.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 2
        data = json.loads(lines[-1])
        assert data["content"][0]["text"] == "working on it"
