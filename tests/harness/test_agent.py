"""Tests for agent.py -- JSON extraction, the Agent call, retries, Codex output and transcripts."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from booley.core.models import AgentCallParams

# Private imports: see the comments at their uses (strict schema file, process lifecycle).
from booley.runtime._codex_backend import (
    _codex_ensure_additional_properties,
    _codex_run_subprocess,
)
from booley.runtime.agent import announce_agent_dispatch, call_agent, extract_json
from booley.runtime.agent_errors import (
    AgentProviderError,
    AgentTimeoutError,
    UsageLimitError,
)
from tests.claude_cli_fake import replay_sdk_stream, sdk_message, sdk_result
from tests.codex_cli_fake import replay_codex_run
from tests.path_tool_fake import PathToolFake


def test_agent_notice_names_role_and_purpose_on_stderr(capsys):
    announce_agent_dispatch(SimpleNamespace(label="triage-report"))
    output = capsys.readouterr()
    assert output.out == ""
    assert "triage-report Specialist to perform the requested Specialist analysis" in output.err
    assert "Ticket" not in output.err


def test_doctor_probe_notice_names_provider_authorization_not_developer_agent(capsys):
    announce_agent_dispatch(SimpleNamespace(label="doctor-developer-probe"))
    err = capsys.readouterr().err
    assert "for Doctor" in err
    assert "provider probe" in err
    assert "Specialist provider authorization" in err
    assert "Developer Agent" not in err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('Here:\n```json\n{"issues_found": 2}\n```\nDone.', {"issues_found": 2}),
        ('```\n{"key": "value"}\n```\n', {"key": "value"}),
        ('Preamble. {"found": true, "count": 5} and trailing.', {"found": True, "count": 5}),
        ('Output: {"message": "use { and } carefully"}', {"message": "use { and } carefully"}),
        ('```json\n{bad json}\n```\n{"fallback": true}', {"fallback": True}),
        ('{"first": 1} {"second": 2}', {"first": 1}),
        ('prefix { invalid {"valid": true}', {"valid": True}),
        (
            '```json\n{\n  "issues": [\n    {"severity": "CRITICAL"}\n  ]\n}\n```',
            {"issues": [{"severity": "CRITICAL"}]},
        ),
        ("No JSON here at all.", None),
        ("", None),
    ],
    ids=[
        "fenced",
        "untagged-fence",
        "bare-in-prose",
        "braces-in-strings",
        "invalid-fence-falls-through",
        "first-of-several",
        "malformed-braces-skipped",
        "multiline-fence",
        "no-json",
        "empty",
    ],
)
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_codex_strict_schema_requires_every_property_and_leaves_the_input_alone():
    # The strict schema exists only in the temp file Codex reads during the call;
    # _call_once deletes it before call returns, so no public result shows it.
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "object", "properties": {"y": {"type": "integer"}}},
            "commands": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}, "purpose": {"type": "string"}},
                },
            },
        },
        "required": ["a"],
    }
    before = json.dumps(schema)

    result = _codex_ensure_additional_properties(schema)

    assert result == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "a": {"type": "string"},
            "b": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"y": {"type": "integer"}},
                "required": ["y"],
            },
            "commands": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"cmd": {"type": "string"}, "purpose": {"type": "string"}},
                    "required": ["cmd", "purpose"],
                },
            },
        },
        "required": ["a", "b", "commands"],
    }
    assert json.dumps(schema) == before


def _agent_message(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "structured", "fallback"),
    [
        (['{"configs": ["a"]}'], {"configs": ["a"]}, False),
        (['{"configs": []}', '{"configs": ["a"]}'], {"configs": ["a"]}, True),
        (['{"configs": []}', 'Final: {"configs": ["a"]} done'], {"configs": ["a"]}, True),
        (['Answer: {"configs": ["a"]} done'], {"configs": ["a"]}, True),
        (["no json here at all"], None, True),
    ],
    ids=["single", "two-messages", "json-then-prose", "prose-with-json", "no-json"],
)
async def test_codex_structured_output_prefers_the_last_message(
    monkeypatch, tmp_path, caplog, texts, structured, fallback
):
    from booley.runtime.agent_backend import CodexBackend

    replay_codex_run(monkeypatch, events=[_agent_message(text) for text in texts])
    params = AgentCallParams(
        prompt="p", model="gpt-5", cwd=tmp_path, output_format={"type": "object"}
    )

    with caplog.at_level(logging.INFO, logger="booley.runtime._codex_backend"):
        result = await CodexBackend(auth_mode="visitor").call(params)

    assert (result.structured, result.structured_fallback) == (structured, fallback)
    # A supported last-segment recovery is not a warning (f9a2875a7).
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "last-segment" in record.getMessage()
    ]


class TestCodexBackend:
    @pytest.mark.parametrize("on_path", [True, False])
    def test_health_check_reports_a_missing_cli(self, monkeypatch, tmp_path, on_path):
        from booley.runtime.agent_backend import CodexBackend

        bin_dir = tmp_path / "bin"
        if on_path:
            PathToolFake.install(bin_dir, "codex", monkeypatch)
        else:
            bin_dir.mkdir()
        monkeypatch.setenv("PATH", str(bin_dir))

        result = CodexBackend(auth_mode="visitor").health_check()

        if on_path:
            assert result is None
        else:
            assert result is not None
            assert "not found" in result

    @pytest.mark.asyncio
    async def test_agent_timeout_is_not_retried(self, tmp_path: Path):
        # A timeout needs a child that outlives the deadline; the shared fake's
        # runner only returns output, so this replaces the single attempt to show
        # call does not retry it.
        from booley.runtime.agent_backend import CodexBackend

        backend = CodexBackend()
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path)
        timeout = AgentTimeoutError("Codex timed out after 7200s")

        with patch.object(backend, "_call_once", new_callable=AsyncMock) as call_once:
            call_once.side_effect = timeout
            with pytest.raises(AgentTimeoutError, match="7200s"):
                await backend.call(params)

        call_once.assert_awaited_once()

    # Codex's process lifecycle (the deadline kill, a child that reads part of
    # stdin, the cancellation kill) needs a live child. The cross-platform shared
    # fake replaces this runner and the fake codex script is POSIX-only, so the
    # next three tests drive the runner with only the process launch
    # (_codex_spawn) replaced.

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
    async def test_success_after_reading_part_of_the_prompt_is_a_failure(self, tmp_path: Path):
        """Exit 0 after a broken prompt pipe would answer a truncated prompt."""
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path)
        reads_a_little = "import sys; sys.stdin.buffer.read(100); print('ok')"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            reads_a_little,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        larger_than_pipe_buffer = "x" * (4 * 1024 * 1024)

        with (
            patch(
                "booley.runtime._codex_backend._codex_spawn",
                new_callable=AsyncMock,
                return_value=(process, tmp_path),
            ),
            pytest.raises(AgentProviderError, match="before reading the whole prompt"),
        ):
            await _codex_run_subprocess(
                [], params, full_prompt=larger_than_pipe_buffer, on_event=None
            )

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


class TestCodexMarkdownTranscript:
    @pytest.mark.asyncio
    async def test_codex_transcript_markdown_shows_tools_and_hides_structured_messages(
        self, monkeypatch, tmp_path: Path
    ):
        from booley.runtime.agent_backend import CodexBackend

        tool_call = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "booley",
                "tool": "sim",
                "arguments": {"config": "default"},
                "status": "completed",
                "result": {
                    "content": [{"type": "text", "text": "EXIT_CODE: 0\n\nRESULT: PASS (1/1)"}]
                },
            },
        }
        launches = replay_codex_run(
            monkeypatch,
            events=[
                {"type": "turn.started"},
                tool_call,
                _agent_message('{"commit_message":"feat(rtl): add mux"}'),
                _agent_message("Implemented the mux."),
            ],
        )
        transcript = tmp_path / "coder.jsonl"

        await CodexBackend(auth_mode="visitor").call(
            AgentCallParams(
                prompt="implement rtl",
                model="gpt-5",
                cwd=tmp_path,
                transcript_path=transcript,
                allowed_agent_capabilities=["Read", "Edit"],
            )
        )

        rendered = transcript.with_suffix(".md").read_text(encoding="utf-8")
        assert "# Actual Prompt Sent" in rendered
        assert "# User Prompt" not in rendered
        assert "**MCP Tool:** `booley.sim`" in rendered
        assert '"config": "default"' in rendered
        assert "RESULT: PASS (1/1)" in rendered
        assert '{"commit_message"' not in rendered
        assert "Implemented the mux." in rendered
        # Edit-only tools still get the full-access sandbox mode.
        assert "--dangerously-bypass-approvals-and-sandbox" in launches[0]["cmd"]

    @pytest.mark.asyncio
    async def test_codex_runtime_markdown_goes_to_human_logs(self, monkeypatch, tmp_path: Path):
        from booley.runtime.agent_backend import CodexBackend
        from booley.runtime.agent_execution import resolve_agent_artifacts

        command = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "review",
                "exit_code": 0,
                "aggregated_output": "review complete",
            },
        }
        replay_codex_run(monkeypatch, events=[{"type": "turn.started"}, command])
        transcript = (
            tmp_path / ".runtime" / "transcripts" / "reviewer" / "1" / "review-tb-quality.jsonl"
        )

        await CodexBackend(auth_mode="visitor").call(
            AgentCallParams(
                prompt="review tb",
                model="gpt-5",
                cwd=tmp_path,
                transcript_path=transcript,
                artifact_paths=resolve_agent_artifacts,
            )
        )

        assert not transcript.with_suffix(".md").exists()
        rendered = (
            tmp_path / "human-logs" / "transcripts" / "reviewer" / "1" / "review-tb-quality.md"
        )
        assert "review complete" in rendered.read_text(encoding="utf-8")


def _params(**overrides) -> AgentCallParams:
    overrides.setdefault("timeout_seconds", 60)
    return AgentCallParams(prompt="task", model="claude-sonnet-4-6", cwd="/tmp", **overrides)


def _fail_first(error: BaseException):
    """A replay step that raises ``error`` on the first attempt only."""
    calls: list[int] = []

    def step(_options):
        calls.append(1)
        if len(calls) == 1:
            raise error

    return step


def _assistant(**fields):
    return sdk_message(
        "AssistantMessage", **{"content": [], "model": "test-model", "usage": None, **fields}
    )


def _rate_limit(status: str, resets_at=None, utilization=None):
    info = SimpleNamespace(
        status=status,
        resets_at=resets_at,
        rate_limit_type="seven_day",
        utilization=utilization,
    )
    return sdk_message("RateLimitEvent", rate_limit_info=info)


def _block(kind: str, **fields):
    return type(kind, (SimpleNamespace,), {})(**fields)


class TestCallAgent:
    @pytest.fixture(autouse=True)
    def _disable_docker_sandbox(self):
        """Disable Docker sandbox so call_agent routes through ClaudeSDKBackend."""
        from booley.config.agent import AgentSettings
        from booley.runtime.agent_backend import ClaudeSDKBackend
        from booley.runtime.agent_config import BackendConfig, set_backend_config

        cfg = BackendConfig(
            settings=AgentSettings(),
            active_backend=ClaudeSDKBackend(auth_mode="visitor"),
        )
        set_backend_config(cfg)
        yield
        set_backend_config(None)

    @pytest.fixture
    def no_backoff(self, monkeypatch):
        """Replace the retry and rate-limit sleep (the clock) and record each wait."""
        sleep = AsyncMock()
        monkeypatch.setattr(anyio, "sleep", sleep)
        return sleep

    @pytest.mark.asyncio
    async def test_successful_call(self, monkeypatch):
        """Happy path: agent returns result message."""
        replay_sdk_stream(
            monkeypatch,
            sdk_result(
                "Fixed the bug",
                structured_output={"fixed": True},
                total_cost_usd=0.05,
                usage={"input_tokens": 1000, "output_tokens": 500},
            ),
        )

        result = await call_agent(_params())

        assert result.output == "Fixed the bug"
        assert result.structured == {"fixed": True}
        assert result.input_tokens == 1000
        assert result.output_tokens == 500

    @pytest.mark.asyncio
    async def test_assistant_message_accumulates_tokens(self, monkeypatch):
        """AssistantMessage usage tokens are accumulated."""
        replay_sdk_stream(
            monkeypatch,
            _assistant(usage={"input_tokens": 500, "output_tokens": 200}),
            _assistant(usage={"input_tokens": 600, "output_tokens": 300}),
            sdk_result("done"),
        )

        result = await call_agent(_params())

        assert result.input_tokens == 1100
        assert result.output_tokens == 500

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "retried"),
        [
            (ConnectionError("ECONNRESET"), True),
            (RuntimeError('API Error: 500 {"type":"error","error":{"type":"api_error"}}'), True),
            (RuntimeError("API Error: 500 internal server error"), True),
            (RuntimeError("[Errno 104] Connection reset by peer"), True),
            (RuntimeError("Remote end closed connection without response"), True),
            (RuntimeError("An existing connection was forcibly closed by the remote host"), True),
            (RuntimeError("Network connection lost"), True),
            (RuntimeError("Failed to establish a new connection: [Errno 111]"), True),
            (RuntimeError("502 Bad Gateway"), True),
            (RuntimeError("503 Service Unavailable"), True),
            (RuntimeError("529 overloaded"), True),
            (RuntimeError("ETIMEDOUT"), True),
            (RuntimeError("stream disconnected before completion"), True),
            (
                Exception(
                    "Command failed with exit code 1 (exit code: 1)\n"
                    "Error output: Check stderr output for details"
                ),
                True,
            ),
            (RuntimeError("Error: authentication failed"), False),
            (ValueError("Invalid JSON in agent output"), False),
            (RuntimeError("Request too large: 1500 bytes"), False),
            (RuntimeError("max_tokens 2500 exceeds the model limit"), False),
            (RuntimeError("request req_015003 failed validation"), False),
            (RuntimeError("Invalid max_tokens: 500.5; expected an integer"), False),
            (RuntimeError("Invalid connection string in config"), False),
        ],
        ids=[
            "connection-reset",
            "api-500",
            "api-500-phrase",
            "connection-reset-by-peer",
            "remote-end-closed",
            "forcibly-closed",
            "connection-lost",
            "new-connection-failed",
            "bad-gateway",
            "service-unavailable",
            "overloaded",
            "timed-out",
            "stream-disconnected",
            "sdk-command-failed",
            "authentication-failed",
            "invalid-json",
            "1500-bytes",
            "max-tokens-2500",
            "request-id-500",
            "decimal-500",
            "connection-string",
        ],
    )
    async def test_retries_only_transient_failures(self, monkeypatch, no_backoff, error, retried):
        attempts = replay_sdk_stream(monkeypatch, _fail_first(error), sdk_result("recovered"))

        if retried:
            assert (await call_agent(_params())).output == "recovered"
            assert len(attempts) == 2
            no_backoff.assert_awaited_once()
        else:
            with pytest.raises(type(error)):
                await call_agent(_params())
            assert len(attempts) == 1
            no_backoff.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured_output", [None, {}])
    async def test_extracts_json_from_text_when_no_structured(
        self, monkeypatch, structured_output
    ):
        """Without structured_output (or an empty one) the JSON comes from the text."""
        replay_sdk_stream(
            monkeypatch,
            sdk_result(
                'Here is the result:\n```json\n{"count": 5}\n```',
                structured_output=structured_output,
            ),
        )

        result = await call_agent(_params(output_format={"type": "object"}))

        assert result.structured == {"count": 5}
        assert result.structured_fallback is True

    @pytest.mark.asyncio
    async def test_successful_call_with_output_format_wrapping(self, monkeypatch):
        """output_format without type=json_schema gets auto-wrapped."""
        seen = replay_sdk_stream(monkeypatch, sdk_result("done", structured_output={"key": "val"}))

        result = await call_agent(_params(output_format={"type": "object", "properties": {}}))

        assert result.structured == {"key": "val"}
        assert seen[0].output_format["type"] == "json_schema"

    @pytest.mark.asyncio
    async def test_writes_transcript(self, monkeypatch, tmp_path: Path):
        """Each assistant turn becomes one transcript line, empty turns included."""
        blocks = [
            _block("TextBlock", text="working"),
            _block("ToolUseBlock", name="Bash", input={"command": "ls"}),
            _block("ThinkingBlock", thinking="hmm"),
        ]
        replay_sdk_stream(
            monkeypatch,
            _assistant(content=blocks, usage={"input_tokens": 1}),
            _assistant(),
            sdk_result(),
        )
        transcript = tmp_path / "transcripts" / "test.jsonl"

        await call_agent(_params(transcript_path=transcript))

        lines = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
        *_, first, empty = lines
        assert "timestamp" in first
        assert first["content"] == [
            {"type": "TextBlock", "text": "working"},
            {"type": "ToolUseBlock", "name": "Bash", "input": {"command": "ls"}},
            {"type": "ThinkingBlock", "thinking": "hmm"},
        ]
        assert first["usage"] == {"input_tokens": 1}
        assert empty["content"] == []
        assert "timestamp" in empty

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("resets_at", "timeout_seconds", "low", "high"),
        [(1200, 300, 260, 260), (None, 120, 118, 119.01)],
        ids=["reset-inside-the-budget", "fallback-backoff-inside-the-budget"],
    )
    async def test_rejected_rate_limit_waits_inside_the_budget_then_stops(
        self, monkeypatch, no_backoff, resets_at, timeout_seconds, low, high
    ):
        monkeypatch.setattr("booley.runtime._claude_backend.wall_clock", lambda: 1000)
        attempts = replay_sdk_stream(monkeypatch, _rate_limit("rejected", resets_at))

        with pytest.raises(UsageLimitError, match="still limited after 5 attempts"):
            await call_agent(_params(timeout_seconds=timeout_seconds))

        waits = [call.args[0] for call in no_backoff.await_args_list]
        assert len(attempts) == 5
        assert len(waits) == 5
        assert all(low <= wait <= high for wait in waits), waits

    @pytest.mark.asyncio
    async def test_allowed_rate_limit_warning_only_logs(self, monkeypatch, no_backoff, caplog):
        attempts = replay_sdk_stream(
            monkeypatch, _rate_limit("allowed_warning", utilization=0.85), sdk_result("ok")
        )

        with caplog.at_level(logging.WARNING):
            assert (await call_agent(_params())).output == "ok"

        no_backoff.assert_not_awaited()
        assert len(attempts) == 1
        assert "85% used" in caplog.text

    # --- Start notice: printed only once the backend confirms its start ---

    @staticmethod
    def _use_codex_backend():
        """Switch the class fixture's Claude backend to a real Codex backend."""
        from booley.config.agent import AgentSettings
        from booley.runtime.agent_backend import CodexBackend
        from booley.runtime.agent_config import BackendConfig, set_backend_config

        set_backend_config(
            BackendConfig(
                settings=AgentSettings(default_cli="codex"), active_backend=CodexBackend()
            )
        )

    @staticmethod
    def _codex_process(*, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        """Build a finished fake Codex child whose pipes replay the given bytes."""
        process = MagicMock()
        process.returncode = returncode
        process.stdin = MagicMock()
        process.stdin.drain = AsyncMock()
        process.stdin.wait_closed = AsyncMock()
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(stdout)
        process.stdout.feed_eof()
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(stderr)
        process.stderr.feed_eof()

        async def finish():
            # Let the stderr drain task read its bytes before the child "exits".
            await asyncio.sleep(0.01)
            return returncode

        process.wait = finish
        return process

    @pytest.mark.asyncio
    async def test_missing_codex_cli_prints_no_start_notice(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        self._use_codex_backend()
        (tmp_path / "bin").mkdir()
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path, label="reviewer")

        with pytest.raises(Exception, match="Codex CLI not found on PATH"):
            await call_agent(params)

        assert "Booley is starting" not in capsys.readouterr().err, (
            "missing CLI must not announce startup"
        )

    @pytest.mark.asyncio
    async def test_claude_failure_before_first_message_prints_no_start_notice(
        self, monkeypatch, capsys
    ):
        # The SDK's CLINotFoundError text; a stand-in keeps this stub-SDK safe.
        replay_sdk_stream(monkeypatch, RuntimeError("Claude Code not found"))

        with pytest.raises(RuntimeError, match="Claude Code not found"):
            await call_agent(_params())

        assert "Booley is starting" not in capsys.readouterr().err, (
            "failed start must not announce startup"
        )

    @pytest.mark.asyncio
    async def test_start_notice_printed_once_after_retried_claude_start(
        self, monkeypatch, no_backoff, capsys
    ):
        attempts = replay_sdk_stream(
            monkeypatch,
            _assistant(usage={"input_tokens": 1, "output_tokens": 1}),
            _fail_first(ConnectionError("ECONNRESET")),
            sdk_result("recovered"),
        )

        result = await call_agent(_params(label="reviewer"))

        assert result.output == "recovered"
        assert len(attempts) == 2
        assert capsys.readouterr().err.count("Booley is starting the reviewer Specialist") == 1

    # The two Codex start-notice tests below keep the process launch (_codex_spawn)
    # patched: on_start fires inside the process runner, which the cross-platform
    # shared fake replaces, and the fake codex script cannot fail only its first
    # launch.

    @pytest.mark.asyncio
    async def test_start_notice_printed_once_after_retried_codex_start(
        self, tmp_path: Path, capsys
    ):
        self._use_codex_backend()
        message = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
        processes = [
            self._codex_process(returncode=1, stderr=b"503 Service Unavailable"),
            self._codex_process(returncode=0, stdout=json.dumps(message).encode() + b"\n"),
        ]
        spawn = AsyncMock(side_effect=[(process, tmp_path) for process in processes])

        with (
            patch("booley.runtime._codex_backend.shutil.which", return_value="/usr/bin/codex"),
            patch("booley.runtime._codex_backend._codex_spawn", spawn),
            patch("booley.runtime._codex_backend.anyio.sleep", new_callable=AsyncMock) as sleep,
        ):
            result = await call_agent(
                AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path, label="reviewer")
            )

        assert result.output == "done"
        assert spawn.await_count == 2
        sleep.assert_awaited_once()
        assert capsys.readouterr().err.count("Booley is starting the reviewer Specialist") == 1

    @pytest.mark.asyncio
    async def test_codex_spawn_failure_prints_no_start_notice(self, tmp_path: Path, capsys):
        from booley.runtime.agent_errors import AgentAuthError

        self._use_codex_backend()
        params = AgentCallParams(prompt="task", model="gpt-5", cwd=tmp_path, label="reviewer")

        with (
            patch("booley.runtime._codex_backend.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "booley.runtime._codex_backend._codex_spawn",
                new_callable=AsyncMock,
                side_effect=AgentAuthError("no credential", provider="codex"),
            ),
            pytest.raises(AgentAuthError),
        ):
            await call_agent(params)

        assert "Booley is starting" not in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_broken_stderr_does_not_abort_started_agent(self, monkeypatch):
        class BrokenPipe:
            def write(self, _text):
                raise OSError(errno.EPIPE, "Broken pipe")

            def flush(self):
                raise OSError(errno.EPIPE, "Broken pipe")

        attempts = replay_sdk_stream(
            monkeypatch,
            _assistant(usage={"input_tokens": 1, "output_tokens": 1}),
            sdk_result("finished"),
        )
        monkeypatch.setattr(sys, "stderr", BrokenPipe())

        outcome = await call_agent(_params())

        assert outcome.output == "finished"
        assert len(attempts) == 1
