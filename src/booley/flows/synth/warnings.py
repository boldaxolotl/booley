"""Normalize EDA warnings and final structural evidence for synthesis.

The public interface deliberately accepts stage-labelled text rather than
paths.  Freshness and filesystem ownership stay with the synthesis pipeline;
warning dialect, grouping, disposition, and representative bounds stay here.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

WarningDisposition = Literal["benign", "advisory", "structural"]

_SOURCE_ORDER = ("sv2v", "yosys", "openroad", "final_check")
_REPRESENTATIVE_LIMIT = 8
_YOSYS_WARNING_RE = re.compile(r"^(?:Warning:|ABC:\s+Warning:)", re.IGNORECASE)
_OPENROAD_WARNING_RE = re.compile(
    r"^\[WARNING(?:\s+([A-Z][A-Z0-9_]*-\d+))?\]\s*(.*)$",
    re.IGNORECASE,
)
_PLAIN_WARNING_RE = re.compile(r"^WARNING:\s*", re.IGNORECASE)
_YOSYS_CHECK_COMPLETE_RE = re.compile(
    r"^Found and reported \d+ problems?\.$",
    re.MULTILINE,
)
_BENIGN_STA_0503_RE = re.compile(
    r"^\[WARNING STA-0503\]\s*find_timing_paths -group_count is deprecated\. "
    r"Use -group_path_count instead\.$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StructuralConditions:
    """Final mapped-netlist structural counts and evidence completeness."""

    complete: bool = False
    comb_loops: int = 0
    multi_driven: int = 0


@dataclass(frozen=True)
class WarningGroup:
    """One normalized diagnostic group with its raw occurrence count."""

    tool: str
    code: str | None
    category: str
    disposition: WarningDisposition
    message: str
    count: int
    rationale: str | None = None

    def to_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "tool": self.tool,
            "code": self.code,
            "category": self.category,
            "disposition": self.disposition,
            "message": self.message,
            "count": self.count,
        }
        if self.rationale:
            detail["rationale"] = self.rationale
        return detail


@dataclass(frozen=True)
class WarningSummary:
    """Bounded structured warning evidence for one synthesis run."""

    total_warnings: int = 0
    unique_warnings: int = 0
    by_tool: Mapping[str, int] = field(default_factory=dict)
    by_category: Mapping[str, int] = field(default_factory=dict)
    by_disposition: Mapping[str, int] = field(default_factory=dict)
    representatives: tuple[WarningGroup, ...] = ()

    @property
    def advisory_warnings(self) -> int:
        return int(self.by_disposition.get("advisory", 0))

    @property
    def structural_warnings(self) -> int:
        return int(self.by_disposition.get("structural", 0))

    @property
    def actionable_warnings(self) -> int:
        return self.advisory_warnings + self.structural_warnings

    def to_detail(self, *, include_representatives: bool = True) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "total_warnings": self.total_warnings,
            "unique_warnings": self.unique_warnings,
            "by_tool": dict(self.by_tool),
            "by_category": dict(self.by_category),
            "by_disposition": dict(self.by_disposition),
        }
        if include_representatives:
            detail["representatives"] = [item.to_detail() for item in self.representatives]
        return detail


@dataclass(frozen=True)
class SynthDiagnostics:
    """Warning inventory plus authoritative final structural evidence."""

    warnings: WarningSummary
    structural: StructuralConditions


@dataclass(frozen=True)
class _WarningRecord:
    tool: str
    code: str | None
    category: str
    disposition: WarningDisposition
    message: str
    rationale: str | None = None


def _normalize_message(lines: list[str]) -> str:
    return " ".join(" ".join(lines).split())


def _multiline_records(text: str, starts: re.Pattern[str]) -> list[str]:
    records: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if starts.match(line):
            if current:
                records.append(_normalize_message(current))
            current = [line]
        elif current and (line.startswith((" ", "\t")) or not line.strip()):
            if line.strip():
                current.append(line)
        elif current:
            records.append(_normalize_message(current))
            current = []
    if current:
        records.append(_normalize_message(current))
    return records


def _category(message: str, code: str | None) -> str:
    lowered = message.lower()
    if "found logic loop in module" in lowered:
        return "combinational_loop"
    if "multiple conflicting drivers for" in lowered:
        return "multi_driver"
    if code in {"STA-0349", "STA-0441"}:
        return "constraint"
    if code == "STA-0503":
        return "deprecation"
    return "other"


def _disposition(message: str, category: str) -> tuple[WarningDisposition, str | None]:
    if category in {"combinational_loop", "multi_driver"}:
        return "structural", None
    if _BENIGN_STA_0503_RE.match(message):
        return (
            "benign",
            "Booley-generated deprecated query; reported for traceability and fixed at source.",
        )
    return "advisory", None


def _yosys_records(text: str) -> list[_WarningRecord]:
    result: list[_WarningRecord] = []
    for message in _multiline_records(text, _YOSYS_WARNING_RE):
        tool = "abc" if message.lower().startswith("abc:") else "yosys"
        category = _category(message, None)
        disposition, rationale = _disposition(message, category)
        result.append(_WarningRecord(tool, None, category, disposition, message, rationale))
    return result


def _openroad_records(text: str) -> list[_WarningRecord]:
    result: list[_WarningRecord] = []
    for line in text.splitlines():
        match = _OPENROAD_WARNING_RE.match(line)
        if match is None and not _PLAIN_WARNING_RE.match(line):
            continue
        code = match.group(1).upper() if match and match.group(1) else None
        message = _normalize_message([line])
        category = _category(message, code)
        disposition, rationale = _disposition(message, category)
        result.append(_WarningRecord("openroad", code, category, disposition, message, rationale))
    return result


def _records(sources: Mapping[str, str]) -> list[_WarningRecord]:
    records: list[_WarningRecord] = []
    for source in _SOURCE_ORDER:
        text = sources.get(source, "")
        if not text:
            continue
        if source == "openroad":
            records.extend(_openroad_records(text))
        elif source == "sv2v":
            records.extend(_sv2v_records(text))
        else:
            records.extend(_yosys_records(text))
    return records


def _sv2v_records(text: str) -> list[_WarningRecord]:
    return [
        _WarningRecord("sv2v", None, "other", "advisory", message)
        for message in _multiline_records(text, _PLAIN_WARNING_RE)
    ]


def _warning_summary(records: list[_WarningRecord]) -> WarningSummary:
    grouped = Counter(
        (item.tool, item.code, item.category, item.disposition, item.message, item.rationale)
        for item in records
    )
    groups = [
        WarningGroup(
            tool=key[0],
            code=key[1],
            category=key[2],
            disposition=key[3],
            message=key[4],
            rationale=key[5],
            count=count,
        )
        for key, count in grouped.items()
    ]
    groups.sort(key=lambda item: (item.tool, item.code or "", item.category, item.message))
    return WarningSummary(
        total_warnings=len(records),
        unique_warnings=len(groups),
        by_tool=dict(sorted(Counter(item.tool for item in records).items())),
        by_category=dict(sorted(Counter(item.category for item in records).items())),
        by_disposition=dict(sorted(Counter(item.disposition for item in records).items())),
        representatives=tuple(groups[:_REPRESENTATIVE_LIMIT]),
    )


def _structural_conditions(sources: Mapping[str, str]) -> StructuralConditions:
    if "final_check" not in sources:
        return StructuralConditions()
    final_check = sources.get("final_check", "")
    if _YOSYS_CHECK_COMPLETE_RE.search(final_check) is None:
        return StructuralConditions()
    records = _yosys_records(final_check)
    return StructuralConditions(
        complete=True,
        comb_loops=sum(item.category == "combinational_loop" for item in records),
        multi_driven=sum(item.category == "multi_driver" for item in records),
    )


def parse_synth_diagnostics(sources: Mapping[str, str]) -> SynthDiagnostics:
    """Parse fresh stage text into bounded warnings and final conditions."""
    return SynthDiagnostics(
        warnings=_warning_summary(_records(sources)),
        structural=_structural_conditions(sources),
    )
