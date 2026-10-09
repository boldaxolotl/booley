"""Goalset seeding at ``booley init`` (ADR 0067 Goalsets).

Covers the create-only contract of :func:`booley.goals.goalsets.seed_goalsets`
and the round trip from a rendered Goalset to translated Goals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from booley.criteria.templates import TEMPLATE_REGISTRY, CriterionSpec
from booley.goals.goalsets import (
    GOALSETS_DIR,
    SEEDED_GOALSETS,
    GoalsetTemplateError,
    render_goalset,
    seed_goalsets,
)
from booley.goals.model import GoalFamily, parse_goal_args
from booley.goals.translate import translate_goals
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime.guarded_write import WriteOutcome

EXPECTED_FILES = {f"{name}.md" for name in SEEDED_GOALSETS}
_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _goal_arguments(markdown: str, *, targets: list[str], spec: str = "docs/spec.md") -> list:
    """Extract the one JSON block and expand it as the agent would.

    Each Goal naming ``<target>`` is repeated once per Target, and ``<spec>``
    becomes a real spec path.
    """
    blocks = _JSON_BLOCK.findall(markdown)
    assert len(blocks) == 1, "a Goalset carries exactly one Goal argument block"
    expanded = []
    for raw_goal in json.loads(blocks[0]):
        goal = {**raw_goal, "spec": spec} if raw_goal.get("spec") == "<spec>" else raw_goal
        if goal.get("target") == "<target>":
            expanded.extend({**goal, "target": target} for target in targets)
        else:
            expanded.append(goal)
    return expanded


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SEEDED_GOALSETS)
def test_rendered_goalset_round_trips_through_parse_and_translate(name: str) -> None:
    markdown = render_goalset(name, TEMPLATE_REGISTRY[name])
    args = parse_goal_args(_goal_arguments(markdown, targets=["core", "soc_top"]))
    translation = translate_goals(args)

    assert translation.warnings == ()
    assert {arg.origin for arg in args} == {name}
    per_target = [spec for spec in TEMPLATE_REGISTRY[name] if spec.per_target]
    reviews = [spec for spec in TEMPLATE_REGISTRY[name] if not spec.per_target]
    assert len(translation.goals) == 2 * len(per_target) + len(reviews)
    assert {goal.key for goal in translation.goals if goal.family is GoalFamily.REVIEW} == {
        spec.name for spec in reviews
    }


def test_rendered_goalset_states_ownership_and_placeholder_rule() -> None:
    markdown = render_goalset("feature", TEMPLATE_REGISTRY["feature"])

    assert markdown.startswith("# Goalset: feature\n")
    assert "belongs to the Project" in markdown
    assert "every Target the change touches" in markdown
    assert "`<target>`" in markdown


def test_optional_criteria_are_omitted() -> None:
    specs = [
        CriterionSpec("sim_pass", per_target=True),
        CriterionSpec("lint_clean", mandatory=False, per_target=True),
    ]
    goals = _goal_arguments(render_goalset("custom", specs), targets=["core"])

    assert [goal["family"] for goal in goals] == ["sim"]


def test_sim_goal_names_only_a_target() -> None:
    markdown = render_goalset("bugfix", TEMPLATE_REGISTRY["bugfix"])

    assert _goal_arguments(markdown, targets=["core"]) == [
        {"family": "sim", "target": "core", "origin": "bugfix"}
    ]


def test_spec_review_and_thresholds_round_trip() -> None:
    specs = [
        CriterionSpec("synthesis_ok", per_target=True, params={"area_kge_max": 120}),
        CriterionSpec("review_rtl_spec_done"),
    ]
    markdown = render_goalset("custom", specs)
    translation = translate_goals(parse_goal_args(_goal_arguments(markdown, targets=["core"])))

    assert [goal.key for goal in translation.goals] == [
        "synthesis_ok_core",
        "review_rtl_spec_done",
    ]
    assert translation.goals[0].params["area_kge_max"] == 120
    assert translation.goals[1].params["spec"] == "docs/spec.md"
    assert "`<spec>`" in markdown


@pytest.mark.parametrize(
    ("name", "specs"),
    [
        ("custom", [CriterionSpec("coverage", per_target=True)]),
        ("custom", [CriterionSpec("lint_clean", per_target=True, params={"x": 1})]),
        ("custom", [CriterionSpec("lint_clean", mandatory=False, per_target=True)]),
        ("../escape", [CriterionSpec("sim_pass", per_target=True)]),
    ],
)
def test_unrenderable_input_fails_loudly(name: str, specs: list[CriterionSpec]) -> None:
    with pytest.raises(GoalsetTemplateError):
        render_goalset(name, specs)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_writes_exactly_the_four_goalsets(tmp_path: Path) -> None:
    results = seed_goalsets(tmp_path, dry_run=False)

    goalsets = tmp_path / GOALSETS_DIR
    assert {path.name for path in goalsets.iterdir()} == EXPECTED_FILES
    assert not (goalsets / "default.md").exists()
    assert [outcome for _, outcome in results] == [WriteOutcome.WRITTEN] * 4
    for name in SEEDED_GOALSETS:
        expected = render_goalset(name, TEMPLATE_REGISTRY[name])
        assert (goalsets / f"{name}.md").read_bytes() == expected.encode("utf-8")


def test_seed_skips_an_existing_goalset_and_leaves_it_untouched(tmp_path: Path) -> None:
    goalsets = tmp_path / GOALSETS_DIR
    goalsets.mkdir()
    own = goalsets / "feature.md"
    own.write_text("# Our feature Goals\n", encoding="utf-8")

    outcomes = {path.name: outcome for path, outcome in seed_goalsets(tmp_path, dry_run=False)}

    assert own.read_text(encoding="utf-8") == "# Our feature Goals\n"
    assert outcomes["feature.md"] is WriteOutcome.SKIPPED
    assert {outcomes[f"{n}.md"] for n in ("bugfix", "refactor", "verification")} == {
        WriteOutcome.WRITTEN
    }
    # A second run finds every Goalset present and rewrites none of them.
    assert {outcome for _, outcome in seed_goalsets(tmp_path, dry_run=False)} == {
        WriteOutcome.SKIPPED
    }
    assert own.read_text(encoding="utf-8") == "# Our feature Goals\n"


def test_seed_dry_run_writes_nothing_not_even_the_directory(tmp_path: Path) -> None:
    results = seed_goalsets(tmp_path, dry_run=True)

    assert [outcome for _, outcome in results] == [WriteOutcome.WRITTEN] * 4
    assert not (tmp_path / GOALSETS_DIR).exists()


# ---------------------------------------------------------------------------
# The init hook
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    path = tmp_path / ".booley_project"
    path.mkdir()
    return path


def _backfill(project_dir: Path, *, check_only: bool) -> None:
    ctx = InitContext(project_root=project_dir.parent, check_only=check_only)
    init_cmd._backfill_config_skeletons(project_dir, ctx)


def test_init_seeds_the_goalsets(
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:

    _backfill(project_dir, check_only=False)

    assert {path.name for path in (project_dir / GOALSETS_DIR).iterdir()} == EXPECTED_FILES
    # booley.toml, tests.toml, and the four Goalsets.
    assert "added 6 config skeleton file(s)" in capsys.readouterr().out


def test_init_check_only_writes_nothing(
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:

    _backfill(project_dir, check_only=True)

    assert list(project_dir.iterdir()) == []
    assert "would add 6 config skeleton file(s)" in capsys.readouterr().out
