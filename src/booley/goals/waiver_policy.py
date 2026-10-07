"""Dependency-neutral identity of the approval files a Goal checkout consumes.

The strict simulation loader validates policy semantics. This identity tracks
all configured approval/proof bytes, so a shared-store edit cannot leave old
Goal evidence apparently fresh. It reads the same checkout-local configuration
and repository anchors as coverage orchestration, never the control checkout.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from booley.config.coverage_waiver_inputs import CoverageWaiverConfig, parse_coverage_waiver_config
from booley.core.config_paths import resolve_toml
from booley.goals.proposals import digest
from booley.runtime.project_dir import resolve_checkout_project_dir

WAIVER_POLICY_DETAIL_KEY = "goal_waiver_policy"


def policy_location(work_dir: Path) -> tuple[CoverageWaiverConfig | None, Path]:
    """Resolve exactly the config and anchor used by this checkout's coverage."""
    project = resolve_checkout_project_dir(work_dir)
    path = resolve_toml(project)
    config = parse_coverage_waiver_config(tomllib.loads(path.read_text()) if path.exists() else {})
    anchor = (
        project if config is not None and config.anchor == "project_data_repository" else work_dir
    )
    return config, anchor


def waiver_policy_fingerprint(
    work_dir: Path, *, overrides: dict[Path, bytes | None] | None = None
) -> dict[str, Any]:
    """Read immutable file identities, or predict captured promotion effects."""
    config, anchor = policy_location(work_dir)
    if config is None:
        value: dict[str, Any] = {"config": None, "files": {}}
        return {**value, "digest": digest(value)}
    root = anchor / config.directory
    if any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
        raise ValueError(f"approved waiver directory is missing or a symlink: {root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"approved waiver policy contains a symlink: {path}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    for path, content in (overrides or {}).items():
        name = path.relative_to(root).as_posix()
        if content is None:
            files.pop(name, None)
        else:
            files[name] = hashlib.sha256(content).hexdigest()
    value = {
        "config": {"anchor": config.anchor, "directory": config.directory},
        "root": str(root.resolve()),
        "files": files,
    }
    return {**value, "digest": digest(value)}
