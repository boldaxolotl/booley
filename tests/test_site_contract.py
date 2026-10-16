"""Project site wording contracts, and the facts it must share with the README.

The site (website/docs/) owns the full feature tour; the README keeps a summary.
The install commands and the pinned PicoRV32 demo revision appear in both, so
these tests fail when either copy drifts from the other.
"""

import re
import tomllib
from pathlib import Path

from booley.goals.model import parse_goal_args
from booley.goals.presentation import goal_presentation
from booley.goals.translate import translate_goals

ROOT = Path(__file__).resolve().parent.parent
SITE = (ROOT / "website/docs/index.md").read_text(encoding="utf-8")
DEMOS = (ROOT / "website/docs/demos.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _site_section(heading: str) -> str:
    """Return the body of one ``## HEADING`` section of the landing page."""
    return SITE.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]


def test_integrated_development_environment_features_lead_with_typed_flows():
    section = SITE.split("### Integrated development environment", 1)[1].split("\n### ", 1)[0]
    features = (
        "**A typed interface for each Booley Flow:**",
        "**Reproducible team environment:**",
        "**One Window:**",
    )

    assert [section.index(feature) for feature in features] == sorted(
        section.index(feature) for feature in features
    )
    assert "**One IDE:**" not in section


def test_inline_goal_examples_match_the_real_goal_presentation():
    """The advertised examples must match validated Goals' public notation."""
    examples = [
        {
            "family": "SYNTH",
            "target": "synth_fifo",
            "thresholds": {"area_increase_at_most": "10%"},
        },
        {
            "family": "CYCLE_COUNT",
            "target": "sim_fifo",
            "test": "smoke",
            "thresholds": {"cycle_count_max": 1000},
        },
        {"family": "COVERAGE", "target": "sim_fifo", "tests": "all", "metrics": {"line": 90}},
        {
            "family": "MUTATION",
            "target": "sim_fifo",
            "scope": ["rtl/fifo.sv"],
            "total": 10,
            "min_detected": 10,
        },
    ]
    translation = translate_goals(parse_goal_args(examples))
    assert translation.warnings == ()
    for goal in translation.goals:
        assert f"`{goal_presentation(goal).text}`" in _site_section("FEATURES")


def test_quick_start_commands_match_the_readme_install_block():
    readme_block = README.split("<!-- booley-smoke:install -->", 1)[1].split(
        "<!-- /booley-smoke:install -->", 1
    )[0]
    readme_commands = re.findall(r"^(?!```)(\S.*)$", readme_block, flags=re.MULTILINE)
    site_commands = re.findall(r"^\$ (.+)$", _site_section("QUICK START"), flags=re.MULTILINE)

    assert site_commands == readme_commands == ["pipx install booley-rtl", "booley bootstrap"]


def test_description_is_the_readme_intro_paragraph():
    readme_intro = README.split("Booley turns Claude Code or Codex", 1)[1].split("\n\n", 1)[0]
    site_intro = _site_section("DESCRIPTION").split("Booley turns Claude Code or Codex", 1)[1]

    assert " ".join(site_intro.split()) == " ".join(readme_intro.split())


def test_demo_link_uses_the_contract_pinned_revision():
    contract = ROOT / ".github/contracts/picorv32-demo.toml"
    pin = tomllib.loads(contract.read_text())["project_ref"]

    assert f"https://github.com/boldaxolotl/booley-prj-picorv32/blob/{pin}/README.md" in DEMOS


def test_readme_links_resolve_to_site_sections():
    """Every README link into the landing page names a section that exists."""
    anchors = re.findall(r"https://boldaxolotl\.github\.io/booley/#([a-z-]+)", README)
    headings = {
        heading.strip().lower().replace(" ", "-")
        for heading in re.findall(r"^##+ (.+)$", SITE, flags=re.MULTILINE)
    }

    assert anchors
    assert set(anchors) <= headings
