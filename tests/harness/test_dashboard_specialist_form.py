"""The reviewer's hand-made Run form (#3816): its focuses, rows, command words and drift guard.

Pure model tests; the wizard pilots are in ``test_dashboard_run_specialist.py``.
"""

from __future__ import annotations

from collections.abc import Collection

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

# The options of a built-in Specialist's parser that its form does not write, each with the reason.
# A Specialist the form does not cover at all is listed in FORMS with the reason string.
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
}
FORMS: dict[str, str | None] = {REVIEWER: None, "coverage_analyst": "form not built yet"}


def reviewer_form():
    """The reviewer's form. Imported here so a Dashboard without it fails the test body, not collection."""
    from booley.harness.dashboard.specialist_form import ReviewerForm

    return ReviewerForm("docs/spec.md")


def option_strings(specialist: Specialist) -> set[str]:
    """Every option spelling of *specialist*'s human parser except help."""
    parser = human_endpoint_parser(specialist)
    return set(parser._option_string_actions) - {"-h", "--help"}


def written_options(values: dict[str, str]) -> set[str]:
    """The option spellings the reviewer form writes for *values*."""
    words = reviewer_form().argv(values)
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
    assert names == sorted(FORMS), "give the new built-in Specialist a FORMS entry"
    for name in names:
        options = option_strings(_load_endpoint(Specialist, name))
        if FORMS[name] is not None:
            assert options, name  # a Specialist without a form is listed as "form not built yet"
            continue
        placed = written_options(FULLEST)
        assert unplaced(options, placed, HIDDEN[name]) == [], name
        assert placed <= options  # the form writes only options the parser has


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
