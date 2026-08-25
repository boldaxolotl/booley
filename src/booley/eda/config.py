"""Typed, fail-closed parsing of project ``[eda.<kind>]`` configuration."""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.config_paths import resolve_toml
from booley.runtime.project_dir import resolve_project_dir

SUPPORTED_EDA_KINDS = frozenset({"vivado"})
PROVISIONING_IMAGE = "image"
PROVISIONING_HOST = "host"
_ALLOWED_KEYS = frozenset({"provisioning"})
_OPAQUE_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ENVIRONMENT_LOOKING = frozenset(
    {"home", "path", "ld_library_path", "ld_preload", "lm_license_file", "xilinxd_license_file"}
)


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


def installation_name_error(value: object) -> str | None:
    """Return why *value* is not a safe opaque installation name."""
    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if _CONTROL_RE.search(value) or not _OPAQUE_NAME_RE.fullmatch(value):
        return "must be an opaque lowercase name (letters, digits, dots, underscores, and hyphens only)"
    if value in {".", ".."} or ".." in value:
        return "must not contain path-like dot segments"
    if value.lower().replace("-", "_") in _ENVIRONMENT_LOOKING:
        return "must not look like an environment variable name"
    return None


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


def validate_host_provisioning_platform(
    configs: dict[str, EdaConfig],
    *,
    platform_name: str | None = None,
) -> None:
    """Reject host-provisioned EDA on unsupported Windows hosts."""
    current_platform = sys.platform if platform_name is None else platform_name
    if current_platform != "win32":
        return
    host_kinds = sorted(
        kind for kind, config in configs.items() if config.provisioning == PROVISIONING_HOST
    )
    if not host_kinds:
        return
    sections = ", ".join(f"[eda.{kind}]" for kind in host_kinds)
    raise EdaConfigError(
        f"host provisioning is unsupported on Windows for {sections}; "
        'set provisioning = "image" or run Booley on a supported Linux x86-64 host'
    )


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


def retired_config_error(raw: dict[str, Any]) -> str | None:
    """Return the first hard-migration error for a removed authority surface."""
    flows = raw.get("flows", {})
    if isinstance(flows, dict):
        for key in ("venue", "backend", "host_setup_commands"):
            if key in flows:
                return _retired_flow_key_error("flows", key, flows[key])
        for name, section in flows.items():
            if not isinstance(section, dict):
                continue
            for key in ("venue", "backend", "host_setup_commands"):
                if key in section:
                    return _retired_flow_key_error(f"flows.{name}", key, section[key])
    sandbox = raw.get("sandbox", {})
    if isinstance(sandbox, dict) and "passthrough_env" in sandbox:
        return "booley.toml [sandbox].passthrough_env is retired; use a host License Profile"
    return None


def _retired_flow_key_error(section: str, key: str, value: object) -> str:
    """Describe the hard migration for one retired Flow execution key."""
    if key == "backend" and str(value).strip() == "none" and section != "flows":
        flow_name = section.removeprefix("flows.")
        return (
            f'booley.toml [{section}].backend = "none" is retired; write instead:\n'
            f"  [flows.{flow_name}]\n  enabled = false"
        )
    if key == "backend":
        return (
            f"booley.toml [{section}].backend is retired; all Flows run inside "
            "the Session Runtime; delete the key"
        )
    return f"booley.toml [{section}].{key} is retired; delete the key"


def load_eda_config(project_root: Path) -> dict[str, EdaConfig]:
    """Load and validate EDA configuration for one Project root."""
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
    configs = parse_eda_config(raw.get("eda"))
    validate_host_provisioning_platform(configs)
    return configs
