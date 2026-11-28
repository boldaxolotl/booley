"""Brief Doctor text shared by presentation adapters, without execution policy."""

from pathlib import Path

from booley.runtime import health


def brief_message(snapshot: health.HealthSnapshot) -> str:
    """Render an already-read snapshot; never reload a report while formatting.

    A stale snapshot leads with its staleness, so no reader takes old findings
    for current health.
    """
    if snapshot.due is not None:
        return (
            f"Automatic Doctor result is out of date ({snapshot.due}); run `booley doctor` "
            f"for current health. Last result: {_findings_message(snapshot)}"
        )
    return _findings_message(snapshot)


def _findings_message(snapshot: health.HealthSnapshot) -> str:
    """Render the snapshot's findings and transcript path, without freshness."""
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
