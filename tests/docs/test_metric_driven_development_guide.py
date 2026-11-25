"""The Metric-Driven Development guide covers its topics, and its examples are valid (#4304)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from tests.harness.test_measure_cli import _measure
from tests.measurement.metrics_project_support import write_metrics_project

GUIDE = Path(__file__).parents[2] / "docs/user/METRIC-DRIVEN-DEVELOPMENT.md"
PROJECT_TARGETS = frozenset({"synth_core", "sim_core", "sim_fifo", "sim_two"})
HEADINGS = [
    "Why track metrics",
    "How it works",
    "Adopt it in stages",
    "Write `metrics.toml`",
    "Check the file",
    "Measure a commit",
    "Give the Developer Agent a relative target",
    "Read the Measurement Report",
    "Base runs",
    "Limits",
]
REQUIRED_TERMS = {
    "Adopt it in stages": ("track",),
    "Write `metrics.toml`": ("off", "track", "notes", "[defaults.", '"clk_i.fmax_mhz_min"'),
    "Check the file": ("booley measure --validate", "booley cheat --metrics"),
    "Measure a commit": ("amend",),
    "Give the Developer Agent a relative target": ("Δ",),
    "Read the Measurement Report": ("amber", "rules loosened", "tests changed"),
    "Base runs": ("booley measure --all",),
    "Limits": ("Git LFS", "filter", "git", "advisory", "absolute path", "Heavy generated"),
}


def _text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _sections() -> dict[str, str]:
    parts = re.split(r"^## (.+)$", _text(), flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_guide_covers_each_topic_in_order() -> None:
    sections = _sections()
    assert list(sections) == HEADINGS
    for heading, terms in REQUIRED_TERMS.items():
        for term in terms:
            assert term in sections[heading], f"{heading!r} lacks {term!r}"


def test_guide_uses_the_glossary_terms() -> None:
    text = _text()
    for term in (
        "Metric-Driven Development",
        "Target Metrics",
        "Measurement Run",
        "Measured Commit",
        "Measurement Report",
        "Base",
    ):
        assert term in text
    assert "sign-off" not in text.lower()


def test_every_metrics_example_passes_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocks = re.findall(r"^```toml\n(.*?)^```", _text(), flags=re.M | re.S)
    assert blocks
    for number, block in enumerate(blocks):
        project = tmp_path / f"example{number}"
        targets = {name for name in tomllib.loads(block) if name != "defaults"}
        assert targets <= PROJECT_TARGETS, targets
        write_metrics_project(project, block)
        assert _measure(monkeypatch, project, "--validate") == 0, capsys.readouterr().err
