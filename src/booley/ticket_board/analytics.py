"""Transcript parsing, token attribution, cost computation, and step duration analysis."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.core.boundary import as_float, as_int
from booley.pricing import MODEL_PRICING as _MODEL_PRICING
from booley.pricing import resolve as _resolve_pricing

from .helpers import parse_arrow, parse_iso
from .paths import STEP_DIR_MAP

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

# Rates live in booley.pricing, shared with the live agent backends. The dict
# shape below is kept because it is this module's published surface (re-exported
# from booley.ticket_board), but it is now a view onto the one table.
PRICING = {
    name: {
        "input": p.input,
        "output": p.output,
        "cache_read": p.cache_read,
        "cache_create": p.cache_write,
    }
    for name, p in _MODEL_PRICING.items()
}
# Fallback for a model we can't price at all -- Sonnet is a reasonable middle
# ground, and reporting *some* cost beats reporting a run as free.
DEFAULT_PRICING = PRICING["claude-sonnet-5"]


def _match_pricing(model_id):
    """Match a model ID string to pricing. Handles partial and family matches."""
    prices = _resolve_pricing(model_id)
    if prices is None:
        return DEFAULT_PRICING
    return {
        "input": prices.input,
        "output": prices.output,
        "cache_read": prices.cache_read,
        "cache_create": prices.cache_write,
    }


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def parse_transcript_usage(jsonl_path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSONL transcript and extract per-message token usage.

    Returns list of dicts: {timestamp, model, input_tokens, output_tokens,
    cache_read_tokens, cache_create_tokens}.
    """
    messages = []
    path = Path(jsonl_path)
    if not path.exists():
        return messages

    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type == "assistant":
                msg = entry.get("message", {})
                usage = msg.get("usage", {})
                model = msg.get("model", "unknown")
                timestamp = entry.get("timestamp", "")
            elif entry_type == "turn.completed" or "usage" in entry:
                usage = entry.get("usage", {})
                model = entry.get("model", "unknown")
                timestamp = entry.get("timestamp", "")
            else:
                continue
            if not usage:
                continue

            cache_read = usage.get("cache_read_input_tokens", 0) or usage.get(
                "cached_input_tokens", 0
            )
            cache_create = usage.get("cache_creation_input_tokens", 0)
            input_tokens = usage.get("input_tokens", 0)
            # Claude's raw assistant usage separates uncached input from both
            # cache categories. Codex turn.completed input already includes
            # cached input. Normalize both to the AgentResult convention where
            # input_tokens is the complete input total.
            if entry_type != "turn.completed":
                input_tokens += cache_read + cache_create

            messages.append(
                {
                    "timestamp": timestamp,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": cache_read,
                    "cache_create_tokens": cache_create,
                }
            )
    return messages


def parse_usage_log(jsonl_path: str | Path) -> list[dict[str, Any]]:
    """Parse Booley per-agent usage.jsonl entries.

    Current runs write one line per `invoke_stage_agent()` call under
    each step directory. These logs are the preferred source for aggregate
    metrics because they include step, label, model, and cost directly.
    """
    entries = []
    path = Path(jsonl_path)
    if not path.exists():
        return entries

    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Boundary: a JSONL line may decode to any type; we need an object.
            if not isinstance(entry, dict):
                continue
            stage = entry.get("stage")
            if not stage:
                continue
            entries.append(
                {
                    "timestamp": entry.get("timestamp", ""),
                    "stage": stage,
                    "label": entry.get("label", ""),
                    "model": entry.get("model", "unknown"),
                    "input_tokens": as_int(entry.get("input_tokens"), 0),
                    "output_tokens": as_int(entry.get("output_tokens"), 0),
                    "cache_read_tokens": as_int(entry.get("cached_tokens"), 0),
                    "cache_create_tokens": as_int(entry.get("cache_create_tokens"), 0),
                    "cost_usd": as_float(entry.get("cost_usd"), 0.0),
                }
            )
    return entries


def collect_step_usage(logs_dir: str | Path, slug: str) -> list[dict[str, Any]]:
    """Collect all current per-step usage.jsonl entries for a ticket."""
    ticket_dir = Path(logs_dir) / slug / "stages"
    entries: list[dict[str, Any]] = []
    for dir_name in STEP_DIR_MAP.values():
        usage_path = ticket_dir / dir_name / "usage.jsonl"
        entries.extend(parse_usage_log(usage_path))
    entries.sort(key=lambda e: e.get("timestamp", ""))
    return entries


def collect_step_transcript_usage(logs_dir: str | Path, slug: str) -> dict[str, dict[str, Any]]:
    """Aggregate legacy step-local transcript token usage by directory.

    This is the compatibility path for runs created before usage.jsonl and
    .runtime/transcripts existed.
    Per-step transcript paths are already authoritative, so using timestamps
    from transitions.log can misattribute tokens to transient states.
    """
    reverse_map = {dir_name: stage for stage, dir_name in STEP_DIR_MAP.items()}
    stages_dir = Path(logs_dir) / slug / "stages"
    result: dict[str, dict[str, Any]] = {}
    for transcript in sorted(stages_dir.glob("*/transcripts/*.jsonl")):
        stage = reverse_map.get(transcript.parent.parent.name)
        if not stage:
            continue
        if stage not in result:
            result[stage] = _empty_step_totals()
        for msg in collect_all_messages(transcript):
            _accumulate(result[stage], msg)
    return result


def parse_transitions_log(log_path: str | Path) -> list[dict[str, str]]:
    """Parse transitions.log into a list of {timestamp, from_state, to_state, stage}.

    Returns list sorted by timestamp. 'stage' is extracted from to_state
    (e.g. "running:planning" -> "planning").
    """
    transitions = []
    path = Path(log_path)
    if not path.exists():
        return transitions

    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            # Format: <timestamp> | <from> -> <to> | <actor> | <detail>
            parts = line.split(" | ")
            if len(parts) < 3:
                continue
            timestamp = parts[0].strip()
            # parts[1] contains "from -> to" (or "from → to")
            transition = parts[1].strip()
            arrow = parse_arrow(transition)
            if arrow is None:
                continue
            to_state = arrow[1]
            # Extract stage from "status:stage"
            stage = to_state.split(":")[-1] if ":" in to_state else to_state
            transitions.append(
                {
                    "timestamp": timestamp,
                    "to_state": to_state,
                    "stage": stage,
                }
            )
    # Sort by timestamp to guarantee ordering for downstream consumers
    transitions.sort(key=lambda x: x["timestamp"])
    return transitions


# ---------------------------------------------------------------------------
# Token attribution
# ---------------------------------------------------------------------------


def attribute_tokens_to_steps(
    messages: list[dict[str, Any]], transitions: list[dict[str, str]]
) -> dict[str, dict[str, Any]]:
    """Attribute token messages to steps using transition timestamps.

    Each message is assigned to the step that was active at its timestamp.
    Returns dict: {step_name: {input_tokens, output_tokens, cache_read_tokens,
    cache_create_tokens, message_count, messages: [msg...]}}.
    The 'messages' list preserves per-message model info for accurate costing.
    """
    if not transitions:
        # No transitions -- put everything under "unknown"
        totals = _empty_step_totals()
        for msg in messages:
            _accumulate(totals, msg)
        return {"unknown": totals} if messages else {}

    # Build sorted list of (timestamp, stage) for bisect lookup
    step_intervals = []
    for t in transitions:
        step_intervals.append((t["timestamp"], t["stage"]))
    step_intervals.sort(key=lambda x: x[0])

    result = {}
    for msg in messages:
        ts = msg["timestamp"]
        # Find which step was active at this timestamp
        stage = _find_active_step(ts, step_intervals)
        if stage not in result:
            result[stage] = _empty_step_totals()
        _accumulate(result[stage], msg)

    return result


def usage_entries_to_steps(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate per-agent usage entries by step."""
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        stage = entry.get("stage")
        if not stage:
            continue
        if stage not in result:
            result[stage] = _empty_step_totals()
        totals = result[stage]
        msg = {
            "timestamp": entry.get("timestamp", ""),
            "model": entry.get("model", "unknown"),
            "input_tokens": entry.get("input_tokens", 0),
            "output_tokens": entry.get("output_tokens", 0),
            "cache_read_tokens": entry.get("cache_read_tokens", 0),
            "cache_create_tokens": entry.get("cache_create_tokens", 0),
            "cost_usd": entry.get("cost_usd", 0.0),
        }
        _accumulate(totals, msg)
        totals["cost_usd"] += msg["cost_usd"]
    return result


def _find_active_step(timestamp, step_intervals):
    """Find which step was active at a given timestamp using intervals."""
    active_step = step_intervals[0][1]  # default to first stage
    for ts, stage in step_intervals:
        if ts <= timestamp:
            active_step = stage
        else:
            break
    return active_step


def _empty_step_totals():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "cost_usd": 0.0,
        "message_count": 0,
        "messages": [],  # individual messages for per-model costing
    }


def _accumulate(totals, msg):
    totals["input_tokens"] += msg["input_tokens"]
    totals["output_tokens"] += msg["output_tokens"]
    totals["cache_read_tokens"] += msg["cache_read_tokens"]
    totals["cache_create_tokens"] += msg["cache_create_tokens"]
    totals["message_count"] += 1
    totals["messages"].append(msg)


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------


def compute_step_cost(step_totals: dict[str, Any]) -> float:
    """Compute cost for a step using per-model pricing from individual messages."""
    if step_totals.get("cost_usd"):
        return float(step_totals["cost_usd"])
    return compute_cost_detailed(step_totals["messages"])


def compute_cost_detailed(messages: list[dict[str, Any]]) -> float:
    """Compute cost from individual messages using per-model pricing.

    Returns total cost in USD.
    """
    cost = 0.0
    for msg in messages:
        # _match_pricing never returns None -- an unpriceable model falls back
        # to DEFAULT_PRICING rather than being reported as free.
        prices = _match_pricing(msg["model"])
        # input_tokens from the API *includes* cache_read and cache_create
        # tokens.  Subtract them so they're only billed at their own rates.
        cache_read = msg["cache_read_tokens"]
        cache_create = msg["cache_create_tokens"]
        net_input = msg["input_tokens"] - cache_read - cache_create
        cost += max(net_input, 0) * prices["input"] / 1_000_000
        cost += msg["output_tokens"] * prices["output"] / 1_000_000
        cost += cache_read * prices["cache_read"] / 1_000_000
        cost += cache_create * prices["cache_create"] / 1_000_000
    return cost


# ---------------------------------------------------------------------------
# Step duration computation
# ---------------------------------------------------------------------------


def compute_step_durations(
    transitions: list[dict[str, str]], end_time: datetime | None = None
) -> dict[str, float]:
    """Compute execution duration for each step from transitions.log.

    Only counts time in ``running:*`` states — idle time in ``blocked:``,
    ``queued:``, or ``review:`` states is excluded so the report reflects
    actual run execution, not wall-clock wait.

    Args:
        transitions: list of transition dicts from parse_transitions_log
        end_time: optional datetime for the last step's end (e.g. ticket's
            last_update). Defaults to now() for running tickets.

    Returns dict: {step_name: duration_seconds}.
    """
    if not transitions:
        return {}

    durations: dict[str, float] = {}
    for i, t in enumerate(transitions):
        to_state = t.get("to_state", "")
        if not to_state.startswith("running:"):
            continue

        stage = t["stage"]
        try:
            start = parse_iso(t["timestamp"])
        except (ValueError, TypeError):
            continue

        if i + 1 < len(transitions):
            try:
                end = parse_iso(transitions[i + 1]["timestamp"])
            except (ValueError, TypeError):
                continue
        else:
            end = end_time or datetime.now(UTC)

        secs = (end - start).total_seconds()
        durations[stage] = durations.get(stage, 0) + secs

    return durations


def collect_all_messages(transcript_path: str | Path) -> list[dict[str, Any]]:
    """Collect messages from main transcript + all subagent transcripts.

    Searches two locations for subagent transcripts:
    1. <session-id>/subagents/ (original session layout)
    2. <transcript-parent>/subagents/ (copied transcript layout)
    """
    transcript_path = Path(transcript_path)
    all_messages = parse_transcript_usage(transcript_path)

    seen_dirs = set()

    # Pattern 1: <session-id>.jsonl -> <session-id>/subagents/agent-*.jsonl
    session_dir = transcript_path.parent / transcript_path.stem
    subagent_dir = session_dir / "subagents"
    if subagent_dir.exists():
        seen_dirs.add(subagent_dir.resolve())
        for sa_file in sorted(subagent_dir.glob("agent-*.jsonl")):
            sa_messages = parse_transcript_usage(sa_file)
            all_messages.extend(sa_messages)

    # Pattern 2: sibling subagents/ dir (for copied transcripts in logs/)
    sibling_dir = transcript_path.parent / "subagents"
    if sibling_dir.exists() and sibling_dir.resolve() not in seen_dirs:
        for sa_file in sorted(sibling_dir.glob("agent-*.jsonl")):
            sa_messages = parse_transcript_usage(sa_file)
            all_messages.extend(sa_messages)

    # Sort all by timestamp
    all_messages.sort(key=lambda m: m["timestamp"])
    return all_messages
