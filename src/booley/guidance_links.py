"""Root guidance links for the project guidance file.

The canonical project guidance file (``AGENTS.md``) lives in the project data
directory (``.booley_project/``), so it is versioned alongside the rest of the
project config. The RTL repo root only carries generated links — ``AGENTS.md``
(for Codex / Booley) and ``CLAUDE.md`` (for Claude Code) — that point at the
canonical file. Those links are recreated per checkout and kept out of the RTL
repo's history via ``.git/info/exclude`` (not the tracked ``.gitignore``), so
they never appear in a commit or a diff.

This module creates and repairs those root links cross-platform.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from booley.harness.git_utils import add_git_excludes
from booley.project_dir import PROJECT_DIR_NAME

logger = logging.getLogger(__name__)

# Canonical guidance file name, stored in the project data dir.
CANON_NAME = "AGENTS.md"
# Generated link names at the RTL repo root. AGENTS.md is read by Codex and
# Booley; CLAUDE.md is read by Claude Code — both resolve to the same canon.
LINK_NAMES = ("AGENTS.md", "CLAUDE.md")

_EXCLUDE_HEADER = "# Booley guidance links (generated; canonical file in project dir)"


def ensure_guidance_links(project_root: Path, project_dir: Path) -> list[Path]:
    """Point ``<project_root>/{AGENTS,CLAUDE}.md`` at ``<project_dir>/AGENTS.md``.

    Idempotent: a link already resolving to the canonical file is left
    untouched. The canonical file must exist. Link names are added to the RTL
    repo's ``.git/info/exclude`` so they stay out of its history.

    Returns the link paths that now exist. Raises ``FileNotFoundError`` when
    the canonical guidance file is missing.
    """
    canon = (project_dir / CANON_NAME).resolve()
    if not canon.is_file():
        raise FileNotFoundError(f"canonical guidance file not found: {canon}")

    target = _portable_target(project_root, canon)
    links: list[Path] = []
    for name in LINK_NAMES:
        link = project_root / name
        _link_file(link, canon, target)
        links.append(link)
    add_git_excludes(project_root, LINK_NAMES, header=_EXCLUDE_HEADER)
    return links


def _portable_target(project_root: Path, canon: Path) -> Path:
    """The path a symlink should point at, valid in both launch contexts (F-13).

    Inside the Session Runtime the project dir is bind-mounted at
    ``/booley-project``, outside the workspace — so a link relative to it reads
    ``../booley-project/AGENTS.md``, which on the host resolves to a sibling of
    the repo that does not exist. The host then sees two dangling links and an
    editor or host-side agent reads no guidance at all.

    ``<project_root>/.booley_project/AGENTS.md`` names the same file in both
    contexts (the container reaches it through the workspace mount), so prefer it
    whenever it exists. Falls back to *canon* for a relocated project dir
    (``[project].dir`` override), where no such repo-local path exists.
    """
    repo_local = project_root / PROJECT_DIR_NAME / CANON_NAME
    return repo_local if repo_local.is_file() else canon


def _points_to(link: Path, canon: Path) -> bool:
    """True iff *link* exists and refers to the canonical file.

    A symlink answers by path; a hardlink (the Windows fallback when Developer
    Mode is off) resolves to *itself*, so it is recognized by identity instead —
    otherwise every run would tear down and rebuild a perfectly good link. A
    plain copy matches neither test and is deliberately replaced: it silently
    goes stale.
    """
    try:
        if link.resolve(strict=True) == canon:
            return True
    except OSError:
        return False
    try:
        return link.samefile(canon)
    except OSError:
        return False


def _exists_nofollow(p: Path) -> bool:
    """Entry exists at *p* without following a (possibly broken) link."""
    try:
        p.lstat()
        return True
    except OSError:
        return False


def _link_file(link: Path, canon: Path, target: Path) -> None:
    """Create or repair a single root link.

    *canon* is the resolved canonical file (used to test whether the link is
    already correct, and as the source for a hardlink/copy); *target* is the
    path the symlink should name — see :func:`_portable_target`.
    """
    if _points_to(link, canon):
        return
    _remove(link)
    if sys.platform == "win32":
        _link_file_windows(link, canon, target)
    else:
        link.symlink_to(os.path.relpath(target, link.parent))


def _remove(link: Path) -> None:
    """Remove any file, symlink, hardlink, copy, or junction at *link*."""
    if not _exists_nofollow(link):
        return
    if link.is_symlink():
        link.unlink()
        return
    if sys.platform == "win32":
        try:
            link.unlink()
        except OSError:
            # A directory junction (shouldn't happen for a file link) needs rmdir.
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, check=False)
        return
    link.unlink()


def _link_file_windows(link: Path, canon: Path, target: Path) -> None:
    """Windows file link: symlink (Dev Mode) -> hardlink (same volume) -> copy.

    ``mklink /J`` is not usable here: junctions are directory-only. A real file
    symlink needs Developer Mode or admin; a hardlink needs neither but only
    works on the same volume; a copy is the last resort and does not stay in
    sync with edits to the canonical file.
    """
    rel = os.path.relpath(target, link.parent)
    try:
        link.symlink_to(rel)
        return
    except OSError:
        pass
    try:
        os.link(canon, link)
        return
    except OSError:
        shutil.copy2(canon, link)
        logger.warning(
            "guidance link %s copied (symlink and hardlink unavailable); edits "
            "to the canonical file will not propagate",
            link,
        )


# Exclude writing lives in ``harness.git_utils.add_git_excludes`` (worktree-aware:
# writes to ``$GIT_COMMON_DIR/info/exclude``, the file git actually honors).
