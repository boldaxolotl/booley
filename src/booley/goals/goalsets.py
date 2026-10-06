"""Seeded Goalsets: Markdown bundles of Goals the Project owns (ADR 0067).

A Goalset is one free-form Markdown file in ``<project dir>/goalsets/``,
written in the vocabulary of the entry tool's Goal arguments
(:data:`~booley.goals.model.GoalArg`). The agent reads the chosen Goalsets at
entry and translates them into Goal arguments, so validation happens at entry,
not on the file.

``init`` seeds the four named Goalsets (``feature``, ``bugfix``, ``refactor``,
``verification``) from the Criteria templates in
:data:`~booley.criteria.templates.TEMPLATE_REGISTRY`, only when a file is
missing. After that the Project owns the file: Booley never rewrites it, and
it never seeds ``default.md`` (the Goalset that applies to every entry).

Each rendered Goalset carries exactly one fenced ``json`` block: the Goal
argument list for that Goalset, with ``<target>`` standing for each Target the
change touches (and ``<spec>`` for a spec review's spec file). Substituting
those placeholders yields input that :func:`~booley.goals.model.parse_goal_args`
and :func:`~booley.goals.translate.translate_goals` accept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from booley.criteria.templates import TEMPLATE_REGISTRY, CriterionSpec
from booley.goals.model import REVIEW_KINDS, REVIEW_VERDICTS, SPEC_REVIEW_KIND, GoalFamily
from booley.goals.translate import CRITERION_FOR_FAMILY
from booley.runtime.guarded_write import WriteOutcome, guarded_write

# Directory, inside the Project directory, that holds every Goalset.
GOALSETS_DIR = "goalsets"

# Goalsets ``init`` seeds, in the order it writes them. ``default`` is never
# seeded: whether every entry carries a Goalset is the Project's decision.
SEEDED_GOALSETS: tuple[str, ...] = ("feature", "bugfix", "refactor", "verification")

# Placeholders the agent replaces when it translates a Goalset into Goal
# arguments. They are valid JSON strings so the block stays parseable.
TARGET_PLACEHOLDER = "<target>"
SPEC_PLACEHOLDER = "<spec>"

# A Goalset name is one file stem: no separators, no leading dot.
_GOALSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# Per-Target families a template Criterion may name, keyed by Criterion name.
_FAMILY_FOR_CRITERION: Mapping[str, GoalFamily] = {
    criterion: family
    for family, criterion in CRITERION_FOR_FAMILY.items()
    if family
    in (GoalFamily.LINT, GoalFamily.SIM, GoalFamily.ELAB, GoalFamily.SYNTH, GoalFamily.FPGA)
}

# What a per-Target Goal of each family asks of every Target the change touches.
_PER_TARGET_PROSE: Mapping[GoalFamily, str] = {
    GoalFamily.LINT: "lints clean",
    GoalFamily.SIM: "passes simulation",
    GoalFamily.ELAB: "elaborates",
    GoalFamily.SYNTH: "synthesizes",
    GoalFamily.FPGA: "completes FPGA implementation",
}

_VERDICT_PROSE: Mapping[str, str] = {
    "clean": "finishes `clean` (no open findings)",
    "done": "finishes `done` (the review ran to the end; advisory findings may stay open)",
}

_REVIEW_NAME = re.compile(
    rf"review_(?P<review>{'|'.join(REVIEW_KINDS)})_(?P<verdict>{'|'.join(REVIEW_VERDICTS)})"
)


class GoalsetTemplateError(ValueError):
    """A template Criterion has no Goal argument a Goalset can spell."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_goalset(name: str, specs: Sequence[CriterionSpec]) -> str:
    """Render one Goalset as Markdown from the Criteria of a template.

    Goal Mode has no optional Goals, so optional Criteria (``mandatory=False``)
    are omitted: a Goalset lists only Goals that must be met. Per-Target
    Criteria render as one Goal argument naming ``<target>``, which the agent
    repeats for every Target the change touches; review Criteria render as a
    review Goal. Every Goal argument records the Goalset as its ``origin``.

    Raises :class:`GoalsetTemplateError` for an invalid *name* or a Criterion
    with no Goal argument shape (for example a Coverage Criterion, which needs
    a record the template cannot supply).
    """
    if not _GOALSET_NAME.fullmatch(name):
        raise GoalsetTemplateError(f"Goalset name {name!r} must be a plain file stem")
    goals = [_goal_argument(spec, origin=name) for spec in specs if spec.mandatory]
    if not goals:
        raise GoalsetTemplateError(f"Goalset {name!r} has no mandatory Criteria to render")
    lines = [
        *_header(name),
        "## Goals",
        "",
        *(f"- {_describe(goal)}" for goal in goals),
        "",
        *_arguments_section(goals),
    ]
    return "\n".join(lines) + "\n"


def _header(name: str) -> list[str]:
    """Title and ownership note at the top of every seeded Goalset."""
    return [
        f"# Goalset: {name}",
        "",
        "This file belongs to the Project: edit, extend, or delete it freely.",
        "`booley init` created it once and never rewrites it.",
        "",
        "When a session enters Goal Mode with this Goalset, the agent translates",
        "it into the entry tool's Goal arguments. Every Goal is mandatory. Write",
        "new Goals in the same vocabulary as the Goal arguments below.",
        "",
    ]


def _arguments_section(goals: Sequence[Mapping[str, Any]]) -> list[str]:
    """The machine-readable Goal argument list, with its substitution rules."""
    rules: list[str] = []
    if any("target" in goal for goal in goals):
        rules.append(
            f"- Repeat each Goal that names `{TARGET_PLACEHOLDER}` once for every Target"
            f" the change touches, replacing `{TARGET_PLACEHOLDER}` with the Target name."
        )
    if any("spec" in goal for goal in goals):
        rules.append(
            f"- Replace `{SPEC_PLACEHOLDER}` with the spec file path, relative to the worktree."
        )
    return [
        "## Goal arguments",
        "",
        *rules,
        "",
        "```json",
        json.dumps(list(goals), indent=2),
        "```",
    ]


def _goal_argument(spec: CriterionSpec, *, origin: str) -> dict[str, Any]:
    """The Goal argument (entry tool input) one template Criterion stands for."""
    family = _FAMILY_FOR_CRITERION.get(spec.name)
    if spec.per_target and family is not None:
        goal: dict[str, Any] = {"family": family.value, "target": TARGET_PLACEHOLDER}
        if spec.params:
            if family not in (GoalFamily.SYNTH, GoalFamily.FPGA):
                raise GoalsetTemplateError(
                    f"Criterion {spec.name!r} takes no parameters, got {sorted(spec.params)}"
                )
            goal["thresholds"] = dict(spec.params)
        return {**goal, "origin": origin}
    review = _REVIEW_NAME.fullmatch(spec.name)
    if review is not None and not spec.per_target and not spec.params:
        goal = {"family": GoalFamily.REVIEW.value, **review.groupdict()}
        if review["review"] == SPEC_REVIEW_KIND:
            goal["spec"] = SPEC_PLACEHOLDER
        return {**goal, "origin": origin}
    raise GoalsetTemplateError(f"Criterion {spec.name!r} has no Goalset Goal argument")


def _describe(goal: Mapping[str, Any]) -> str:
    """One prose line for a Goal argument built by :func:`_goal_argument`."""
    family = GoalFamily(goal["family"])
    if family is GoalFamily.REVIEW:
        line = f"`review`: the `{goal['review']}` review {_VERDICT_PROSE[goal['verdict']]}"
        if "spec" in goal:
            line += ", judged against the spec file the Goal names"
        return line + "."
    line = f"`{family.value}`: every Target the change touches {_PER_TARGET_PROSE[family]}"
    thresholds = goal.get("thresholds")
    if thresholds:
        line += " within " + ", ".join(f"`{key}` {value}" for key, value in thresholds.items())
    return line + "."


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_goalsets(project_dir: Path, *, dry_run: bool) -> list[tuple[Path, WriteOutcome]]:
    """Create each missing seeded Goalset under ``<project_dir>/goalsets/``.

    Writes are create-only: an existing file is :attr:`WriteOutcome.SKIPPED`
    and left untouched, because the Project owns it. ``default.md`` is never
    written. Under *dry_run* nothing is created, not even the directory, and
    each result is the outcome a real run would have.
    """
    goalsets_dir = project_dir / GOALSETS_DIR
    results: list[tuple[Path, WriteOutcome]] = []
    for name in SEEDED_GOALSETS:
        path = goalsets_dir / f"{name}.md"
        content = render_goalset(name, TEMPLATE_REGISTRY[name])
        # guarded_write creates the parent directory only when it writes.
        results.append((path, guarded_write(path, content, dry_run=dry_run, newline="\n")))
    return results
