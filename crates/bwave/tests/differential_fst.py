#!/usr/bin/env python3
"""Shared helpers for comparing FST stores produced by different writers.

The command matrix deliberately exercises metadata, snapshots, event streams,
search, and JSON output.  Normalization is limited to representation-level
differences that do not change waveform semantics.
"""

from __future__ import annotations

import json
import re
import subprocess
from decimal import Decimal, InvalidOperation

COMMANDS: tuple[tuple[str, ...], ...] = (
    ("list",),
    ("list", "--format", "json"),
    ("stats", "--async", "-s", "*"),
    ("stats", "--async", "-s", "*", "--format", "json"),
    ("signal", "--async", "-t", "0t:80t", "--limit", "200"),
    ("value", "--async", "--at", "0t"),
    ("value", "--async", "--at", "50t"),
    ("find", "*clk*", "rising", "--async", "--limit", "20"),
    ("wave", "--async", "-t", "0t:80t", "-s", "*clk*", "--limit", "20"),
)

_UNKNOWN_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])([xX]+|[zZ]+)(?![A-Za-z0-9_])")
_FLOAT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?(?![A-Za-z0-9_])"
)
_EVENT_RE = re.compile(r"^(?P<tick>\d+|cycle\s+\d+)\s+(?P<signal>\S+)\s+(?P<value>\S+)$")
_COLUMN_GAP_RE = re.compile(r"[ \t]{2,}")


def run(executable: str, command: tuple[str, ...], store: str) -> tuple[str, str, int]:
    """Run one query command against *store*."""
    proc = subprocess.run(
        [executable, command[0], store, *command[1:]],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _normalize_float(match: re.Match[str]) -> str:
    try:
        value = Decimal(match.group(0)).normalize()
    except InvalidOperation:
        return match.group(0)
    return format(value, "f")


def _normalize_scalar_text(text: str) -> str:
    text = _UNKNOWN_TOKEN_RE.sub(lambda match: match.group(1)[0].lower(), text)
    return _FLOAT_TOKEN_RE.sub(_normalize_float, text)


def _normalize_json(value: object, store: str) -> object:
    if isinstance(value, dict):
        return {key: _normalize_json(item, store) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_json(item, store) for item in value]
    if isinstance(value, str):
        return _normalize_scalar_text(value.replace(store, "<TRACE>"))
    return value


def normalize(output: str, store: str) -> str:
    """Canonicalize harmless writer-specific output representation deltas."""
    text = output.replace("\r\n", "\n").replace(store, "<TRACE>").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        return json.dumps(
            _normalize_json(parsed, store),
            sort_keys=True,
            separators=(",", ":"),
        )

    lines: list[str] = []
    last_value: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = _normalize_scalar_text(raw_line.rstrip())
        event = _EVENT_RE.match(line)
        if event is not None:
            signal = event.group("signal")
            value = event.group("value")
            if last_value.get(signal) == value:
                continue
            last_value[signal] = value
        else:
            # Human-readable tables pad columns according to the longest name.
            # Writers may expose different hierarchy/type metadata, which changes
            # that padding without changing any displayed field.
            line = _COLUMN_GAP_RE.sub(" ", line)
        lines.append(line)
    return "\n".join(lines)
