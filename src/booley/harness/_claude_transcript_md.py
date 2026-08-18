"""Claude JSONL-to-Markdown transcript renderer.

Reads back a Claude SDK JSONL transcript and renders a human-readable
Markdown sidecar. This cluster is self-contained: it shares no mutable state
with :class:`ClaudeSDKBackend` and only reads files the backend already wrote,
so it lives apart from the backend proper (single-responsibility).

``_claude_backend`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from pathlib import Path

from .prompt_artifacts import human_readable_sidecar_path


def _claude_md_prompt_lines(entry: dict) -> list[str]:
    """Markdown for a ``prompt`` transcript entry (system + user prompts)."""
    lines: list[str] = []
    sys_prompt = entry.get("system_prompt", "")
    usr_prompt = entry.get("user_prompt", "")
    if sys_prompt:
        lines.append("# System Prompt\n")
        lines.append(f"```\n{sys_prompt}\n```\n")
    if usr_prompt:
        lines.append("# User Prompt\n")
        lines.append(f"```\n{usr_prompt}\n```\n")
    lines.append("---\n")
    return lines


def _claude_md_block_lines(block: dict) -> list[str]:
    """Markdown for a single assistant content block, keyed on its type."""
    lines: list[str] = []
    btype = block.get("type", "")
    if btype == "TextBlock":
        text = block.get("text", "")
        if text:
            for text_line in text.splitlines():
                lines.append(f"> {text_line}")
            lines.append("")
    elif btype == "ToolUseBlock":
        name = block.get("name", "?")
        inp = block.get("input", {})
        if isinstance(inp, dict):
            short_keys = {
                k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
                for k, v in inp.items()
            }
            inp_str = json.dumps(short_keys, indent=2, default=str)
        else:
            inp_str = str(inp)
        lines.append(f"**Claude capability: {name}**\n```json\n{inp_str}\n```\n")
    elif btype == "ToolResultBlock":
        content = block.get("content", "")
        if isinstance(content, str) and content:
            preview = content[:500]
            if len(content) > 500:
                preview += f"\n... ({len(content)} chars total)"
            lines.append(f"```\n{preview}\n```\n")
    elif btype == "ThinkingBlock":
        thinking = block.get("thinking", "")
        if thinking:
            preview = thinking[:300]
            if len(thinking) > 300:
                preview += "..."
            lines.append(f"*Thinking: {preview}*\n")
    return lines


def _claude_md_usage_lines(usage: dict) -> list[str]:
    """Markdown token-usage footer for an assistant turn (empty if no usage)."""
    if not usage:
        return []
    parts = [f"{usage.get('input_tokens', 0):,} in"]
    cached = usage.get("cache_read_input_tokens", 0)
    if cached:
        parts.append(f"{cached:,} cached")
    parts.append(f"{usage.get('output_tokens', 0):,} out")
    return [f"---\n*Tokens: {', '.join(parts)}*\n"]


def _claude_write_markdown(transcript_path: Path | None) -> None:
    """Read back JSONL and write human-readable Markdown to the human log tree."""
    if transcript_path is None or not transcript_path.exists():
        return
    md_path = human_readable_sidecar_path(transcript_path, ".md")
    lines: list[str] = []
    turn_num = 0

    with transcript_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") == "prompt":
                lines.extend(_claude_md_prompt_lines(entry))
                continue

            # Assistant turn
            turn_num += 1
            lines.append(f"## Turn {turn_num}\n")

            for block in entry.get("content", []):
                lines.extend(_claude_md_block_lines(block))

            lines.extend(_claude_md_usage_lines(entry.get("usage", {})))

    md_path.parent.mkdir(parents=True, exist_ok=True)
    with md_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines).lstrip("\n"))
