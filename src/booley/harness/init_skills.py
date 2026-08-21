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
import stat
import subprocess
from pathlib import Path

from booley.harness.colors import bold_chrome
from booley.harness.init_common import InitContext, err, info, ok, skip, warn
from booley.runtime.platform_paths import IS_WINDOWS


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
    try:
        link.symlink_to(rel)
    except OSError:
        return False
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


def _link_target(link: Path) -> str | None:
    """Return a link or junction's lexical target without requiring it to exist."""
    if not (link.is_symlink() or _is_windows_junction(link)):
        return None
    try:
        raw = os.readlink(link)  # noqa: PTH115 — lexical target may be dangling
    except OSError:
        return None
    return raw if os.path.isabs(raw) else os.path.join(str(link.parent), raw)  # noqa: PTH117, PTH118


def _is_booley_skill_link(link: Path, src: Path) -> bool:
    """True when *link* targets a skill in the active Booley installation."""
    # os.path (not pathlib) is deliberate here: this is lexical path handling on
    # a possibly-dangling symlink target. pathlib's resolve-based API would
    # follow the link or require it to exist, defeating the dangling-link check.
    target = _link_target(link)
    if target is None:
        return False
    parent = os.path.normcase(
        os.path.normpath(os.path.dirname(os.path.normpath(target)))  # noqa: PTH120
    )
    return parent == os.path.normcase(os.path.normpath(str(src)))


def _is_packaged_booley_skill_link(link: Path) -> bool:
    """True when a link target has Booley's stable packaged-skills layout."""
    target = _link_target(link)
    if target is None:
        return False
    parent = os.path.normcase(os.path.normpath(os.path.dirname(target)))  # noqa: PTH120
    suffix = os.path.normcase(os.path.join("booley", "data", "skills"))  # noqa: PTH118
    return parent == suffix or parent.endswith(f"{os.sep}{suffix}")


def _exists_nofollow(path: Path) -> bool:
    """True when an entry occupies *path*, including a dangling link."""
    try:
        path.lstat()
    except OSError:
        return False
    return True


def _is_windows_junction(path: Path) -> bool:
    """Recognize a Windows junction without following its possibly stale target."""
    if not IS_WINDOWS or path.is_symlink():
        return False
    try:
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_skill_link(link: Path) -> None:
    """Remove a symlink or junction without touching its target."""
    if _is_windows_junction(link):
        link.rmdir()
    else:
        link.unlink()


def _prune_stale_skill_links(skills_target: Path, src: Path, current: set[str]) -> None:
    """Remove superseded/dangling Booley skill links from *skills_target*.

    A dangling link with a current skill name is repaired even when it points
    into an older installation. Non-current links must resolve under *src* and
    be dangling or use the superseded ``booley-setup-*`` convention. Real user
    directories are never removed.
    """
    if not skills_target.is_dir():
        return
    for entry in sorted(skills_target.iterdir()):
        name = entry.name
        dangling = _exists_nofollow(entry) and not entry.exists()
        link_like = entry.is_symlink() or _is_windows_junction(entry)

        # A current-name link may point into a removed installation rather than
        # the active package's `src`. Repair it only when the lexical target
        # proves it came from Booley's packaged skill tree; an unrelated user
        # link can also be dangling and must remain untouched.
        current_dangling = (
            name in current and dangling and link_like and _is_packaged_booley_skill_link(entry)
        )
        if not current_dangling and (name in current or not _is_booley_skill_link(entry, src)):
            continue
        superseded = name.startswith("booley-setup-")  # old per-step naming
        if not (current_dangling or dangling or superseded):
            continue
        try:
            _remove_skill_link(entry)
        except OSError as exc:
            warn(f"could not prune stale skill link {name}: {exc}")
            continue
        info(f"pruned stale skill link {name} from {skills_target}")


def _points_to_skill(link: Path, skill_dir: Path) -> bool:
    """True when an existing entry resolves to the requested packaged skill."""
    try:
        return link.resolve(strict=True) == skill_dir.resolve(strict=True)
    except OSError:
        return False


def _deploy_skill(skill_dir: Path, skills_target: Path, *, verbose: bool) -> str:
    """Deploy one skill and return ``new``, ``existing``, or ``failed``."""
    link = skills_target / skill_dir.name
    if link.exists():
        if _points_to_skill(link, skill_dir):
            if verbose:
                skip(f"  {skill_dir.name} already exists")
            return "existing"
        err(f"  cannot deploy {skill_dir.name}: {link} is occupied")
        return "failed"
    if _make_junction_or_symlink(link, skill_dir):
        if verbose:
            ok(f"  {skill_dir.name}")
        return "new"
    err(f"  junction/symlink failed for {skill_dir.name}")
    info(f'  manual: mklink /J "{link}" "{skill_dir.resolve()}"')
    return "failed"


def _deploy_skills_to_target(
    skills_target: Path,
    src: Path,
    skill_dirs: list[Path],
    *,
    verbose: bool,
) -> int:
    """Reconcile all packaged skills in one host skills directory."""
    skills_target.mkdir(parents=True, exist_ok=True)
    current_names = {d.name for d in skill_dirs}
    _prune_stale_skill_links(skills_target, src, current_names)

    counts = {"new": 0, "existing": 0, "failed": 0}
    for skill_dir in skill_dirs:
        outcome = _deploy_skill(skill_dir, skills_target, verbose=verbose)
        counts[outcome] += 1

    summary = (
        f"{bold_chrome(str(skills_target))}: "
        f"{counts['new']} new, {counts['existing']} existing, {counts['failed']} failed"
    )
    (err if counts["failed"] else ok)(summary)
    return counts["failed"]


def _deploy_skills(ctx: InitContext) -> None:
    """Deploy package skills into system-level skills dir via junction (Win) or symlink (Unix)."""
    from booley.runtime.paths import skills_dir

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

    total_failed = sum(
        _deploy_skills_to_target(target, src, skill_dirs, verbose=ctx.verbose)
        for target in targets
    )

    if total_failed:
        ctx.record("skills", "err", f"{total_failed} link(s) failed")
    else:
        ctx.record("skills", "ok", f"deployed to {len(targets)} target(s)")
