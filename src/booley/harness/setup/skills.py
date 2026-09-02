"""Host adapter for system-level skill reconciliation during ``booley init``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.harness.setup.common import InitContext, warn
from booley.runtime.skill_links import SkillLinkReport, reconcile_skill_links


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

    targets = [agents_dir / "skills"]
    if claude_dir.is_dir():
        resolved_claude = claude_dir.resolve()
        resolved_agents = agents_dir.resolve()
        if resolved_claude != resolved_agents:
            targets.append(claude_dir / "skills")
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


def _deploy_skills(ctx: InitContext) -> None:
    """Compatibility adapter for the superseding host-skill reconciler."""
    from booley.runtime.paths import skills_dir

    ctx.step_banner("skill deployment")
    source = skills_dir()
    if not source.is_dir():
        warn(f"package skills directory not found: {source}")
        ctx.record("skills", "warn", "skills dir missing")
        return

    reconciliations = reconcile_host_skills(
        source,
        dry_run=ctx.check_only,
        allow_retarget=ctx.force,
    )
    failures = sum(
        sum(event.failed for event in item.report.events)
        + len(item.report.diagnostics)
        + int(item.report.fatal is not None)
        for item in reconciliations
    )
    if failures:
        ctx.record("skills", "err", f"{failures} reconciliation issue(s)")
    elif ctx.check_only:
        ctx.record("skills", "warn", f"checked {len(reconciliations)} target(s)")
    else:
        ctx.record("skills", "ok", f"deployed to {len(reconciliations)} target(s)")
