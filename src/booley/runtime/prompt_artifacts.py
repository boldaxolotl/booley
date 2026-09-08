"""Persist exact agent prompts as standalone artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from booley.core.models import AgentArtifactPaths
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_base_path(transcript_path: Path) -> Path:
    """Return the sidecar base path for a transcript path."""
    if transcript_path.suffix:
        return transcript_path.with_suffix("")
    return transcript_path


def adjacent_artifact_paths(transcript_path: Path | None) -> AgentArtifactPaths:
    """Default standalone artifacts beside an explicitly supplied transcript."""
    if transcript_path is None:
        return AgentArtifactPaths()
    base = _prompt_base_path(transcript_path)
    return AgentArtifactPaths(
        base.with_suffix(".prompt.json"),
        base.with_suffix(".prompt.md"),
        transcript_path.with_suffix(".md"),
    )


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
    paths: AgentArtifactPaths,
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
    if paths.prompt_json is None and paths.prompt_markdown is None:
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
        if paths.prompt_json is not None:
            paths.prompt_json.parent.mkdir(parents=True, exist_ok=True)
            paths.prompt_json.write_text(
                json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8"
            )
        if paths.prompt_markdown is not None:
            paths.prompt_markdown.parent.mkdir(parents=True, exist_ok=True)
            paths.prompt_markdown.write_text(_format_markdown(record), encoding="utf-8")
    except OSError:
        logger.warning("Failed to write prompt artifacts for %s", paths.prompt_json)
