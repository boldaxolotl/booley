"""Regression checks for bundled prompt/reference markdown."""

from __future__ import annotations

from pathlib import Path

REFS_DIR = Path(__file__).resolve().parents[2] / "src" / "booley" / "data" / "refs"


def test_reference_docs_do_not_use_stale_paths_or_flags():
    text = "\n".join(path.read_text(encoding="utf-8") for path in REFS_DIR.rglob("*.md"))

    assert ".booley/docs/refs" not in text
    assert ".booley_project/tmp" not in text
    assert "../project/" not in text
    assert "--vcd" not in text
    assert "shipped with the booley-ticket-create skill" not in text


def test_code_review_guides_do_not_duplicate_output_contract():
    review_dir = REFS_DIR / "code_review"
    text = "\n".join(path.read_text(encoding="utf-8") for path in review_dir.rglob("*.md"))

    assert "## Output Format" not in text
    assert "Also output structured JSON" not in text
    assert "markdown is for humans, JSON is the contract" not in text
    assert "SUMMARY: X" not in text
