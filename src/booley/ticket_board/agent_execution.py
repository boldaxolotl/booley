"""Compose agent artifact layout and notification policy for Booley execution."""

from __future__ import annotations

import os
from dataclasses import replace
from functools import partial
from pathlib import Path

from booley.core.models import AgentArtifactPaths, AgentCallParams
from booley.runtime.prompt_artifacts import adjacent_artifact_paths
from booley.runtime.timefmt import compact_utc_now
from booley.ticket_board.notifications import notify_rate_limit
from booley.ticket_board.paths import HUMAN_LOGS_DIR, RUNTIME_DIR, ticket_runtime_dir


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


def _fallback_prompt_path(label: str | None) -> Path | None:
    """Return a fallback prompt path when transcript logging is disabled."""
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_dir:
        return None
    runtime_dir = (
        Path(os.environ.get("BOOLEY_RUNTIME_DIR", ""))
        if os.environ.get("BOOLEY_RUNTIME_DIR")
        else ticket_runtime_dir(logs_dir)
    )
    label = label or "agent"
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)
    stamp = compact_utc_now(microseconds=True)
    return runtime_dir / "prompts" / f"{safe_label}-{stamp}.jsonl"


def resolve_agent_artifacts(
    transcript_path: Path | None,
    *,
    label: str | None = None,
) -> AgentArtifactPaths:
    """Resolve canonical machine and human log destinations for one attempt."""
    prompt_source = transcript_path or _fallback_prompt_path(label)
    paths = adjacent_artifact_paths(prompt_source)
    if prompt_source is None:
        return paths
    base = prompt_source.with_suffix("") if prompt_source.suffix else prompt_source
    return replace(
        paths,
        prompt_markdown=human_readable_sidecar_path(base, ".prompt.md"),
        transcript_markdown=(
            human_readable_sidecar_path(transcript_path, ".md") if transcript_path else None
        ),
    )


def configure_agent_call(params: AgentCallParams) -> AgentCallParams:
    """Bind Booley execution policy without mutating a shared backend instance."""
    return replace(
        params,
        artifact_paths=partial(resolve_agent_artifacts, label=params.label),
        notify_rate_limit=notify_rate_limit,
    )
