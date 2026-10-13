"""Presentation-only Target spelling, independent of Goal/evidence identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from booley.core.boundary import require_dict, require_str_value
from booley.goals.model import GoalRecord, GoalSpec
from booley.runtime.atomic_files import atomic_replace_bytes

NAMES_FILE = "goal-names.json"


def spec_digest(goal: GoalSpec) -> str:
    """Bind display metadata to the exact unchanged stored specification."""
    return hashlib.sha256(json.dumps(goal.to_json(), sort_keys=True).encode()).hexdigest()


def resolve_spelling(goal: GoalSpec, root: Path) -> GoalSpec:
    """Resolve a qualified authored selector using the catalog's ambiguity policy."""
    if goal.target is None or "#" not in goal.target:
        return goal
    # Public argument parsing and release-contract codecs are stdlib-only consumers.
    # Catalog dependencies are needed only at actual Target admission.
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import FuseSocError

    try:
        target = TargetCatalog.build(root).select(goal.target)
        subject = target.identity if "#" in target.selector else target.selector
    except (FuseSocError, OSError, ValueError, RuntimeError):
        return goal  # Unavailable/removed declarations retain the explicit selector.
    return replace(goal, display_target=subject) if subject != goal.target else goal


def read_names(root: Path) -> dict[str, str]:
    """Read optional display metadata without creating or repairing any paths."""
    path = root / NAMES_FILE
    if path.is_symlink():
        raise ValueError("unsupported Goal naming metadata")
    if not path.exists():
        return {}
    raw = require_dict(json.loads(path.read_bytes()), field="Goal naming metadata")
    return {
        key: require_str_value(value, field="Goal display Target") for key, value in raw.items()
    }


def apply_names(goal: GoalSpec, names: dict[str, str]) -> GoalSpec:
    """Decorate a typed specification; serialized identity and equality stay intact."""
    decorated = replace(goal, display_target=names.get(spec_digest(goal), goal.display_target))
    return (
        replace(decorated, display_target=display_subject(decorated))
        if decorated.display_target is not None
        else decorated
    )


def display_subject(goal: GoalSpec) -> str | None:
    """A display subject may abbreviate the authored Target, never substitute another."""
    if goal.target is None or goal.display_target is None:
        return goal.target
    from booley.targets.catalog import selector_is_alias

    return (
        goal.display_target if selector_is_alias(goal.target, goal.display_target) else goal.target
    )


def optional_names(root: Path) -> dict[str, str]:
    """Loss of live display metadata cannot invalidate acceptance/lifecycle authority."""
    try:
        return read_names(root)
    except (OSError, ValueError):
        return {}


def record_names(record: GoalRecord, names: dict[str, str]) -> GoalRecord:
    """Restore observational names on record load, including catalog-less history."""
    return replace(
        record,
        goals=tuple(replace(row, spec=apply_names(row.spec, names)) for row in record.goals),
    )


def names_for(goals: Iterable[GoalSpec]) -> dict[str, str]:
    """Only specifications whose display spelling differs need sidecar entries."""
    return {
        spec_digest(goal): goal.display_target for goal in goals if goal.display_target is not None
    }


def save_names(root: Path, goals: Iterable[GoalSpec]) -> None:
    """Record display subjects during authorized admission, never during status reads."""
    additional = names_for(goals)
    if additional:
        with suppress(OSError, ValueError):
            atomic_replace_bytes(
                root / NAMES_FILE,
                json.dumps({**additional, **optional_names(root)}, sort_keys=True).encode(),
            )
