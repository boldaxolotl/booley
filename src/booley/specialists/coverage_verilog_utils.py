"""Verilog name/value canonicalization + JSON-block extraction helpers.

Pure, stateless free functions extracted from ``coverage_analyst`` (Principle 8,
SRP). They cover three concerns used by ``CoverageAnalystSpecialist``:

  * Signal-name canonicalization — strip hierarchy/bit-range to a leaf name and
    match specialist-supplied names against bwave's hierarchical stats.
  * Verilog value canonicalization — normalize literals ('d3, 'hFF, 0xAB, 3%d,
    enum/localparam symbols) to canonical uppercase hex for comparison, plus the
    RTL parsing that builds the symbolic-name -> value map.
  * LLM-output plumbing — coerce untrusted FSM register data into a trusted
    shape and pull a JSON object out of fenced/bare model output.

Sole consumer: ``coverage_analyst.CoverageAnalystSpecialist`` (imports these back).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coverage_analyst import SignalStats

logger = logging.getLogger(__name__)


def _signal_leaf(name: str) -> str:
    """Strip hierarchy prefix and bit-range suffix to get the bare signal name.

    "dut.state[2:0]" -> "state", "top.ctrl.en" -> "en", "state" -> "state"
    """
    # Strip Verilog bit-range suffix  [N:M] or [N]
    bare = re.sub(r"\[\d+(?::\d+)?\]$", "", name)
    # Return leaf after last hierarchy separator
    return bare.rsplit(".", 1)[-1]


def _find_signal(
    signal_stats: list[SignalStats],
    sig_name: str,
    *,
    merge_ambiguous: bool = False,
) -> SignalStats | None:
    """Find a SignalStats entry matching *sig_name* (flat or hierarchical).

    Tries exact match first, then falls back to leaf-name matching.

    When *merge_ambiguous* is True and multiple candidates share the same leaf
    name (e.g. two sub-instances of the same module), their value histograms are
    merged into a synthetic SignalStats.  This is the correct semantic for FSM
    coverage: the specialist identifies FSMs from the module definition, so all
    instances share the same RTL and their observed states should be unioned.
    """
    # Lazy import breaks the coverage_analyst <-> coverage_verilog_utils cycle.
    from .coverage_analyst import SignalStats

    # Exact match (fast path)
    for s in signal_stats:
        if s.name == sig_name:
            return s
    # Leaf match: specialist gives "state", bwave has "dut.state[2:0]"
    query_leaf = _signal_leaf(sig_name)
    candidates = [s for s in signal_stats if _signal_leaf(s.name) == query_leaf]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1 and merge_ambiguous:
        logger.info(
            "Merging %d ambiguous leaf matches for '%s': %s",
            len(candidates),
            sig_name,
            [c.name for c in candidates],
        )
        merged_hist: dict[str, int] = {}
        for c in candidates:
            for k, v in c.value_hist.items():
                merged_hist[k] = merged_hist.get(k, 0) + v
        return SignalStats(
            name=sig_name,
            transitions=max(c.transitions for c in candidates),
            value_hist=merged_hist,
        )
    return None


def _build_rtl_name_map(rtl_context: str) -> dict[str, str]:
    """Parse RTL source text for enum and localparam definitions.

    Returns a mapping from symbolic name (uppercased) to its canonical numeric
    value (via _canon_value), e.g. {"IDLE": "0", "STATE_ACTIVE": "1"}.
    """
    name_map: dict[str, str] = {}

    # localparam / parameter: `localparam FOO = 4'd3;` or `localparam FOO = 3;`
    for m in re.finditer(
        r"\b(?:localparam|parameter)\s+(\w+)\s*=\s*([^;,\)]+)",
        rtl_context,
    ):
        sym, val = m.group(1).strip(), m.group(2).strip()
        canon = _canon_value(val)
        name_map[sym.upper()] = canon

    # `define: ``define FOO 4'd3`
    for m in re.finditer(r"`define\s+(\w+)\s+(\S+)", rtl_context):
        sym, val = m.group(1).strip(), m.group(2).strip()
        canon = _canon_value(val)
        name_map[sym.upper()] = canon

    # typedef enum: `typedef enum logic [1:0] {IDLE=2'd0, ACTIVE=2'd1, DONE=2'd2} state_t;`
    for m in re.finditer(
        r"typedef\s+enum\b[^{]*\{([^}]+)\}",
        rtl_context,
        re.DOTALL,
    ):
        body = m.group(1)
        idx = 0
        for raw_entry in body.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            if "=" in entry:
                parts = entry.split("=", 1)
                sym = parts[0].strip()
                val = parts[1].strip()
                canon = _canon_value(val)
                name_map[sym.upper()] = canon
                # Track index for auto-increment of subsequent entries
                try:
                    idx = int(canon, 16) + 1
                except ValueError:
                    idx += 1
            else:
                sym = entry.split()[0] if entry.split() else entry
                name_map[sym.upper()] = hex(idx)[2:].upper().lstrip("0") or "0"
                idx += 1

    return name_map


def _is_numeric_verilog_literal(v: str) -> bool:
    """True if v is an unambiguous numeric literal, not a potential symbolic name.

    Recognizes Verilog radix ('h, 'd, 'b), C-style (0x, 0b), legacy suffixes
    (%d, %h, %b), and bare decimals. Bare hex like "A0" returns False since
    it's ambiguous with symbolic names.
    """
    v = v.strip()
    if "'" in v:
        return True
    if len(v) > 2 and v[:2] in ("0x", "0X", "0b", "0B"):
        return True
    if v.endswith(("%d", "%h", "%b")):
        return True
    return bool(v.isdigit())


def _sanitize_fsm_registers(raw: object) -> list[dict[str, Any]]:
    """Coerce LLM-supplied FSM register data into a trusted internal shape.

    Boundary validator (Principle 5). Accepts whatever the model produced and
    returns a list of dicts, each guaranteed to have a non-empty string
    ``signal`` and a list of string ``expected_values``. Malformed entries
    (non-dict, missing/blank signal) are dropped; non-string expected values
    are stringified. Non-list input degrades to an empty list.
    """
    if not isinstance(raw, list):
        return []

    clean: list[dict[str, Any]] = []
    for reg in raw:
        if not isinstance(reg, dict):
            continue
        signal = reg.get("signal")
        if not isinstance(signal, str) or not signal.strip():
            continue
        raw_expected = reg.get("expected_values", [])
        expected = raw_expected if isinstance(raw_expected, list) else []
        # Downstream calls .strip()/.upper() on each value — force to str.
        clean.append(
            {
                "signal": signal,
                "expected_values": [str(v) for v in expected],
            }
        )
    return clean


def _resolve_fsm_enum_names(
    fsm_registers: list[dict[str, Any]],
    rtl_context: str,
) -> list[dict[str, Any]]:
    """Replace symbolic enum/localparam names in FSM expected_values with numeric equivalents.

    Non-numeric expected values are looked up in a name map built from RTL
    source. Unresolved names are kept as-is (scoring will fail to match them,
    which is the correct conservative behavior).
    """
    if not fsm_registers or not rtl_context:
        return fsm_registers

    name_map = _build_rtl_name_map(rtl_context)
    if not name_map:
        return fsm_registers

    resolved = []
    for reg in fsm_registers:
        expected = reg.get("expected_values", [])
        new_expected = []
        for v in expected:
            if _is_numeric_verilog_literal(v):
                new_expected.append(v)
                continue
            mapped = name_map.get(v.upper().strip())
            if mapped is not None:
                try:
                    new_expected.append(f"'d{int(mapped, 16)}")
                except ValueError:
                    new_expected.append(v)
            else:
                new_expected.append(v)
        resolved.append({**reg, "expected_values": new_expected})

    return resolved


def _canon_value(v: str) -> str:  # noqa: PLR0911 — one early return per Verilog literal / format variant
    """Normalize a value string to canonical uppercase hex for comparison.

    Handles Verilog literals ('d3 -> 3, 'hFF -> FF), legacy %d/%h/%b
    suffixes (3%d -> 3), 0b/0x prefixes, and bare hex strings.
    Values containing x/z (don't-care / high-impedance) are non-comparable.
    """
    v = v.strip()
    # x/z values are non-comparable — return as-is uppercased
    # Strip 0x/0X prefix before checking so hex prefix isn't mistaken for 'x'
    if re.search(r"[xXzZ]", v.replace("0x", "").replace("0X", "")):
        return v.upper()
    # Strip legacy %d/%h/%b suffix
    for sfx in ("%d", "%h", "%b"):
        if v.endswith(sfx):
            v = v[: -len(sfx)]
            break
    # Parse Verilog literal prefix: 'd3, 'hFF, 'b1010, 8'd255
    if "'" in v:
        idx = v.index("'")
        rest = v[idx + 1 :]
        if len(rest) >= 2:
            radix_char = rest[0].lower()
            digits = rest[1:]
            try:
                if radix_char == "h":
                    return digits.upper().lstrip("0") or "0"
                if radix_char == "d":
                    return hex(int(digits))[2:].upper().lstrip("0") or "0"
                if radix_char == "b":
                    return hex(int(digits, 2))[2:].upper().lstrip("0") or "0"
            except ValueError:
                pass
    # 0b/0B binary prefix
    if len(v) > 2 and v[:2] in ("0b", "0B"):
        try:
            return hex(int(v, 2))[2:].upper().lstrip("0") or "0"
        except ValueError:
            pass
    # 0x/0X hex prefix
    if len(v) > 2 and v[:2] in ("0x", "0X"):
        return v[2:].upper().lstrip("0") or "0"
    return v.upper().lstrip("0") or "0"


def _extract_json_block(raw: str) -> dict | None:
    """Extract a JSON object from LLM output (markdown fences, bare JSON, or brace matching)."""
    json_match = re.search(r"```(?:json)?\s*\n(.*?)```", raw, re.DOTALL)
    text = json_match.group(1).strip() if json_match else raw

    if not json_match:
        brace_depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start is not None:
                    text = text[start : i + 1]
                    break

    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    return data if isinstance(data, dict) else None
