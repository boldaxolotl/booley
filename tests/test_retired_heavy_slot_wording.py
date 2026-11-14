"""Guard: nothing refers to the retired heavy Job Slot class or its switch and keys.

Memory admission is how EDA Jobs are admitted (ADR 0087). The heavy Job Slot
class, ``max_heavy``, ``heavy_memory`` and the development switch are gone. Only
the code and docs that explain the retirement may still name them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCANNED = ("src/", "tests/", "docs/", "qa/")
_RETIRED = re.compile(
    r"max_heavy|heavy_memory|BOOLEY_DEV_MEMORY_ADMISSION|CLASS_HEAVY|HeavyCapacity"
    r"|heavy slot|heavy job|job slots",
    re.IGNORECASE,
)

# Files that name the retired words because they implement, test, or record the retirement.
_WHOLE_FILES = frozenset(
    {
        "src/booley/config/jobs.py",
        "tests/config/test_jobs.py",
        "tests/harness/test_cli_retired_jobs_keys.py",
        "tests/harness/test_doctor.py",
        "tests/harness/test_doctor_retired_jobs_main_copy.py",
        "tests/harness/test_measure_command_flows.py",
        "tests/mcp_tools/test_baseline_goal_tools_retired_keys.py",
        "tests/mcp_tools/test_mcp_dispatch.py",
        "tests/test_retired_heavy_slot_wording.py",
        "CHANGELOG.md",
        "src/booley/data/refs/CHANGELOG.md",
        "changelog.d/3247.md",
    }
)
# ADRs record decisions as they stood.
_WHOLE_PREFIXES = ("docs/adr/",)
# Docs where only a paragraph that says the keys were retired or replaced may name them.
_RETIRED_PARAGRAPH_FILES = frozenset(
    {
        "docs/user/TROUBLESHOOTING.md",
        "docs/GLOSSARY.md",
        "src/booley/data/skills/booley-setup/steps/2-project-config.md",
    }
)
_EXPLAINS_RETIREMENT = re.compile(r"retire|replaced", re.IGNORECASE)
_GLOSSARY = _ROOT / "docs" / "GLOSSARY.md"


def _tracked_files() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", *_SCANNED],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return [name for name in listing.split("\0") if name]


def _read_text(relative: str) -> str | None:
    path = _ROOT / relative
    if not path.is_file():
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_allowed(relative: str) -> bool:
    return relative in _WHOLE_FILES or relative.startswith(_WHOLE_PREFIXES)


def test_no_heavy_slot_wording_outside_the_allowlist() -> None:
    offenders: list[str] = []
    for relative in _tracked_files():
        if _is_allowed(relative):
            continue
        text = _read_text(relative)
        if text is None:
            continue
        if relative in _RETIRED_PARAGRAPH_FILES:
            blocks = [b for b in re.split(r"\n\s*\n", text) if not _EXPLAINS_RETIREMENT.search(b)]
            text = "\n\n".join(blocks)
        offenders.extend(f"{relative}: {match.group(0)}" for match in _RETIRED.finditer(text))
    assert offenders == [], "retired heavy-slot wording found:\n" + "\n".join(offenders)


def _glossary_entry(name: str) -> str:
    """Return the text of one ``**Name**:`` glossary entry, up to the next blank line."""
    text = _GLOSSARY.read_text(encoding="utf-8")
    match = re.search(rf"^\*\*{re.escape(name)}\*\*:\n(.+?)(?:\n\n|\Z)", text, re.M | re.S)
    assert match is not None, f"GLOSSARY.md has no **{name}** entry"
    return match.group(1)


def test_job_glossary_entries_speak_of_the_memory_budget() -> None:
    for name in ("Job", "Standalone Job", "Job Class"):
        entry = _glossary_entry(name)
        assert "Memory Budget" in entry, f"**{name}** does not name the Memory Budget"
        assert "slot" not in entry.replace("light slot", ""), f"**{name}** speaks of slots"
