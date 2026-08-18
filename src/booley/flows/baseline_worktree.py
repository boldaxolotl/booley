"""Ephemeral git worktree for baseline (delta) synthesis / implementation runs.

Both :mod:`asic_synthesize` and :mod:`fpga_impl` accept ``--baseline <git ref>``:
re-run the flow at a past commit and diff the QoR metrics. The original
implementation checked the baseline ref out *in place* (``git checkout``), which
clobbers the caller's working tree — so it was gated to Ticket Mode, where
the developer provides a disposable per-ticket worktree. Interactive Mode has
no such sandbox, so ``--baseline`` was simply refused there (ADR 0012).

This module materializes the baseline ref in a **throwaway ``git worktree``**
instead of mutating the current tree, so the caller's working tree is never
touched and delta mode works identically in both modes. The worktree lives under
``<project>/.booley_project/`` — git-ignored (never pollutes ``git status``) and
inside the Session Runtime workspace. It is force-removed on context exit,
even if the body raises.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from booley.fusesoc.fusesoc_registry import state_cores_dir

logger = logging.getLogger(__name__)


class BaselineWorktreeError(RuntimeError):
    """The baseline checkout could not be fully materialized.

    Carries an actionable message (e.g. the ref does not exist, a submodule
    cannot be checked out, or the project is not a git repo) that the Flow
    surfaces as an infrastructure error.
    """


def git_short_sha(ref: str, cwd: Path) -> str:
    """Resolve *ref* to its short SHA, falling back to a truncated ref string."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", ref],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ref[:8]


@contextmanager
def baseline_worktree(project_root: Path, ref: str) -> Iterator[Path]:
    """Check out *ref* in a throwaway detached worktree; yield its path.

    The worktree is created under ``<project_root>/.booley_project/`` with a
    PID-suffixed name, so two baseline runs in the same project (e.g. concurrent
    interactive sessions) never collide on the directory. ``--detach`` checks the
    ref out as a detached HEAD, sidestepping git's "ref already checked out in
    another worktree" error when *ref* is a branch already live elsewhere (a
    ticket run, the main worktree). The worktree is force-removed and pruned on
    exit whether or not the body raised.

    Raises :class:`BaselineWorktreeError` if the baseline tree cannot be fully
    created (bad ref, missing submodule source, not a git repository, ...).
    """
    project_root = Path(project_root)
    short = git_short_sha(ref, project_root)
    wt_dir = project_root / ".booley_project" / f".baseline-wt-{os.getpid()}-{short}"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    # A crashed prior run can leave a stale worktree registration behind; prune
    # first so ``add`` cannot fail on a dangling entry for this exact path.
    _git(project_root, "worktree", "prune", timeout=30)

    add = _git(
        project_root,
        "worktree",
        "add",
        "--detach",
        "--force",
        str(wt_dir),
        ref,
        timeout=120,
    )
    if add.returncode != 0:
        detail = (add.stderr or add.stdout or "").strip()
        raise BaselineWorktreeError(
            f"git worktree add for baseline ref {ref!r} failed: {detail or add.returncode}"
        )

    try:
        _populate_submodules(wt_dir, ref)
        _copy_stealth_cores(project_root, wt_dir, ref)
        _copy_root_quarantine_marker(project_root, wt_dir)
        yield wt_dir
    finally:
        rm = _git(
            project_root,
            "worktree",
            "remove",
            "--force",
            str(wt_dir),
            timeout=60,
        )
        if rm.returncode != 0:
            # Leave the dir for `git worktree prune` to reap later rather than
            # failing the run — the metrics are already collected by now.
            logger.warning(
                "baseline worktree cleanup failed for %s: %s",
                wt_dir,
                (rm.stderr or rm.stdout or "").strip(),
            )
        _git(project_root, "worktree", "prune", timeout=30)


def _copy_root_quarantine_marker(project_root: Path, wt_dir: Path) -> None:
    """Preserve an untracked root FuseSoC quarantine in a baseline checkout."""
    marker = project_root / "FUSESOC_IGNORE"
    if marker.is_file():
        shutil.copy2(marker, wt_dir / marker.name)


def _populate_submodules(wt_dir: Path, ref: str) -> None:
    """Materialize the baseline ref's recursive gitlink source tree."""
    if not (wt_dir / ".gitmodules").is_file():
        return
    try:
        update = _git(
            wt_dir,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--checkout",
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise BaselineWorktreeError(
            f"initializing submodules for baseline ref {ref!r} timed out after 300s"
        ) from exc
    if update.returncode == 0:
        return
    detail = (update.stderr or update.stdout or "").strip()
    raise BaselineWorktreeError(
        f"initializing submodules for baseline ref {ref!r} failed: {detail or update.returncode}"
    )


def _copy_stealth_cores(project_root: Path, wt_dir: Path, ref: str) -> None:
    """Mirror the stealth authored-cores dir (ADR 0036) into the worktree.

    ``.booley_project/`` is git-excluded, so a fresh ``git worktree add``
    checkout carries no ``.booley_project/cores/`` — and the baseline run
    scans cores relative to the *worktree*, so stealth-only projects would
    silently compare against zero cores. Copying the *live* stealth cores is
    the pragmatic choice: Flow config is likewise read from the live project
    dir, not the baseline ref.

    Symlinks are copied **as symlinks**. A stealth ``.core`` resolves its
    filesets relative to itself, so it reaches the RTL through core-relative
    resolution links (ADR 0036: ``cores/rtl -> ../../rtl``). Dereferencing
    those — ``copytree``'s default — writes the *live working tree's* RTL into
    the baseline checkout, so both sides of a ``--baseline`` delta synthesize
    the modified design and report a reassuring ~+0.0% that measured nothing.
    Kept as a link, the same relative path resolves inside the worktree, i.e.
    against the baseline ref's RTL.

    Raises :class:`BaselineWorktreeError` on copy failure: a partial copy
    would produce silently-wrong baseline metrics, the one thing delta mode
    exists to prevent.
    """
    src = state_cores_dir(project_root)
    if not src.is_dir():
        return
    project_root = Path(os.path.normpath(str(project_root)))
    wt_dir = Path(os.path.normpath(str(wt_dir)))
    dst = state_cores_dir(wt_dir)
    try:
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        missing = _retarget_links(project_root, wt_dir, src, dst)
    except OSError as exc:
        raise BaselineWorktreeError(
            f"copying stealth cores {src} into baseline worktree failed: {exc}"
        ) from exc
    if missing:
        raise BaselineWorktreeError(
            f"baseline ref {ref!r} has no source for stealth-core resolution "
            f"link(s) {', '.join(missing)}: the target exists in the working tree "
            "but not in the checkout, so the baseline cannot be built from that "
            "ref. Commit the target paths, or pick a ref that contains them."
        )


def _symlinks_under(root: Path) -> list[Path]:
    """Every symlink in *root*'s tree, links to directories included.

    ``os.walk`` does not follow symlinked directories (``followlinks`` defaults
    to ``False``), so a resolution link is reported once and never descended
    into — the walk stays inside the copied cores tree.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            entry = Path(dirpath) / name
            if entry.is_symlink():
                found.append(entry)
    return found


def _link_target_under(link: Path, root: Path) -> Path | None:
    """Where *link* points if that lands inside *root*, else ``None``.

    Purely lexical (``normpath``, never ``resolve``): one hop, without chasing
    the target's own symlinks, so the answer depends only on the link text and
    cannot be perturbed by a symlinked path component above the project.
    """
    target = Path(os.path.normpath(link.parent / link.readlink()))
    root = Path(os.path.normpath(root))
    return target if target == root or root in target.parents else None


def _retarget_links(project_root: Path, wt_dir: Path, src: Path, dst: Path) -> list[str]:
    """Point each copied link at whichever tree actually holds its target.

    Classified by where the link pointed in the *live* tree (read off the source
    link, whose text the copy reproduces verbatim) — four cases:

    - **Outside the project** (a vendored drop, a shared library tree): left
      alone. It names the same external thing from either tree, and rebasing
      would invent a path that does not exist.
    - **Inside the mirrored ``cores/`` tree**: re-pointed at the copy, which
      holds the same content. A relative link already resolves there and only
      gets its own text back; an absolute one stops naming the live tree.
    - **Elsewhere in the state dir** (ADR 0036 blesses ``cores/worktrees ->
      ../worktrees``, e.g. a core pinning a frozen checkout): re-pointed at the
      LIVE state dir. ``.booley_project/`` is git-excluded, so no checkout of any
      ref has one — and that storage is deliberately ref-independent, like the
      Flow config a baseline run likewise reads from the live project dir.
    - **Repo content** (``cores/rtl -> ../../rtl``): re-pointed at the
      worktree's copy, which is the whole point of delta mode. A relative link
      already resolves there; an absolute one (``/home/me/prj/rtl``) still names
      the live tree and would reintroduce exactly the dereferencing this copy
      avoids, so it is rewritten.

    Returns the links in that last group whose target is missing from the
    checkout — present in the working tree but untracked, or added after the
    ref. Left dangling, the flow either dies on a confusing path error or
    silently falls back to the current sources, which is the failure mode this
    copy exists to prevent; the caller refuses the run instead.
    """
    state_dir = src.parent
    missing: list[str] = []
    for link in _symlinks_under(dst):
        rel = link.relative_to(dst)
        # Classify from the SOURCE link: its relative text resolves against the
        # live tree, whereas the copy's identical text resolves in the worktree.
        target = _link_target_under(src / rel, project_root)
        if target is None:
            continue
        if _is_within(target, src):
            _repoint(link, dst / target.relative_to(src))
            continue
        if _is_within(target, state_dir):
            _repoint(link, target)
            continue
        in_worktree = wt_dir / target.relative_to(project_root)
        _repoint(link, in_worktree)
        if not in_worktree.exists() and target.exists():
            missing.append(str(rel))
    return sorted(missing)


def _is_within(path: Path, root: Path) -> bool:
    """True when *path* is *root* or sits under it (lexical, no resolution)."""
    return path == root or root in path.parents


def _repoint(link: Path, target: Path) -> None:
    """Replace *link* with a relative symlink to *target*.

    Relative rather than absolute so the link keeps meaning the same thing
    through the Session Runtime workspace mount, like links authored by a
    project.
    """
    link.unlink()
    link.symlink_to(os.path.relpath(target, link.parent))


def _git(cwd: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand under *cwd*, never raising on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
