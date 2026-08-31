"""Host adapter for system-level skill reconciliation during ``booley init``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.harness.colors import bold_chrome
from booley.harness.init_common import InitContext, err, info, ok, skip, warn
from booley.runtime.skill_links import SkillLinkEvent, SkillLinkReport, reconcile_skill_links


@dataclass(frozen=True, slots=True)
class HostSkillReconciliation:
    """One detected host skill target and its reconciliation result."""

    target: Path
    report: SkillLinkReport


def _find_skill_targets() -> list[Path]:
    """Detect system-level skill directories to deploy into."""
    home = Path.home()
    agents_dir = home / ".agents"
    claude_dir = home / ".claude"

    targets: list[Path] = []
    if agents_dir.is_dir():
        targets.append(agents_dir / "skills")
    if claude_dir.is_dir():
        resolved_claude = claude_dir.resolve()
        resolved_agents = agents_dir.resolve() if agents_dir.exists() else None
        if resolved_claude != resolved_agents:
            targets.append(claude_dir / "skills")
    if not targets:
        targets.append(agents_dir / "skills")
    return targets


def reconcile_host_skills(
    source: Path,
    *,
    dry_run: bool,
    allow_retarget: bool,
) -> tuple[HostSkillReconciliation, ...]:
    """Reconcile packaged skills across every detected host agent directory."""
    return tuple(
        HostSkillReconciliation(
            target,
            reconcile_skill_links(
                target,
                source,
                dry_run=dry_run,
                allow_retarget=allow_retarget,
            ),
        )
        for target in _find_skill_targets()
    )


def _render_event(event: SkillLinkEvent, *, verbose: bool, dry_run: bool) -> None:
    label = event.preview_label if dry_run else event.outcome
    message = f"  {label} {event.name}"
    if event.detail:
        message += f": {event.detail}"
    if (
        event.previous_target is not None
        and event.desired_target is not None
        and event.previous_target != event.desired_target
    ):
        message += f"\n    current: {event.previous_target}\n    requested: {event.desired_target}"
    if event.failed:
        err(message)
    elif event.always_visible:
        info(message)
    elif verbose:
        (ok if event.changed else skip)(message)


def _failure_count(report: SkillLinkReport) -> int:
    event_failures = sum(event.failed for event in report.events)
    return event_failures + len(report.diagnostics) + int(report.fatal is not None)


def _render_report(
    skills_target: Path,
    report: SkillLinkReport,
    *,
    verbose: bool,
    dry_run: bool,
) -> int:
    if report.fatal:
        err(f"  skill reconciliation failed: {report.fatal}")
    for event in report.events:
        _render_event(event, verbose=verbose, dry_run=dry_run)
    for diagnostic in report.diagnostics:
        err(f"  skill reconciliation: {diagnostic}")
    failures = _failure_count(report)
    changed = sum(event.changed for event in report.events)
    summary = f"{bold_chrome(str(skills_target))}: {changed} changed, {failures} failed"
    (err if failures else ok)(summary)
    return failures


def _deploy_skills_to_target(
    skills_target: Path,
    src: Path,
    *,
    verbose: bool,
    dry_run: bool,
    allow_retarget: bool,
) -> int:
    """Reconcile packaged skills in one host agent directory."""
    if allow_retarget and not dry_run:
        preview = reconcile_skill_links(
            skills_target,
            src,
            dry_run=True,
            allow_retarget=True,
        )
        for event in preview.events:
            if event.replaces_target:
                _render_event(event, verbose=True, dry_run=True)
    report = reconcile_skill_links(
        skills_target,
        src,
        dry_run=dry_run,
        allow_retarget=allow_retarget,
    )
    return _render_report(
        skills_target,
        report,
        verbose=verbose,
        dry_run=dry_run,
    )


def _deploy_skills(ctx: InitContext) -> None:
    """Reconcile package skills into each detected host agent directory."""
    from booley.runtime.paths import skills_dir

    ctx.step_banner("skill deployment")

    src = skills_dir()
    if not src.is_dir():
        warn(f"package skills directory not found: {src}")
        ctx.record("skills", "warn", "skills dir missing")
        return

    targets = _find_skill_targets()
    total_failed = sum(
        _deploy_skills_to_target(
            target,
            src,
            verbose=ctx.verbose,
            dry_run=ctx.check_only,
            allow_retarget=ctx.force,
        )
        for target in targets
    )

    if total_failed:
        ctx.record("skills", "err", f"{total_failed} reconciliation issue(s)")
    elif ctx.check_only:
        ctx.record("skills", "warn", f"checked {len(targets)} target(s)")
    else:
        ctx.record("skills", "ok", f"deployed to {len(targets)} target(s)")
