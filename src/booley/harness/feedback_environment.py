"""Compose Feedback's environment from Harness-owned Doctor state."""

from __future__ import annotations

from pathlib import Path

from booley.feedback.render import Environment, collect_environment
from booley.harness import doctor_stamp


def resolve_feedback_environment(project_dir: Path) -> Environment:
    """Resolve one fail-soft Feedback environment for a logical operation."""
    stamp = doctor_stamp.load_stamp(project_dir)
    deep = stamp.get("deep") if stamp is not None else None
    return collect_environment(doctor_deep_clean=deep if isinstance(deep, bool) else None)
