"""System-level skill deployment for ``booley init`` (Step 8).

Extracted from ``init_cmd.py`` (Single Responsibility): detects the host's
agent skill directories (``~/.agents/`` and/or ``~/.claude/``), prunes
superseded/dangling Booley-created links, and links the package's bundled
skills into them via a Windows junction or a POSIX symlink.

Depends only on ``init_common`` for console output and :class:`InitContext`;
it never imports back from ``init_cmd``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from booley.harness.colors import bold_chrome
from booley.harness.init_common import InitContext, err, info, ok, skip, warn
from booley.platform_paths import IS_WINDOWS


def _make_junction_or_symlink(link: Path, target: Path) -> bool:
    if IS_WINDOWS:
        abs_target = target.resolve()
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(abs_target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    rel = os.path.relpath(target, link.parent)
    link.symlink_to(rel)
    return True


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
        agents_dir.mkdir(parents=True, exist_ok=True)
        targets.append(agents_dir / "skills")
    return targets


def _is_booley_skill_link(link: Path, src: Path) -> bool:
    """True when *link* is a symlink/junction Booley deployed into a skills dir.

    Booley always creates each skill link pointing at ``<skills_dir>/<name>`` — so
    a Booley-owned link's target (or intended target, for a now-dangling one) has
    ``src`` as its parent. A user's own real directory is never a symlink/junction
    and never matches, so it is safe from pruning.
    """
    # os.path (not pathlib) is deliberate here: this is lexical path handling on
    # a possibly-dangling symlink target. pathlib's resolve-based API would
    # follow the link or require it to exist, defeating the dangling-link check.
    src_norm = os.path.normpath(str(src))
    if link.is_symlink():
        raw = os.readlink(link)  # noqa: PTH115 — see note above (lexical, no resolve)
        target = raw if os.path.isabs(raw) else os.path.join(str(link.parent), raw)  # noqa: PTH117, PTH118
        # Compare intended parent even when the target no longer exists (dangling).
        return os.path.normpath(os.path.dirname(os.path.normpath(target))) == src_norm  # noqa: PTH120
    # Windows junction: a reparse-point directory whose real path resolves under src.
    if IS_WINDOWS and link.is_dir():
        try:
            real = os.path.realpath(link)
        except OSError:
            return False
        return os.path.normpath(os.path.dirname(real)) == src_norm  # noqa: PTH120
    return False


def _prune_stale_skill_links(skills_target: Path, src: Path, current: set[str]) -> None:
    """Remove superseded/dangling Booley skill links from *skills_target*.

    Prunes an entry only when it is a Booley-created link (points into the
    package skills dir *src*) that is NOT one of the *current* package skills and
    is either (a) dangling — its package skill was removed — or (b) named with the
    superseded ``booley-setup-*`` per-step convention. Real user directories are
    never symlinks/junctions into *src*, so they are left untouched.
    """
    if not skills_target.is_dir():
        return
    for entry in sorted(skills_target.iterdir()):
        name = entry.name
        if name in current or not _is_booley_skill_link(entry, src):
            continue
        dangling = not entry.exists()  # broken symlink/junction target
        superseded = name.startswith("booley-setup-")  # old per-step naming
        if not (dangling or superseded):
            continue
        try:
            if IS_WINDOWS and entry.is_dir() and not entry.is_symlink():
                entry.rmdir()  # junction: removed as an empty dir reparse point
            else:
                entry.unlink()
        except OSError as exc:
            warn(f"could not prune stale skill link {name}: {exc}")
            continue
        info(f"pruned stale skill link {name} from {skills_target}")


def _deploy_skills(ctx: InitContext) -> None:
    """Deploy package skills into system-level skills dir via junction (Win) or symlink (Unix)."""
    from booley.paths import skills_dir

    ctx.step_banner("skill deployment")

    src = skills_dir()
    if not src.is_dir():
        warn(f"package skills directory not found: {src}")
        ctx.record("skills", "warn", "skills dir missing")
        return

    skill_dirs = [d for d in src.iterdir() if d.is_dir()]
    if not skill_dirs:
        skip("no skills found in package data")
        ctx.record("skills", "skip", "empty")
        return

    targets = _find_skill_targets()

    if ctx.check_only:
        for t in targets:
            warn(f"would deploy {len(skill_dirs)} skill(s) to {t}")
        ctx.record("skills", "warn", f"{len(skill_dirs)} skills")
        return

    current_names = {d.name for d in skill_dirs}
    for skills_target in targets:
        skills_target.mkdir(parents=True, exist_ok=True)

        # Drop superseded per-step / dangling links from a previous version so
        # the consolidated skill doesn't share a confusing double namespace.
        _prune_stale_skill_links(skills_target, src, current_names)

        deployed = 0
        for skill_dir in skill_dirs:
            link = skills_target / skill_dir.name
            if link.exists():
                if ctx.verbose:
                    skip(f"  {skill_dir.name} already exists")
                continue

            if _make_junction_or_symlink(link, skill_dir):
                deployed += 1
                if ctx.verbose:
                    ok(f"  {skill_dir.name}")
            else:
                err(f"  junction/symlink failed for {skill_dir.name}")
                info(f'  manual: mklink /J "{link}" "{skill_dir.resolve()}"')

        ok(
            f"{bold_chrome(str(skills_target))}: {deployed} new, {len(skill_dirs) - deployed} existing"
        )

    ctx.record("skills", "ok", f"deployed to {len(targets)} target(s)")
