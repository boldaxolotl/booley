"""Shared boundary coercion/validation primitives for untrusted JSON/TOML data.

WHAT: A small, reusable toolkit for the recurring problem of parsing values that
crossed a trust boundary — ``booley.toml`` sections, usage logs, endpoint-report
metrics, and similar untrusted JSON. These are the same three or four shapes
(dict, string-list, finite number, positive int) that ~13 call sites across the
tree had each hand-rolled with their own ``isinstance``/``float()``/try-except.

WHY stdlib-only: this lives in ``booley.core``, the dependency-free bottom of
the import graph (see ``core/__init__.py``). It must not import from
``harness``/``mcp_tools``/``ticket_board``, and it deliberately does NOT reach for
``pydantic``/``jsonschema`` (both project deps) — those model whole documents,
whereas this guards individual raw scalars at the boundary.

Two flavours per shape:
  * ``as_*`` — lenient: bad input degrades to a caller-supplied *default*.
  * ``require_*`` — strict: bad input raises :class:`BoundaryError`.

THE BOOL TRAP: ``isinstance(True, int)`` is ``True`` in Python — ``bool`` is a
subclass of ``int``. So ``True`` would sail through a naive ``isinstance(v, int)``
numeric check and a ``config timeout of True seconds`` would silently become
``1``. Every numeric coercion below rejects ``bool`` explicitly. Likewise NaN and
``inf`` are valid ``float`` instances but poison arithmetic/serialization, so
numeric coercions require :func:`math.isfinite`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

__all__ = [
    "BoundaryError",
    "as_dict",
    "as_float",
    "as_int",
    "as_positive_int",
    "as_str",
    "as_str_list",
    "is_str_list",
    "require_bool",
    "require_dict",
    "require_finite_number",
    "require_int",
    "require_list",
    "require_opt_str",
    "require_str",
]


class BoundaryError(ValueError):
    """Raised by the strict ``require_*`` helpers when a boundary value is invalid."""


# ---------------------------------------------------------------------------
# Mappings / dicts
# ---------------------------------------------------------------------------


def as_dict(value: Any, *, default: dict | None = None) -> dict | None:
    """Return *value* as a plain dict if it is a Mapping, else *default*."""
    if isinstance(value, Mapping):
        return dict(value)
    return default


def require_dict(value: Any, *, field: str = "value") -> dict:
    """Return *value* as a plain dict, or raise if it is not a Mapping."""
    if not isinstance(value, Mapping):
        raise BoundaryError(f"{field} must be a mapping, got {type(value).__name__}")
    return dict(value)


def require_list(value: Any, *, field: str = "value") -> list:
    """Return *value* when it is a list, or raise with boundary context."""
    if not isinstance(value, list):
        raise BoundaryError(f"{field} must be a list, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------


def as_str(value: Any, default: str | None = None) -> str | None:
    """Return *value* if it is a ``str`` (bool/int are NOT stringified), else *default*."""
    return value if isinstance(value, str) else default


def require_str(mapping: Mapping[str, Any], key: str) -> str:
    """Return ``mapping[key]`` as a non-empty string, or raise (mirrors ``_require_str``)."""
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise BoundaryError(f"{key} must be a non-empty string")
    return value


def require_opt_str(
    mapping: Mapping[str, Any], key: str, *, field: str | None = None
) -> str | None:
    """Return ``mapping[key]`` as a non-empty string, ``None`` when absent, or raise.

    The optional-config-knob shape: an absent (or explicit ``None``) key means
    "not configured" and returns ``None`` so the caller can skip the knob; a
    present value must be a non-empty ``str``. Anything else — notably a TOML
    ``true`` that would otherwise be stringified into an argv token like
    ``--timing-engine True`` — raises :class:`BoundaryError`. *field* overrides
    the key name in the error message (e.g. ``"[flows.x] 'knob'"``).
    """
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BoundaryError(f"{field or key} must be a non-empty string, got {value!r}")
    return value


def is_str_list(value: Any) -> bool:
    """Return True iff *value* is a list whose every element is a ``str``.

    Strict predicate (the duplicated ``_is_string_list`` shape): a bare string
    is NOT a list, and one non-string element rejects the whole value. Use this
    to *validate*; use :func:`as_str_list` to *coerce* lenient input.
    """
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def as_str_list(value: Any, *, default: list[str] | None = None) -> list[str]:
    """Coerce *value* to a list of strings; degrade to *default* (or ``[]``).

    Accepts a bare ``str`` as a single-element list (the classic TOML
    ``source_dirs = "rtl"`` footgun — left alone a string iterates
    character-by-character downstream). A list is filtered to its ``str``
    entries; anything else, or a list that filters empty, falls back to
    *default*. This is the shape duplicated as ``_is_string_list`` /
    ``as_str_list`` / ``_string_list`` across the tree.
    """
    fallback = list(default) if default is not None else []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        strings = [item for item in value if isinstance(item, str)]
        return strings or fallback
    return fallback


# ---------------------------------------------------------------------------
# Booleans
# ---------------------------------------------------------------------------


def require_bool(
    mapping: Mapping[str, Any], key: str, *, default: bool = False, field: str | None = None
) -> bool:
    """Return ``mapping[key]`` as a bool; absent → *default*, non-bool → raise.

    Strict on purpose: TOML has a real boolean type, so a string ``"true"`` or
    an int ``1`` in a bool knob is a config error, not something to coerce —
    ``bool("false")`` is ``True``, the classic truthiness surprise. *field*
    overrides the key name in the error message.
    """
    if key not in mapping:
        return default
    value = mapping[key]
    if not isinstance(value, bool):
        raise BoundaryError(f"{field or key} must be a boolean (true/false), got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Numbers — every path rejects bool (True is an int!) and non-finite floats.
# ---------------------------------------------------------------------------


def _finite_number(value: Any) -> int | float | None:
    """Return *value* as an int/float if it is a finite non-bool number, else None.

    Rejects ``bool`` because ``isinstance(True, int)`` is ``True``; rejects
    NaN/inf because they are valid floats but break arithmetic and JSON.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def as_int(value: Any, default: int | None = None) -> int | None:
    """Coerce *value* to ``int``; degrade to *default* on bad input.

    Accepts finite int/float and numeric strings (``"12"``, ``"3.0"``).
    Rejects ``bool`` (``True`` is an ``int`` subclass — a stray boolean flag
    must not become ``1``) and non-finite floats.
    """
    number = _finite_number(value)
    if number is not None:
        return int(number)
    if isinstance(value, str):
        try:
            parsed = float(value)  # tolerate "3.0"; float() also parses ints
        except (TypeError, ValueError):
            return default
        return int(parsed) if math.isfinite(parsed) else default
    return default


def require_int(value: Any, *, field: str = "value") -> int:
    """Return a strict integer, rejecting bool and all coercible non-int values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoundaryError(f"{field} must be an integer, got {value!r}")
    return value


def as_float(value: Any, default: float | None = None) -> float | None:
    """Coerce *value* to ``float``; degrade to *default* on bad input.

    Accepts finite int/float and numeric strings. Rejects ``bool`` (``True`` is
    an ``int`` subclass) and non-finite floats (NaN/inf) from any source.
    """
    number = _finite_number(value)
    if number is not None:
        return float(number)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) else default
    return default


def require_finite_number(value: Any, *, field: str) -> float:
    """Return *value* as a finite float, or raise (bool-rejecting, NaN/inf-rejecting).

    Rejects ``bool`` because it is an ``int`` subclass; does NOT coerce strings —
    this is the strict metric-parsing shape (``_float_metric``/``_int_metric``)
    where a non-number is a contract violation, not something to silently accept.
    """
    number = _finite_number(value)
    if number is None:
        raise BoundaryError(f"{field} must be a finite number, got {value!r}")
    return float(number)


def as_positive_int(value: Any, default: int, *, field: str | None = None) -> int:
    """Return *value* as a strictly-positive ``int``, else *default* (mirrors ``_coerce_positive_int``).

    Rejects ``bool`` and non-int types outright (no string coercion — config
    knobs are typed in TOML) and rejects ``value <= 0``. When *field* is given,
    a rejected non-``None`` value is a caller concern, so return the default;
    callers that want to warn should compare the result to *value*.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value <= 0:
        return default
    return value
