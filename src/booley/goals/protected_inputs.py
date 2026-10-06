"""Protected inputs: the files that decide how evidence is produced (ADR 0067 D7).

At entry the Goal Record stores the protected paths (``protected_paths``) and
two digests over them: the working view (``protected_digest``) and the HEAD
view (``protected_head_digest``). Later calls recompute both and compare like
with like (:func:`protected_input_violations`); a protected path list that
changed is itself a violation.

Resolver contract
=================

Booley's consumers of the protected set do not share one resolver, and several
choose between candidates at run time (a snapshot copy if it exists, else the
main checkout's). Emulating each choice would let a selection mismatch hide a
change, so the digest covers the **superset**: every file any consumer could
read for a protected input. A candidate that does not exist is digested as
absent. Every path is recorded relative to one of four roots, so the same
worktree reached through another spelling (a bind-mount alias, a symlink)
records the same list:

- ``worktree``: the Goal worktree root (``work_dir``);
- ``project``: the session Project directory the caller resolved
  (:func:`booley.runtime.project_dir.resolve_project_dir`; in a Sandbox the
  shared ``BOOLEY_PROJECT_DIR``);
- ``main``: the primary checkout of the worktree's repository;
- ``abs``: an absolute path, only for a ``[project].dir`` override that lies
  outside the other three.

=============================== =============================================== ===================================
Protected input                 Consumers and what they read                    Candidates digested
=============================== =============================================== ===================================
Project ``booley.toml``         Session-global readers (``project_config``      ``project:booley.toml``; the
                                globals, EDA requests, endpoint config,         ``booley.toml`` in
                                Specialist ``[agent]``/``[models]``) read the   ``worktree:.booley_project/``,
                                session Project directory. Flow readers given   ``worktree:.booley/project/``,
                                ``work_dir`` read the worktree snapshot         ``main:.booley_project/``,
                                (``resolve_checkout_project_dir``, which may    ``main:.booley/project/``; the
                                follow a ``[project].dir`` override) or the     directory
                                fixed ``.booley_project`` / ``.booley/project`` ``resolve_checkout_project_dir``
                                path, falling back to the main checkout's copy. returns
Checkout ``booley.toml``        ``[project].dir`` routing walks up from the     ``worktree:booley.toml``
                                worktree root.
legacy ``pipeline.toml``        Every reader above falls back to it beside      the ``pipeline.toml`` sibling of
                                a missing ``booley.toml`` (``resolve_toml``).   every ``booley.toml`` candidate
``hooks/``                      Runner hooks read the session Project           ``project:hooks``,
                                directory; worktree preparation reads the       ``worktree:.booley_project/hooks``,
                                snapshot, or the directory                      ``hooks`` in the directory
                                ``resolve_checkout_project_dir`` returns.       ``resolve_checkout_project_dir``
                                No Flow or Specialist reads it.                 returns
``.managed/``                   The Git hook adapters run the worktree's        ``worktree:.booley_project/.managed``,
                                bundle file when it exists, else the main       ``main:.booley_project/.managed``,
                                checkout's; ``init`` writes the session         ``project:.managed``
                                Project directory's.
``generators/``                 No Booley reader exists; FuseSoC generators     ``project:generators``
                                are declared per ``.core``. Digested where it
                                is authored.
``mcp_tools/``                  Discovered once, at MCP server start, from      ``project:mcp_tools``,
                                ``BOOLEY_PROJECT_DIR``, else the server's       ``main:.booley_project/mcp_tools``
                                working directory (the main checkout). The
                                snapshot copy is never run.
``FUSESOC_IGNORE``              Core discovery scans the ``work_dir`` root.     ``worktree:FUSESOC_IGNORE``
=============================== =============================================== ===================================

Encoding rules:

- A directory is digested recursively by its files and symlinks; adding or
  deleting a file changes the digest. Empty subdirectories are not entries
  (Git cannot track one either). ``__pycache__`` directories are skipped:
  Python rewrites them when it imports a custom MCP tool.
- A symlink is digested by its target text **and** by what it resolves to:
  a file's content, a directory's tree (followed, with a visited set so a
  cycle is recorded as ``cycle`` instead of recursing), or ``dangling``.
  Consumers open the target, so a change behind a link is a change.
- The working view digests the bytes on disk. The HEAD view applies only to
  ``worktree`` paths: each entry HEAD tracks contributes its committed blob,
  marked tracked; each other entry contributes its working bytes, marked
  untracked. A committed deletion therefore changes the HEAD view even when
  the file is restored as untracked bytes, and a checkout that converts line
  endings never differs from itself. Paths outside the worktree have no HEAD
  and contribute their working view.
- HEAD is that of the innermost repository containing the path, so a paired
  Project checkout nested in the worktree is read from its own HEAD. A
  tracked symlink's target is read through the HEAD view as well.
- A directory that cannot be listed fails the snapshot; it is never digested
  as empty.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from booley.core.project_dir import PROJECT_DIR_NAME, resolve_checkout_project_dir
from booley.goals.checkout import CheckoutError, GoalCheckout
from booley.runtime.git import git_common_dir

# The protected set as the rules text names it (ADR 0067 "Protected inputs").
PROTECTED_INPUT_NAMES: tuple[str, ...] = (
    "booley.toml",
    f"{PROJECT_DIR_NAME}/booley.toml",
    f"{PROJECT_DIR_NAME}/hooks/",
    f"{PROJECT_DIR_NAME}/.managed/",
    f"{PROJECT_DIR_NAME}/generators/",
    f"{PROJECT_DIR_NAME}/mcp_tools/",
    "FUSESOC_IGNORE",
)
# Every name a Project configuration file may have, newest first (resolve_toml).
CONFIG_FILE_NAMES: tuple[str, ...] = ("booley.toml", "pipeline.toml")
# The legacy Project directory layout resolve_booley_toml still reads.
LEGACY_PROJECT_DIR = ".booley/project"
_SKIPPED_DIRECTORIES = frozenset({"__pycache__"})
_DIGEST_FORMAT = "booley-protected-inputs/2"

Entry = dict[str, object]
Tree = dict[str, Entry]


class ProtectedInputError(RuntimeError):
    """A protected input could not be read, or a recorded path could not be parsed."""


class RootKind(StrEnum):
    """The root a protected path is recorded relative to (module docstring)."""

    WORKTREE = "worktree"
    PROJECT = "project"
    MAIN = "main"
    ABSOLUTE = "abs"


@dataclass(frozen=True)
class ProtectedPath:
    """One protected path: a root and a POSIX path relative to it (absolute for ``abs``)."""

    root: RootKind
    relative: str

    def encode(self) -> str:
        """The form stored in ``record.json``: ``<root>:<path>``."""
        return f"{self.root.value}:{self.relative}"

    @classmethod
    def decode(cls, text: str) -> ProtectedPath:
        """Parse a stored path, raising :class:`ProtectedInputError`."""
        root, separator, relative = text.partition(":")
        try:
            kind = RootKind(root)
        except ValueError:
            raise ProtectedInputError(f"protected path {text!r} has an unknown root") from None
        if not separator or not relative:
            raise ProtectedInputError(f"protected path {text!r} has no path")
        return cls(kind, relative)


@dataclass(frozen=True)
class ProtectedInputRoots:
    """Where the resolvers look: the Goal worktree and the session's Project directory."""

    worktree: Path
    project_dir: Path

    def main_checkout(self) -> Path | None:
        """The primary checkout of the worktree's repository (``None`` for a bare one)."""
        try:
            common = git_common_dir(self.worktree)
        except RuntimeError as exc:
            raise ProtectedInputError(str(exc)) from exc
        return common.parent if common.name == ".git" else None

    def absolute(self, path: ProtectedPath, main: Path | None) -> Path | None:
        """Where *path* is now; ``None`` when its root does not exist."""
        if path.root is RootKind.ABSOLUTE:
            return Path(path.relative)
        base = {
            RootKind.WORKTREE: self.worktree,
            RootKind.PROJECT: self.project_dir,
            RootKind.MAIN: main,
        }[path.root]
        return None if base is None else base.joinpath(*PurePosixPath(path.relative).parts)


@dataclass(frozen=True)
class ProtectedSnapshot:
    """The protected paths and both digests over them."""

    paths: tuple[ProtectedPath, ...]
    working_digest: str
    head_digest: str

    @property
    def encoded_paths(self) -> tuple[str, ...]:
        """The paths as ``record.json`` stores them."""
        return tuple(path.encode() for path in self.paths)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_protected_inputs(roots: ProtectedInputRoots) -> tuple[ProtectedPath, ...]:
    """Every candidate a consumer could read for the protected set (module docstring)."""
    worktree, project, main = RootKind.WORKTREE, RootKind.PROJECT, RootKind.MAIN
    config_dirs = [
        (project, ""),
        (worktree, PROJECT_DIR_NAME),
        (worktree, LEGACY_PROJECT_DIR),
        (main, PROJECT_DIR_NAME),
        (main, LEGACY_PROJECT_DIR),
        _checkout_project_dir(roots),
        (worktree, ""),  # the checkout root's own booley.toml
    ]
    paths = [
        ProtectedPath(kind, _join(directory, name))
        for kind, directory in config_dirs
        for name in CONFIG_FILE_NAMES
    ]
    checkout_kind, checkout_dir = _checkout_project_dir(roots)
    paths += [
        ProtectedPath(project, "hooks"),
        ProtectedPath(worktree, f"{PROJECT_DIR_NAME}/hooks"),
        # Preparation reads the hooks of the directory Flow readers resolve,
        # which a `[project].dir` override moves away from the fixed path.
        ProtectedPath(checkout_kind, _join(checkout_dir, "hooks")),
        ProtectedPath(worktree, f"{PROJECT_DIR_NAME}/.managed"),
        ProtectedPath(main, f"{PROJECT_DIR_NAME}/.managed"),
        ProtectedPath(project, ".managed"),
        ProtectedPath(project, "generators"),
        ProtectedPath(project, "mcp_tools"),
        ProtectedPath(main, f"{PROJECT_DIR_NAME}/mcp_tools"),
        ProtectedPath(worktree, "FUSESOC_IGNORE"),
    ]
    return tuple(dict.fromkeys(paths))


def _checkout_project_dir(roots: ProtectedInputRoots) -> tuple[RootKind, str]:
    """The directory Flow readers resolve for the worktree, relative to its root."""
    try:
        resolved = resolve_checkout_project_dir(roots.worktree)
    except FileNotFoundError:
        return RootKind.WORKTREE, PROJECT_DIR_NAME  # nothing resolves: the fixed path
    main = roots.main_checkout()
    bases = [(RootKind.WORKTREE, roots.worktree), (RootKind.PROJECT, roots.project_dir)]
    if main is not None:
        bases.append((RootKind.MAIN, main))
    for kind, base in bases:
        for spelling in (base, base.resolve()):
            try:
                relative = resolved.relative_to(spelling).as_posix()
            except ValueError:
                continue
            return kind, "" if relative == "." else relative
    return RootKind.ABSOLUTE, resolved.as_posix()


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


# ---------------------------------------------------------------------------
# Digests and violations
# ---------------------------------------------------------------------------


def snapshot_protected_inputs(roots: ProtectedInputRoots) -> ProtectedSnapshot:
    """Resolve the protected set and digest both views as a run would read it now."""
    paths = resolve_protected_inputs(roots)
    return ProtectedSnapshot(
        paths,
        digest_protected_inputs(paths, roots, head=False),
        digest_protected_inputs(paths, roots, head=True),
    )


def digest_protected_inputs(
    paths: Sequence[ProtectedPath], roots: ProtectedInputRoots, *, head: bool
) -> str:
    """One ``sha256:`` digest over *paths*: the working view, or with *head* the HEAD view."""
    main = roots.main_checkout()
    head_view = _HeadView() if head else None
    entries: list[Entry] = []
    for path in paths:
        location = roots.absolute(path, main)
        tree = {} if location is None else _disk_tree(location)
        if head_view is not None and path.root is RootKind.WORKTREE and location is not None:
            tree = head_view.committed_tree(location, tree, frozenset())
        entries.append({"path": path.encode(), "tree": tree})
    payload = json.dumps(
        {"format": _DIGEST_FORMAT, "head": head, "inputs": entries},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def protected_input_violations(
    recorded_paths: Sequence[str],
    working_digest: str,
    head_digest: str | None,
    roots: ProtectedInputRoots,
) -> list[str]:
    """Why the protected inputs no longer match the entry snapshot; empty when they do.

    Checks, in order, reporting the first that fails: the protected path list
    is unchanged; the working view equals the working view at entry; the HEAD
    view equals the HEAD view at entry.
    """
    recorded = tuple(ProtectedPath.decode(text) for text in recorded_paths)
    current = resolve_protected_inputs(roots)
    if current != recorded:
        changed = sorted({p.encode() for p in current} ^ {p.encode() for p in recorded})
        return [f"protected inputs now resolve to different files: {', '.join(changed)}"]
    if digest_protected_inputs(current, roots, head=False) != working_digest:
        return ["a protected input differs from its state at entry"]
    if (
        head_digest is not None
        and digest_protected_inputs(current, roots, head=True) != head_digest
    ):
        return ["a protected input differs from its state at entry in HEAD"]
    return []


# ---------------------------------------------------------------------------
# Working view
# ---------------------------------------------------------------------------


def _disk_tree(path: Path) -> Tree:
    """The entries of *path*, keyed relative to it: ``.`` is the path itself.

    An absent path has no entries; a file or symlink is one leaf; a directory
    is ``.`` plus every file and symlink below it.
    """
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_dir():
        return {".": _leaf(path, frozenset())}
    return {".": {"kind": "directory"}, **dict(_walk(path, frozenset()))}


def _walk(directory: Path, seen: frozenset[str]) -> Iterator[tuple[str, Entry]]:
    """Every file and symlink under *directory*, keyed by its POSIX path relative to it."""
    for current, subdirs, files in os.walk(directory, followlinks=False, onerror=_unreadable):
        subdirs[:] = sorted(name for name in subdirs if name not in _SKIPPED_DIRECTORIES)
        base = Path(current)
        for name in subdirs:
            entry = base / name
            if entry.is_symlink():  # os.walk lists a directory symlink as a subdirectory
                yield entry.relative_to(directory).as_posix(), _leaf(entry, seen)
        for name in sorted(files):
            entry = base / name
            yield entry.relative_to(directory).as_posix(), _leaf(entry, seen)


def _unreadable(error: OSError) -> None:
    """Fail the snapshot: a directory that cannot be listed is not an empty one."""
    raise ProtectedInputError(
        f"cannot list protected input {error.filename}: {error.strerror or error}"
    ) from error


def _leaf(path: Path, seen: frozenset[str]) -> Entry:
    """A symlink by its target text and resolution; a regular file by its content."""
    try:
        if path.is_symlink():
            target = str(path.readlink())
            return {"kind": "symlink", "target": target, "resolves": _follow(path, seen)}
        if not path.is_file():
            return {"kind": "special"}
        return {"kind": "file", "sha256": _content_hash(path.read_bytes())}
    except OSError as exc:
        raise ProtectedInputError(f"cannot read protected input {path}: {exc}") from exc


def _follow(link: Path, seen: frozenset[str]) -> Entry:
    """What *link* (a symlink, or the path its target text names) resolves to.

    A file's content, a directory's tree, or why it resolves to nothing.
    """
    try:
        target = link.resolve(strict=True)
    except FileNotFoundError:
        return {"kind": "dangling"}
    except (OSError, RuntimeError):  # a symlink loop
        return {"kind": "cycle"}
    key = str(target)
    if key in seen:
        return {"kind": "cycle"}
    if target.is_dir():
        return {"kind": "directory", "tree": dict(_walk(target, seen | {key}))}
    if target.is_file():
        return {"kind": "file", "sha256": _content_hash(target.read_bytes())}
    return {"kind": "special"}


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# HEAD view
# ---------------------------------------------------------------------------


class _HeadView:
    """Committed content of protected paths inside the Goal worktree (module docstring).

    Each path is read from the HEAD of the innermost repository containing
    it: the Goal worktree's, or a paired Project checkout nested inside it.
    """

    def committed_tree(self, location: Path, disk: Tree, seen: frozenset[str]) -> Tree:
        """*disk* with each entry marked tracked or not, tracked ones as HEAD holds them."""
        tracked = self._tracked(location, seen)
        tree: Tree = {
            key: {**value, "tracked": False}
            for key, value in disk.items()
            if key not in tracked and value.get("kind") != "directory"
        }
        tree.update(tracked)
        if any(key != "." for key in tree):
            # A directory, in HEAD or on disk: its membership lives in its entries.
            tree["."] = {"kind": "directory"}
        return tree

    def _tracked(self, location: Path, seen: frozenset[str]) -> Tree:
        found = _repository_of(location)
        if found is None:
            return {}  # outside every repository: nothing is committed
        checkout, relative = found
        try:
            rows = checkout.head_tree(relative)
        except CheckoutError as exc:
            raise ProtectedInputError(f"cannot list HEAD for {location}: {exc}") from exc
        entries: Tree = {}
        for row in rows:
            key = "." if row.path == relative else row.path.removeprefix(f"{relative}/")
            if any(part in _SKIPPED_DIRECTORIES for part in key.split("/")):
                continue
            entries[key] = self._entry(checkout, row.mode, row.object_id, location / key, seen)
        return entries

    def _entry(
        self,
        checkout: GoalCheckout,
        mode: str,
        object_id: str,
        location: Path,
        seen: frozenset[str],
    ) -> Entry:
        if mode == "160000":
            return {"kind": "submodule", "commit": object_id, "tracked": True}
        try:
            content = checkout.blob(object_id)
        except CheckoutError as exc:
            raise ProtectedInputError(f"cannot read HEAD object {object_id}: {exc}") from exc
        if mode == "120000":
            target = content.decode("utf-8", "surrogateescape")
            resolves = self._follow_committed(location.parent / target, seen)
            return {"kind": "symlink", "target": target, "resolves": resolves, "tracked": True}
        return {"kind": "file", "sha256": _content_hash(content), "tracked": True}

    def _follow_committed(self, target: Path, seen: frozenset[str]) -> Entry:
        """What a committed symlink target holds in the HEAD view.

        The target is resolved as a checkout of HEAD would resolve it, and its
        content is read through the HEAD view too: a committed change behind
        the link shows even when the working bytes were restored.
        """
        try:
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError):  # a symlink loop
            return {"kind": "cycle"}
        key = str(resolved)
        if key in seen:
            return {"kind": "cycle"}
        tree = self.committed_tree(resolved, _disk_tree(resolved), seen | {key})
        return {"kind": "head-view", "tree": tree} if tree else {"kind": "dangling"}


def _repository_of(location: Path) -> tuple[GoalCheckout, str] | None:
    """The innermost repository containing *location* and its POSIX path inside it."""
    directory = location if location.is_dir() and not location.is_symlink() else location.parent
    below: list[str] = [] if directory == location else [location.name]
    while not directory.is_dir():
        if directory.parent == directory:
            return None
        below.insert(0, directory.name)
        directory = directory.parent
    try:
        found = GoalCheckout(directory).containing_repository()
    except CheckoutError as exc:
        raise ProtectedInputError(f"cannot find the repository of {location}: {exc}") from exc
    if found is None:
        return None
    toplevel, prefix = found
    relative = "/".join(part for part in (*prefix.split("/"), *below) if part)
    return GoalCheckout(toplevel), relative or "."
