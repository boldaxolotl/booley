"""Distill raw JSONL agent transcripts into compact crash-recovery summaries.

Raw transcripts embed the full prior context verbatim — including any earlier
transcript dumps the crashed agent read — so pointing a recovery agent at them
compounds context: benchmark runs measured 43.8% of input tokens burned on
retry reconstruction reads, with one 786k-token turn from three generations of
transcript-in-transcript nesting. This module extracts just the signal (agent
reasoning, commands + exit codes, MCP tool verdicts, the final message) into a
bounded Markdown summary the recovery prompt can point at instead.

Handles both backend transcript shapes best-effort:

* Codex ``codex exec --json`` events (``item.completed`` with agent_message /
  command_execution / mcp_tool_call / file_change items, ``turn.failed`` /
  ``error``) — same field access as ``_codex_transcript_md``.
* Claude SDK JSONL entries (``prompt`` header, then assistant turns whose
  ``content`` holds TextBlock / ThinkingBlock / ToolUseBlock / ToolResultBlock
  dicts) — same shapes ``_claude_transcript_md`` reads back.

Never raises: any failure degrades to a one-line fallback string.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per-item truncation budgets (characters).
_MESSAGE_CHARS = 400
_COMMAND_CHARS = 200
_OUTPUT_CHARS = 200
_ARGS_CHARS = 200
_RESULT_CHARS = 200
_FINAL_MESSAGE_CHARS = 2000

# Default size budget for a distilled summary (bytes).
_DEFAULT_MAX_BYTES = 24_000


def _clip(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars with an ellipsis marker, single-pass safe."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"... ({len(text)} chars total)"


def _first_line(text: str) -> str:
    """First non-empty line of a blob — where Flow reports put the verdict."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _mcp_result_verdict(result: Any) -> str:
    """Extract a one-line verdict from a Codex MCP tool result object."""
    if not isinstance(result, dict):
        return ""
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            line = _first_line(str(item.get("text", "")))
            if line:
                return line
    return ""


def _brief_args(args: Any) -> str:
    """Compact single-line JSON rendering of MCP tool arguments."""
    if not args:
        return ""
    try:
        rendered = json.dumps(args, default=str, separators=(", ", ": "))
    except (TypeError, ValueError):
        rendered = str(args)
    return _clip(rendered, _ARGS_CHARS)


# ---------------------------------------------------------------------------
# Codex event extraction (mirrors _codex_transcript_md._codex_md_item_lines)
# ---------------------------------------------------------------------------


def _codex_command_line(item: dict) -> str:
    """One-line summary of a command_execution item."""
    cmd = _clip(str(item.get("command", "")), _COMMAND_CHARS)
    exit_code = item.get("exit_code")
    entry = f"- **Command:** `{cmd}`"
    if exit_code is not None:
        entry += f" (exit {exit_code})"
    output = str(item.get("aggregated_output", ""))
    if output.strip():
        entry += f" — {_clip(_first_line(output), _OUTPUT_CHARS)}"
    return entry


def _codex_mcp_line(item: dict) -> str:
    """One-line summary of an mcp_tool_call item: MCP tool + args + verdict."""
    entry = f"- **MCP tool:** `{item.get('server', '?')}.{item.get('tool', '?')}`"
    args = _brief_args(item.get("arguments"))
    if args:
        entry += f" args: {args}"
    verdict = _mcp_result_verdict(item.get("result"))
    if verdict:
        entry += f" -> {_clip(verdict, _RESULT_CHARS)}"
    error = item.get("error")
    if error:
        entry += f" -> ERROR: {_clip(str(error), _RESULT_CHARS)}"
    return entry


def _distill_codex_item(item: dict) -> tuple[list[str], str | None]:
    """Summary lines for one completed Codex item, plus final-message candidate."""
    lines: list[str] = []
    final_candidate: str | None = None
    itype = item.get("type", "")

    if itype == "agent_message":
        text = str(item.get("text", ""))
        if text.strip():
            lines.append(f"- **Agent:** {_clip(text, _MESSAGE_CHARS)}")
            final_candidate = text
    elif itype == "reasoning":
        text = str(item.get("text", ""))
        if text.strip():
            lines.append(f"- **Reasoning:** {_clip(text, _MESSAGE_CHARS)}")
    elif itype == "command_execution":
        lines.append(_codex_command_line(item))
    elif itype == "file_change":
        for change in item.get("changes", []):
            if isinstance(change, dict):
                lines.append(f"- **File {change.get('kind', '?')}:** `{change.get('path', '?')}`")
    elif itype == "mcp_tool_call":
        lines.append(_codex_mcp_line(item))

    return lines, final_candidate


def _distill_codex_event(event: dict) -> tuple[list[str], str | None]:
    """Summary lines for one Codex JSONL event, plus final-message candidate."""
    etype = event.get("type", "")
    if etype == "item.completed":
        item = event.get("item", {})
        if isinstance(item, dict):
            return _distill_codex_item(item)
    elif etype == "error":
        return [f"- **Error:** {_clip(str(event.get('message', event)), _MESSAGE_CHARS)}"], None
    elif etype == "turn.failed":
        err = event.get("error", {})
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return [f"- **Turn failed:** {_clip(str(msg), _MESSAGE_CHARS)}"], None
    return [], None


# ---------------------------------------------------------------------------
# Claude entry extraction (mirrors _claude_transcript_md._claude_md_block_lines)
# ---------------------------------------------------------------------------


def _distill_claude_block(block: dict) -> tuple[list[str], str | None]:
    """Summary lines for one assistant content block, plus final candidate."""
    lines: list[str] = []
    final_candidate: str | None = None
    btype = block.get("type", "")

    if btype == "TextBlock":
        text = str(block.get("text", ""))
        if text.strip():
            lines.append(f"- **Agent:** {_clip(text, _MESSAGE_CHARS)}")
            final_candidate = text

    elif btype == "ThinkingBlock":
        text = str(block.get("thinking", ""))
        if text.strip():
            lines.append(f"- **Reasoning:** {_clip(text, _MESSAGE_CHARS)}")

    elif btype == "ToolUseBlock":
        name = str(block.get("name", "?"))
        inp = block.get("input")
        command = inp.get("command") if isinstance(inp, dict) else None
        if isinstance(command, str) and command.strip():
            # Bash-style file operations: surface the command line itself.
            lines.append(f"- **Command:** `{_clip(command, _COMMAND_CHARS)}` (via {name})")
        else:
            entry = f"- **MCP tool:** `{name}`"
            args = _brief_args(inp)
            if args:
                entry += f" args: {args}"
            lines.append(entry)

    elif btype == "ToolResultBlock":
        content = block.get("content", "")
        if isinstance(content, str) and content.strip():
            lines.append(f"  -> {_clip(_first_line(content), _RESULT_CHARS)}")

    return lines, final_candidate


def _distill_claude_entry(entry: dict) -> tuple[list[str], str | None]:
    """Summary lines for one Claude JSONL entry, plus final-message candidate."""
    # The prompt header re-embeds the entire system+user prompt — the exact
    # payload distillation exists to keep OUT of the recovery context.
    if entry.get("type") == "prompt":
        return [], None
    lines: list[str] = []
    final_candidate: str | None = None
    for block in entry.get("content", []):
        if not isinstance(block, dict):
            continue
        block_lines, candidate = _distill_claude_block(block)
        lines.extend(block_lines)
        if candidate is not None:
            final_candidate = candidate
    return lines, final_candidate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _trim_to_bytes(text: str, max_bytes: int) -> str:
    """Head+tail trim ``text`` to at most ``max_bytes`` UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "\n\n... [{omitted} bytes of middle omitted] ...\n\n"
    # Reserve marker space generously (byte count rendered later is bounded).
    budget = max(max_bytes - len(marker) - 20, 0)
    head_bytes = (budget * 3) // 5  # bias toward the head: setup + early decisions
    tail_bytes = budget - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[len(encoded) - tail_bytes :].decode("utf-8", errors="ignore")
    omitted = len(encoded) - head_bytes - tail_bytes
    return head + marker.format(omitted=omitted) + tail


def _distill(transcript_path: Path, max_bytes: int) -> str:
    """Core distillation; may raise — callers wrap with the fallback."""
    lines: list[str] = [f"# Distilled transcript: {transcript_path.name}", ""]
    final_message: str | None = None
    parsed = 0

    with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # best-effort: skip garbage lines
            if not isinstance(event, dict):
                continue
            parsed += 1
            # Codex events carry a dotted "type"; Claude entries carry a
            # "content" block list (or type == "prompt"). Try codex first —
            # its "type" values never collide with Claude's.
            if "item" in event or str(event.get("type", "")).startswith(
                ("item.", "turn.", "thread.", "error")
            ):
                event_lines, candidate = _distill_codex_event(event)
            else:
                event_lines, candidate = _distill_claude_entry(event)
            lines.extend(event_lines)
            if candidate is not None:
                final_message = candidate

    if parsed == 0:
        return ""

    if final_message and final_message.strip():
        lines.append("")
        lines.append("## Final message")
        lines.append("")
        lines.append(_clip(final_message, _FINAL_MESSAGE_CHARS))

    return _trim_to_bytes("\n".join(lines).strip() + "\n", max_bytes)


def distill_transcript(transcript_path: Path, *, max_bytes: int = _DEFAULT_MAX_BYTES) -> str:
    """Distill a JSONL transcript into a compact Markdown summary.

    Best-effort and non-raising: unparseable lines are skipped, and any
    failure (missing file, encoding, ...) returns a one-line fallback.
    """
    try:
        summary = _distill(transcript_path, max_bytes)
    except Exception:  # noqa: BLE001 — distillation is advisory; never break recovery
        logger.warning("Transcript distillation failed for %s", transcript_path, exc_info=True)
        return f"(transcript summary unavailable: could not parse {transcript_path.name})"
    if not summary.strip():
        return f"(transcript summary unavailable: no parseable events in {transcript_path.name})"
    return summary


def write_distilled_summary(transcript_path: Path) -> Path | None:
    """Write the distilled summary next to the transcript as ``<stem>.summary.md``.

    Returns the summary path, or None when the transcript is missing/empty or
    the write fails. Never raises.
    """
    try:
        if not transcript_path.exists():
            return None
        # Use _distill directly: an empty/unparseable transcript yields "",
        # which means "no summary file" rather than a file with the fallback.
        summary = _distill(transcript_path, _DEFAULT_MAX_BYTES)
        if not summary.strip():
            return None
        summary_path = transcript_path.with_name(f"{transcript_path.stem}.summary.md")
        summary_path.write_text(summary, encoding="utf-8")
        return summary_path
    except Exception:  # noqa: BLE001 — summary is advisory; never break recovery
        logger.warning("Could not write distilled summary for %s", transcript_path, exc_info=True)
        return None
