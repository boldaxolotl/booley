"""Tests for standalone prompt artifact persistence."""

from __future__ import annotations

import json

from booley.runtime.prompt_artifacts import write_prompt_artifacts


def test_write_prompt_artifacts_json_and_markdown(tmp_path):
    transcript = tmp_path / "coder.jsonl"

    write_prompt_artifacts(
        transcript,
        system_prompt="system rules",
        user_prompt="do the work",
        full_prompt="system rules\n\n---\n\ndo the work",
        metadata={"label": "coder", "model": "test-model"},
    )

    payload = json.loads((tmp_path / "coder.prompt.json").read_text(encoding="utf-8"))
    rendered = (tmp_path / "coder.prompt.md").read_text(encoding="utf-8")

    assert payload["system_prompt"] == "system rules"
    assert payload["user_prompt"] == "do the work"
    assert payload["full_prompt"] == "system rules\n\n---\n\ndo the work"
    assert payload["metadata"]["label"] == "coder"
    assert rendered == (
        "# Actual Prompt Sent\n\n```text\nsystem rules\n\n---\n\ndo the work\n```\n"
    )


def test_write_prompt_artifacts_runtime_markdown_goes_to_human_logs(tmp_path):
    transcript = tmp_path / ".runtime" / "transcripts" / "coder" / "1" / "coder.jsonl"

    write_prompt_artifacts(
        transcript,
        system_prompt=None,
        user_prompt="do the work",
        metadata={"label": "coder", "model": "test-model"},
    )

    assert (transcript.parent / "coder.prompt.json").exists()
    assert not (transcript.parent / "coder.prompt.md").exists()
    rendered = tmp_path / "human-logs" / "transcripts" / "coder" / "1" / "coder.prompt.md"
    assert "do the work" in rendered.read_text(encoding="utf-8")


def test_write_prompt_artifacts_falls_back_to_logs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))

    write_prompt_artifacts(
        None,
        system_prompt=None,
        user_prompt="specialist prompt",
        metadata={"label": "reviewer/quality"},
    )

    matches = list((tmp_path / ".runtime" / "prompts").glob("reviewer_quality-*.prompt.json"))
    assert len(matches) == 1
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    assert payload["user_prompt"] == "specialist prompt"
    human_matches = list(
        (tmp_path / "human-logs" / "prompts").glob("reviewer_quality-*.prompt.md")
    )
    assert len(human_matches) == 1
