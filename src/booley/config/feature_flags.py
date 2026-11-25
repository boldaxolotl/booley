"""Maintainer-only Feature Flags (ADR 0095).

A Feature Flag keeps a major feature or breaking change invisible until a minor
release deletes the flag. Flags are registered here and enabled per Project by
``[features].enable`` in ``booley.toml``; no environment variable or user setting
enables one. ``feature_enabled`` is the one query every gate uses.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.booley_dir import resolve_booley_dir, resolve_checkout_booley_dir
from booley.core.boundary import BoundaryError, is_str_list, require_dict
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.core.config_paths import resolve_toml

FEATURES_TABLE = "features"


@dataclass(frozen=True)
class FeatureFlag:
    """One registered flag."""

    name: str  # lowercase, hyphenated
    summary: str  # one line
    issue: int  # tracking issue number


#: Every registered flag; deleting an entry enables its feature.
FEATURE_FLAGS: tuple[FeatureFlag, ...] = (
    FeatureFlag("web-run-panel", "booley dashboard --web: the Run panel", 3874),
)


class UnknownFeatureFlagError(LookupError):
    """Code asked about a name that is not registered."""


class FeatureConfigError(ValueError):
    """``booley.toml`` ``[features]`` is invalid."""


def parse_enabled_features(data: Mapping[str, Any]) -> frozenset[str]:
    """Validate ``[features].enable`` and return the enabled flag names."""
    if FEATURES_TABLE not in data:
        return frozenset()
    try:
        table = require_dict(data[FEATURES_TABLE], field="booley.toml [features]")
    except BoundaryError as exc:
        raise FeatureConfigError(str(exc)) from exc
    names = table.get("enable", [])
    if not is_str_list(names):
        raise FeatureConfigError("booley.toml [features].enable must be a list of strings")
    registered = [flag.name for flag in FEATURE_FLAGS]
    for name in names:
        if name not in registered:
            raise FeatureConfigError(
                f"booley.toml [features].enable names unknown Feature Flag {name!r}; "
                f"registered flags: {', '.join(registered) or 'none'}. "
                "Remove a flag that no longer exists"
            )
    return frozenset(names)


def enabled_features(project_root: Path | None = None) -> frozenset[str]:
    """The flags a Project enables; none outside a Project.

    With *project_root* the selected checkout's ``booley.toml`` is read, otherwise
    the ambient Project's (current directory or ``BOOLEY_DIR``).
    """
    try:
        directory = (
            resolve_booley_dir(cache_result=False)
            if project_root is None
            else resolve_checkout_booley_dir(project_root)
        )
    except (FileNotFoundError, SourceCheckoutProjectError):
        return frozenset()
    try:
        with resolve_toml(directory).open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return frozenset()
    return parse_enabled_features(data)


def feature_enabled(name: str, enabled: frozenset[str] | None = None) -> bool:
    """Whether the registered flag *name* is on; surfaces that hold a set pass it."""
    if name not in {flag.name for flag in FEATURE_FLAGS}:
        raise UnknownFeatureFlagError(f"unknown Feature Flag {name!r}")
    return name in (enabled_features() if enabled is None else enabled)
