"""The Result Key's Tracked files part: working-tree bytes of every tracked entry (ADR 0076 §2)."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from booley.core.config_paths import GOALSETS_DIR
from booley.result_cache._canonical import frame
from booley.runtime.pinned_history import raw_git
from booley.runtime.project_repositories import is_git_worktree_root, run_git

_FORMAT_TAG = b"booley-result-cache-tracked-v1"
# Marker for "not inside this participant"; never a valid relative path.
_OUTSIDE = b"\0"
_GITLINK_MODE = b"160000"


def git_output(repository: Path, *args: str) -> bytes:
    """Raw Git output with the file-system monitor, ``core.ignoreCase`` and the untracked cache off.

    A monitor hook can hide a modified file, ``core.ignoreCase=true`` (a
    repository made on a case-insensitive host, used here) an untracked one,
    and a stale untracked cache (a mount that never updates directory mtimes)
    an untracked file.
    """
    return raw_git(
        repository,
        *("-c", "core.fsmonitor=false", "-c", "core.ignoreCase=false"),
        *("-c", "core.untrackedCache=false"),
        *args,
    )


def relative_posix(path: Path, root: Path) -> str | None:
    """*path* below *root* (both absolute) as POSIX text, ``""`` for *root*, None when outside."""
    if not path.is_relative_to(root):
        return None
    return "/".join(path.relative_to(root).parts)


def is_configuration_file(relative: str, booley_relative: str | None, config_name: str) -> bool:
    """Whether *relative* (participant-relative) is a file the configuration key covers.

    Those are the resolved config file, ``tests.toml``, ``criteria.toml`` and the
    Goalsets (direct children ``goalsets/<name>.md``) of the Booley Directory
    at *booley_relative*; the tracked-files digest leaves them out.
    """
    if booley_relative is None:
        return False
    prefix = f"{booley_relative}/" if booley_relative else ""
    if not relative.startswith(prefix):
        return False
    inside = PurePosixPath(relative[len(prefix) :])
    if inside.parts[:1] == (GOALSETS_DIR,):
        return len(inside.parts) == 2 and inside.suffix == ".md"
    return str(inside) in {config_name, "tests.toml", "criteria.toml"}


@dataclass
class TrackedTree:
    """One participant's tracked entries, walked repositories, tracked symlinks and reasons."""

    label: str
    root: Path
    booley_relative: str | None  # the Booley Directory's path in this participant, if inside
    project_relative: str | None  # the Project Root's path in this participant, if inside
    config_name: str
    repositories: list[Path] = field(default_factory=list)
    links: list[Path] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    records: list[tuple[bytes, ...]] = field(default_factory=list)

    def digest(self) -> str:
        """``sha256:<hex>`` of the layout and entries; roots, HEADs and timestamps are never keyed."""
        hasher = hashlib.sha256()
        positions = (self.booley_relative, self.project_relative)
        frame(
            hasher,
            _FORMAT_TAG,
            self.label.encode(),
            *(_OUTSIDE if place is None else place.encode() for place in positions),
        )
        for record in sorted(self.records):
            frame(hasher, *record)
        return f"sha256:{hasher.hexdigest()}"


def walk_tracked(
    label: str, root: Path, booley_dir: Path, project_root: Path, config_name: str
) -> TrackedTree:
    """Walk the participant at *root* and each initialised submodule, recording every entry."""
    tree = TrackedTree(
        label,
        root,
        relative_posix(booley_dir, root),
        relative_posix(project_root, root),
        config_name,
    )
    _walk_repository(tree, root, b"")
    return tree


def _walk_repository(tree: TrackedTree, repository: Path, prefix: bytes) -> None:
    tree.repositories.append(repository)
    for line in filter(None, git_output(repository, "ls-files", "-z", "-s", "-v").split(b"\0")):
        info, _, raw_path = line.partition(b"\t")
        tag, mode, oid, stage = info.split(b" ")
        relative = os.fsdecode(prefix + raw_path)
        path = tree.root / relative
        if mode == _GITLINK_MODE:
            state = _gitlink_state(tree, path, relative, oid)
        else:
            state = _entry_state(tree, path, relative)
        # A gitlink at a configuration path is still keyed: only files and links are excluded.
        if mode == _GITLINK_MODE or not is_configuration_file(
            relative, tree.booley_relative, tree.config_name
        ):
            tree.records.append((prefix + raw_path, tag, mode, stage, *state))


def _entry_state(tree: TrackedTree, path: Path, relative: str) -> tuple[bytes, ...]:
    """Working-tree state of one tracked file: bytes read by Python, no Git filter."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return (b"absent",)
    if stat.S_ISLNK(status.st_mode):
        tree.links.append(path)
        # Raw text: Path.readlink() would drop a trailing "/" and "." parts, which change behavior.
        return (b"link", os.fsencode(os.readlink(path)))  # noqa: PTH115
    if not stat.S_ISREG(status.st_mode):
        tree.reasons.append(f"{tree.label}:{relative} is neither a file nor a symlink")
        return (b"other",)
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            hasher.update(chunk)
    executable = b"x" if status.st_mode & stat.S_IXUSR else b"-"
    return (b"file", executable, hasher.hexdigest().encode())


def _gitlink_state(tree: TrackedTree, path: Path, relative: str, oid: bytes) -> tuple[bytes, ...]:
    """Recurse into an initialised submodule; only an absent or empty path is uninitialised."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return (b"gitlink", oid, b"uninitialised")
    if stat.S_ISDIR(status.st_mode):
        if is_git_worktree_root(path):
            # status prints nothing for an unborn HEAD, nor (with assume-unchanged or
            # skip-worktree on the gitlink) for a HEAD off the recorded commit.
            head = run_git(path, "rev-parse", "--verify", "-q", "HEAD^{commit}")
            if head.returncode:
                tree.reasons.append(
                    f"{tree.label}:{relative} is a submodule repository with no commit"
                )
            elif head.stdout.strip().encode() != oid:
                tree.reasons.append(f"{tree.label}:{relative} is not at its recorded commit")
            _walk_repository(tree, path, os.fsencode(relative) + b"/")
            return (b"gitlink", oid, b"initialised")
        if not any(path.iterdir()):
            return (b"gitlink", oid, b"uninitialised")
    tree.reasons.append(f"{tree.label}:{relative} is populated but not an initialised submodule")
    return (b"gitlink", oid, b"populated")
