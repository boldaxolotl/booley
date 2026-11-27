"""Runtime execution works with caller-owned paths and provider retries."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.claude_cli_fake import replay_sdk_attempts, sdk_message, sdk_result, write_claude_cli
from tests.codex_cli_fake import answer_codex_on_host
from tests.subprocess_budget import run_bounded_child

from booley.core.models import AgentArtifactPaths, AgentCallParams
from booley.runtime import _claude_backend as claude
from booley.runtime import _codex_backend as codex
from booley.runtime.agent_errors import UsageLimitError
from booley.runtime.prompt_artifacts import write_prompt_artifacts


def test_prompt_writer_writes_only_the_requested_record(tmp_path):
    paths = AgentArtifactPaths(tmp_path / "prompt.json")
    write_prompt_artifacts(paths, system_prompt=None, user_prompt="work")
    assert json.loads(paths.prompt_json.read_text())["full_prompt"] == "work"
    assert [path.name for path in tmp_path.iterdir()] == ["prompt.json"]


def test_prompt_write_failure_does_not_fail_execution(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("keep")
    write_prompt_artifacts(
        AgentArtifactPaths(blocked / "prompt.json"), system_prompt=None, user_prompt="work"
    )
    assert blocked.read_text() == "keep"


_NOW = 1_800_000_000
_FAR_RESET = 1_800_108_000
FAR_RESET_MESSAGE = (
    "Claude usage limit reached (seven_day); resets at 2027-01-16T14:00:00Z,"
    " too late to wait within this call's timeout"
)


def rejected_seven_day(resets_at):
    info = SimpleNamespace(status="rejected", resets_at=resets_at, rate_limit_type="seven_day")
    return sdk_message("RateLimitEvent", rate_limit_info=info)


@pytest.fixture
def clock(monkeypatch):
    """Pin the wall clock and replace the retry sleep; return the sleep mock."""
    monkeypatch.setattr(claude, "wall_clock", lambda: _NOW)
    sleep = AsyncMock()
    monkeypatch.setattr(claude.anyio, "sleep", sleep)
    return sleep


def test_public_claude_call_retries_with_injected_policy(tmp_path, monkeypatch, clock):
    replay = replay_sdk_attempts(monkeypatch, [rejected_seven_day(None)], [sdk_result("done")])
    destinations = []

    def resolve(transcript):
        destinations.append(transcript)
        stem = transcript.stem
        return AgentArtifactPaths(
            tmp_path / "machine" / f"{stem}.json",
            tmp_path / "human" / f"{stem}.prompt.md",
            tmp_path / "human" / f"{stem}.md",
        )

    params = AgentCallParams(
        prompt="work",
        model="test-model",
        cwd=tmp_path,
        transcript_path=tmp_path / "raw" / "agent.jsonl",
        label="reviewer",
        artifact_paths=resolve,
    )
    result = asyncio.run(claude.ClaudeSDKBackend(auth_mode="visitor").call(params))
    assert result.output == "done"
    assert replay.prompts == ["work", "work"]
    assert [p.name for p in destinations] == ["reviewer.jsonl", "reviewer-retry2.jsonl"]
    clock.assert_awaited_once_with(claude.RATE_LIMIT_FALLBACK_BACKOFF_S)
    for name in ("reviewer", "reviewer-retry2"):
        record = json.loads((tmp_path / "machine" / f"{name}.json").read_text())
        assert record["user_prompt"] == "work"
        assert (tmp_path / "human" / f"{name}.md").exists()


def _limit_call(tmp_path):
    params = AgentCallParams(
        prompt="work", model="test-model", cwd=tmp_path, timeout_seconds=300, label="reviewer"
    )
    return claude.ClaudeSDKBackend(auth_mode="visitor").call(params)


def test_claude_usage_limit_beyond_the_call_timeout_fails_without_sleeping(
    tmp_path, monkeypatch, caplog, clock
):
    replay = replay_sdk_attempts(monkeypatch, [rejected_seven_day(_FAR_RESET)])
    caplog.set_level("DEBUG")

    with pytest.raises(UsageLimitError) as raised:
        asyncio.run(_limit_call(tmp_path))

    assert str(raised.value) == FAR_RESET_MESSAGE
    assert replay.prompts == ["work"]
    clock.assert_not_awaited()
    assert replay.closed == replay.opened
    assert not [r for r in caplog.records if r.levelno >= 30]
    assert any("Crash context: 0 turns completed" in note for note in raised.value.__notes__)


def test_claude_rejections_on_every_attempt_end_as_usage_limit(
    tmp_path, monkeypatch, caplog, clock
):
    replay = replay_sdk_attempts(monkeypatch, [rejected_seven_day(_NOW + 5)])

    with pytest.raises(UsageLimitError) as raised:
        asyncio.run(_limit_call(tmp_path))

    assert str(raised.value) == (
        "Claude usage limit reached (seven_day); resets at 2027-01-15T08:00:05Z;"
        " still limited after 5 attempts"
    )
    assert len(replay.prompts) == 5
    assert replay.closed == replay.opened
    noisy = ("Crash context", "SDK CLI stderr")
    assert not [
        r
        for r in caplog.records
        if r.levelno >= 30 and (r.name == "asyncio" or r.getMessage().startswith(noisy))
    ]
    assert any("Crash context" in note for note in raised.value.__cause__.__notes__)


_SDK_STOP_SCRIPT = """
import asyncio, json, logging, sys
from pathlib import Path
import claude_agent_sdk
from booley.core.models import AgentCallParams
from booley.runtime import _claude_backend as claude
from booley.runtime.agent_errors import UsageLimitError

records = []
class Handler(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            records.append(record.name + ": " + record.getMessage())
logging.getLogger().addHandler(Handler())

def sdk_query(*, prompt, options):
    options.cli_path = sys.argv[1]
    return claude_agent_sdk.query(prompt=prompt, options=options)

claude.query = sdk_query
claude.wall_clock = lambda: int(sys.argv[2])
params = AgentCallParams(
    prompt="work", model="m", cwd=Path(sys.argv[3]), timeout_seconds=300, label="reviewer"
)
try:
    asyncio.run(claude.ClaudeSDKBackend(auth_mode="visitor").call(params))
except UsageLimitError as error:
    print(json.dumps({"message": str(error), "notes": error.__notes__, "records": records}))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a POSIX script")
def test_claude_usage_limit_stop_through_the_sdk_is_quiet(tmp_path):
    event = {
        "type": "rate_limit_event",
        "uuid": "u1",
        "session_id": "s1",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": _FAR_RESET,
            "rateLimitType": "seven_day",
        },
    }
    cli = write_claude_cli(tmp_path, [event])
    source = Path(__file__).resolve().parents[2] / "src"

    # A fresh interpreter: harness conftests stub the SDK module in the test process.
    result = run_bounded_child(
        [sys.executable, "-c", _SDK_STOP_SCRIPT, str(cli), str(_NOW), str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(source)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["message"] == FAR_RESET_MESSAGE
    assert outcome["records"] == []
    assert any("Crash context" in note for note in outcome["notes"])
    assert "asynchronous generator" not in result.stderr


def test_claude_limit_event_after_the_result_keeps_the_result(tmp_path, monkeypatch):
    replay_sdk_attempts(monkeypatch, [sdk_result("done"), rejected_seven_day(_FAR_RESET)])

    assert asyncio.run(_limit_call(tmp_path)).output == "done"


def test_public_codex_call_writes_to_injected_destinations(tmp_path, monkeypatch):
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "review",
                "aggregated_output": "checked",
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    codex_cli = answer_codex_on_host(tmp_path / "bin", monkeypatch, events)
    paths = AgentArtifactPaths(
        tmp_path / "machine.json", tmp_path / "prompt.md", tmp_path / "transcript.md"
    )
    resolve = Mock(return_value=paths)
    params = AgentCallParams(
        prompt="work",
        model="test-model",
        cwd=tmp_path,
        system_prompt="rules",
        label="reviewer",
        transcript_path=tmp_path / "raw" / "agent.jsonl",
        artifact_paths=resolve,
    )
    result = asyncio.run(codex.CodexBackend(auth_mode="visitor").call(params))
    assert result.output == "done"
    resolve.assert_called_once_with(tmp_path / "raw" / "reviewer.jsonl")
    assert json.loads(paths.prompt_json.read_text())["full_prompt"] == "rules\n\n---\n\nwork"
    rendered = paths.transcript_markdown.read_text()
    assert "done" in rendered and "checked" in rendered
    assert not (tmp_path / "raw" / "reviewer.md").exists()
    assert paths.prompt_markdown.exists()
    assert [call.input for call in codex_cli.calls()] == ["rules\n\n---\n\nwork"]
