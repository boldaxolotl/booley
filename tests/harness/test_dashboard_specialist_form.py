"""The reviewer's hand-made Run form (#3816): its focuses, rows, command words and drift guard.

Pure model tests; the wizard pilots are in ``test_dashboard_run_specialist.py``.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path

import pytest

from booley.flows.endpoint_cli import human_endpoint_parser
from booley.mcp.registry import discover_mcp_inventory
from booley.specialists.reviewer import (
    RTL_FOCUS_CATEGORIES,
    TB_FOCUS_CATEGORIES,
    ReviewerSpecialist,
)
from booley.specialists.specialist import Specialist

REVIEWER = "reviewer"
# Every value the form can hold, the More section open.
FULLEST = {
    "category": "rtl",
    "focus": "spec",
    "spec": "docs/spec.md",
    "scope": "fifo.v,ctl.v",
    "instruction": "mind resets",
    "more": "on",
    "max_turns": "12",
    "timeout": "45m",
}

COVERAGE_FULLEST = {
    "campaign": ".booley/flow-reports/sim/3/targets/sim_fifo/coverage.json",
    "instruction": "focus the FIFO",
    "more": "on",
    "max_turns": "9",
    "timeout": "20m",
}

# The options of a built-in Specialist's parser that its form does not write, each with the reason.
# Every built-in Specialist has a form (FORMS); a new one fails the drift guard until it gets one.
HIDDEN: dict[str, dict[str, str]] = {
    REVIEWER: {
        "-C": "the worktree chooses the Project",
        "--project": "the worktree chooses the Project",
        "--work-dir": "the worktree chooses the Project",
        "--timeout-ms": "hidden alias of --timeout, which the form writes",
        "--report-dir": "the Job writes to its default report directory",
        "--diagnostic": "a Standalone Job never records Goal evidence",
        "--transcript-dir": "the Job worker sets it beside job.log",
        "--verify": "continues the Developer Agent's own review loop, not a first review",
        "--waive": "continues the Developer Agent's own review loop, not a first review",
        "--dry-run": "a Job is a real run; Review already checks the command",
        "--base": "not on the form yet: a later ticket can add it",
        "--steer": "not on the form yet: a later ticket can add it",
        "--spec-text": "not on the form yet: the spec path covers the form's use",
        "--fail-on": "not on the form yet: a later ticket can add it",
    },
    "coverage_analyst": {
        "-C": "the worktree chooses the Project",
        "--project": "the worktree chooses the Project",
        "--work-dir": "the worktree chooses the Project",
        "--timeout-ms": "hidden alias of --timeout, which the form writes",
        "--report-dir": "the Job writes to its default report directory",
        "--diagnostic": "a Standalone Job never records Goal evidence",
        "--transcript-dir": "the Job worker sets it beside job.log",
    },
}
# Each built-in Specialist's form builder and the fullest values its form can hold.
FORMS: dict[str, dict[str, str]] = {REVIEWER: FULLEST, "coverage_analyst": COVERAGE_FULLEST}


def reviewer_form():
    """The reviewer's form. Imported here so a Dashboard without it fails the test body, not collection."""
    from booley.harness.dashboard.specialist_form import ReviewerForm

    return ReviewerForm("docs/spec.md")


def coverage_form():
    from booley.harness.dashboard.specialist_form import CoverageAnalystForm

    return CoverageAnalystForm()


def form_of(name: str):
    """The hand-made form of the built-in Specialist *name*."""
    return reviewer_form() if name == REVIEWER else coverage_form()


def option_strings(specialist: Specialist) -> set[str]:
    """Every option spelling of *specialist*'s human parser except help."""
    parser = human_endpoint_parser(specialist)
    return set(parser._option_string_actions) - {"-h", "--help"}


def written_options(values: dict[str, str], name: str = REVIEWER) -> set[str]:
    """The option spellings the form of *name* writes for *values*."""
    words = form_of(name).argv(values)
    return {word.split("=")[0] for word in words if word.startswith("--")}


def unplaced(
    options: Collection[str], placed: Collection[str], hidden: Collection[str]
) -> list[str]:
    """What the drift guard reports: options nobody placed or hid, and hidden ones that are gone."""
    problems = [
        f"{option} is neither on the form nor hidden"
        for option in sorted(set(options) - set(placed) - set(hidden))
    ]
    problems += [
        f"{option} is hidden but the parser lacks it"
        for option in sorted(set(hidden) - set(options))
    ]
    return problems


# --- AC3 -------------------------------------------------------------------------------------------


def test_focus_choices_follow_the_category():
    from booley.harness.dashboard.specialist_form import focuses

    form = reviewer_form()
    assert focuses("rtl") == ("spec", "bugs", "protocol", "security", "optimization", "code_style")
    assert set(focuses("rtl")) == RTL_FOCUS_CATEGORIES
    assert focuses("tb") == ("quality",)
    assert set(focuses("tb")) == TB_FOCUS_CATEGORIES
    base = {"category": "rtl", "scope": "a.v"}
    # A category with one focus selects it; a focus the category lacks is dropped.
    assert form.changed({**base, "category": "tb", "focus": ""})["focus"] == "quality"
    assert form.changed({**base, "category": "tb", "focus": "bugs"})["focus"] == "quality"
    assert form.changed({**base, "focus": "quality"})["focus"] == ""
    assert form.changed({**base, "focus": "bugs"})["focus"] == "bugs"
    focus_rows = {
        category: next(f for f in form.fields({"category": category}) if f.dest == "focus")
        for category in ("rtl", "tb")
    }
    assert focus_rows["tb"].values() == ("quality",)
    assert focus_rows["rtl"].shown("") == ""  # required: no "default" spelling


def test_spec_path_row_shows_only_for_the_spec_focus():
    form = reviewer_form()

    def labels(values: dict[str, str]) -> list[str]:
        return [field.label for field in form.fields(values)]

    closed = ["Category", "Focus", "Scope", "Instruction", "More"]
    assert labels({"category": "rtl"}) == closed
    assert labels({"category": "rtl", "focus": "bugs"}) == closed
    assert labels({"category": "rtl", "focus": "spec"}) == [
        "Category",
        "Focus",
        "Spec path",
        "Scope",
        "Instruction",
        "More",
    ]
    assert labels({"category": "rtl", "more": "on"}) == [*closed, "Max turns", "Timeout"]
    more = next(f for f in form.fields({"category": "rtl"}) if f.dest == "more")
    assert (more.shown(""), more.shown("on")) == ("▸ max turns, timeout", "▾")
    # A stale prefill never reaches another focus's command.
    values = {"category": "rtl", "focus": "bugs", "spec": "docs/spec.md", "scope": "a.v"}
    assert form.argv(values) == ("--category", "rtl", "--focus", "bugs", "--scope", "a.v")


# --- AC5 -------------------------------------------------------------------------------------------


def test_verify_waive_and_dry_run_cannot_be_set_from_the_form():
    form = reviewer_form()
    forbidden = {"--verify", "--waive", "--dry-run", "--transcript-dir"}
    flags = {field.flag for field in form.fields(FULLEST)}
    assert flags.isdisjoint(forbidden)
    assert form.argv(FULLEST) == (
        "--category",
        "rtl",
        "--focus",
        "spec",
        "--spec",
        "docs/spec.md",
        "--scope",
        "fifo.v,ctl.v",
        "--instruction",
        "mind resets",
        "--max-turns",
        "12",
        "--timeout",
        "45m",
    )
    # A value starting with a dash is one value, never an option.
    assert "--instruction=--verify" in form.argv({**FULLEST, "instruction": "--verify"})


# --- AC7 -------------------------------------------------------------------------------------------


def test_every_builtin_specialist_option_is_on_its_form_or_hidden():
    from booley.harness.dashboard.launch import _load_endpoint

    names = sorted(info.name for info in discover_mcp_inventory() if info.kind == "specialist")
    assert names == sorted(FORMS), "give the new built-in Specialist a form and a FORMS entry"
    for name in names:
        options = option_strings(_load_endpoint(Specialist, name))
        placed = written_options(FORMS[name], name)
        assert unplaced(options, placed, HIDDEN[name]) == [], name
        assert placed <= options  # the form writes only options the parser has
        assert all(HIDDEN[name].values()), name  # every hidden option carries its reason


def test_drift_guard_names_an_option_a_specialist_gains():
    parser = human_endpoint_parser(ReviewerSpecialist())
    parser.add_argument("--new-knob", help="an option added later")
    gained = set(parser._option_string_actions) - {"-h", "--help"}
    assert unplaced(gained, written_options(FULLEST), HIDDEN[REVIEWER]) == [
        "--new-knob is neither on the form nor hidden"
    ]
    assert unplaced(gained - {"--fail-on"}, written_options(FULLEST), HIDDEN[REVIEWER]) == [
        "--new-knob is neither on the form nor hidden",
        "--fail-on is hidden but the parser lacks it",
    ]


# --- the coverage_analyst form (#2305) ---------------------------------------------------------


def test_coverage_form_writes_the_record_and_the_optional_words():
    form = coverage_form()
    assert form.initial() == {}
    assert [field.label for field in form.fields({})] == ["Coverage Record", "Instruction", "More"]
    assert [field.label for field in form.fields({"more": "on"})][-2:] == ["Max turns", "Timeout"]
    assert form.refusal({}) == "Choose a Coverage Record"
    assert form.refusal({"campaign": "  "}) == "Choose a Coverage Record"
    assert form.refusal(COVERAGE_FULLEST) == ""
    assert form.argv({"campaign": "a/coverage.json"}) == ("--record", "a/coverage.json")
    assert form.argv(COVERAGE_FULLEST) == (
        "--record",
        COVERAGE_FULLEST["campaign"],
        "--instruction",
        "focus the FIFO",
        "--max-turns",
        "9",
        "--timeout",
        "20m",
    )
    assert "--instruction=--x" in form.argv({**COVERAGE_FULLEST, "instruction": "--x"})


def write_campaign(invocation: Path, target: str, *, complete: bool | str = True) -> Path:
    """One Target's campaign under *invocation*; ``complete="bad"`` writes a malformed projection."""
    directory = invocation / "targets" / target
    directory.mkdir(parents=True)
    path = directory / "coverage.json"
    path.write_text("{}\n", encoding="utf-8")
    projection = directory / "simulation.json"
    if complete == "bad":
        projection.write_text("[not json", encoding="utf-8")
    else:
        projection.write_text(
            json.dumps({"complete": complete, "target": target}), encoding="utf-8"
        )
    return path


def test_finished_campaigns_are_listed_newest_first_with_target_and_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.harness.dashboard.specialist_form import SimRun, finished_campaigns

    monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+00:00")
    worktree = tmp_path / "wt"
    reports = worktree / ".booley" / "flow-reports" / "sim"
    write_campaign(reports / "2", "sim_fifo")
    write_campaign(reports / "10", "sim_fifo")
    write_campaign(reports / "10", "sim_wide")
    write_campaign(reports / "11", "sim_fifo", complete=False)
    write_campaign(reports / "11", "sim_wide", complete="bad")
    (reports / "11" / "targets" / "sim_bare").mkdir()  # a Target directory without a campaign
    (reports / "13").mkdir()  # an invocation without a targets directory
    outside = tmp_path / "runtime" / "sim" / "12"
    outside_path = write_campaign(outside, "sim_fifo")
    runs = [
        SimRun(reports / "10", "2024-03-01T10:00:00Z"),
        SimRun(reports / "2", "2024-03-02T09:00:00Z"),
        SimRun(reports / "11", "2024-03-03T09:00:00Z"),
        SimRun(reports / "13", "2024-03-04T09:00:00Z"),
        SimRun(outside, "not a time"),
    ]
    found = finished_campaigns(worktree, runs)
    base = ".booley/flow-reports/sim"
    assert [choice.path for choice in found] == [
        f"{base}/2/targets/sim_fifo/coverage.json",
        f"{base}/10/targets/sim_fifo/coverage.json",
        f"{base}/10/targets/sim_wide/coverage.json",
        outside_path.as_posix(),
    ]
    assert [choice.label for choice in found] == [
        "sim_fifo  2024-03-02 09:00  sim #2",
        "sim_fifo  2024-03-01 10:00  sim #10",
        "sim_wide  2024-03-01 10:00  sim #10",
        "sim_fifo  unknown  sim #12",
    ]
    assert finished_campaigns(worktree, []) == ()


def test_an_unreadable_campaign_does_not_hide_the_readable_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.harness.dashboard.specialist_form import SimRun, finished_campaigns

    reports = tmp_path / "wt" / ".booley" / "flow-reports" / "sim"
    write_campaign(reports / "1", "sim_a")
    write_campaign(reports / "1", "sim_b")
    real_is_file = Path.is_file

    def is_file(self: Path) -> bool:
        if self.parent.name == "sim_a":  # e.g. a link into a folder the user cannot enter
            raise PermissionError(13, "denied", str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    found = finished_campaigns(tmp_path / "wt", [SimRun(reports / "1", "2024-03-01T10:00:00Z")])
    assert [choice.path for choice in found] == [
        ".booley/flow-reports/sim/1/targets/sim_b/coverage.json"
    ]
