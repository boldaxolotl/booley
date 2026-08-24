"""Shared result types for project-configuration audits."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConfigFindingSeverity(StrEnum):
    """Presentation-independent severity for a configuration finding."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ConfigFinding:
    """One structural configuration finding with optional warning identity."""

    severity: ConfigFindingSeverity
    message: str
    fix: str = ""
    check_id: str | None = None
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigTableAudit:
    """Findings produced while auditing one structural configuration table."""

    findings: tuple[ConfigFinding, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Whether the table contains no failing structural findings."""
        return all(item.severity is not ConfigFindingSeverity.FAIL for item in self.findings)


def failure(message: str, fix: str) -> ConfigTableAudit:
    """Return a one-finding failed table audit."""
    return ConfigTableAudit((fail_finding(message, fix),))


def fail_finding(message: str, fix: str) -> ConfigFinding:
    """Build a failed configuration finding."""
    return ConfigFinding(ConfigFindingSeverity.FAIL, message, fix)


def pass_finding(message: str) -> ConfigFinding:
    """Build a passing configuration finding."""
    return ConfigFinding(ConfigFindingSeverity.PASS, message)


def warn_finding(
    message: str,
    check_id: str,
    *,
    subject: str | None = None,
) -> ConfigFinding:
    """Build a warning with stable waiver identity."""
    return ConfigFinding(
        ConfigFindingSeverity.WARN,
        message,
        check_id=check_id,
        subject=subject,
    )
