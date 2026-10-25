"""Synthesis adapter data for the baseline engine: units, keys and Goal selection.

A relative SYNTH Goal compares against its baseline Target synthesised at the
Goal's base commit. The unit measured is that Target plus the recipe
fingerprint frozen at entry (or at an approved Goal Change). This module
stays Flow-neutral so the store can register the family without importing
any Flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.criteria.thresholds import has_relative_threshold
from booley.evidence.fields import RECIPE_FINGERPRINT_PARAM
from booley.goals.model import GoalFamily, GoalRecord, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.state_store import read_goal_state_file


def is_relative_synth(goal: RecordedGoal) -> bool:
    """Only a baseline-relative SYNTH Goal needs a baseline synthesis."""
    return goal.spec.family is GoalFamily.SYNTH and has_relative_threshold(goal.spec.params)


def relative_synth_goals(record: GoalRecord) -> tuple[RecordedGoal, ...]:
    """The Goals of *record* that get a synthesis baseline Job."""
    return tuple(goal for goal in record.goals if is_relative_synth(goal))


def baseline_target(goal: RecordedGoal) -> str:
    """The Target selector a relative Goal compares against."""
    target = goal.spec.params.get(BASELINE_TARGET_PARAM, goal.spec.target)
    if not isinstance(target, str) or not target:
        raise ValueError(f"Goal {goal.spec.key} names no baseline Target")
    return target


def synth_units(
    project: Path, record: GoalRecord, goal: RecordedGoal
) -> tuple[dict[str, Any], ...]:
    """The baseline Target with the recipe fingerprint frozen in the Goal state."""
    state = read_goal_state_file(record_paths(project, record.id).state_file)
    entry = None if state is None else state.criteria.get(goal.spec.key)
    fingerprint = None if entry is None else entry.params.get(RECIPE_FINGERPRINT_PARAM)
    if not isinstance(fingerprint, str):
        raise ValueError(f"Goal {goal.spec.key} has no frozen synthesis recipe")
    return ({"name": baseline_target(goal), "fingerprint": fingerprint},)


def synth_result_key(
    target_identity: str, unit: dict[str, Any], stamp: str, protected_digest: str
) -> dict[str, str]:
    """Contents-addressed key: Target identity, frozen recipe, contents and Protected Inputs."""
    return {
        "target": target_identity,
        "name": unit["name"],
        "fingerprint": unit["fingerprint"],
        "contents": stamp,
        "protected_inputs": protected_digest,
    }
