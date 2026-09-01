"""Host adapter for system-level skill reconciliation during ``booley init``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
