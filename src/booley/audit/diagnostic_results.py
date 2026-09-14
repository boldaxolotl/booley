"""Lossless observations shared by diagnostic owners, without presentation policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """Diagnostic severity before a caller applies warning waivers."""

    PASS = "pass"
    NOTE = "note"
    WARN = "warn"
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    """An observation; every warning has a durable, prose-independent identity."""

    severity: Severity
    message: str
    fix: str = ""
    check_id: str | None = None
    subject: str | None = None
    dedupe: str | None = None

    def __post_init__(self) -> None:
        if self.severity is Severity.WARN and (
            self.check_id is None
            or re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", self.check_id) is None
        ):
            raise ValueError("diagnostic warnings require a stable check ID")
        if self.subject is not None and not self.subject.strip():
            raise ValueError("diagnostic subject must not be empty")


@dataclass(frozen=True, slots=True)
class DiagnosticDetail:
    """Uncounted verbose text following one finding in the ordered report."""

    after_finding: int
    message: str


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """Ordered observations and uncounted details shown only in verbose output."""

    findings: tuple[DiagnosticFinding, ...] = ()
    details: tuple[DiagnosticDetail, ...] = ()


@dataclass
class Findings:
    """Internal accumulation shared by owners; callers consume frozen reports."""

    _findings: list[DiagnosticFinding] = field(default_factory=list)
    _details: list[DiagnosticDetail] = field(default_factory=list)

    def pass_(self, message: str) -> None:
        self._findings.append(DiagnosticFinding(Severity.PASS, message))

    def note(self, message: str, fix: str = "") -> None:
        self._findings.append(DiagnosticFinding(Severity.NOTE, message, fix))

    def skip(self, message: str) -> None:
        self._findings.append(DiagnosticFinding(Severity.SKIP, message))

    def fail(self, message: str, fix: str) -> None:
        self._findings.append(DiagnosticFinding(Severity.FAIL, message, fix))

    def warn(
        self,
        message: str,
        fix: str = "",
        *,
        check_id: str,
        subject: str | None = None,
        dedupe: str | None = None,
    ) -> None:
        self._findings.append(
            DiagnosticFinding(Severity.WARN, message, fix, check_id, subject, dedupe)
        )

    def detail(self, message: str) -> None:
        self._details.append(DiagnosticDetail(len(self._findings), message))

    def report(self) -> DiagnosticReport:
        return DiagnosticReport(tuple(self._findings), tuple(self._details))
