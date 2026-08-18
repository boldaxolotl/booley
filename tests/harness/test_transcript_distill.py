"""Tests for ``transcript_distill`` — bounded crash-recovery summaries.

Regression context: crash-recovery prompts used to point the resumed agent at
the raw JSONL transcript ("Scan for reasoning and decisions"), whose contents
then got appended into the *new* transcript — three generations of nesting
produced a 786k-token turn and 43.8% of benchmark input tokens went to retry
reconstruction reads. Distillation replaces those reads with a compact,
size-capped Markdown summary.
"""

from __future__ import annotations

import json
from pathlib import Path

from booley.harness.transcript_distill import distill_transcript, write_distilled_summary

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _codex_events() -> list[dict]:
    """Synthetic codex exec --json events (shapes from _codex_transcript_md)."""
    return [
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "I should lint first."},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Starting with lint to establish a baseline.",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/bin/bash -lc 'verilator --lint-only top.sv'",
                "exit_code": 1,
                "aggregated_output": "%Warning-WIDTH: top.sv:12\nmore lines\n",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "status": "completed",
                "server": "booley",
                "tool": "lint",
                "arguments": {"target": "config_a"},
                "result": {"content": [{"type": "text", "text": "PASS: lint clean (0 warnings)"}]},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"kind": "update", "path": "rtl/top.sv"}],
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "All criteria met; submitting report."},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 50}},
    ]


def _write_codex_transcript(path: Path, *, garbage: bool = False) -> None:
    lines = [json.dumps(e) for e in _codex_events()]
    if garbage:
        # Interleave junk the parser must skip: non-JSON, bare scalars, lists.
        lines.insert(1, "this is not json {{{")
        lines.insert(3, '"bare string"')
        lines.insert(5, "[1, 2, 3]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_claude_transcript(path: Path) -> None:
    """Synthetic Claude SDK JSONL (shapes from _claude_backend transcript writer)."""
    entries = [
        {
            "type": "prompt",
            "system_prompt": "SECRET-SYSTEM-PROMPT " * 100,
            "user_prompt": "SECRET-USER-PROMPT " * 100,
        },
        {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {"type": "ThinkingBlock", "thinking": "Need to check the FSM encoding."},
                {"type": "TextBlock", "text": "Running simulation now."},
                {
                    "type": "ToolUseBlock",
                    "name": "Bash",
                    "input": {"command": "make sim TARGET=config_a"},
                },
            ],
        },
        {
            "usage": {},
            "content": [
                {"type": "ToolResultBlock", "content": "FAIL: 2 assertions fired\ndetail..."},
                {
                    "type": "ToolUseBlock",
                    "name": "mcp__booley__simulate",
                    "input": {"target": "config_a"},
                },
                {"type": "TextBlock", "text": "Simulation failed; fixing the reset path."},
            ],
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# distill_transcript
# ---------------------------------------------------------------------------


class TestDistillCodex:
    def test_summary_contains_key_events(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        _write_codex_transcript(transcript)

        summary = distill_transcript(transcript)

        # Agent message, command + exit code, MCP verdict, file change
        assert "Starting with lint" in summary
        assert "verilator --lint-only top.sv" in summary
        assert "exit 1" in summary
        assert "booley.lint" in summary
        assert "PASS: lint clean" in summary
        assert "rtl/top.sv" in summary
        # Last agent message is promoted to the final-message section
        assert "## Final message" in summary
        assert "All criteria met; submitting report." in summary

    def test_respects_max_bytes_with_omission_marker(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        # Big transcript: many long agent messages
        events = []
        for i in range(200):
            events.append(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": f"msg {i}: " + "x" * 300},
                }
            )
        transcript.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

        max_bytes = 4000
        summary = distill_transcript(transcript, max_bytes=max_bytes)

        assert len(summary.encode("utf-8")) <= max_bytes
        assert "bytes of middle omitted" in summary
        # Head and tail both survive the trim
        assert "msg 0:" in summary
        assert "msg 199:" in summary

    def test_skips_garbage_lines(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        _write_codex_transcript(transcript, garbage=True)

        summary = distill_transcript(transcript)

        assert "Starting with lint" in summary
        assert "not json" not in summary

    def test_missing_file_returns_fallback_not_raise(self, tmp_path: Path):
        summary = distill_transcript(tmp_path / "nope.jsonl")
        assert "unavailable" in summary
        assert "\n" not in summary.strip()  # one-line fallback

    def test_all_garbage_returns_fallback(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        transcript.write_text("junk\nmore junk\n", encoding="utf-8")
        summary = distill_transcript(transcript)
        assert "unavailable" in summary


class TestDistillClaude:
    def test_summary_contains_key_events(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_002.jsonl"
        _write_claude_transcript(transcript)

        summary = distill_transcript(transcript)

        assert "Running simulation now." in summary
        assert "make sim TARGET=config_a" in summary
        assert "FAIL: 2 assertions fired" in summary
        assert "mcp__booley__simulate" in summary
        assert "Need to check the FSM encoding." in summary
        # Final message = last TextBlock
        assert "## Final message" in summary
        assert "fixing the reset path" in summary

    def test_prompt_header_is_excluded(self, tmp_path: Path):
        """The prompt entry re-embeds the full context — must never leak."""
        transcript = tmp_path / "developer.run_002.jsonl"
        _write_claude_transcript(transcript)

        summary = distill_transcript(transcript)

        assert "SECRET-SYSTEM-PROMPT" not in summary
        assert "SECRET-USER-PROMPT" not in summary


# ---------------------------------------------------------------------------
# write_distilled_summary
# ---------------------------------------------------------------------------


class TestWriteDistilledSummary:
    def test_writes_sidecar_next_to_transcript(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        _write_codex_transcript(transcript)

        out = write_distilled_summary(transcript)

        assert out == tmp_path / "developer.run_001.summary.md"
        assert out.exists()
        assert "Starting with lint" in out.read_text(encoding="utf-8")

    def test_missing_transcript_returns_none(self, tmp_path: Path):
        assert write_distilled_summary(tmp_path / "gone.jsonl") is None

    def test_empty_transcript_returns_none(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        transcript.write_text("", encoding="utf-8")
        assert write_distilled_summary(transcript) is None

    def test_unparseable_transcript_returns_none(self, tmp_path: Path):
        transcript = tmp_path / "developer.run_001.jsonl"
        transcript.write_text("garbage\n", encoding="utf-8")
        assert write_distilled_summary(transcript) is None
