"""Runtime execution works with caller-owned paths and provider retries."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.subprocess_budget import run_bounded_child

from booley.core.models import AgentArtifactPaths, AgentCallParams
from booley.runtime import _claude_backend as claude
from booley.runtime.agent_errors import UsageLimitError
from booley.runtime.prompt_artifacts import write_prompt_artifacts


def test_prompt_writer_uses_arbitrary_destinations(tmp_path):
    paths = AgentArtifactPaths(tmp_path / "machine" / "input.json", tmp_path / "readable.txt")
    write_prompt_artifacts(paths, system_prompt="rules", user_prompt="work")
    record = json.loads(paths.prompt_json.read_text())
    assert record["full_prompt"] == "rules\n\n---\n\nwork"
    assert record["user_chars"] == 4
    assert "work" in paths.prompt_markdown.read_text()


def test_prompt_write_failure_does_not_fail_execution(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("keep")
    write_prompt_artifacts(
        AgentArtifactPaths(blocked / "prompt.json"), system_prompt=None, user_prompt="work"
    )
    assert blocked.read_text() == "keep"


@dataclass
class _RateLimit:
    rate_limit_info: SimpleNamespace


def _successful_result():
    return claude.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session",
        total_cost_usd=0,
        usage={},
        result="done",
        structured_output=None,
    )


_NOW = 1_800_000_000
_FAR_RESET = 1_800_108_000  # 30 hours after _NOW
FAR_RESET_MESSAGE = (
    "Claude usage limit reached (seven_day); resets at 2027-01-16T14:00:00Z,"
    " too late to wait within this call's timeout"
)


def rejected_seven_day(resets_at):
    return _RateLimit(
        SimpleNamespace(status="rejected", resets_at=resets_at, rate_limit_type="seven_day")
    )


def test_public_claude_call_retries_with_injected_policy(tmp_path, monkeypatch):
    calls = []
    destinations = []

    async def query(*, prompt, options):
        calls.append(prompt)
        if len(calls) == 1:
            yield rejected_seven_day(None)
        else:
            yield _successful_result()

    def resolve(transcript):
        destinations.append(transcript)
        stem = transcript.stem
        return AgentArtifactPaths(
            tmp_path / "machine" / f"{stem}.json",
            tmp_path / "human" / f"{stem}.prompt.md",
            tmp_path / "human" / f"{stem}.md",
        )

    monkeypatch.setattr(claude, "RateLimitEvent", _RateLimit)
    monkeypatch.setattr(claude, "query", query)
    sleep = AsyncMock()
    monkeypatch.setattr(claude.anyio, "sleep", sleep)
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
    assert calls == ["work", "work"]
    assert [p.name for p in destinations] == ["reviewer.jsonl", "reviewer-retry2.jsonl"]
    sleep.assert_awaited_once_with(claude.RATE_LIMIT_FALLBACK_BACKOFF_S)
    for name in ("reviewer", "reviewer-retry2"):
        assert (
            json.loads((tmp_path / "machine" / f"{name}.json").read_text())["user_prompt"]
            == "work"
        )
        assert (tmp_path / "human" / f"{name}.md").exists()


def fake_limit_query(monkeypatch, resets_at):
    """Make every Claude query yield one rejected limit event; return (queries, sleep mock)."""
    queries = []

    async def query(*, prompt, options):
        queries.append(prompt)
        yield rejected_seven_day(resets_at)

    monkeypatch.setattr(claude, "RateLimitEvent", _RateLimit)
    monkeypatch.setattr(claude, "query", query)
    monkeypatch.setattr(claude, "wall_clock", lambda: _NOW)
    sleep = AsyncMock()
    monkeypatch.setattr(claude.anyio, "sleep", sleep)
    return queries, sleep


def _limit_params(tmp_path):
    return AgentCallParams(
        prompt="work", model="test-model", cwd=tmp_path, timeout_seconds=300, label="reviewer"
    )


def test_claude_usage_limit_beyond_the_call_timeout_fails_without_sleeping(tmp_path, monkeypatch):
    queries, sleep = fake_limit_query(monkeypatch, _FAR_RESET)
    backend = claude.ClaudeSDKBackend(auth_mode="visitor")

    with pytest.raises(UsageLimitError) as raised:
        asyncio.run(backend.call(_limit_params(tmp_path)))

    assert str(raised.value) == FAR_RESET_MESSAGE
    assert queries == ["work"]
    sleep.assert_not_awaited()


def test_claude_rejections_on_every_attempt_end_as_usage_limit(tmp_path, monkeypatch):
    queries, _sleep = fake_limit_query(monkeypatch, _NOW + 5)

    with pytest.raises(UsageLimitError) as raised:
        asyncio.run(claude.ClaudeSDKBackend(auth_mode="visitor").call(_limit_params(tmp_path)))

    assert str(raised.value) == (
        "Claude usage limit reached (seven_day); resets at 2027-01-15T08:00:05Z;"
        " still limited after 5 attempts"
    )
    assert len(queries) == 5


def test_claude_limit_event_after_the_result_keeps_the_result(tmp_path, monkeypatch):
    async def query(*, prompt, options):
        yield _successful_result()
        yield rejected_seven_day(_FAR_RESET)

    monkeypatch.setattr(claude, "RateLimitEvent", _RateLimit)
    monkeypatch.setattr(claude, "query", query)
    monkeypatch.setattr(claude, "wall_clock", lambda: _NOW)

    result = asyncio.run(
        claude.ClaudeSDKBackend(auth_mode="visitor").call(_limit_params(tmp_path))
    )

    assert result.output == "done"


def test_runtime_import_and_artifact_use_never_load_ticket_board(tmp_path):
    script = textwrap.dedent("""
        import importlib.abc
        import sys
        from pathlib import Path
        class RejectBoard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "booley.ticket_board" or fullname.startswith("booley.ticket_board."):
                    raise AssertionError("Unexpected dependency: " + fullname)
        sys.meta_path.insert(0, RejectBoard())
        from booley.core.models import AgentArtifactPaths
        from booley.runtime import job_records, prompt_artifacts, agent_backend
        root = Path(sys.argv[1])
        rec = job_records.JobRecord("r", "sim", "t", 1)
        job_records.write_record(rec, root)
        assert job_records.read_record("r", root) == rec
        prompt_artifacts.write_prompt_artifacts(
            AgentArtifactPaths(root / "prompt.json"), system_prompt=None, user_prompt="work"
        )
        assert (root / "prompt.json").exists()
        assert not any(name.startswith("booley.ticket_board") for name in sys.modules)
    """)
    source = Path(__file__).resolve().parents[2] / "src"
    result = run_bounded_child(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(source)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_codex_call_writes_to_injected_destinations(tmp_path, monkeypatch):
    from booley.runtime import _codex_backend as codex

    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    output = "\n".join(json.dumps(event) for event in events)
    monkeypatch.setattr(codex.shutil, "which", Mock(return_value=str(tmp_path / "codex")))
    monkeypatch.setattr(codex, "_codex_run_subprocess", AsyncMock(return_value=(output, "", 0)))
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
    result = asyncio.run(codex.CodexBackend().call(params))
    assert result.output == "done"
    resolve.assert_called_once_with(tmp_path / "raw" / "reviewer.jsonl")
    assert json.loads(paths.prompt_json.read_text())["full_prompt"] == "rules\n\n---\n\nwork"
    assert "done" in paths.transcript_markdown.read_text()
    assert paths.prompt_markdown.exists()
