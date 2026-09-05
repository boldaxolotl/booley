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

For Ticket Mode's paired ``.booley_project`` repository, the outer baseline
worktree receives a nested detached worktree at the ticket branch's fork point.
That preserves the baseline revision's Target definitions and recipe instead of
mirroring live project data into both comparisons.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict, as_str
from booley.fusesoc.fusesoc_registry import state_cores_dir
from booley.runtime.submodule_materialization import (
    SubmoduleMaterializationError,
    materialize_submodules,
)
from booley.runtime.ticket_repositories import paired_project_repository
from booley.targets.target import TargetHandle

from .recipe_evidence import BASELINE_REF_PARAM

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


def git_full_sha(ref: str, cwd: Path) -> str | None:
    """Resolve *ref* to a full commit SHA, returning None on invalid input."""
    result = _git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}", timeout=30)
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_ticket_baseline(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    targets: Sequence[TargetHandle],
    requested: str | None,
    project_root: Path,
    flow_name: str,
) -> tuple[str | None, str | None, str | None]:
    """Resolve and enforce one immutable ticket baseline across selected Targets."""
    matched_params: list[dict[Any, Any]] = []
    for target in targets:
        names = {
            f"{criterion_prefix}{target.selector}",
            f"{criterion_prefix}{target.identity}",
            f"{criterion_prefix}{target.name}",
        }
        matches = []
        for key, entry in criteria.items():
            params = as_dict(getattr(entry, "params", None)) or {}
            belongs_to_family = key.startswith(criterion_prefix)
            if key in names or (belongs_to_family and params.get("target") == target.identity):
                matches.append(params)
        if len(matches) > 1:
            return (
                requested,
                None,
                (f"{flow_name}: no unique persisted criterion for {target.identity!r}"),
            )
        matched_params.extend(matches)
    refs = {ref for params in matched_params if (ref := as_str(params.get(BASELINE_REF_PARAM)))}
    if not refs:
        return requested, None, None
    if len(refs) != 1:
        return requested, None, f"{flow_name}: selected criteria carry conflicting baseline refs"
    expected = next(iter(refs))
    selected = requested or expected
    actual_sha = git_full_sha(selected, project_root)
    expected_sha = git_full_sha(expected, project_root)
    if actual_sha is None or expected_sha is None:
        return selected, None, f"{flow_name}: ticket baseline ref cannot be resolved to a commit"
    if actual_sha != expected_sha:
        error = (
            f"{flow_name}: baseline-relative ticket criteria require the pinned "
            f"base_sha {expected_sha}; got {actual_sha}"
        )
        return selected, None, error
    return selected, actual_sha, None


@contextmanager
def baseline_worktree(project_root: Path, ref: str) -> Iterator[Path]:
    """Yield a fully materialized detached worktree for *ref*, then remove it."""
    project_root = Path(project_root)
    wt_dir = _create_baseline_worktree(project_root, ref)
    paired_baseline: Path | None = None
    try:
        _materialize_baseline_submodules(project_root, wt_dir, ref)
        paired_baseline = _install_paired_project_baseline(project_root, wt_dir)
        if paired_baseline is None:
            _copy_stealth_cores(project_root, wt_dir, ref)
        _copy_root_quarantine_marker(project_root, wt_dir)
        yield wt_dir
    finally:
        _cleanup_baseline_worktree(project_root, wt_dir, paired_baseline)


def _create_baseline_worktree(project_root: Path, ref: str) -> Path:
    short = git_short_sha(ref, project_root)
    worktree = project_root / ".booley_project" / f".baseline-wt-{os.getpid()}-{short}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(project_root, "worktree", "prune", timeout=30)
    result = _git(
        project_root,
        "-c",
        "submodule.recurse=false",
        "worktree",
        "add",
        "--detach",
        "--force",
        str(worktree),
        ref,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BaselineWorktreeError(
            f"git worktree add for baseline ref {ref!r} failed: {detail or result.returncode}"
        )
    return worktree


def _materialize_baseline_submodules(project_root: Path, worktree: Path, ref: str) -> None:
    try:
        materialize_submodules(project_root, worktree)
    except SubmoduleMaterializationError as exc:
        raise BaselineWorktreeError(
            f"initializing submodules for baseline ref {ref!r} failed offline: {exc}"
        ) from exc


def _cleanup_baseline_worktree(
    project_root: Path, worktree: Path, paired_baseline: Path | None
) -> None:
    if paired_baseline is not None:
        _remove_paired_project_baseline(project_root, paired_baseline)
    result = _git(project_root, "worktree", "remove", "--force", str(worktree), timeout=60)
    if result.returncode != 0:
        logger.warning(
            "baseline worktree cleanup failed for %s: %s",
            worktree,
            (result.stderr or result.stdout or "").strip(),
        )
    _git(project_root, "worktree", "prune", timeout=30)


def _install_paired_project_baseline(project_root: Path, wt_dir: Path) -> Path | None:
    """Check out the paired project repository at its ticket fork point."""
    repository = paired_project_repository(project_root)
    if repository is None:
        return None
    base_sha = _paired_project_base_sha(repository.worktree)
    destination = wt_dir / ".booley_project"
    add = _git(
        repository.worktree,
        "-c",
        "submodule.recurse=false",
        "worktree",
        "add",
        "--detach",
        "--force",
        str(destination),
        base_sha,
        timeout=120,
    )
    if add.returncode != 0:
        detail = (add.stderr or add.stdout or "").strip()
        raise BaselineWorktreeError(
            f"could not materialize paired project baseline {base_sha[:12]}: "
            f"{detail or add.returncode}"
        )
    return destination


def _paired_project_base_sha(project_worktree: Path) -> str:
    """Resolve the immutable fork point of a paired ticket project branch."""
    ticket_file = os.environ.get("BOOLEY_TICKET_FILE", "")
    if ticket_file:
        from booley.runtime.project_dir import resolve_project_dir
        from booley.ticket_board.acceptance_targets import resolve_commit
        from booley.ticket_board.helpers import detect_project_root
        from booley.ticket_board.io import TicketIO

        ticket_path = Path(ticket_file)
        project_root = detect_project_root()
        basis = TicketIO(
            resolve_project_dir(project_root) / "tickets",
            project_root=project_root,
        ).load_basis(ticket_path.stem, runtime_ticket_path=ticket_path)
        if basis is not None and basis.project_sha:
            try:
                return resolve_commit(project_worktree, basis.project_sha)
            except ValueError as exc:
                raise BaselineWorktreeError(
                    f"recorded paired project Acceptance Basis cannot be resolved: {exc}"
                ) from exc
    upstream = _git(project_worktree, "rev-parse", "@{upstream}", timeout=30)
    if upstream.returncode != 0:
        raise BaselineWorktreeError("paired project ticket branch has no baseline upstream")
    merge_base = _git(
        project_worktree,
        "merge-base",
        upstream.stdout.strip(),
        "HEAD",
        timeout=30,
    )
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        raise BaselineWorktreeError("could not resolve paired project ticket fork point")
    return merge_base.stdout.strip()


def _remove_paired_project_baseline(project_root: Path, baseline: Path) -> None:
    """Remove the nested project worktree before its outer worktree disappears."""
    repository = paired_project_repository(project_root)
    if repository is None:
        logger.warning("paired project repository disappeared before baseline cleanup")
        return
    result = _git(
        repository.worktree,
        "worktree",
        "remove",
        "--force",
        str(baseline),
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning(
            "paired project baseline cleanup failed for %s: %s",
            baseline,
            (result.stderr or result.stdout or "").strip(),
        )
    _git(repository.worktree, "worktree", "prune", timeout=30)


def _copy_root_quarantine_marker(project_root: Path, wt_dir: Path) -> None:
    """Preserve an untracked root FuseSoC quarantine in a baseline checkout."""
    marker = project_root / "FUSESOC_IGNORE"
    if marker.is_file():
        shutil.copy2(marker, wt_dir / marker.name)


def _copy_stealth_cores(project_root: Path, wt_dir: Path, ref: str) -> None:
    """Mirror the stealth authored-cores dir (ADR 0036) into the worktree.

    ``.booley_project/`` is git-excluded, so a fresh ``git worktree add``
    checkout carries no ``.booley_project/cores/`` — and the baseline run
    scans cores relative to the *worktree*, so stealth-only projects would
    silently compare against zero cores. For an unpaired project, copying the
    *live* stealth cores is the pragmatic choice: Flow config is likewise read
    from the live project dir, not the baseline ref. Paired project repositories
    take the separate fork-point worktree path before this helper is called.

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
    target = _lexical_path(link.parent / link.readlink())
    root = _lexical_path(root)
    return target if target == root or root in target.parents else None


def _lexical_path(path: Path) -> Path:
    """Normalize an absolute path without following its symlink target."""
    value = os.path.abspath(os.path.normpath(path))  # noqa: PTH100 - lexical; do not follow links
    if os.name == "nt" and value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


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
