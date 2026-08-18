"""Codex JSONL parsing and Markdown transcript rendering.

Parses ``codex exec --json`` output into token/usage totals plus a raw event
list, and renders a human-readable Markdown sidecar from those events. This
cluster shares no mutable state with :class:`CodexBackend`; it only consumes
strings/event dicts the backend hands it, so it lives apart from the backend
proper (single-responsibility).

``_codex_backend`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from .prompt_artifacts import human_readable_sidecar_path


def _is_structured_only_agent_text(text: str) -> bool:
    """Return true for bare structured-output JSON chatter."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and set(payload) <= {"commit_message"}


def _mcp_result_text(result: Any) -> str:
    """Extract readable text content from a Codex MCP tool result object."""
    if not isinstance(result, dict):
        return ""
    chunks: list[str] = []
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if text:
                chunks.append(str(text))
    return "\n\n".join(chunks)


def _truncate_transcript_block(text: str, *, limit: int = 4000) -> str:
    """Keep Markdown transcripts readable while preserving useful MCP tool output."""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n... ({len(text)} chars total)"


class CodexParsedEvents(NamedTuple):
    """Result of parsing Codex JSONL output.

    Tuple-compatible (NamedTuple) so existing positional destructures keep
    working, while new callers can read fields by name.
    """

    output: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    error_msg: str | None
    events: list[dict]


def _codex_parse_events(raw_output: str) -> CodexParsedEvents:
    """Parse Codex JSONL output into a :class:`CodexParsedEvents`."""
    output_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    error_msg: str | None = None
    events: list[dict] = []

    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Codex may emit bare strings/numbers/lists as JSONL lines; only dicts carry "type".
        if not isinstance(event, dict):
            continue
        events.append(event)

        etype = event.get("type", "")
        if etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    output_parts.append(text)
        elif etype == "turn.completed":
            usage = event.get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
            cached_tokens += usage.get("cached_input_tokens", 0)
        elif etype == "error":
            error_msg = event.get("message", str(event))
        elif etype == "turn.failed":
            err = event.get("error", {})
            error_msg = err.get("message", str(err))

    output = "\n\n".join(output_parts)
    return CodexParsedEvents(output, input_tokens, output_tokens, cached_tokens, error_msg, events)


def _strip_bash_wrapper(cmd: str) -> str:
    """Strip ``/bin/bash -lc '...'`` wrapper for display."""
    prefix = "/bin/bash -lc "
    if not cmd.startswith(prefix):
        return cmd
    inner = cmd[len(prefix) :]
    if len(inner) >= 2 and inner[0] in ('"', "'") and inner[-1] == inner[0]:
        inner = inner[1:-1]
    return inner


def _codex_md_item_lines(item: dict) -> list[str]:
    """Markdown for one completed Codex ``item`` (message/command/file/MCP)."""
    lines: list[str] = []
    itype = item.get("type", "")

    if itype == "agent_message":
        text = item.get("text", "")
        if text and not _is_structured_only_agent_text(text):
            for text_line in text.splitlines():
                lines.append(f"> {text_line}")
            lines.append("")

    elif itype == "command_execution":
        cmd_display = _strip_bash_wrapper(item.get("command", ""))
        exit_code = item.get("exit_code")
        output = item.get("aggregated_output", "")

        block = [f"$ {cmd_display}"]
        if output.strip():
            block.append(output.rstrip())
        if exit_code:
            block.append(f"# exit code: {exit_code}")
        lines.append("```console\n" + "\n".join(block) + "\n```\n")

    elif itype == "file_change":
        for change in item.get("changes", []):
            kind = change.get("kind", "?")
            path = change.get("path", "?")
            lines.append(f"**{kind.capitalize()}:** `{path}`\n")

    elif itype == "mcp_tool_call" and item.get("status") == "completed":
        server = item.get("server", "?")
        mcp_tool = item.get("tool", "?")
        args = item.get("arguments", {})
        lines.append(f"**MCP Tool:** `{server}.{mcp_tool}`\n")
        if args:
            args_text = json.dumps(args, indent=2, default=str)
            lines.append(f"```json\n{args_text}\n```\n")
        result_text = _mcp_result_text(item.get("result"))
        if result_text:
            result_text = _truncate_transcript_block(result_text)
            lines.append(f"```\n{result_text}\n```\n")
        error = item.get("error")
        if error:
            error_text = _truncate_transcript_block(str(error))
            lines.append(f"```\nERROR: {error_text}\n```\n")

    return lines


def _codex_md_usage_lines(usage: dict) -> list[str]:
    """Markdown token-usage footer for a completed Codex turn."""
    parts = [f"{usage.get('input_tokens', 0):,} in"]
    cached = usage.get("cached_input_tokens", 0)
    if cached:
        parts.append(f"{cached:,} cached")
    parts.append(f"{usage.get('output_tokens', 0):,} out")
    reasoning = usage.get("reasoning_output_tokens", 0)
    if reasoning:
        parts.append(f"{reasoning:,} reasoning")
    return [f"---\n*Tokens: {', '.join(parts)}*\n"]


def _codex_write_markdown(
    events: list[dict],
    transcript_path: Path | None,
    *,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
) -> None:
    """Write a human-readable Markdown transcript in the human log tree."""
    if transcript_path is None or not events:
        return
    md_path = human_readable_sidecar_path(transcript_path, ".md")

    lines: list[str] = []

    if system_prompt or user_prompt:
        # Deferred import: _codex_build_prompt stays in _codex_backend (shared
        # with the backend proper), which re-exports this module — a top-level
        # import here would form a cycle.
        from ._codex_backend import _codex_build_prompt

        actual_prompt = _codex_build_prompt(user_prompt or "", system_prompt)
        lines.append("# Actual Prompt Sent\n")
        lines.append(f"```\n{actual_prompt}\n```\n")
        lines.append("---\n")

    turn_num = 0

    for event in events:
        etype = event.get("type", "")

        if etype == "turn.started":
            turn_num += 1
            lines.append(f"\n## Turn {turn_num}\n")
        elif etype == "item.completed":
            lines.extend(_codex_md_item_lines(event.get("item", {})))
        elif etype == "turn.completed":
            lines.extend(_codex_md_usage_lines(event.get("usage", {})))

    md_path.parent.mkdir(parents=True, exist_ok=True)
    with md_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines).lstrip("\n"))
