"""Clean-commit participants, the clean rule and symlink containment (ADR 0076 §1)."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.booley_layout import BooleyLayoutError
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.core.config_paths import resolve_toml
from booley.result_cache._canonical import frame
from booley.result_cache.tracked_files import (
    TrackedTree,
    git_output,
    relative_posix,
    walk_tracked,
)
from booley.runtime.git_ignore import ignored_paths
from booley.runtime.history_commit import FileCommitError
from booley.runtime.project_repositories import is_git_worktree_root, run_git

_MAX_SHOWN_RECORDS = 5


class CleanCommitError(RuntimeError):
    """Git or the file system failed during the check; the run is not cacheable."""


class _NotCacheableError(Exception):
    """Participant selection refused the run with this reason."""


@dataclass(frozen=True)
class Participant:
    label: str  # "rtl" or "booley"
    root: Path  # resolved repository top level
    head: str  # HEAD commit id, a label only: never keyed


@dataclass(frozen=True)
class CleanCommit:
    """Whether a run starts from clean participants, and their tracked-files digest.

    ``cacheable`` covers participant selection, the clean rule, out-of-tree
    symlinks and tracked bytes only. It does not yet cover ignored files
    (including locally excluded ones), Booley-owned paths or snapshot equality
    (#2099), the configuration (#2212), nor the tool, image, Booley and host
    parts of the Result Key.
    """

    booley_dir: Path | None
    participants: tuple[Participant, ...]
    reasons: tuple[str, ...]  # empty exactly when the run is cacheable
    tracked_digest: str | None  # "sha256:<hex>" when cacheable, else None

    @property
    def cacheable(self) -> bool:
        return not self.reasons


def check_clean_commit(checkout_root: Path) -> CleanCommit:
    """Select the run's participants, check them clean and digest their tracked files.

    A refusal is a reason on the result. A Git or file-system failure raises
    :class:`CleanCommitError`; the caller treats both as "not cacheable".
    """
    project_root = Path(checkout_root).resolve()
    try:
        booley_dir, roots = _select_participants(Path(checkout_root), project_root)
        participants = tuple(_participant(label, root) for label, root in roots)
        reasons, digest = _check(booley_dir, project_root, participants)
    except _NotCacheableError as refusal:
        return CleanCommit(None, (), (str(refusal),), None)
    except (FileCommitError, SourceCheckoutProjectError, BooleyLayoutError, OSError) as exc:
        raise CleanCommitError(f"cannot check clean commit at {checkout_root}: {exc}") from exc
    return CleanCommit(booley_dir, participants, reasons, None if reasons else digest)


def _select_participants(
    checkout_root: Path, project_root: Path
) -> tuple[Path, list[tuple[str, Path]]]:
    top = run_git(checkout_root, "rev-parse", "--show-toplevel")
    if top.returncode or not top.stdout.strip():
        raise _NotCacheableError(f"{checkout_root} is not in a Git repository")
    rtl_root = Path(top.stdout.strip()).resolve()
    if relative_posix(project_root, rtl_root) is None:
        raise _NotCacheableError(
            f"Project Root {project_root} is not inside its repository's work tree {rtl_root}"
        )
    if project_root != rtl_root and ignored_paths(
        rtl_root, [project_root], directories=frozenset({project_root})
    ):
        raise _NotCacheableError(f"Project Root {project_root} is ignored by the RTL repository")
    try:
        booley_dir = resolve_checkout_booley_dir(checkout_root).resolve()
    except FileNotFoundError as exc:
        raise _NotCacheableError(f"no Booley Directory: {exc}") from exc
    if is_git_worktree_root(booley_dir) and not booley_dir.samefile(rtl_root):
        return booley_dir, [("rtl", rtl_root), ("booley", booley_dir)]
    inside = relative_posix(booley_dir, rtl_root)
    config = "/".join(filter(None, (inside, resolve_toml(booley_dir).name)))
    # Literal pathspecs: a directory named ``cfg?`` must not match a sibling ``cfgx``.
    if inside is not None and git_output(
        rtl_root, "--literal-pathspecs", "ls-files", "-z", "--", config
    ):
        return booley_dir, [("rtl", rtl_root)]
    raise _NotCacheableError(
        f"Booley Directory {booley_dir} is neither tracked in the RTL repository "
        "nor its own repository"
    )


def _participant(label: str, root: Path) -> Participant:
    head = run_git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if head.returncode:
        raise _NotCacheableError(f"{label} repository {root} has no commit")
    return Participant(label, root, head.stdout.strip())


def _check(
    booley_dir: Path, project_root: Path, participants: Sequence[Participant]
) -> tuple[tuple[str, ...], str]:
    config_name = resolve_toml(booley_dir).name
    trees = [
        walk_tracked(p.label, p.root, booley_dir, project_root, config_name) for p in participants
    ]
    nested = participants[1].root if len(participants) > 1 else None
    reasons = [reason for tree in trees for reason in tree.reasons]
    for tree in trees:
        reasons += [r for repo in tree.repositories for r in _dirty(tree.label, repo, nested)]
    roots = [tree.root for tree in trees]
    for tree in trees:
        links = [
            *tree.links,
            *(l for repo in tree.repositories for l in _ignored_links(repo, nested)),
        ]
        reasons += [r for link in links if (r := _outside_reason(tree, link, roots))]
    hasher = hashlib.sha256()
    for tree in sorted(trees, key=lambda tree: tree.label):
        frame(hasher, tree.label.encode(), tree.digest().encode())
    return tuple(reasons), f"sha256:{hasher.hexdigest()}"


def _dirty(label: str, repository: Path, nested: Path | None) -> list[str]:
    """Reasons for modified, staged or untracked entries in one repository (ADR §1 Clean)."""
    # Git shows the nested participant as one untracked directory and never descends into it.
    inside = None if nested is None else relative_posix(nested, repository)
    exempt = f"?? {inside}/".encode() if inside else None
    status = git_output(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    records = [record for record in status.split(b"\0") if record and record != exempt]
    shown = ", ".join(os.fsdecode(record) for record in records[:_MAX_SHOWN_RECORDS])
    return [f"{label} repository {repository} is not clean: {shown}"] if records else []


def _ignored_links(repository: Path, nested: Path | None) -> Iterator[Path]:
    """Ignored symlinks; Git lists an ignored nested repository only as ``dir/``, so walk it."""
    listing = git_output(
        repository, "ls-files", "-z", "--others", "--ignored", "--exclude-standard"
    )
    for entry in filter(None, listing.split(b"\0")):
        path = repository / os.fsdecode(entry)
        if not entry.endswith(b"/"):
            if _is_link(path):
                yield path
        elif nested is None or not path.samefile(nested):
            # os.walk never follows links; an unreadable directory must fail, not be skipped.
            for current, dirnames, filenames in os.walk(path, onerror=_raise):
                names = [name for name in (*dirnames, *filenames) if _is_link(Path(current, name))]
                yield from (Path(current, name) for name in names)
                dirnames[:] = [name for name in dirnames if name not in names]


def _raise(error: OSError) -> None:
    raise error


def _is_link(path: Path) -> bool:
    """Unlike ``Path.is_symlink`` (which hides ``EACCES`` on Python 3.14), raises when unreadable."""
    return stat.S_ISLNK(path.lstat().st_mode)


def _outside_reason(tree: TrackedTree, link: Path, roots: Sequence[Path]) -> str | None:
    """A reason when *link* leaves the participants, lexically or resolved, or reaches ``.git``."""
    raw = link.readlink()
    lexical = Path(os.path.normpath(link.parent / raw))
    # ``..`` after a name may step out through a directory link: refuse it, and the lexical
    # rule rejects a hop through an outside link that points back inside.
    stepped = any(a != ".." and b == ".." for a, b in zip(raw.parts, raw.parts[1:], strict=False))
    if not stepped and all(
        _is_inside(path, roots) for path in (lexical, Path(os.path.realpath(link)))
    ):
        return None
    where = relative_posix(link, tree.root)
    return f"symlink {tree.label}:{where} resolves outside the participants ({raw})"


def _is_inside(path: Path, roots: Sequence[Path]) -> bool:
    """Component-wise under a participant root, with no ``.git`` component."""
    return ".git" not in path.parts and any(path.is_relative_to(root) for root in roots)
