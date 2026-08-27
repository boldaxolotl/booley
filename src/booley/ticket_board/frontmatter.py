"""YAML frontmatter parsing and serialization for ticket markdown files."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Field ordering for format_frontmatter
_FM_FIELD_ORDER = [
    # Core fields
    "summary",
    "type",
    "branch",
    "scope",
    # Planning fields (optional, adjacent)
    "spec",
    # Relationship fields
    "on_success",
    "integration_base",
    "dependencies",
    "priority",
    # Immutable runtime (set once, never mutated during execution)
    "feature_branch",
    "created",
    "base_sha",
    "target_contract",
    "target_contract_history",
]

# Chars that require quoting a YAML string value.
# '#' is included so values containing '#' round-trip safely: on the next
# parse, an unquoted `key: foo # bar` would have ' # bar' stripped as an
# inline comment (see _strip_inline_yaml_comment below).  Quoting forces
# the parser to treat the '#' as literal.
_YAML_SPECIAL_RE = re.compile(r"[:\"\[\]\{\}#]")


def _strip_inline_yaml_comment(val):
    """Strip an unquoted inline YAML comment from *val*.

    Rules (conservative to avoid silent data loss on ticket strings that
    legitimately contain '#'):
      * If the value starts with a quote or list-bracket, return as-is.
      * Otherwise, scan left-to-right tracking quote state (single, double).
        Strip at the first '#' that is (a) outside any quoted segment AND
        (b) preceded by whitespace AND (c) followed by whitespace (or end
        of string).

    Compared to the previous ``after_hash[0].isalpha()`` heuristic, this
    preserves:
      * 'fix issue#42'          — '#' not preceded by space (no strip)
      * 'see #123 alpha'        — '#' preceded by space but followed by '1' (no strip)
      * '"foo # bar"'           — leading quote skips stripping entirely
      * 'hex FF00AA # checksum' — still correctly strips the comment

    The old heuristic silently truncated values like 'branch: foo # alpha-bar'
    (alpha char after '# ' was treated as a comment) and failed to quote
    '#'-containing values on round-trip.
    """
    if not val:
        return val
    if val[0] in ('"', "'", "["):
        return val
    in_single = False
    in_double = False
    prev = ""
    for i, ch in enumerate(val):
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_single and not in_double and prev.isspace():
            nxt = val[i + 1] if i + 1 < len(val) else ""
            # Require whitespace (or EOL) after '#' so '#123'-style data
            # references are kept intact.
            if nxt == "" or nxt.isspace():
                return val[:i].rstrip()
        prev = ch
    return val


def _split_respecting_quotes(s, delimiter=","):
    """Split on *delimiter* only when outside quotes and nested collections."""
    parts = []
    current = []
    in_single = False
    in_double = False
    nesting: list[str] = []
    closing = {"[": "]", "{": "}", "(": ")"}
    for ch in s:
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif not in_single and not in_double and ch in closing:
            nesting.append(closing[ch])
            current.append(ch)
        elif not in_single and not in_double and nesting and ch == nesting[-1]:
            nesting.pop()
            current.append(ch)
        elif ch == delimiter and not in_single and not in_double and not nesting:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _yaml_scalar(val):
    """Format a scalar value for YAML output."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    s = str(val)
    # Quote empty strings so round-trip preserves "" (bare "key:" parses as
    # an empty block list, which then fails string-typed field validation).
    if s == "":
        return '""'
    # Quote strings that look like booleans or numbers (including negative)
    if s.lower() in ("true", "false") or re.match(r"^-?\d+$", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    # Quote strings with special YAML chars
    if _YAML_SPECIAL_RE.search(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _coerce_scalar(val):
    """Coerce a bare YAML scalar string to the matching Python type.

    Handles null, booleans, and integers; returns the original string
    otherwise.  Quoted values must be unquoted by the caller before
    calling this -- quoting is how YAML forces strings.
    """
    low = val.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.match(r"^-?\d+$", val):
        return int(val)
    return val


def _handle_level2_list_item(line, current_section_dict):
    """Append a 6-space-indented list item to the last key in current_section_dict."""
    item = re.sub(r"^      -\s+", "", line).strip()
    if not current_section_dict:
        return
    last_key = list(current_section_dict.keys())[-1]
    last_val = current_section_dict[last_key]
    if isinstance(last_val, list):
        last_val.append(_coerce_scalar(item))
    elif isinstance(last_val, (str, int, float, bool)) or last_val is None:
        current_section_dict[last_key] = [last_val, _coerce_scalar(item)]
    else:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "Expected list for key %r, got %s; skipping item %r",
            last_key,
            type(last_val).__name__,
            item,
        )


def _peek_for_list_items(fm_lines, idx):
    """Check if lines after idx contain 6-space-indented list items."""
    peek = idx + 1
    while peek < len(fm_lines) and not fm_lines[peek].strip():
        peek += 1
    return peek < len(fm_lines) and re.match(r"^      -\s+", fm_lines[peek])


def _peek_for_dict_items(fm_lines, idx):
    """Check if lines after idx contain 6-space-indented dict keys."""
    peek = idx + 1
    while peek < len(fm_lines) and not fm_lines[peek].strip():
        peek += 1
    return peek < len(fm_lines) and re.match(r"^      \w[\w.-]*:\s*", fm_lines[peek])


def _parse_level3_dict(fm_lines, start_idx):
    """Parse 6-space-indented dict keys into a dict. Returns (dict, next_idx)."""
    result: dict[str, Any] = {}
    idx = start_idx
    last_key: str | None = None
    while idx < len(fm_lines):
        line = fm_lines[idx]
        if not line.strip():
            idx += 1
            continue
        m = re.match(r"^      (\w[\w.-]*):\s*(.*)", line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            parsed = _parse_inline_value(v)
            # Check for level-4 list items (8-space indent with -)
            if parsed is None and not v:
                peek = idx + 1
                while peek < len(fm_lines) and not fm_lines[peek].strip():
                    peek += 1
                if peek < len(fm_lines) and re.match(r"^        -\s+", fm_lines[peek]):
                    parsed = []
            result[k] = parsed
            last_key = k
            idx += 1
            continue
        # Level-4 list item (8-space indent with -)
        m_list = re.match(r"^        -\s+(.*)", line)
        if m_list and last_key is not None:
            item = _coerce_scalar(m_list.group(1).strip())
            val = result[last_key]
            if isinstance(val, list):
                val.append(item)
            elif val is None:
                result[last_key] = [item]
            idx += 1
            continue
        break
    return result, idx


def _parse_nested_line(line, fm_lines, idx, result, current_section, current_section_dict):
    """Process a single line in a nested block. Returns (current_section, current_section_dict, next_idx, done)."""
    # Level-1 key (2-space indent)
    m1 = re.match(r"^  (\w[\w.-]*):\s*(.*)", line)
    if m1:
        if current_section is not None:
            result[current_section] = current_section_dict
        section = m1.group(1)
        val = m1.group(2).strip()
        if val:
            result[section] = _parse_inline_value(val)
            return None, {}, idx + 1, False
        return section, {}, idx + 1, False

    # Level-2 key (4-space indent)
    m2 = re.match(r"^    (\w[\w.-]*):\s*(.*)", line)
    if m2 and current_section is not None:
        k, v = m2.group(1), m2.group(2).strip()
        parsed = _parse_inline_value(v)
        if parsed is None and not v:
            if _peek_for_list_items(fm_lines, idx):
                parsed = []
            elif _peek_for_dict_items(fm_lines, idx):
                parsed, next_idx = _parse_level3_dict(fm_lines, idx + 1)
                current_section_dict[k] = parsed
                return current_section, current_section_dict, next_idx, False
        current_section_dict[k] = parsed
        return current_section, current_section_dict, idx + 1, False

    # Level-2 list item (6-space indent)
    if re.match(r"^      -\s+", line) and current_section is not None:
        _handle_level2_list_item(line, current_section_dict)
        return current_section, current_section_dict, idx + 1, False

    return current_section, current_section_dict, idx, True  # unrecognized


def _parse_nested_block(fm_lines: list[str], start_idx: int) -> tuple[dict[str, Any], int]:
    """Parse a 2-level nested YAML dict block starting after ``key:``.

    Returns (parsed_dict, next_line_index).
    """
    result: dict[str, Any] = {}
    idx = start_idx
    current_section: str | None = None
    current_section_dict: dict[str, Any] = {}

    while idx < len(fm_lines):
        line = fm_lines[idx]
        if not line.strip():
            idx += 1
            continue
        if re.match(r"^(\w[\w.-]*):", line):
            break  # top-level key -> left the nested block

        current_section, current_section_dict, idx, done = _parse_nested_line(
            line, fm_lines, idx, result, current_section, current_section_dict
        )
        if done:
            break

    if current_section is not None:
        result[current_section] = current_section_dict
    return result, idx


def _parse_inline_value(val: str) -> Any:  # noqa: PLR0911 — one early return per inline YAML value shape (null/list/dict/bool/num/str)
    """Parse a single inline YAML value (scalar, list, or dict)."""
    val = _strip_inline_yaml_comment(val)
    if not val:
        return None
    if val == "[]":
        return []
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if inner:
            return [
                _parse_inline_value(item.strip())
                for item in _split_respecting_quotes(inner)
                if item.strip()
            ]
        return []
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        if inner:
            result = {}
            for raw_pair in _split_respecting_quotes(inner):
                pair = raw_pair.strip()
                if not pair:
                    continue
                if ":" not in pair:
                    continue
                k, v = pair.split(":", 1)
                result[k.strip()] = _parse_inline_value(v.strip())
            return result
        return {}
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        inner = val[1:-1]
        if val.startswith('"'):
            inner = inner.replace("\\\\", "\\").replace('\\"', '"')
        return inner
    return _coerce_scalar(val)


# Keys that use nested block-dict format (2-level indentation)
_NESTED_BLOCK_KEYS = frozenset({"criteria", "on_success", "target_contract"})


def _extract_fm_block(text):
    """Extract frontmatter lines and body from text.

    Returns (fm_lines, body) or (None, text) if no frontmatter found.
    """
    if text and text[0] == "﻿":
        text = text[1:]
    lines = text.split("\n")
    markers = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(markers) < 2:
        return None, text
    start, end = markers[0], markers[1]
    fm_lines = lines[start + 1 : end]
    body = "\n".join(lines[end + 1 :]).strip()
    return fm_lines, body


def _parse_key_value(key, val, fm_lines, idx, fields):
    """Parse a key: value pair, handling nested blocks, lists, dicts, scalars.

    Returns (current_key, current_list, next_idx). When next_idx is not None,
    caller should set idx = next_idx and continue (nested block consumed lines).
    """
    val = _strip_inline_yaml_comment(val)

    if not val and key in _NESTED_BLOCK_KEYS:
        nested, next_idx = _parse_nested_block(fm_lines, idx + 1)
        fields[key] = nested
        return None, None, next_idx

    if not val:
        return key, [], None  # start of block list

    fields[key] = _parse_inline_value(val)
    return None, None, None


def _process_fm_line(line, fm_lines, idx, fields, current_key, current_list):
    """Process a single frontmatter line. Returns (current_key, current_list, next_idx)."""
    # List item: "  - value"
    if re.match(r"^\s+-\s+", line):
        item = re.sub(r"^\s+-\s+", "", line).strip()
        if current_key and current_list is not None:
            current_list.append(_parse_inline_value(item) if item else item)
        return current_key, current_list, idx + 1

    # Scalar: "key: value" or "key:"
    m = re.match(r"^(\w[\w.-]*):\s*(.*)", line)
    if m:
        if current_key and current_list is not None:
            fields[current_key] = current_list
        key = m.group(1)
        val = m.group(2).strip()
        current_key, current_list, next_idx = _parse_key_value(key, val, fm_lines, idx, fields)
        if next_idx is not None:
            return current_key, current_list, next_idx

    return current_key, current_list, idx + 1


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from ticket markdown.

    Supports flat scalars, inline/block lists, inline dicts, and
    2-level nested dicts for keys in _NESTED_BLOCK_KEYS.
    Returns (fields_dict, body_str).
    """
    fm_lines, body = _extract_fm_block(text)
    if fm_lines is None:
        return {}, body

    fields = {}
    current_key = None
    current_list = None
    idx = 0

    while idx < len(fm_lines):
        line = fm_lines[idx]
        if not line.strip():
            idx += 1
            continue
        current_key, current_list, idx = _process_fm_line(
            line, fm_lines, idx, fields, current_key, current_list
        )

    if current_key and current_list is not None:
        fields[current_key] = current_list

    # New format: criteria lives in a ## Criteria body section
    if "criteria" not in fields:
        from .criteria_markdown import parse_criteria_section

        criteria = parse_criteria_section(body)
        if criteria is not None:
            fields["criteria"] = criteria

    return fields, body


def _format_nested_dict(key, val, lines):
    """Format a nested block dict (criteria, on_success) as YAML lines."""
    lines.append(f"{key}:")
    for section_name, section_val in val.items():
        if isinstance(section_val, dict):
            lines.append(f"  {section_name}:")
            for sk, sv in section_val.items():
                if isinstance(sv, list):
                    lines.append(f"    {sk}:")
                    for item in sv:
                        lines.append(f"      - {_yaml_scalar(item)}")
                elif isinstance(sv, dict):
                    lines.append(f"    {sk}:")
                    for dk, dv in sv.items():
                        if isinstance(dv, list):
                            lines.append(f"      {dk}:")
                            for item in dv:
                                lines.append(f"        - {_yaml_scalar(item)}")
                        else:
                            lines.append(f"      {dk}: {_yaml_scalar(dv)}")
                else:
                    lines.append(f"    {sk}: {_yaml_scalar(sv)}")
        else:
            lines.append(f"  {section_name}: {_yaml_scalar(section_val)}")


def _format_target_contract(val, lines):
    """Format the schema's one-level mapping with an inline Target list."""
    lines.append("target_contract:")
    for field_name, field_value in val.items():
        if isinstance(field_value, list):
            items = ", ".join(_yaml_inline_value(item) for item in field_value)
            lines.append(f"  {field_name}: [{items}]")
        else:
            lines.append(f"  {field_name}: {_yaml_scalar(field_value)}")


def _yaml_inline_value(value: Any) -> str:
    """Format a scalar or collection for the frontmatter parser's inline subset."""
    if isinstance(value, dict):
        items = ", ".join(f"{key}: {_yaml_inline_value(item)}" for key, item in value.items())
        return f"{{{items}}}"
    if isinstance(value, list):
        return f"[{', '.join(_yaml_inline_value(item) for item in value)}]"
    return _yaml_scalar(value)


def _format_field(key, val, lines):
    """Append YAML lines for a single field (list, dict, or scalar)."""
    if isinstance(val, list):
        if not val:
            lines.append(f"{key}: []")
        else:
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
    elif isinstance(val, dict):
        if not val:
            lines.append(f"{key}: {{}}")
        elif key == "target_contract":
            _format_target_contract(val, lines)
        elif key in _NESTED_BLOCK_KEYS:
            _format_nested_dict(key, val, lines)
        else:
            items = ", ".join(f"{k}: {_yaml_scalar(v)}" for k, v in val.items())
            lines.append(f"{key}: {{{items}}}")
    else:
        lines.append(f"{key}: {_yaml_scalar(val)}")


def format_frontmatter(fields: dict[str, Any], body: str) -> str:
    """Serialize a dict back to YAML frontmatter + body text.

    Field ordering follows _FM_FIELD_ORDER, then any remaining keys
    alphabetically. None-valued fields are serialized as ``key: null``.
    Runtime fields are stripped (they live in progress.json).
    """
    from .constants import RUNTIME_FIELDS

    fields = {k: v for k, v in fields.items() if k not in RUNTIME_FIELDS}

    # Move criteria from YAML to a ## Criteria body section
    criteria = fields.pop("criteria", None)
    if criteria:
        from .criteria_markdown import inject_criteria_into_body

        body = inject_criteria_into_body(criteria, body)

    lines = ["---"]

    ordered_keys = [k for k in _FM_FIELD_ORDER if k in fields]
    extra_keys = sorted(k for k in fields if k not in _FM_FIELD_ORDER)

    for key in ordered_keys + extra_keys:
        if key == "status":
            continue
        val = fields[key]
        if val is None:
            lines.append(f"{key}: null")
            continue
        _format_field(key, val, lines)

    lines.append("---")
    if body:
        return "\n".join(lines) + "\n" + body + "\n"
    return "\n".join(lines) + "\n"


def update_frontmatter(
    file_path: str | Path, updates: dict[str, Any], remove_keys: list[str] | None = None
) -> dict[str, Any]:
    """Read file, parse frontmatter, merge updates, remove keys, write back.

    For None or "" values in updates, the key is removed.
    Returns the updated fields dict.
    """
    file_path = Path(file_path)
    with file_path.open(encoding="utf-8") as f:
        text = f.read()
    fields, body = parse_frontmatter(text)

    # Apply updates
    for k, v in updates.items():
        if v is None or v == "":
            fields.pop(k, None)
        else:
            fields[k] = v

    # Remove specified keys
    if remove_keys:
        for k in remove_keys:
            fields.pop(k, None)

    # Verify the complete candidate before replacing the ticket.  This turns
    # serialization into a transactional boundary: a formatter regression must
    # fail closed instead of silently changing a contract that already passed
    # validation (for example list[dict] campaign criteria becoming strings).
    content = format_frontmatter(fields, body)
    candidate_fields, candidate_body = parse_frontmatter(content)

    from .constants import RUNTIME_FIELDS
    from .criteria_markdown import strip_criteria_from_body

    protected_fields = {
        key: value
        for key, value in fields.items()
        if key not in RUNTIME_FIELDS and key != "status"
    }
    if candidate_fields != protected_fields:
        raise ValueError("serialization changed ticket fields; refusing to replace ticket")

    original_body = strip_criteria_from_body(body).strip()
    serialized_body = strip_criteria_from_body(candidate_body).strip()
    if serialized_body != original_body:
        raise ValueError("serialization changed ticket body; refusing to replace ticket")

    # Atomic write: tmp file + rename to avoid corruption on crash.
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(file_path.parent), suffix=".tmp", prefix=file_path.stem
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp_name).replace(file_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    return fields
