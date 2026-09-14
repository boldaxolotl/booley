"""Typed, fail-closed parsing of project ``[eda.<kind>]`` configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.flow_enablement import retired_config_error
from booley.core.config_paths import resolve_toml
from booley.core.project_dir import resolve_project_dir

SUPPORTED_EDA_KINDS = frozenset({"vivado"})
PROVISIONING_IMAGE = "image"
PROVISIONING_HOST = "host"
_ALLOWED_KEYS = frozenset({"provisioning"})


class EdaConfigError(ValueError):
    """Project EDA configuration violates the built-in provisioning schema."""


@dataclass(frozen=True)
class EdaConfig:
    """One EDA kind's provisioning request.

    Project data can select only the source class. Installations, mounts,
    paths, wrappers, environment, and licensing remain host-owned policy.
    """

    kind: str
    provisioning: str = PROVISIONING_IMAGE


def parse_eda_config(raw: object) -> dict[str, EdaConfig]:
    """Parse the top-level ``[eda]`` table into immutable typed records."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EdaConfigError("booley.toml [eda] must be a table")
    unknown = set(raw) - SUPPORTED_EDA_KINDS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise EdaConfigError(f"unsupported EDA kind(s): {names}; supported: vivado")
    return {kind: _parse_kind(kind, section) for kind, section in raw.items()}


def _parse_kind(kind: str, section: Any) -> EdaConfig:
    if not isinstance(section, dict):
        raise EdaConfigError(f"booley.toml [eda.{kind}] must be a table")
    if "installation" in section:
        raise EdaConfigError(
            f"booley.toml [eda.{kind}].installation is retired; "
            "select the installation with `booley eda grant add --installation ...`"
        )
    unknown = set(section) - _ALLOWED_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise EdaConfigError(f"booley.toml [eda.{kind}] has unknown key(s): {names}")
    provisioning = section.get("provisioning", PROVISIONING_IMAGE)
    if provisioning not in {PROVISIONING_IMAGE, PROVISIONING_HOST}:
        raise EdaConfigError(f"booley.toml [eda.{kind}].provisioning must be 'image' or 'host'")
    return EdaConfig(kind=kind, provisioning=provisioning)


def load_eda_config(project_root: Path) -> dict[str, EdaConfig]:
    """Load declarative EDA requests without host provisioning policy."""
    try:
        project_dir = resolve_project_dir(project_root)
    except FileNotFoundError:
        return {}
    path = resolve_toml(project_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EdaConfigError(f"cannot read {path}: {exc}") from exc
    migration = retired_config_error(raw)
    if migration:
        raise EdaConfigError(migration)
    return parse_eda_config(raw.get("eda"))
