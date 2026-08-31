"""Strict, user-owned configuration for Project-independent Host Bootstrap."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, as_str, require_dict, require_int, require_list
from booley.runtime.auth_token import config_dir

DEFAULT_IDLE_TIMEOUT_SECONDS = 7200
DEFAULT_MAX_SESSIONS = 4
HOST_CONFIG_FILENAME = "config.toml"

_TOP_LEVEL_KEYS = frozenset({"interactive"})
_INTERACTIVE_KEYS = frozenset({"idle_timeout_seconds", "max_sessions", "egress_allowlist"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InteractiveHostPolicy:
    """Global Interactive Mode policy applied to the local Docker daemon."""

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


def load_host_policy(path: Path | None = None) -> InteractiveHostPolicy:
    """Load the optional host policy without creating or rewriting its file."""
    resolved = path or host_config_path()
    if not resolved.exists():
        return InteractiveHostPolicy()
    try:
        with resolved.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise HostConfigError(resolved, "", f"malformed TOML: {exc}") from exc
    except OSError as exc:
        raise HostConfigError(resolved, "", f"cannot read file: {exc}") from exc
    return _parse_document(document, resolved)


def _parse_document(document: object, path: Path) -> InteractiveHostPolicy:
    root = _table(document, path, "root")
    _reject_unknown(root, _TOP_LEVEL_KEYS, path, "root")
    raw_interactive = root.get("interactive", {})
    interactive = _table(raw_interactive, path, "interactive")
    _reject_unknown(interactive, _INTERACTIVE_KEYS, path, "interactive")
    return InteractiveHostPolicy(
        idle_timeout_seconds=_positive_int(
            interactive,
            "idle_timeout_seconds",
            DEFAULT_IDLE_TIMEOUT_SECONDS,
            path,
        ),
        max_sessions=_positive_int(
            interactive,
            "max_sessions",
            DEFAULT_MAX_SESSIONS,
            path,
        ),
        egress_allowlist=_egress_allowlist(interactive, path),
    )


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


def _positive_int(section: Mapping[str, Any], key: str, default: int, path: Path) -> int:
    if key not in section:
        return default
    try:
        value = require_int(section[key], field=f"interactive.{key}")
    except BoundaryError as exc:
        raise HostConfigError(path, f"interactive.{key}", "must be an integer") from exc
    if value <= 0:
        raise HostConfigError(path, f"interactive.{key}", "must be positive")
    return value


def _egress_allowlist(section: Mapping[str, Any], path: Path) -> tuple[str, ...]:
    raw = section.get("egress_allowlist", [])
    try:
        values = require_list(raw, field="interactive.egress_allowlist")
    except BoundaryError as exc:
        raise HostConfigError(
            path, "interactive.egress_allowlist", "must be an array of hostnames"
        ) from exc
    domains: list[str] = []
    for index, value in enumerate(values):
        field = f"interactive.egress_allowlist[{index}]"
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
    """Return the exact migration instruction for retired Project policy fields."""
    raw = document.get("interactive")
    if not isinstance(raw, Mapping):
        return None
    retired = [key for key in _INTERACTIVE_KEYS if key in raw]
    if not retired:
        return None
    target = destination or host_config_path()
    lines = [
        f"booley.toml [interactive] host policy is retired; move these fields to {target}:",
        "[interactive]",
    ]
    for key in sorted(retired):
        lines.append(f"{key} = {_toml_value(raw[key])}")
    lines.append(
        "Remove the moved fields from the Project booley.toml; Booley will not migrate them automatically."
    )
    return "\n".join(lines)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return str(value)
