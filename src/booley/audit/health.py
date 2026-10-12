"""Typed machinery health, its persisted document shape and freshness policy.

Pure policy over explicit inputs: it neither executes Doctor nor touches the
filesystem, so every consumer interprets one observation identically.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from booley.audit.diagnostic_results import (
    DiagnosticFinding,
    DiagnosticReport,
    HealthAssessment,
    Severity,
)
from booley.core.boundary import (
    require_bool_value,
    require_dict,
    require_int,
    require_list,
    require_opt_str,
    require_str,
    require_str_value,
)

_UPGRADE_ONLY_CHECKS = frozenset({"upgrade.review-pending", "upgrade.runtime-stale"})
HEALTHY_MAX_AGE_DAYS = 7
_HEALTHY_MAX_AGE = timedelta(days=HEALTHY_MAX_AGE_DAYS)
_UNHEALTHY_RETRY_AGE = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """One validated observation and freshness decision, detached from the reader."""

    diagnostics: DiagnosticReport
    finding_hash: str
    transcript: Path
    due: str | None

    @property
    def assessment(self) -> HealthAssessment:
        return self.diagnostics.health


def finding_documents(findings: Iterable[DiagnosticFinding]) -> list[dict[str, Any]]:
    """Retain the legacy five-field on-disk finding and announcement hash shape."""
    return [
        {
            "severity": f.severity.value,
            "message": f.message,
            "fix": f.fix,
            "check_id": f.check_id,
            "subject": f.subject,
        }
        for f in findings
    ]


def _parse_finding(value: object) -> DiagnosticFinding:
    data = require_dict(value, field="finding")
    return DiagnosticFinding(
        Severity(require_str(data, "severity")),
        require_str(data, "message"),
        require_str_value(data.get("fix", ""), field="fix", allow_empty=True),
        require_opt_str(data, "check_id"),
        require_opt_str(data, "subject"),
    )


def parse_diagnostics(document: dict[str, Any]) -> DiagnosticReport:
    """Validate legacy observations and reject contradictory active health fields."""
    if "schema" in document and require_int(document["schema"], field="schema") != 1:
        raise ValueError("unsupported health report schema")
    counts = require_dict(document.get("counts"), field="counts")
    for key, value in counts.items():
        if require_int(value, field=f"counts.{key}") < 0:
            raise ValueError("diagnostic counts must not be negative")
    diagnostics = DiagnosticReport(
        tuple(
            _parse_finding(value)
            for value in require_list(document.get("findings"), field="findings")
        )
    )
    assessment = diagnostics.health
    if (counts.get("fail"), counts.get("warn")) != (assessment.fail_count, assessment.warn_count):
        raise ValueError("active counts disagree with findings")
    if (
        "clean" in document
        and require_bool_value(document["clean"], field="clean") != assessment.clean
    ):
        raise ValueError("clean flag disagrees with findings")
    if (
        "exit_code" in document
        and require_int(document["exit_code"], field="exit_code") != assessment.exit_code
    ):
        raise ValueError("exit code disagrees with findings")
    return diagnostics


def freshness_reason(
    diagnostics: DiagnosticReport,
    *,
    inputs_changed: bool,
    checked_at: datetime | None,
    now: datetime,
) -> str | None:
    """Evaluate input drift, then age, for one validated prior observation."""
    if inputs_changed:
        return "Doctor inputs changed"
    if checked_at is None:
        return "automatic Doctor timestamp unreadable"
    max_age = (
        _HEALTHY_MAX_AGE
        if diagnostics.health.clean or _has_only_upgrade_review_findings(diagnostics)
        else _UNHEALTHY_RETRY_AGE
    )
    if now - checked_at >= max_age:
        return "automatic Doctor result expired"
    return None


def _has_only_upgrade_review_findings(diagnostics: DiagnosticReport) -> bool:
    """True when rerunning Doctor daily cannot change the active findings."""
    active = diagnostics.active_findings
    return bool(active) and all(f.check_id in _UPGRADE_ONLY_CHECKS for f in active)
