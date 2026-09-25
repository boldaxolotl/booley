"""Keep QA missions and skills pointing at files that exist."""

import re
from pathlib import Path

import pytest
import yaml

from booley.ticket_board import generate_slug

QA_ROOT = Path(__file__).resolve().parents[2] / "qa"
MISSIONS = sorted(QA_ROOT.glob("missions/*/MISSION.md"))

# Backticked mission-relative paths such as `fixtures/stealth.md` or
# `../../shared/coverage/RUNBOOK.md`; commands and globs are skipped.
ASSET_PREFIXES = (
    "fixtures/",
    "prompts/",
    "tickets/",
    "spec/",
    "evaluator/",
    "probes/",
    "../../shared/",
)
BACKTICKED = re.compile(r"`([^`\s]+)`")
MARKDOWN_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def _asset_references(text: str) -> set[str]:
    """Return backticked asset paths without glob or placeholder characters."""
    return {
        token.rstrip("/")
        for token in BACKTICKED.findall(text)
        if token.startswith(ASSET_PREFIXES) and not any(ch in token for ch in "*<>{}$")
    }


def test_every_mission_is_present():
    assert {path.parent.name for path in MISSIONS} == {"coverage", "picorv32", "taxi", "uart"}


@pytest.mark.parametrize("mission", MISSIONS, ids=lambda path: path.parent.name)
def test_mission_asset_references_exist(mission: Path):
    missing = sorted(
        ref
        for ref in _asset_references(mission.read_text())
        if not (mission.parent / ref).exists()
    )
    assert not missing, f"{mission.relative_to(QA_ROOT)} references missing assets: {missing}"


@pytest.mark.parametrize(
    "document",
    [*sorted(QA_ROOT.glob("*/SKILL.md")), *sorted(QA_ROOT.glob("*.md"))],
    ids=lambda path: str(path.relative_to(QA_ROOT)),
)
def test_relative_links_resolve(document: Path):
    links = {link for link in MARKDOWN_LINK.findall(document.read_text()) if "://" not in link}
    missing = sorted(link for link in links if not (document.parent / link).exists())
    assert not missing, f"{document.relative_to(QA_ROOT)} links to missing files: {missing}"


def _ticket_frontmatter(document: str) -> dict:
    """Parse the YAML front matter of the fenced Ticket packet inside a payload file."""
    packet = document.split("```markdown\n---\n", 1)[1].split("\n---\n", 1)[0]
    # Placeholder lines are rendered per run; the identity fields never use them.
    return yaml.safe_load("\n".join(line for line in packet.splitlines() if "{{" not in line))


def test_picorv32_ticket_dependency_matches_derived_slug():
    tickets = QA_ROOT / "missions" / "picorv32" / "tickets"
    provider = _ticket_frontmatter((tickets / "continuity.md").read_text())
    consumer = _ticket_frontmatter((tickets / "evolution.md").read_text())
    assert consumer["dependencies"] == [generate_slug(provider["summary"])]
