"""Canonical Goal names from typed specifications, separate from persisted identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from booley.core.boundary import as_dict
from booley.criteria.presentation import CriterionPresentation, criterion_presentation
from booley.goals.capabilities import CRITERION_FOR_FAMILY, LEGACY_REVIEWS
from booley.goals.model import GoalFamily, GoalRecord, GoalSpec
from booley.goals.naming import apply_names, display_subject, names_for
from booley.review.goal_package import proposal_facts

if TYPE_CHECKING:
    from booley.goals.proposals import ProposalView

UNAVAILABLE_GOAL = "Goal unavailable (unsupported record)"


def _text(value: str) -> str:
    """Prevent terminal controls in identity while retaining ordinary Unicode."""
    return "".join(character if character.isprintable() else " " for character in value)


@dataclass(frozen=True)
class GoalPresentation:
    """One invariant name, with readable requirements and observations beside it."""

    name: str
    details: str = ""
    requirement: str = ""
    observation: str = ""

    @property
    def text(self) -> str:
        """The name and distinguishing details, shared by every textual surface."""
        return self.name + (f" ({self.details})" if self.details else "")

    def criterion(self) -> CriterionPresentation:
        """Neutral Dashboard/producer data; identity never comes from a Criterion key."""
        return CriterionPresentation(
            self.name, self.details, self.requirement, self.observation, self.details
        )


def goal_presentation(goal: GoalSpec, entry: object = None) -> GoalPresentation:
    """Render a typed Goal without catalog lookups, saved labels or key splitting."""
    review = LEGACY_REVIEWS.get(goal.key) if goal.family is GoalFamily.REVIEW else None
    if goal.family is GoalFamily.REVIEW:
        if review is None or goal.target is not None:
            return GoalPresentation(UNAVAILABLE_GOAL)
        subject, verdict = review
        criterion = goal.key
    else:
        if not isinstance(goal.target, str) or not goal.target.strip():
            return GoalPresentation(UNAVAILABLE_GOAL)
        subject, verdict = display_subject(goal) or goal.target, ""
        criterion = CRITERION_FOR_FAMILY[goal.family]
    # Reuse evaluation-owned requirement semantics with an explicit semantic family
    # and Target. Persisted presentation overrides and encoded identity are excluded.
    facts: dict[str, Any] = as_dict(entry, default={}) or {}
    params = {**goal.params, "target": subject}
    support = criterion_presentation(criterion, {**facts, "params": params, "presentation": {}})
    details = _details(goal, support.requirement, verdict)
    return GoalPresentation(
        f"{goal.family.name}:{_text(subject)}", details, support.requirement, support.observation
    )


def _details(goal: GoalSpec, requirement: str, verdict: str) -> str:
    params = goal.params
    parts = [verdict] if verdict else ([requirement] if requirement else [])
    if display_subject(goal) != goal.target:
        parts.append(f"Target selector {goal.target}")
    if goal.family is GoalFamily.MUTATION:
        if params.get("auto"):
            parts.append("automatic workload")
        if "total" not in params and "min_detected" in params:
            parts.append(f"≥ {params['min_detected']} mutations detected")
        if "min_detected" not in params and "total" in params:
            parts.append(f"{params['total']} mutations")
    for field, label in (
        ("test", "test"),
        ("tests", "tests"),
        ("test_selector", "tests"),
        ("baseline_target", "baseline"),
        ("_baseline_target", "baseline"),
        ("scope", "scope"),
        ("spec", ""),
    ):
        value = params.get(field)
        if value is None:
            continue
        rendered = (
            ", ".join(map(str, cast("list[Any] | tuple[Any, ...]", value)))
            if isinstance(value, (list, tuple))
            else str(value)
        )
        part = f"{label} {rendered}".strip()
        if part not in parts:
            parts.append(part)
    return _text(", ".join(parts))


def present_spec(raw: object, *, names: dict[str, str] | None = None) -> str:
    """Project frozen storage metadata at a neutral presentation boundary."""
    try:
        goal = GoalSpec.from_json(raw, where="Goal presentation")
    except ValueError:
        return UNAVAILABLE_GOAL
    return goal_presentation(apply_names(goal, names or {})).text


def goal_addresses(record: GoalRecord) -> dict[str, Any]:
    """Machine-only addresses for proposals; human text contains public names."""
    return {
        "record_id": record.id,
        "goals": [
            {
                "goal_key": goal.spec.key,
                "name": goal_presentation(goal.spec).name,
                "details": goal_presentation(goal.spec).details,
            }
            for goal in record.goals
        ],
    }


def proposal_presentation(view: ProposalView) -> dict[str, Any]:
    """Human approval facts, without the saved payload's machine identity fields."""
    specs = [view.proposal.after]
    if view.proposal.before is not None:
        specs.append(view.proposal.before)
    names = names_for(specs)
    result = {
        **proposal_facts(view.proposal.to_json(), lambda raw: present_spec(raw, names=names)),
        "state": view.state,
    }
    if view.decision is not None:
        result["decision"] = view.decision.to_json()
    return result
