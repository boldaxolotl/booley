"""Strict, user-owned configuration for Project-independent Host Bootstrap."""

from __future__ import annotations

import ipaddress
import json
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, as_str, require_dict, require_int, require_list
from booley.core.user_paths import config_dir

DEFAULT_IDLE_TIMEOUT_SECONDS = 7200
DEFAULT_MAX_SESSIONS = 4
HOST_CONFIG_FILENAME = "config.toml"
HOST_POLICY_MIGRATION_GUIDANCE = (
    "If the host config uses legacy [interactive], rename it to [sandbox] first, "
    "preserving all existing settings. Update the existing [sandbox] table "
    "instead of adding a second policy table."
)

_TOP_LEVEL_KEYS = frozenset({"sandbox", "interactive"})
_SANDBOX_KEYS = frozenset({"idle_timeout_seconds", "max_sessions", "egress_allowlist"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SandboxHostPolicy:
    """Global Sandbox Policy applied to the local Docker daemon."""

    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    egress_allowlist: tuple[str, ...] = ()


class HostConfigError(ValueError):
    """A host configuration boundary failed strict validation."""

    def __init__(self, path: Path, field: str, detail: str) -> None:
        self.path = path
        self.field = field
        self.detail = detail
        location = f" ({field})" if field else ""
        super().__init__(f"invalid Booley host config {path}{location}: {detail}")


def host_config_path() -> Path:
    """Return the XDG-aware user-owned host policy path."""
    return config_dir() / HOST_CONFIG_FILENAME


def load_host_policy(
    path: Path | None = None, *, on_deprecation: Callable[[str], None] | None = None
) -> SandboxHostPolicy:
    """Load the optional host policy without creating or rewriting its file."""
    resolved = path or host_config_path()
    if not resolved.exists():
        return SandboxHostPolicy()
    try:
        with resolved.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise HostConfigError(resolved, "", f"malformed TOML: {exc}") from exc
    except OSError as exc:
        raise HostConfigError(resolved, "", f"cannot read file: {exc}") from exc
    policy = _parse_document(document, resolved)
    if on_deprecation is not None and "interactive" in document:
        on_deprecation(_legacy_host_policy_message(document, resolved))
    return policy


def _parse_document(document: object, path: Path) -> SandboxHostPolicy:
    root = _table(document, path, "root")
    _reject_unknown(root, _TOP_LEVEL_KEYS, path, "root")
    table = "sandbox" if "sandbox" in root else "interactive"
    section = _table(root.get(table, {}), path, table)
    _reject_unknown(section, _SANDBOX_KEYS, path, table)
    return SandboxHostPolicy(
        idle_timeout_seconds=_positive_int(
            section,
            "idle_timeout_seconds",
            DEFAULT_IDLE_TIMEOUT_SECONDS,
            path,
            table,
        ),
        max_sessions=_positive_int(
            section,
            "max_sessions",
            DEFAULT_MAX_SESSIONS,
            path,
            table,
        ),
        egress_allowlist=_egress_allowlist(section, path, table),
    )


def _legacy_host_policy_message(document: Mapping[str, Any], path: Path) -> str:
    if "sandbox" in document:
        return (
            f"{path}: [interactive] is deprecated and ignored because [sandbox] is present. "
            "Remove [interactive]; [sandbox] supplies the entire Sandbox Policy (no merge)."
        )
    section = document["interactive"]
    lines = [f"{path}: [interactive] is deprecated; replace it with:", "[sandbox]"]
    lines.extend(f"{key} = {_toml_value(section[key])}" for key in sorted(section))
    lines.append("Booley will not rewrite the host config automatically.")
    return "\n".join(lines)


def _table(value: object, path: Path, field: str) -> dict[str, Any]:
    try:
        return require_dict(value, field=field)
    except BoundaryError as exc:
        raise HostConfigError(path, field, "must be a TOML table") from exc


def _reject_unknown(
    section: Mapping[str, Any], allowed: frozenset[str], path: Path, field: str
) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise HostConfigError(path, field, f"unknown key(s): {joined}")


def _positive_int(
    section: Mapping[str, Any], key: str, default: int, path: Path, table: str
) -> int:
    if key not in section:
        return default
    try:
        value = require_int(section[key], field=f"{table}.{key}")
    except BoundaryError as exc:
        raise HostConfigError(path, f"{table}.{key}", "must be an integer") from exc
    if value <= 0:
        raise HostConfigError(path, f"{table}.{key}", "must be positive")
    return value


def _egress_allowlist(section: Mapping[str, Any], path: Path, table: str) -> tuple[str, ...]:
    raw = section.get("egress_allowlist", [])
    try:
        values = require_list(raw, field=f"{table}.egress_allowlist")
    except BoundaryError as exc:
        raise HostConfigError(
            path, f"{table}.egress_allowlist", "must be an array of hostnames"
        ) from exc
    domains: list[str] = []
    for index, value in enumerate(values):
        field = f"{table}.egress_allowlist[{index}]"
        hostname = as_str(value)
        if hostname is None:
            raise HostConfigError(path, field, "must be a hostname string")
        domains.append(_hostname(hostname, path, field))
    return tuple(domains)


def _hostname(value: str, path: Path, field: str) -> str:
    hostname = value.strip().lower()
    invalid_syntax = any(token in hostname for token in ("://", "/", "\\", ":", "*"))
    if not hostname or invalid_syntax or hostname.endswith(".") or len(hostname) > 253:
        raise HostConfigError(
            path, field, "must be a hostname only (no scheme, path, port, or wildcard)"
        )
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise HostConfigError(path, field, "IP literals are not allowed")
    labels = hostname.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise HostConfigError(path, field, f"invalid hostname {value!r}")
    return hostname


def retired_project_policy_message(
    document: Mapping[str, Any], *, destination: Path | None = None
) -> str | None:
    """Return migration instructions for host policy misplaced in Project config."""
    target = destination or host_config_path()
    messages: list[str] = []
    for table in ("interactive", "sandbox"):
        raw = document.get(table)
        if not isinstance(raw, Mapping):
            continue
        misplaced = sorted(_SANDBOX_KEYS.intersection(raw))
        if not misplaced:
            continue
        reason = (
            "host policy is retired" if table == "interactive" else "contains host-only policy"
        )
        lines = [
            f"booley.toml [{table}] {reason}; move these fields to {target}:",
            "[sandbox]",
        ]
        lines.extend(f"{key} = {_toml_value(raw[key])}" for key in misplaced)
        lines.extend(
            (
                HOST_POLICY_MIGRATION_GUIDANCE,
                "Remove the moved fields from the Project booley.toml; "
                "Booley will not migrate them automatically.",
            )
        )
        messages.append("\n".join(lines))
    return "\n\n".join(messages) or None


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return str(value)
