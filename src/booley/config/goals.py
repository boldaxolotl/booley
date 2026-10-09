"""Validated Goal presentation configuration."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_finite_number

DEFAULT_QUIET_AFTER = 7200.0


def quiet_after(project_dir: Path) -> float:
    """Read [goals].quiet_after in seconds, 60 through 604800 (default two hours)."""
    path = project_dir / "booley.toml"
    if not path.exists():
        return DEFAULT_QUIET_AFTER
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_quiet_after(data)


def parse_quiet_after(data: Mapping[str, Any]) -> float:
    """Validate the Project config boundary without filesystem access."""
    section = require_dict(data.get("goals", {}), field="[goals]")
    value = require_finite_number(
        section.get("quiet_after", DEFAULT_QUIET_AFTER), field="[goals].quiet_after"
    )
    if not 60 <= value <= 604800:
        raise ValueError("[goals].quiet_after must be 60..604800 seconds")
    return float(value)


def parse_dashboard(data: Mapping[str, Any]) -> bool:
    """The single strict parser for the Dashboard attach-task knob."""
    from booley.core.boundary import require_bool, require_dict

    return require_bool(
        require_dict(data.get("sandbox", {}), field="[sandbox]"), "dashboard", default=True
    )
