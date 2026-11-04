"""Every surface that teaches waiting for Jobs teaches the argument-free poll (#2538)."""

from __future__ import annotations

import re
from pathlib import Path

from booley.mcp import catalog
from booley.mcp import server as mcp

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_GUIDANCE = (
    "src/booley/data/cheatsheet.md",
    "src/booley/data/skills/booley-goal/SKILL.md",
    "src/booley/data/skills/booley-setup/SKILL.md",
    "src/booley/data/skills/booley-heal/SKILL.md",
    "docs/user/FLOW_REFERENCE.md",
)


def _plain(text: str) -> str:
    """Flatten wrapping and markup so wording checks ignore layout."""
    return re.sub(r"\s+", " ", re.sub(r"[`'\"*]", "", text))


def test_poll_surfaces_teach_argument_free_wait(monkeypatch):
    monkeypatch.setattr(mcp, "_job_phase", lambda _run_id: "RUNNING")
    monkeypatch.setattr(mcp, "_running_progress", lambda _run_id: None)
    surfaces = {
        "tool description": catalog._POLL_MCP_TOOL_DESCRIPTION,
        "hand-off": mcp._format_job_running("sim", "sim-1"),
        "RUNNING reply": mcp._format_job_running_poll("sim-1"),
    }
    for name, text in surfaces.items():
        plain = _plain(text)
        assert "no run_id" in plain, name
        assert "first" in plain, name
    assert "Prefer few long polls" not in catalog._POLL_MCP_TOOL_DESCRIPTION
    assert "run_id=sim-1" in surfaces["hand-off"]
    assert "run_id=sim-1" in mcp._format_job_attached("sim", "sim-1")
    assert catalog._poll_mcp_tool_def()["schema"]["required"] == []


def test_packaged_guidance_teaches_argument_free_wait():
    for path in PACKAGED_GUIDANCE:
        plain = _plain((REPO_ROOT / path).read_text(encoding="utf-8"))

        assert "booley_poll with no run_id" in plain, path
