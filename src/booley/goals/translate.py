"""Translate Goal arguments into Goals judged by Criteria (ADR 0067 D4, D10, D11).

A Goal is a Criterion that is always mandatory, so translation does not invent
a second grammar: each :data:`~booley.goals.model.GoalArg` is spelled as the
Criterion authoring entry it stands for and handed to
:meth:`CriteriaTemplate.from_yaml`, which applies every Criteria rule
(threshold names and values, mutually exclusive thresholds, baseline pairs,
Coverage and Cycle Count records). The resulting :class:`CriterionSpec`
expands to the Goal's key, so Goal keys stay Criterion keys.

Several Goals for one key, from Goalsets and ad-hoc Goals together, merge to
the stricter Goal (D11) and every merge is reported as a warning:

- Thresholds: a threshold only one Goal sets is kept; one both set takes the
  stricter value, by the direction its Criterion evaluates it.
- Review Goals conflict by review, not by key: ``clean`` (no findings) wins
  over ``done`` (a terminal advisory review whose findings may stay open).
- Settings that are not thresholds have no stricter value: a different
  baseline Target, coverage test selection, spec file, or any mutation
  setting is a :class:`GoalConflictError`, as is a merge whose combined
  thresholds the Criteria rules refuse (for example ``area_um2_max`` with
  ``area_kge_max``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from booley.criteria.templates import CriteriaTemplate, CriterionSpec
from booley.criteria.thresholds import describe_threshold
from booley.goals.model import (
    CoverageGoalArg,
    CycleCountGoalArg,
    GoalArg,
    GoalFamily,
    GoalSpec,
    ImplementationGoalArg,
    MutationGoalArg,
    ReviewGoalArg,
    TargetGoalArg,
)

# Criterion family each per-Target Goal family is judged by.
CRITERION_FOR_FAMILY: Mapping[GoalFamily, str] = {
    GoalFamily.LINT: "lint_clean",
    GoalFamily.SIM: "sim_pass",
    GoalFamily.ELAB: "elab_pass",
    GoalFamily.SYNTH: "synthesis_ok",
    GoalFamily.FPGA: "fpga_impl_ok",
    GoalFamily.COVERAGE: "coverage",
    GoalFamily.MUTATION: "mutation_score",
}

_ArgT = TypeVar("_ArgT")


class GoalTranslationError(ValueError):
    """A Goal argument does not satisfy the Criteria rules for its family."""


class GoalConflictError(GoalTranslationError):
    """Two Goals for one key have no stricter Goal to merge to."""


@dataclass(frozen=True)
class Translation:
    """Goals translated from entry arguments, with one warning per merge."""

    goals: tuple[GoalSpec, ...]
    warnings: tuple[str, ...]

    @property
    def criteria(self) -> tuple[CriterionSpec, ...]:
        """The mandatory Criteria the Goals are judged by, in Goal order."""
        return tuple(criterion_spec(goal) for goal in self.goals)


def criterion_spec(goal: GoalSpec) -> CriterionSpec:
    """The mandatory Criterion that judges *goal*; it expands to ``goal.key``."""
    params = dict(goal.params)
    if goal.family in (GoalFamily.CYCLE_COUNT, GoalFamily.REVIEW):
        return CriterionSpec(goal.key, params=params)
    assert goal.target is not None, f"per-Target Goal {goal.key} has no Target"
    return CriterionSpec(
        CRITERION_FOR_FAMILY[goal.family],
        per_target=True,
        targets=[goal.target],
        params=params,
    )


def translate_goals(args: Sequence[GoalArg]) -> Translation:
    """Translate entry arguments into merged Goals.

    Raises :class:`GoalTranslationError` naming the argument when one breaks a
    Criteria rule, and :class:`GoalConflictError` when Goals for one key
    cannot merge.
    """
    groups: dict[str, list[tuple[GoalArg, GoalSpec]]] = {}
    for index, arg in enumerate(args):
        goal = _translate_one(arg, where=f"goals[{index}]")
        groups.setdefault(_merge_group(arg, goal), []).append((arg, goal))
    goals: list[GoalSpec] = []
    warnings: list[str] = []
    for members in groups.values():
        goal, warning = _merge(members)
        goals.append(goal)
        if warning is not None:
            warnings.append(warning)
    return Translation(tuple(goals), tuple(warnings))


# ---------------------------------------------------------------------------
# One Goal
# ---------------------------------------------------------------------------


def _authoring_entry(arg: GoalArg) -> tuple[str, Any]:
    """The Criterion authoring entry (YAML key and value) *arg* stands for."""
    if isinstance(arg, TargetGoalArg):
        return CRITERION_FOR_FAMILY[arg.family], [arg.target]
    if isinstance(arg, ImplementationGoalArg):
        target: Any = arg.target
        if arg.baseline is not None:
            target = {"baseline": arg.baseline, "candidate": arg.target}
        return CRITERION_FOR_FAMILY[arg.family], {"targets": [target], **arg.thresholds}
    if isinstance(arg, CycleCountGoalArg):
        return "cycle_count", [{"target": arg.target, "test": arg.test, **arg.thresholds}]
    if isinstance(arg, CoverageGoalArg):
        metrics = {metric: {"min_pct": value} for metric, value in arg.metrics.items()}
        tests = arg.tests if arg.tests == "all" else list(arg.tests)
        record = {"targets": [arg.target], "metrics": metrics, "tests": tests}
        return "coverage", [record]
    if isinstance(arg, MutationGoalArg):
        return "mutation_score", [{"target": arg.target, **arg.params}]
    return f"review_{arg.review}_{arg.verdict}", None


def _translate_one(arg: GoalArg, *, where: str) -> GoalSpec:
    """Translate one argument through the Criteria authoring rules."""
    key, value = _authoring_entry(arg)
    try:
        specs = CriteriaTemplate.from_yaml({"mandatory": {key: value}}).specs
    except ValueError as exc:
        raise GoalTranslationError(f"{where} ({_describe(arg)}): {exc}") from None
    assert len(specs) == 1, f"{where} translated to {len(specs)} Criteria"
    expanded = specs[0].expand([])
    assert len(expanded) == 1, f"{where} expanded to {len(expanded)} Criterion keys"
    params = dict(specs[0].params)
    if isinstance(arg, ReviewGoalArg) and arg.spec is not None:
        params["spec"] = arg.spec
    target = None if isinstance(arg, ReviewGoalArg) else arg.target
    return GoalSpec(expanded[0][0], arg.family, target, params, (arg.origin,))


def _describe(arg: GoalArg) -> str:
    if isinstance(arg, ReviewGoalArg):
        return f"review {arg.review} {arg.verdict}"
    return f"{arg.family.value} {arg.target}"


def _merge_group(arg: GoalArg, goal: GoalSpec) -> str:
    """Goals in one group merge; review Goals group by review, others by key."""
    if isinstance(arg, ReviewGoalArg):
        return f"review_{arg.review}"
    return goal.key


# ---------------------------------------------------------------------------
# Merging Goals for one key (D11)
# ---------------------------------------------------------------------------


Members = list[tuple[GoalArg, GoalSpec]]


def _merge(members: Members) -> tuple[GoalSpec, str | None]:
    """Merge one group to its stricter Goal and describe the merge."""
    first_arg, first_goal = members[0]
    if len(members) == 1:
        return first_goal, None
    if len({arg.family for arg, _ in members}) > 1:
        raise GoalConflictError(f"cannot merge {_group_name(members)}: different Goal families")
    origins = tuple(dict.fromkeys(o for _, goal in members for o in goal.origins))
    merged_arg, decisions = _MERGERS[first_arg.family](members)
    if merged_arg is first_arg:
        merged = first_goal
    else:
        try:
            merged = _translate_one(merged_arg, where=_group_name(members))
        except GoalTranslationError as exc:
            raise GoalConflictError(f"cannot merge {_group_name(members)}: {exc}") from None
    merged = replace(merged, origins=origins)
    detail = "; ".join(decisions) if decisions else "identical Goals"
    return merged, f"Goal {merged.key} from {', '.join(origins)} merged: {detail}"


def _group_name(members: Members) -> str:
    keys = ", ".join(dict.fromkeys(goal.key for _, goal in members))
    origins = ", ".join(dict.fromkeys(o for _, goal in members for o in goal.origins))
    return f"Goals {keys} (from {origins})"


def _require_equal(members: Members, what: str, setting: Callable[[GoalArg], object]) -> None:
    """Refuse a merge whose members differ in a setting that has no stricter value."""
    values = sorted({repr(setting(arg)) for arg, _ in members})
    if len(values) > 1:
        raise GoalConflictError(
            f"cannot merge {_group_name(members)}: they name different {what} "
            f"({', '.join(values)}) and neither is stricter"
        )


def _merge_values(
    members: Members,
    authored: Callable[[GoalArg], Mapping[str, Any]],
    normalized: Callable[[GoalSpec, str], Any],
    stricter: Callable[[str, Any, Any], bool],
) -> tuple[dict[str, Any], list[str]]:
    """Union of authored bounds, each taken from the Goal whose bound is stricter.

    *authored* gives an argument's bounds as written, *normalized* the value
    the Criterion compares, and *stricter* whether one normalized value is
    stricter than another for a bound name.
    """
    chosen: dict[str, tuple[Any, Any, str]] = {}  # name -> (authored, normalized, origin)
    decisions: list[str] = []
    for arg, goal in members:
        for name, written in authored(arg).items():
            value = normalized(goal, name)
            kept = chosen.get(name)
            if kept is None:
                chosen[name] = (written, value, arg.origin)
                continue
            if value == kept[1]:
                continue
            if stricter(name, value, kept[1]):
                chosen[name] = (written, value, arg.origin)
            decisions.append(
                f"{name} {kept[1]!r} (from {kept[2]}) and {value!r} (from {arg.origin}) "
                f"-> stricter {chosen[name][1]!r}"
            )
    return {name: kept[0] for name, kept in chosen.items()}, decisions


def stricter_threshold(param: str, candidate: Any, current: Any) -> bool:
    """Whether threshold *candidate* is stricter than *current* for *param*.

    The direction is the one the Criterion evaluates: an upper bound (``le``)
    is stricter when smaller, a lower bound (``ge``) when larger. ``reduce_``
    thresholds bound the negated change (``measured <= -threshold`` or
    ``>= -threshold``), as the Cycle Count and QoR checks evaluate them. A
    clock-scoped threshold (``<clock>.<param>``) has its unscoped direction.
    """
    base = param.rpartition(".")[2]
    descriptor = describe_threshold(base)
    if descriptor is None:
        raise GoalConflictError(f"threshold {param!r} has no defined stricter value")
    sign = -1 if "reduce_" in base else 1
    if descriptor.operator == "le":
        return sign * candidate < sign * current
    return sign * candidate > sign * current


def _merge_identical(members: Members) -> tuple[GoalArg, list[str]]:
    """lint, sim, and elab Goals for one key are identical by construction."""
    return members[0][0], []


def _merge_review(members: Members) -> tuple[GoalArg, list[str]]:
    reviews = [arg for arg, _ in members if isinstance(arg, ReviewGoalArg)]
    _require_equal(members, "spec files", lambda arg: _as(arg, ReviewGoalArg).spec)
    clean = [arg for arg in reviews if arg.verdict == "clean"]
    done = [arg for arg in reviews if arg.verdict == "done"]
    if not clean or not done:
        return reviews[0], []
    decision = (
        f"'clean' (from {', '.join(a.origin for a in clean)}) wins over "
        f"'done' (from {', '.join(a.origin for a in done)})"
    )
    return clean[0], [decision]


def _merge_implementation(members: Members) -> tuple[GoalArg, list[str]]:
    _require_equal(
        members, "baseline Targets", lambda arg: _as(arg, ImplementationGoalArg).baseline
    )
    thresholds, decisions = _merge_values(
        members,
        lambda arg: _as(arg, ImplementationGoalArg).thresholds,
        lambda goal, name: goal.params[name],
        stricter_threshold,
    )
    return replace(_as(members[0][0], ImplementationGoalArg), thresholds=thresholds), decisions


def _merge_cycle_count(members: Members) -> tuple[GoalArg, list[str]]:
    thresholds, decisions = _merge_values(
        members,
        lambda arg: _as(arg, CycleCountGoalArg).thresholds,
        lambda goal, name: goal.params[name],
        stricter_threshold,
    )
    return replace(_as(members[0][0], CycleCountGoalArg), thresholds=thresholds), decisions


def _merge_coverage(members: Members) -> tuple[GoalArg, list[str]]:
    # A coverage floor is a minimum percentage: the larger floor is stricter.
    _require_equal(
        members,
        "coverage test selections",
        lambda arg: _coverage_tests_key(_as(arg, CoverageGoalArg)),
    )
    floors, decisions = _merge_values(
        members,
        lambda arg: _as(arg, CoverageGoalArg).metrics,
        lambda goal, name: goal.params["metrics"][name]["min_pct"],
        lambda _name, candidate, current: candidate > current,
    )
    return replace(_as(members[0][0], CoverageGoalArg), metrics=floors), decisions


def _coverage_tests_key(arg: CoverageGoalArg) -> object:
    return arg.tests if arg.tests == "all" else sorted(arg.tests)


def _merge_mutation(members: Members) -> tuple[GoalArg, list[str]]:
    # Mutation settings (scope, detected and total counts, auto) have no
    # defined stricter value, so only identical mutation Goals merge.
    _require_equal(members, "mutation settings", lambda arg: _as(arg, MutationGoalArg).params)
    return members[0][0], []


def _as(arg: GoalArg, kind: type[_ArgT]) -> _ArgT:
    """Narrow a group member to its family's argument type (one family per group)."""
    assert isinstance(arg, kind), f"{arg!r} is not a {kind.__name__}"
    return arg


_MERGERS: Mapping[GoalFamily, Callable[[Members], tuple[GoalArg, list[str]]]] = {
    GoalFamily.LINT: _merge_identical,
    GoalFamily.SIM: _merge_identical,
    GoalFamily.ELAB: _merge_identical,
    GoalFamily.SYNTH: _merge_implementation,
    GoalFamily.FPGA: _merge_implementation,
    GoalFamily.CYCLE_COUNT: _merge_cycle_count,
    GoalFamily.COVERAGE: _merge_coverage,
    GoalFamily.MUTATION: _merge_mutation,
    GoalFamily.REVIEW: _merge_review,
}
