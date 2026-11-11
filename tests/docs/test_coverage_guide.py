"""The coverage guide points at the command, not at the point store's file format."""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).parents[2] / "docs/user/COVERAGE.md"


def _section(text: str, heading: str) -> str:
    """The body of one ``###`` section, up to the next heading of the same or higher level."""
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^#{{1,3}} )", text, re.M | re.S)
    assert match, f"missing section: {heading}"
    return match.group(1)


def test_finding_uncovered_points_uses_the_command() -> None:
    section = _section(GUIDE.read_text(encoding="utf-8"), "Finding uncovered points")

    assert "booley coverage uncovered" in section
    for retired in ("zcat", "jq", "coverage-points.jsonl.gz"):
        assert retired not in section
