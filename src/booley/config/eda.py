"""Typed, fail-closed parsing of project ``[eda.<kind>]`` configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.flow_enablement import retired_config_error
from booley.core.booley_dir import resolve_booley_dir
from booley.core.config_paths import BooleyTomlError, resolve_toml

SUPPORTED_EDA_KINDS = frozenset({"vivado"})
PROVISIONING_IMAGE = "image"
PROVISIONING_HOST = "host"
_ALLOWED_KEYS = frozenset({"provisioning"})


class EdaConfigError(BooleyTomlError):
    """Project EDA configuration violates the built-in provisioning schema."""


@dataclass(frozen=True)
class EdaConfig:
    """One EDA kind's provisioning request.

    Project configuration can select only the source class. Installations, mounts,
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
    unknown = set(section) - _ALLOWED_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise EdaConfigError(f"booley.toml [eda.{kind}] has unknown key(s): {names}")
    provisioning = section.get("provisioning", PROVISIONING_IMAGE)
    if provisioning not in {PROVISIONING_IMAGE, PROVISIONING_HOST}:
        raise EdaConfigError(f"booley.toml [eda.{kind}].provisioning must be 'image' or 'host'")
    return EdaConfig(kind=kind, provisioning=provisioning)


_LOCATION_SUFFIX = re.compile(r"\(at line (\d+), column \d+\)$")


def eda_config_path(project_root: Path) -> Path | None:
    """Return the Project ``booley.toml`` the EDA loader reads, or ``None`` when absent."""
    try:
        booley_dir = resolve_booley_dir(project_root)
    except FileNotFoundError:
        return None
    path = resolve_toml(booley_dir)
    return path if path.is_file() else None


def toml_error_line(exc: tomllib.TOMLDecodeError) -> int | None:
    lineno: int | None = getattr(exc, "lineno", None)
    if lineno is not None:
        return lineno
    match = _LOCATION_SUFFIX.search(str(exc))
    return int(match.group(1)) if match else None


def load_eda_config(project_root: Path) -> dict[str, EdaConfig]:
    """Load declarative EDA requests without host provisioning policy."""
    path = eda_config_path(project_root)
    if path is None:
        return {}
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        line = toml_error_line(exc) if isinstance(exc, tomllib.TOMLDecodeError) else None
        raise EdaConfigError(f"cannot read {path}: {exc}", path=path, line=line) from exc
    try:
        migration = retired_config_error(raw)
        if migration:
            raise EdaConfigError(migration)
        return parse_eda_config(raw.get("eda"))
    except EdaConfigError as exc:
        raise EdaConfigError(str(exc), path=path) from exc
