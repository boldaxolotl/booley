"""Render and parse criteria as a Markdown body section.

Promotes criteria from YAML frontmatter to a visible ## Criteria section
in the ticket body, while preserving the dict format downstream code
expects: {"mandatory": {...}, "optional": {...}}.
"""

from __future__ import annotations

import json
import re
from typing import Any

_COVERAGE_PREFIX = "coverage_"
_REVIEW_CLEAN_RE = re.compile(r"^review_(.+)_clean$")
_BULLET_RE = re.compile(r"^-\s+\*\*(.+?)\*\*(?:\s*\(.*?\))?(?::\s*(.*))?$")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_criteria_section(criteria: dict[str, Any]) -> str:
    """Render criteria dict as a ``## Criteria`` markdown section.

    Input shape: ``{"mandatory": {...}, "optional": {...}}``.
    Returns markdown string (no leading newline).
    """
    mandatory = criteria.get("mandatory", {})
    optional = criteria.get("optional", {})

    parts: list[str] = ["## Criteria"]

    if optional:
        parts.append("")
        parts.append("### Mandatory")

    parts.append("")
    parts.extend(_render_items(mandatory))

    if optional:
        parts.append("")
        parts.append("### Optional")
        parts.append("")
        parts.extend(_render_items(optional))

    return "\n".join(parts)


def _render_items(items: dict[str, Any]) -> list[str]:
    """Render criteria items as bullets, grouping coverage_* and review_*_clean."""
    lines: list[str] = []
    coverage: dict[str, Any] = {}
    reviews: list[str] = []
    coverage_rendered = False
    reviews_rendered = False

    # First pass: collect groups
    for key, val in items.items():
        if key.startswith(_COVERAGE_PREFIX) and not isinstance(val, (list, dict)):
            coverage[key[len(_COVERAGE_PREFIX) :]] = val
        elif _REVIEW_CLEAN_RE.match(key) and val is True:
            reviews.append(_REVIEW_CLEAN_RE.match(key).group(1))

    # Second pass: render in order, inserting groups at first occurrence
    for key, val in items.items():
        if key.startswith(_COVERAGE_PREFIX) and not isinstance(val, (list, dict)):
            if not coverage_rendered and coverage:
                parts = ", ".join(f"{m} >= {v}" for m, v in coverage.items())
                lines.append(f"- **coverage**: {parts}")
                coverage_rendered = True
            continue
        if _REVIEW_CLEAN_RE.match(key) and val is True:
            if not reviews_rendered and reviews:
                focuses = ", ".join(reviews)
                lines.append(f"- **reviews**: {focuses} -- all must be clean")
                reviews_rendered = True
            continue
        lines.append(_render_bullet(key, val))

    # Safety: render groups that had no positional anchor
    if not coverage_rendered and coverage:
        parts = ", ".join(f"{m} >= {v}" for m, v in coverage.items())
        lines.append(f"- **coverage**: {parts}")
    if not reviews_rendered and reviews:
        focuses = ", ".join(reviews)
        lines.append(f"- **reviews**: {focuses} -- all must be clean")

    return lines


def _render_bullet(key: str, val: Any) -> str:
    """Render a single criterion as a markdown bullet."""
    if isinstance(val, bool):
        return f"- **{key}**" if val else f"- **{key}**: false"
    if isinstance(val, list):
        if any(isinstance(item, (dict, list)) for item in val):
            encoded = json.dumps(val, separators=(",", ":"), ensure_ascii=False)
            return f"- **{key}**: `json:{encoded}`"
        return f"- **{key}**: {', '.join(f'`{item}`' for item in val)}"
    if isinstance(val, dict):
        if _dict_requires_json(val):
            encoded = json.dumps(val, separators=(",", ":"), ensure_ascii=False)
            return f"- **{key}**: `json:{encoded}`"
        parts = []
        for dk, dv in val.items():
            if isinstance(dv, list):
                parts.append(f"{dk}: {', '.join(str(x) for x in dv)}")
            else:
                parts.append(f"{dk}: {dv}")
        return f"- **{key}**: {', '.join(parts)}"
    return f"- **{key}**: {val}"


def _dict_requires_json(value: dict[str, Any]) -> bool:
    """Return whether a mapping contains structure the prose form loses."""
    for nested in value.values():
        if isinstance(nested, dict):
            return True
        if isinstance(nested, list) and any(isinstance(item, (dict, list)) for item in nested):
            return True
    return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_criteria_section(body: str) -> dict[str, Any] | None:
    """Extract ``## Criteria`` from *body* into the canonical criteria dict.

    Returns ``{"mandatory": {...}, "optional": {...}}`` or ``None``.
    """
    lines = body.split("\n")

    start = None
    for i, line in enumerate(lines):
        if re.match(r"^## Criteria\b", line):
            start = i
            break
    if start is None:
        return None

    criteria_lines: list[str] = []
    for i in range(start + 1, len(lines)):
        if re.match(r"^## ", lines[i]):
            break
        criteria_lines.append(lines[i])

    return _parse_subsections(criteria_lines)


def _parse_subsections(lines: list[str]) -> dict[str, Any] | None:
    has_subs = any(re.match(r"^### (Mandatory|Optional)\b", l) for l in lines)

    if not has_subs:
        items = _parse_bullets(lines)
        return {"mandatory": items} if items else None

    sections: dict[str, dict[str, Any]] = {}
    current: str | None = None
    current_lines: list[str] = []

    for line in lines:
        m = re.match(r"^### (Mandatory|Optional)\b", line)
        if m:
            if current is not None:
                parsed = _parse_bullets(current_lines)
                if parsed:
                    sections[current] = parsed
            current = m.group(1).lower()
            current_lines = []
        else:
            current_lines.append(line)

    if current is not None:
        parsed = _parse_bullets(current_lines)
        if parsed:
            sections[current] = parsed

    return sections if sections else None


def _parse_bullets(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for raw_line in lines:
        line = raw_line.strip()
        if not line or not line.startswith("- "):
            continue
        m = _BULLET_RE.match(line)
        if not m:
            continue

        key = m.group(1)
        raw_val = m.group(2)

        if raw_val is None or not raw_val.strip():
            if key not in ("coverage", "reviews"):
                result[key] = True
            continue

        raw_val = raw_val.strip()

        if key == "coverage":
            _expand_coverage(raw_val, result)
        elif key == "reviews":
            _expand_reviews(raw_val, result)
        else:
            result[key] = _parse_value(raw_val)

    return result


def _expand_coverage(text: str, result: dict[str, Any]) -> None:
    """``toggle >= 90, fsm = 100`` -> ``coverage_toggle: 90, ...``"""
    for part in text.split(", "):
        m = re.match(r"(\w+)\s*(?:>=|≥|=)\s*(\d+)", part.strip())
        if m:
            result[f"coverage_{m.group(1)}"] = int(m.group(2))


def _expand_reviews(text: str, result: dict[str, Any]) -> None:
    """``rtl_spec, tb_spec -- all must be clean`` -> ``review_*_clean: True``"""
    focuses_part = re.split(r"\s*(?:--|—)\s*", text)[0]
    for raw_focus in focuses_part.split(", "):
        focus = raw_focus.strip()
        if focus:
            result[f"review_{focus}_clean"] = True


def _parse_value(text: str) -> Any:
    """Parse a bullet value into the appropriate Python type."""
    backticks = re.findall(r"`([^`]+)`", text)
    if backticks:
        return _parse_backticks(backticks)
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if re.match(r"^-?\d+$", text):
        return int(text)
    if re.search(r"\w+:\s", text):
        return _parse_dict_value(text)
    return text


def _parse_backticks(values: list[str]) -> Any:
    """Decode canonical structured lists and legacy backticked mappings."""
    if len(values) == 1 and values[0].startswith("json:"):
        try:
            return json.loads(values[0].removeprefix("json:"))
        except json.JSONDecodeError:
            return values
    return [_parse_backtick_value(value) for value in values]


def _parse_backtick_value(value: str) -> Any:
    """Recover structured list items while preserving legacy string items."""
    if not value.startswith("{"):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, dict) else value


def _parse_dict_value(text: str) -> dict[str, Any]:
    """``k1: v1, k2: v2, k3: a, b`` -> dict (continuation = list)."""
    parts = [p.strip() for p in text.split(", ")]
    result: dict[str, Any] = {}
    current_key: str | None = None

    for part in parts:
        if ": " in part:
            k, v = part.split(": ", 1)
            current_key = k.strip()
            result[current_key] = _coerce(v.strip())
        elif current_key is not None:
            prev = result[current_key]
            if isinstance(prev, list):
                prev.append(_coerce(part))
            else:
                result[current_key] = [prev, _coerce(part)]

    # Single-valued "targets" must still be a list for downstream consumers.
    if "targets" in result and not isinstance(result["targets"], list):
        result["targets"] = [result["targets"]]

    return result


def _coerce(val: str) -> Any:
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if re.match(r"^-?\d+$", val):
        return int(val)
    return val


# ---------------------------------------------------------------------------
# Body manipulation
# ---------------------------------------------------------------------------


def inject_criteria_into_body(criteria: dict[str, Any], body: str) -> str:
    """Prepend rendered ``## Criteria`` section to *body* (idempotent)."""
    body = strip_criteria_from_body(body).strip()
    section = render_criteria_section(criteria)
    if body:
        return "\n" + section + "\n\n" + body
    return "\n" + section


def strip_criteria_from_body(body: str) -> str:
    """Remove ``## Criteria`` section (heading through next ``##`` or end)."""
    lines = body.split("\n")
    result: list[str] = []
    in_criteria = False
    for line in lines:
        if re.match(r"^## Criteria\b", line):
            in_criteria = True
            continue
        if in_criteria and re.match(r"^## ", line):
            in_criteria = False
        if not in_criteria:
            result.append(line)
    return "\n".join(result)
