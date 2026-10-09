"""Keep QA missions and skills pointing at files that exist."""

import json
import re
from pathlib import Path

import pytest

from booley.goals.model import parse_goal_args
from booley.goals.translate import translate_goals

QA_ROOT = Path(__file__).resolve().parents[2] / "qa"
MISSIONS = sorted(QA_ROOT.glob("missions/*/MISSION.md"))

# Backticked mission-relative paths such as `fixtures/stealth.md` or
# `../../shared/coverage/RUNBOOK.md`; commands and globs are skipped.
ASSET_PREFIXES = (
    "fixtures/",
    "prompts/",
    "goals/",
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


@pytest.mark.parametrize("skill", sorted(QA_ROOT.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_installable_skill_markdown_links_do_not_escape_skill_directory(skill: Path):
    links = {link for link in MARKDOWN_LINK.findall(skill.read_text()) if "://" not in link}
    escaping = sorted(link for link in links if ".." in Path(link).parts)
    assert not escaping, f"{skill.relative_to(QA_ROOT)} has escaping links: {escaping}"


def test_qa_skills_derive_repository_inputs_from_loaded_skill_path():
    combined = "\n".join(path.read_text() for path in sorted(QA_ROOT.glob("*/SKILL.md")))
    for required in (
        "real path of this loaded `SKILL.md`",
        "qa/DISK.md",
        "qa/AREAS.md",
        "qa/SMOKE.md",
        "qa/missions/",
    ):
        assert required in combined


def test_qa_run_uses_the_canonical_host_install():
    skill = (QA_ROOT / "booley-qa-run" / "SKILL.md").read_text()
    hard_rules = skill.split("## Hard rules", 1)[1].split("## Run directory", 1)[0]
    install_step = skill.split("2. **", 1)[1].split("\n3. **", 1)[0]
    compact_hard_rules = " ".join(hard_rules.split())
    compact = " ".join(install_step.split())

    assert "venv" not in compact
    assert "may change only through step 2's Human Maintainer-approved" in compact_hard_rules
    assert "replacement stays installed after the run" in compact_hard_rules
    assert "is not a `resources.md` row" in compact_hard_rules
    for required in (
        "git fetch origin main",
        "git rev-parse origin/main",
        "booley --version",
        "test the installed build",
        "install `origin/main` as the canonical host install",
        "booley bootstrap",
        "Record the choice in `log.md`",
    ):
        assert required in compact


JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
GOAL_FIXTURES = sorted(QA_ROOT.glob("missions/*/goals/*.md")) + sorted(
    QA_ROOT.glob("shared/*/goals/*.md")
)
AD_HOC_FIXTURES = sorted(QA_ROOT.glob("missions/*/goals/*.json")) + sorted(
    QA_ROOT.glob("shared/*/goals/*.json")
)


@pytest.mark.parametrize("fixture", GOAL_FIXTURES, ids=lambda p: str(p.relative_to(QA_ROOT)))
def test_goalsets_parse_and_translate(fixture: Path) -> None:
    markdown = fixture.read_text(encoding="utf-8")
    assert markdown.startswith(f"# Goalset: {fixture.stem}\n")
    assert "## Goals" in markdown
    blocks = JSON_BLOCK.findall(markdown)
    assert len(blocks) == 1
    raw = json.loads(blocks[0].replace("<target>", "qa_target").replace("<spec>", "docs/spec.md"))
    args = parse_goal_args(raw)
    translated = translate_goals(args)
    assert translated.goals
    assert all(arg.origin == fixture.stem for arg in args)
    assert all(goal.origins == (fixture.stem,) for goal in translated.goals)


@pytest.mark.parametrize("fixture", AD_HOC_FIXTURES, ids=lambda p: str(p.relative_to(QA_ROOT)))
def test_ad_hoc_goals_parse_and_translate(fixture: Path) -> None:
    raw = json.loads(
        fixture.read_text(encoding="utf-8")
        .replace("<target>", "qa_target")
        .replace("<spec>", "docs/spec.md")
    )
    translated = translate_goals(parse_goal_args(raw))
    assert translated.goals
    assert all(goal.origins == ("ad-hoc",) for goal in translated.goals)


def _heading_anchor(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def test_capability_map_links_resolve_to_mission_areas() -> None:
    document = QA_ROOT / "AREAS.md"
    links = re.findall(r"\[([^]]+)\]\(([^)]+)\)", document.read_text(encoding="utf-8"))
    assert links
    for label, link in links:
        path, anchor = link.split("#", 1)
        mission = document.parent / path
        assert mission.is_file(), link
        headings = re.findall(r"^### (.+)$", mission.read_text(encoding="utf-8"), re.MULTILINE)
        matches = [heading for heading in headings if _heading_anchor(heading) == anchor]
        assert len(matches) == 1, link
        assert label.split("/", 1)[1] == matches[0].split()[1], label
