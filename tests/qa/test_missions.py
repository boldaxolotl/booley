"""Keep QA missions and skills pointing at files that exist."""

import json
import re
from pathlib import Path

import pytest

from booley.goals.model import GoalArg, parse_goal_args
from booley.goals.translate import translate_goals
from booley.runtime.paths import skills_dir
from booley.runtime.qa_skill_selection import QA_SKILL_NAMES

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
    assert {path.parent.name for path in MISSIONS} == {
        "coverage",
        "gui",
        "mutation",
        "picorv32",
        "taxi",
        "uart",
    }


@pytest.mark.parametrize("mission", MISSIONS, ids=lambda path: path.parent.name)
def test_mission_asset_references_exist(mission: Path):
    text = mission.read_text()
    links = {link for link in MARKDOWN_LINK.findall(text) if "://" not in link}
    missing = sorted(
        ref for ref in _asset_references(text) | links if not (mission.parent / ref).exists()
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


def _goal_args(text: str) -> tuple[GoalArg, ...]:
    raw = json.loads(text.replace("<target>", "qa_target").replace("<spec>", "docs/spec.md"))
    return parse_goal_args(raw)


@pytest.mark.parametrize("fixture", GOAL_FIXTURES, ids=lambda p: str(p.relative_to(QA_ROOT)))
def test_goalsets_parse_and_translate(fixture: Path) -> None:
    markdown = fixture.read_text(encoding="utf-8")
    assert markdown.startswith(f"# Goalset: {fixture.stem}\n")
    assert "## Goals" in markdown
    blocks = JSON_BLOCK.findall(markdown)
    assert len(blocks) == 1
    args = _goal_args(blocks[0])
    translated = translate_goals(args)
    assert translated.goals
    assert all(arg.origin == fixture.stem for arg in args)
    assert all(goal.origins == (fixture.stem,) for goal in translated.goals)


@pytest.mark.parametrize("fixture", AD_HOC_FIXTURES, ids=lambda p: str(p.relative_to(QA_ROOT)))
def test_ad_hoc_goals_parse_and_translate(fixture: Path) -> None:
    translated = translate_goals(_goal_args(fixture.read_text(encoding="utf-8")))
    assert translated.goals
    assert all(goal.origins == ("ad-hoc",) for goal in translated.goals)


@pytest.mark.parametrize(
    "fixture",
    [*GOAL_FIXTURES, *AD_HOC_FIXTURES],
    ids=lambda p: str(p.relative_to(QA_ROOT)),
)
def test_mutation_scopes_name_source_files(fixture: Path) -> None:
    text = fixture.read_text(encoding="utf-8")
    payload = JSON_BLOCK.findall(text)[0] if fixture.suffix == ".md" else text
    for goal in translate_goals(_goal_args(payload)).goals:
        if goal.family.value == "mutation":
            for entry in goal.params["scope"]:
                assert not entry.endswith(("/", "\\")), entry
                assert Path(entry).suffix in {".v", ".sv", ".vhd", ".vhdl"}, entry


def test_uart_review_contract_contains_frozen_spec_text() -> None:
    spec = QA_ROOT / "missions/uart/spec"
    contract = (spec / "contract.md").read_text(encoding="utf-8")
    assert (spec / "corpus/hw/ip/uart/doc/registers.md").read_text(encoding="utf-8") in contract
    assert (spec / "mmio-addendum.md").read_text(encoding="utf-8") in contract
    assert len(contract) <= 30_000


def test_capability_map_links_resolve_to_mission_areas() -> None:
    document = QA_ROOT / "AREAS.md"
    links = re.findall(
        r"\[([^]]+)\]\(([^)]+)\)( \(optional\))?", document.read_text(encoding="utf-8")
    )
    assert links
    for label, link, marker in links:
        assert "#" not in link, link
        mission = document.parent / link
        assert mission.is_file(), link
        headings = dict(
            re.findall(
                r"^### \d+\. ([\w-]+) —(.*)$", mission.read_text(encoding="utf-8"), re.MULTILINE
            )
        )
        area = label.split("/", 1)[1]
        assert area in headings, label
        # The map marks exactly the areas the mission schedules outside its primary timebox.
        assert bool(marker) == ("optional follow-up" in headings[area]), label


def test_qa_skill_names_do_not_overlap_packaged_product_skills() -> None:
    assert all((QA_ROOT / name / "SKILL.md").is_file() for name in QA_SKILL_NAMES)
    assert QA_SKILL_NAMES.isdisjoint(path.name for path in skills_dir().iterdir())


AREA_HEADING = re.compile(r"^### (\d+)\. ([\w-]+) — .*\(~(\d+) min", re.MULTILINE)


def _areas(mission: Path) -> list[tuple[str, int]]:
    """Return each area's slug and timebox minutes, in mission order."""
    text = mission.read_text(encoding="utf-8")
    headings = AREA_HEADING.findall(text)
    # Every `###` heading under "## Areas" is an area with a number and a timebox.
    areas_section = text.split("## Areas", 1)[1].split("\n## ", 1)[0]
    assert len(headings) == areas_section.count("\n### "), mission
    assert [int(number) for number, _, _ in headings] == list(range(1, len(headings) + 1))
    return [(slug, int(minutes)) for _, slug, minutes in headings]


@pytest.mark.parametrize("mission", MISSIONS, ids=lambda path: path.parent.name)
def test_mission_areas_are_numbered_and_timeboxed(mission: Path) -> None:
    slugs = [slug for slug, _ in _areas(mission)]
    assert len(slugs) == len(set(slugs)), mission


def test_gui_mission_follows_the_shared_runbook_in_issue_order() -> None:
    mission = QA_ROOT / "missions" / "gui" / "MISSION.md"
    areas = _areas(mission)
    assert [slug for slug, _ in areas] == [
        "setup",
        "dashboard-default",
        "dashboard-live",
        "bwave-gui",
        "stop-resume",
        "terminal-output",
        "goal-finish",
        "live-preview",
        "matrix",
        "docs-site",
        "cleanup",
    ]
    assert sum(minutes for _, minutes in areas) <= 240  # the mission's 4-hour budget
    assert "](../../shared/codespaces/RUNBOOK.md)" in mission.read_text(encoding="utf-8")


def test_gui_check_skill_follows_the_runbook_and_posts_through_the_guard() -> None:
    skill = (QA_ROOT / "booley-gui-check" / "SKILL.md").read_text(encoding="utf-8")
    assert "booley-gui-check" in QA_SKILL_NAMES
    assert (QA_ROOT / "shared" / "codespaces" / "RUNBOOK.md").is_file()
    assert "qa/shared/codespaces/RUNBOOK.md" in skill
    # The PR comment goes through the confidential-content publisher, never raw `gh`.
    assert "confidential_content_guard.py --repo . publish-pr comment" in skill


MUTATION_AREAS = [
    "mut-determinism",
    "mut-sites",
    "mut-selection",
    "mut-scoring",
    "mut-timeouts",
    "mut-scope",
    "mut-failures",
    "mut-removed",
    "cleanup",
]


def _area_text(mission: Path, slug: str) -> str:
    """Return one area's text, from its `###` heading to the next heading."""
    text = mission.read_text(encoding="utf-8")
    sections = re.split(r"^#{2,3} ", text, flags=re.MULTILINE)
    (section,) = [part for part in sections if re.match(rf"\d+\. {slug} ", part)]
    return section


def test_mutation_mission_areas_follow_the_brief() -> None:
    mission = QA_ROOT / "missions" / "mutation" / "MISSION.md"
    assert mission.is_file(), "qa/missions/mutation/MISSION.md is missing"
    areas = _areas(mission)
    assert [slug for slug, _ in areas] == MUTATION_AREAS
    assert sum(minutes for _, minutes in areas) <= 480  # the mission's 8-hour budget
    assert "8 h total" in mission.read_text(encoding="utf-8")


def test_mutation_mission_is_indexed() -> None:
    mission = QA_ROOT / "missions" / "mutation" / "MISSION.md"
    assert mission.is_file(), "qa/missions/mutation/MISSION.md is missing"
    readme = (QA_ROOT / "README.md").read_text(encoding="utf-8")
    assert "](missions/mutation/MISSION.md)" in readme
    assert "](shared/mutation/RUNBOOK.md)" in readme
    row = next(
        line
        for line in (QA_ROOT / "AREAS.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| Mutation testing |")
    )
    linked = set(re.findall(r"\[mutation/(mut-[\w-]+)\]", row))
    assert linked == {slug for slug in MUTATION_AREAS if slug.startswith("mut-")}


# Words of the removed model-driven mutation flow and of its old dry-run wording.
RETIRED_MUTATION_WORDS = re.compile(r"\b(lock|regen\w*|steer\w*|LLM|proposes|same seed)\b")


def test_uart_and_taxi_mutation_areas_describe_the_mutation_flow() -> None:
    areas = {
        "uart": _area_text(QA_ROOT / "missions" / "uart" / "MISSION.md", "reviews-mutation"),
        "taxi": _area_text(QA_ROOT / "missions" / "taxi" / "MISSION.md", "goal-observability"),
    }
    stale = {
        name: sorted(set(RETIRED_MUTATION_WORDS.findall(text))) for name, text in areas.items()
    }
    assert not {name: words for name, words in stale.items() if words}, (
        "stale mutation wording (sizing, seeds and selection belong to the mutation mission)"
    )
