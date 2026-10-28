"""Brief Doctor text shared by presentation adapters, without execution policy."""

from pathlib import Path
from typing import Literal, NamedTuple

from booley.runtime import health

HealthSeverity = Literal["fail", "warn", "ok", "unavailable", "unknown"]


# Trailing marker of a due result; the Dashboard draws it in the attention color.
RECHECK_DUE_SUFFIX = " · recheck due"


class CompactLine(NamedTuple):
    """Compact Doctor line: full text, count severity, and the due marker as data."""

    text: str
    severity: HealthSeverity
    # Trailing part of ``text`` that renderers style apart from the counts; "" when not due.
    due_suffix: str


def count_line(snapshot: health.HealthSnapshot | None) -> CompactLine:
    """Render compact active counts and freshness from one already-read observation."""
    if snapshot is None:
        return CompactLine("Doctor: unavailable: no readable automatic result", "unavailable", "")
    assessment = snapshot.assessment
    counts = []
    if assessment.fail_count:
        counts.append(f"{assessment.fail_count} FAIL")
    if assessment.warn_count:
        counts.append(f"{assessment.warn_count} WARN")
    text = "Doctor: " + (" · ".join(counts) if counts else "OK")
    severity: HealthSeverity = (
        "fail" if assessment.fail_count else "warn" if assessment.warn_count else "ok"
    )
    due_suffix = RECHECK_DUE_SUFFIX if snapshot.due else ""
    return CompactLine(text + due_suffix, severity, due_suffix)


def brief_message(snapshot: health.HealthSnapshot) -> str:
    """Render an already-read snapshot; never reload a report while formatting."""
    assessment = snapshot.assessment
    if assessment.clean:
        return f"Automatic Doctor is clean. Full report: {snapshot.transcript}"
    findings = [f.message for f in snapshot.diagnostics.active_findings]
    top = "; ".join(findings[:3])
    extra = f"; {len(findings) - 3} more" if len(findings) > 3 else ""
    return (
        f"Automatic Doctor found {assessment.fail_count} FAIL, {assessment.warn_count} WARN: "
        f"{top}{extra}. Full report: {snapshot.transcript}"
    )


def current_summary(project_root: Path) -> str:
    """Read the current diagnostic snapshot without consuming an announcement."""
    snapshot = health.read_health(project_root)
    if snapshot is None:
        return "Automatic Doctor has not completed a run yet."
    return brief_message(snapshot)


def consume_changed_summary(
    project_root: Path, *, channel: str, issues_only: bool = False
) -> str | None:
    """Render the shared owner's changed observation for one reporting channel."""
    snapshot = health.consume_changed_snapshot(
        project_root, channel=channel, issues_only=issues_only
    )
    return brief_message(snapshot) if snapshot is not None else None
