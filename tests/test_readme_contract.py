"""User-facing README wording and ordering contracts."""

import json
from pathlib import Path

from booley.goals.model import GoalFamily, parse_goal_args
from booley.goals.translate import translate_goals

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_integrated_development_environment_features_lead_with_one_window():
    section = README.split("## Integrated Development Environment", 1)[1].split("\n## ", 1)[0]
    features = (
        "**One Window:**",
        "**Reproducible team environment:**",
        "**A typed interface for each Booley Flow:**",
    )

    assert [section.index(feature) for feature in features] == sorted(
        section.index(feature) for feature in features
    )
    assert "**One IDE:**" not in section


def test_install_alternative_is_not_padded():
    section = README.split("**Alternative: pip user install**", 1)[1]
    assert "only for interpreters that permit user" in section
    assert "python3 -m pip install --user booley-rtl" in section


def test_primary_pipx_install_and_upgrade_are_pinned():
    assert "pipx install booley-rtl\nbooley bootstrap\n" in README
    assert "pipx upgrade booley-rtl\nbooley bootstrap --update\n" in README
    assert README.index("**Reopen your terminal**") < README.index("pipx install booley-rtl")
    assert "py -m pip install --user pipx" in README
    assert "uv tool install booley-rtl" in README
    assert "uv tool upgrade booley-rtl" in README


def test_try_the_demo_leads_with_the_demo_readme_link():
    section = README.split("### Level 2: Try the demo yourself", 1)[1].split("\n### ", 1)[0]

    assert section.strip() == (
        "**[Follow the demo repository's README]"
        "(https://github.com/boldaxolotl/booley-prj-picorv32#readme)** "
        "to try the demo, after you [install Booley](#installation)."
    )


def test_goalset_example_translates_through_the_real_goal_boundary():
    """The Goalset excerpt must produce the advertised mandatory Goals."""
    example = README.split("```json\n", 1)[1].split("```", 1)[0]
    translation = translate_goals(parse_goal_args(json.loads(example)))

    assert translation.warnings == ()
    assert {goal.family for goal in translation.goals} == {
        GoalFamily.LINT,
        GoalFamily.SIM,
        GoalFamily.COVERAGE,
        GoalFamily.REVIEW,
        GoalFamily.SYNTH,
    }
    assert all(criterion.mandatory for criterion in translation.criteria)
    assert all(goal.origins == ("fifo-feature",) for goal in translation.goals)
    coverage = next(goal for goal in translation.goals if goal.family is GoalFamily.COVERAGE)
    synthesis = next(goal for goal in translation.goals if goal.family is GoalFamily.SYNTH)
    assert coverage.target == "sim_fifo"
    assert synthesis.target == "synth_fifo"


def test_installation_names_host_agent_cli_prerequisite():
    section = README.split("## Installation", 1)[1].split("\n## ", 1)[0]
    assert "host agent CLI on PATH for Project Setup" in section
    assert "[Claude Code](https://code.claude.com/docs/en/setup)" in section
    assert "[Codex](https://developers.openai.com/codex/cli)" in section
