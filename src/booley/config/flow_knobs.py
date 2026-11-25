"""One validator per ``[flows.<name>]`` knob that Flows and Doctor both read.

Each reader raises :class:`BoundaryError`; its text is both the Flow's refusal
and Doctor's FAIL, so a knob is judged the same wherever it is read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from types import MappingProxyType
from typing import Any

from booley.core.boundary import BoundaryError, is_str_list, require_bool, require_positive_int


def _field(flow_name: str, knob: str) -> str:
    return f"booley.toml [flows.{flow_name}].{knob}"


def timeout_ms(flow_name: str, section: Mapping[str, Any], knob: str) -> int | None:
    """Return a positive timeout knob in milliseconds, or ``None`` when unset."""
    if knob not in section:
        return None
    return require_positive_int(section[knob], field=_field(flow_name, knob))


def enabled(flow_name: str, section: Mapping[str, Any]) -> bool:
    """Return ``enabled`` (default ``True``); anything but a boolean raises."""
    return require_bool(section, "enabled", default=True, field=_field(flow_name, "enabled"))


def pre_run_commands(flow_name: str, section: Mapping[str, Any]) -> list[str]:
    """Return the shell lines to run before each sim run; unset yields ``[]``."""
    value = section.get("pre_run_commands")
    if value is None:
        return []
    if not is_str_list(value) or not all(value):
        raise BoundaryError(
            f"{_field(flow_name, 'pre_run_commands')} must be a list of strings, "
            "none empty (shell lines run before each sim run)"
        )
    return list(value)


SHARED_KNOBS: Mapping[str, Callable[[str, Mapping[str, Any]], object]] = MappingProxyType(
    {
        "timeout_ms": partial(timeout_ms, knob="timeout_ms"),
        "build_timeout_ms": partial(timeout_ms, knob="build_timeout_ms"),
        "trace_convert_timeout_ms": partial(timeout_ms, knob="trace_convert_timeout_ms"),
        "enabled": enabled,
        "pre_run_commands": pre_run_commands,
    }
)
