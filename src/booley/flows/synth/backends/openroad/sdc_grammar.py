"""Strict line grammar for SDC and wire-RC Tcl that cannot read anything unkeyed.

OpenROAD runs SDC and wire-RC files as full Tcl, which can read files, the
environment, the clock or random numbers. A file is provably side-effect free
only when every line matches this grammar; OpenROAD reuse
(:mod:`booley.flows.synth.openroad_reuse`) and SDC staging rely on it.
"""

from __future__ import annotations

import re
import unicodedata

# fmt: off
# Constraint commands with no file operand or I/O; no Tcl builtins at all.
_SDC_COMMANDS = frozenset({
    "create_clock", "create_generated_clock", "set_input_delay", "set_output_delay",
    "set_false_path", "set_multicycle_path", "set_max_delay", "set_min_delay",
    "set_clock_groups", "set_clock_uncertainty", "set_clock_latency", "set_clock_transition",
    "set_propagated_clock", "set_load", "set_driving_cell", "set_input_transition",
    "set_max_fanout", "set_max_transition", "set_max_capacitance", "set_case_analysis",
    "set_disable_timing", "set_timing_derate", "set_ideal_network", "set_units", "group_path",
    "set_dont_touch", "set_layer_rc", "set_wire_rc",
})
_SDC_GETTERS = frozenset({
    "get_ports", "get_pins", "get_cells", "get_nets", "get_clocks", "all_inputs",
    "all_outputs", "all_clocks", "all_registers", "current_design",
})
# fmt: on
_WS = r"[ \t]"
_PLAIN = r"[A-Za-z0-9_.\-/*:+]+"
# Bus indices are legal only inside a getter's brace-list, where Tcl reads them literally.
_OBJECT = r"(?:[A-Za-z0-9_.\-/*:+]|\[[0-9*:]+\])+"
_BRACE_PLAIN = rf"\{{{_WS}*(?:{_PLAIN}(?:{_WS}+{_PLAIN})*)?{_WS}*\}}"
_BRACE_OBJECT = rf"\{{{_WS}*(?:{_OBJECT}(?:{_WS}+{_OBJECT})*)?{_WS}*\}}"
_GETTER = rf"\[{_WS}*[a-z_]+(?:{_WS}+(?:{_PLAIN}|{_BRACE_OBJECT}))*{_WS}*\]"
_COMMAND_LINE = re.compile(
    rf"{_WS}*(?P<cmd>{_PLAIN})(?:{_WS}+(?:{_GETTER}|{_BRACE_PLAIN}|{_PLAIN}))*{_WS}*"
)
_BRACKET_COMMAND = re.compile(rf"\[{_WS}*([a-z_]+)")


def tcl_decline_reason(text: bytes) -> str | None:
    """Why an SDC/wire-RC file is not provably side-effect free, or ``None``.

    Every line must be blank, a full-line ``#`` comment, or exactly one
    allowlisted constraint command whose arguments are plain words, one-line
    brace-lists, or getter brackets. With no ``$ \\ " ;``, Tcl performs no
    variable or backslash substitution and runs one command per line; the only
    command substitutions are the getters, whose arguments are literal. The
    character rules make the grammar's lines exactly Tcl's lines.
    """
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    bad = next((c for c in decoded if unicodedata.category(c) == "Cc" and c not in "\t\n\r"), None)
    if bad is not None:
        return f"control character {bad!r}"
    if "\r" in decoded.replace("\r\n", ""):
        return "carriage return without newline"
    for number, raw in enumerate(decoded.replace("\r\n", "\n").split("\n"), 1):
        reason = _line_decline_reason(raw)
        if reason is not None:
            return f"line {number}: {reason}"
    return None


def _line_decline_reason(raw: str) -> str | None:
    """Classify one line after stripping only space and tab (Tcl's separators)."""
    line = raw.strip(" \t")
    if not line or line.startswith("#"):
        return None
    if any(c in ';\\$"' or (c != "\t" and not " " <= c <= "~") for c in raw):
        return "non-ASCII character or one of ';', '\\', '$', '\"' in a command"
    match = _COMMAND_LINE.fullmatch(raw)
    if match is None:
        return "argument is not a plain word, brace-list or getter"
    if match["cmd"] not in _SDC_COMMANDS:
        return f"command {match['cmd']!r} is not allowed"
    bracket = next((g for g in _BRACKET_COMMAND.findall(raw) if g not in _SDC_GETTERS), None)
    if bracket is not None:
        return f"bracket command {bracket!r} is not allowed"
    return None
