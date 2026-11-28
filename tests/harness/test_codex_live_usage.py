"""Live Codex rollout usage accounting."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from booley.runtime._codex_live_usage import CodexLiveUsage
from tests.time_budget import hang_guard_s


def _token_record(
    total_input: int, cached: int, output: int, context: int, limit: int = 258_400
) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total_input,
                        "cached_input_tokens": cached,
                        "output_tokens": output,
                    },
                    "last_token_usage": {"input_tokens": context},
                    "model_context_window": limit,
                },
            },
        }
    )


async def _tail(tmp_path, *lines: str):
    """Tail a rollout file holding ``lines``; return the usage and its events."""
    events: list[dict] = []
    usage = CodexLiveUsage("gpt-5.6-terra", events.append, tmp_path)
    thread_id = "019fdce3-acaa-7ee0-aef2-7433643145c0"
    rollout = tmp_path / "2026" / "08" / "07" / f"rollout-now-{thread_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_bytes("".join(f"{line}\n" for line in lines).encode("utf-8"))

    usage.start(thread_id)
    deadline = time.monotonic() + hang_guard_s()
    while not events and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await usage.close()
    return usage, events


@pytest.mark.asyncio
async def test_rollout_snapshots_become_deltas_and_absolute_context(tmp_path):
    _, events = await _tail(
        tmp_path,
        _token_record(100_000, 80_000, 2_000, 90_000),
        _token_record(240_000, 200_000, 3_500, 142_000),
    )

    assert [event["output_tokens"] for event in events] == [2_000, 1_500]
    assert events[0]["cost_usd"] > 0
    assert events[1]["context_tokens"] == 142_000
    assert events[1]["context_limit"] == 258_400


@pytest.mark.asyncio
async def test_regressive_or_malformed_snapshots_are_ignored_and_completion_adds_only_unseen(
    tmp_path,
):
    usage, events = await _tail(
        tmp_path, _token_record(100, 80, 20, 90), _token_record(99, 80, 20, 90), "not json"
    )
    assert len(events) == 1

    usage.completed({"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20})
    assert len(events) == 1

    usage.completed({"input_tokens": 150, "cached_input_tokens": 100, "output_tokens": 30})
    assert events[-1]["output_tokens"] == 10
    assert "context_tokens" not in events[-1]
