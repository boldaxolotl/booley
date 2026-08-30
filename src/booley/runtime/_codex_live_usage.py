"""Live Codex usage bridge for the Console status bar.

``codex exec --json`` reports usage only when the whole turn completes.  Codex
also persists the richer event stream used by its TUI, including cumulative
``token_count`` snapshots after each model response.  This module tails that
stream when available and degrades to the public ``turn.completed`` event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

from booley.core.boundary import as_dict, as_int

from ._cost import estimate_cost

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.1


class CodexLiveUsage:
    """Convert cumulative Codex snapshots into Console usage deltas."""

    def __init__(
        self,
        model: str,
        on_event: Callable[[dict], None] | None,
        session_root: Path | None,
    ) -> None:
        self._model = model
        self._on_event = on_event
        self._session_root = session_root
        self._totals = (0, 0, 0)
        self._context = (0, None)
        self._stop = asyncio.Event()
        self._watch_task: asyncio.Task | None = None

    def start(self, thread_id: str) -> None:
        """Start tailing the rollout belonging to *thread_id*."""
        if not self._on_event or not self._session_root or self._watch_task:
            return
        self._watch_task = asyncio.create_task(self._watch_rollout(thread_id))

    def completed(self, usage: object) -> None:
        """Consume the public end-of-turn usage as a fallback/final correction."""
        values = as_dict(usage, default={}) or {}
        self._emit_snapshot(
            as_int(values.get("input_tokens"), 0) or 0,
            as_int(values.get("cached_input_tokens"), 0) or 0,
            as_int(values.get("output_tokens"), 0) or 0,
        )

    async def close(self) -> None:
        """Stop the rollout tail after draining its final complete records."""
        self._stop.set()
        if self._watch_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task

    def _emit_snapshot(
        self,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        *,
        context_tokens: int | None = None,
        context_limit: int | None = None,
    ) -> None:
        old_in, old_cached, old_out = self._totals
        if input_tokens < old_in or cached_tokens < old_cached or output_tokens < old_out:
            logger.debug("Ignoring regressive Codex usage snapshot")
            return
        delta = (input_tokens - old_in, cached_tokens - old_cached, output_tokens - old_out)
        context = (
            (context_tokens or 0, context_limit) if context_tokens is not None else self._context
        )
        if not any(delta) and context == self._context:
            return
        self._totals = (input_tokens, cached_tokens, output_tokens)
        self._context = context
        event = {
            "type": "usage",
            "output_tokens": delta[2],
            "cost_usd": estimate_cost(self._model, delta[0], delta[1], delta[2]),
        }
        if context_tokens is not None:
            event["context_tokens"] = context_tokens
        if context_limit is not None:
            event["context_limit"] = context_limit
        try:
            assert self._on_event is not None
            self._on_event(event)
        except Exception:  # display failure must not abort a paid agent turn
            logger.debug("Codex live usage callback failed", exc_info=True)

    async def _watch_rollout(self, thread_id: str) -> None:
        path = await self._find_rollout(thread_id)
        if path is None:
            return
        position = 0
        while not self._stop.is_set():
            position = self._read_rollout(path, position)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=_POLL_INTERVAL_S)
        self._read_rollout(path, position)

    async def _find_rollout(self, thread_id: str) -> Path | None:
        assert self._session_root is not None
        pattern = f"rollout-*{thread_id}.jsonl"
        while not self._stop.is_set():
            matches = list(self._session_root.glob(f"*/*/*/{pattern}"))
            if matches:
                return max(matches, key=lambda path: path.stat().st_mtime_ns)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=_POLL_INTERVAL_S)
        return None

    def _read_rollout(self, path: Path, position: int) -> int:
        try:
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(position)
                while line := stream.readline():
                    if not line.endswith("\n"):
                        break
                    position = stream.tell()
                    self._consume_rollout_line(line)
        except OSError:
            logger.debug("Codex rollout read failed: %s", path, exc_info=True)
        return position

    def _consume_rollout_line(self, line: str) -> None:
        try:
            record = as_dict(json.loads(line), default={}) or {}
        except json.JSONDecodeError:
            return
        payload = as_dict(record.get("payload"), default={}) or {}
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            return
        info = as_dict(payload.get("info"), default={}) or {}
        total = as_dict(info.get("total_token_usage"), default={}) or {}
        last = as_dict(info.get("last_token_usage"), default={}) or {}
        self._emit_snapshot(
            as_int(total.get("input_tokens"), 0) or 0,
            as_int(total.get("cached_input_tokens"), 0) or 0,
            as_int(total.get("output_tokens"), 0) or 0,
            context_tokens=as_int(last.get("input_tokens"), 0) or 0,
            context_limit=as_int(info.get("model_context_window")),
        )
