"""Persist exact agent prompts as standalone artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from booley.ticket_board.paths import HUMAN_LOGS_DIR, RUNTIME_DIR, ticket_runtime_dir
from booley.timefmt import compact_utc_now, utc_now_rfc3339

logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_base_path(transcript_path: Path) -> Path:
    """Return the sidecar base path for a transcript path."""
    if transcript_path.suffix:
        return transcript_path.with_suffix("")
    return transcript_path


def human_readable_sidecar_path(source_path: Path, suffix: str) -> Path:
    """Return the Markdown sidecar path for a runtime artifact.

    Run-time artifacts live under ``.runtime``. Their rendered Markdown
    copies belong under the parallel ``human-logs`` tree so people can browse
    readable logs without digging through machine state.
    """
    source_path = Path(source_path)
    parts = list(source_path.parts)
    try:
        runtime_index = parts.index(RUNTIME_DIR)
    except ValueError:
        return source_path.with_suffix(suffix)

    parts[runtime_index] = HUMAN_LOGS_DIR
    return Path(*parts).with_suffix(suffix)


def _fallback_prompt_path(metadata: dict[str, Any] | None) -> Path | None:
    """Return a fallback prompt path when transcript logging is disabled."""
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_dir:
        return None
    runtime_dir = (
        Path(os.environ.get("BOOLEY_RUNTIME_DIR", ""))
        if os.environ.get("BOOLEY_RUNTIME_DIR")
        else ticket_runtime_dir(logs_dir)
    )
    label = str((metadata or {}).get("label") or "agent")
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)
    stamp = compact_utc_now(microseconds=True)
    return runtime_dir / "prompts" / f"{safe_label}-{stamp}.jsonl"


def _format_markdown(record: dict[str, Any]) -> str:
    lines = [
        "# Actual Prompt Sent",
        "",
        "```text",
        record["full_prompt"],
        "```",
        "",
    ]
    return "\n".join(lines)


def write_prompt_artifacts(
    transcript_path: Path | None,
    *,
    system_prompt: str | None,
    user_prompt: str,
    full_prompt: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write JSON and Markdown prompt sidecars next to a transcript.

    This is deliberately best-effort: prompt logging must never change solver
    behavior or fail an otherwise valid run.
    """
    if transcript_path is None:
        transcript_path = _fallback_prompt_path(metadata)
        if transcript_path is None:
            return

    system_text = system_prompt or ""
    full_text = (
        full_prompt
        if full_prompt is not None
        else (f"{system_text}\n\n---\n\n{user_prompt}" if system_text else user_prompt)
    )
    record = {
        "type": "agent_prompt",
        "timestamp": utc_now_rfc3339(),
        "metadata": metadata or {},
        "system_prompt": system_text,
        "user_prompt": user_prompt,
        "full_prompt": full_text,
        "system_sha256": _sha256(system_text),
        "user_sha256": _sha256(user_prompt),
        "full_sha256": _sha256(full_text),
        "system_chars": len(system_text),
        "user_chars": len(user_prompt),
        "full_chars": len(full_text),
    }

    try:
        base = _prompt_base_path(transcript_path)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".prompt.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        markdown_path = human_readable_sidecar_path(base, ".prompt.md")
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(
            _format_markdown(record),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to write prompt artifacts for %s", transcript_path)
