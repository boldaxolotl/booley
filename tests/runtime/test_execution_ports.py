"""Runtime execution works with caller-owned paths and notification behavior."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from booley.core.models import AgentArtifactPaths, AgentCallParams
from booley.runtime import _claude_backend as claude
from booley.runtime.agent_errors import TransientAPIError
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


@pytest.mark.parametrize("notification_fails", [False, True])
def test_public_claude_call_retries_with_injected_policy(
    tmp_path, monkeypatch, notification_fails
):
    calls = []
    destinations = []
    notified = Mock(side_effect=OSError("offline") if notification_fails else None)

    async def query(*, prompt, options):
        calls.append(prompt)
        if len(calls) == 1:
            yield _RateLimit(
                SimpleNamespace(
                    status="rejected",
                    resets_at=None,
                    rate_limit_type="seven_day",
                )
            )
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
        notify_rate_limit=notified,
    )
    result = asyncio.run(claude.ClaudeSDKBackend().call(params))
    assert result.output == "done"
    assert calls == ["work", "work"]
    assert [p.name for p in destinations] == ["reviewer.jsonl", "reviewer-retry2.jsonl"]
    notified.assert_called_once_with("seven_day", claude.RATE_LIMIT_FALLBACK_BACKOFF_S, None)
    sleep.assert_awaited_once_with(claude.RATE_LIMIT_FALLBACK_BACKOFF_S)
    for name in ("reviewer", "reviewer-retry2"):
        assert (
            json.loads((tmp_path / "machine" / f"{name}.json").read_text())["user_prompt"]
            == "work"
        )
        assert (tmp_path / "human" / f"{name}.md").exists()


@pytest.mark.parametrize("cancelled", [False, True])
def test_rate_limit_budget_resumes_on_completion_or_cancellation(monkeypatch, cancelled):
    budget = Mock()
    sleep = AsyncMock(side_effect=asyncio.CancelledError if cancelled else None)

    async def wait(awaitable, _budget):
        await awaitable

    monkeypatch.setattr(claude.anyio, "sleep", sleep)
    monkeypatch.setattr(claude, "run_with_developer_budget", wait)
    event = _RateLimit(SimpleNamespace(status="rejected", resets_at=None, rate_limit_type=None))
    with pytest.raises(asyncio.CancelledError if cancelled else TransientAPIError):
        asyncio.run(claude._handle_rate_limit_event(event, budget))
    budget.pause.assert_called_once_with("claude-rate-limit", "provider rate limit")
    budget.resume.assert_called_once_with("claude-rate-limit")


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
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_codex_call_writes_to_injected_destinations(tmp_path, monkeypatch):
    from booley.runtime import _codex_backend as codex

    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    output = "\n".join(json.dumps(event) for event in events)
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
