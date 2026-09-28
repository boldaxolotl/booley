"""Deterministic, bounded rendering for safe diagnostic record differences."""

from __future__ import annotations

import math
from collections.abc import Mapping

_MISSING = object()
_MAX_FIELD_LENGTH = 128
_MAX_VALUE_LENGTH = 256
_MAX_OUTPUT_LENGTH = 4096
_ELLIPSIS = "..."


def format_differences(recorded: Mapping[str, object], actual: Mapping[str, object]) -> str:
    """Render unequal fields from *recorded* to *actual* without raising.

    Callers must provide diagnostic-safe projections rather than arbitrary domain
    objects. Fields already present in the recorded projection retain that
    projection's order; fields present only in the actual projection are sorted.
    """
    try:
        fields = list(recorded)
        fields.extend(sorted(key for key in actual if key not in recorded))
        differences = []
        for field in fields:
            old = recorded.get(field, _MISSING)
            new = actual.get(field, _MISSING)
            if _equal(old, new):
                continue
            name = _bounded(_escape_controls(str(field)), _MAX_FIELD_LENGTH)
            differences.append(f"{name} {_render(old)} -> {_render(new)}")
        if not differences:
            return "difference unavailable"
        return _bounded("; ".join(differences), _MAX_OUTPUT_LENGTH)
    except Exception:  # noqa: BLE001 -- diagnostics must not mask the original failure
        return "difference unavailable"


def _equal(left: object, right: object) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001 -- hostile boundary values can raise from equality
        return False


def _render(value: object) -> str:
    try:
        return _bounded(_render_unbounded(value), _MAX_VALUE_LENGTH)
    except Exception:  # noqa: BLE001 -- hostile boundary values can raise while rendering
        return f"<unrenderable {type(value).__name__}>"


def _render_unbounded(  # noqa: PLR0911 -- explicit type cases keep rendering fail-safe
    value: object,
) -> str:
    if value is _MISSING:
        return "<missing>"
    if value is None:
        return "<none>"
    if isinstance(value, str):
        return _escape_controls(value) if value else "<empty>"
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        items = sorted(
            ((_render(key), _render(item)) for key, item in value.items()),
            key=lambda item: item[0],
        )
        return "{" + ", ".join(f"{key}: {item}" for key, item in items) + "}"
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(_render(item) for item in value)) + "}"
    if isinstance(value, tuple):
        return "(" + ", ".join(_render(item) for item in value) + ")"
    if isinstance(value, list):
        return "[" + ", ".join(_render(item) for item in value) + "]"
    return _escape_controls(str(value))


def _escape_controls(value: str) -> str:
    rendered = []
    for character in value:
        codepoint = ord(character)
        if character == "\n":
            rendered.append(r"\n")
        elif character == "\r":
            rendered.append(r"\r")
        elif character == "\t":
            rendered.append(r"\t")
        elif codepoint < 32 or codepoint == 127:
            rendered.append(f"\\x{codepoint:02x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - len(_ELLIPSIS)] + _ELLIPSIS
