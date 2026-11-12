"""Loader for captured real Yosys artifacts stored under ``fixtures/``."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_capture(name: str) -> dict[str, str]:
    """Split a capture into ``{artifact file name: text}`` by its ``--- name ---`` headers."""
    sections: dict[str, str] = {}
    current = ""
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("--- ") and line.rstrip().endswith(" ---"):
            current = line.strip("- \n")
            sections[current] = ""
        else:
            sections[current] += line
    return sections


def diagnostic_sources(name: str) -> dict[str, str]:
    """Return the stage-labelled sources the synthesis pipeline builds from a capture."""
    sections = load_capture(name)
    sources: dict[str, str] = {}
    for file_name, text in sections.items():
        if file_name == "yosys.log":
            sources["yosys"] = text
        elif file_name.startswith("premap_check_"):
            sources["premap_check"] = text
        elif file_name.startswith("check_"):
            sources["final_check"] = text
    return sources
