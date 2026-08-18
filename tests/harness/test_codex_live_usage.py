"""Live Codex rollout usage accounting."""

from __future__ import annotations

import asyncio
import json

import pytest

from booley.harness._codex_live_usage import CodexLiveUsage


def _token_record(
    total_input: int,
    cached: int,
    output: int,
    context: int,
    limit: int = 258_400,
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


def test_rollout_snapshots_become_deltas_and_absolute_context(tmp_path):
    events: list[dict] = []
    usage = CodexLiveUsage("gpt-5.6-terra", events.append, tmp_path)

    usage._consume_rollout_line(_token_record(100_000, 80_000, 2_000, 90_000))
    usage._consume_rollout_line(_token_record(240_000, 200_000, 3_500, 142_000))

    assert [event["output_tokens"] for event in events] == [2_000, 1_500]
    assert events[0]["cost_usd"] > 0
    assert events[1]["context_tokens"] == 142_000
    assert events[1]["context_limit"] == 258_400


def test_completed_event_only_emits_unseen_usage(tmp_path):
    events: list[dict] = []
    usage = CodexLiveUsage("gpt-5.6-terra", events.append, tmp_path)
    usage._consume_rollout_line(_token_record(100, 80, 20, 90))

    usage.completed({"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20})
    assert len(events) == 1

    usage.completed({"input_tokens": 150, "cached_input_tokens": 100, "output_tokens": 30})
    assert events[-1]["output_tokens"] == 10
    assert "context_tokens" not in events[-1]


def test_regressive_or_malformed_snapshots_are_ignored(tmp_path):
    events: list[dict] = []
    usage = CodexLiveUsage("gpt-5.6-terra", events.append, tmp_path)
    usage._consume_rollout_line(_token_record(100, 80, 20, 90))
    usage._consume_rollout_line(_token_record(99, 80, 20, 90))
    usage._consume_rollout_line("not json")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_watcher_finds_thread_rollout_and_drains_it(tmp_path):
    events: list[dict] = []
    usage = CodexLiveUsage("gpt-5.6-terra", events.append, tmp_path)
    thread_id = "019fdce3-acaa-7ee0-aef2-7433643145c0"
    rollout = tmp_path / "2026" / "08" / "07" / f"rollout-now-{thread_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(_token_record(100, 80, 20, 90) + "\n", encoding="utf-8")

    usage.start(thread_id)
    for _ in range(20):
        if events:
            break
        await asyncio.sleep(0.02)
    await usage.close()

    assert events[0]["output_tokens"] == 20
