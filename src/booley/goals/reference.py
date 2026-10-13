"""Public Goal catalog, using the same typed names as status and entry."""

from __future__ import annotations

from pathlib import Path

from booley.criteria.endpoint_catalog import CriterionEndpointCatalog
from booley.criteria.templates import load_base_criteria
from booley.goals.capabilities import CRITERION_FOR_FAMILY
from booley.goals.model import REVIEW_KINDS, GoalFamily, GoalSpec
from booley.goals.presentation import goal_presentation


def render_goal_reference(
    endpoint_catalog: CriterionEndpointCatalog, *, project_criteria_path: Path | None = None
) -> str:
    """Only supported Goal families; custom evidence definitions are not Goal families."""
    families = {criterion: family for family, criterion in CRITERION_FOR_FAMILY.items()}
    definitions = {definition.name: definition for definition in load_base_criteria()}
    lines = [
        "| Goal | Requirement | Evidence producer |",
        "|------|-------------|-------------------|",
    ]
    for criterion, family in families.items():
        goal = GoalSpec(criterion, family, "{target}", {}, ("ad-hoc",))
        definition = definitions[criterion]
        lines.append(_row(goal, definition.description, criterion, endpoint_catalog))
    for kind in REVIEW_KINDS:
        criterion = f"review_{kind}"
        goal = GoalSpec(criterion + "_clean", GoalFamily.REVIEW, None, {}, ("ad-hoc",))
        lines.append(_row(goal, definitions[criterion].description, criterion, endpoint_catalog))
    lines += [
        "",
        "`{target}` is the bare Target name when unique, or its qualified selector otherwise. Details (thresholds, tests, baseline,",
        "review verdict and spec) follow the name in parentheses. Review Goals require",
        "`clean` or `done`; the name is identical for both requirements.",
        "",
        "Goal argument `family` uses the uppercase capability before the colon.",
        "Internal evidence keys and Project-defined evidence bindings are not Goal names.",
    ]
    return "\n".join(lines)


def _row(
    goal: GoalSpec, description: str, criterion: str, catalog: CriterionEndpointCatalog
) -> str:
    from booley.criteria.reference import format_endpoint

    binding = catalog.binding_for(criterion)
    producer = format_endpoint(binding.command) if binding is not None else "—"
    clean = " ".join(description.split()).replace("|", "\\|")
    return f"| `{goal_presentation(goal).name}` | {clean} | {producer} |"
