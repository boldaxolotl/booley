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
- ``project``: the Main Booley Directory the caller resolved
  (:func:`booley.runtime.booley_dir.resolve_booley_dir`; in a Sandbox the
  shared ``BOOLEY_DIR``);
- ``main``: the primary checkout of the worktree's repository;
- ``abs``: an absolute path, only for a ``[project].dir`` override that lies
  outside the other three.

=============================== ================================================================= ========================================
Protected input                 Consumers and what they read                                      Candidates digested
=============================== ================================================================= ========================================
Project ``booley.toml``         Session-global readers (``project_config``                        ``project:booley.toml``; the
                                globals, EDA requests, endpoint config,                           ``booley.toml`` in
                                Specialist ``[agent]``/``[models]``) read the                     ``worktree:.booley/``,
                                ``work_dir`` read the Worktree Booley Directory                   ``main:.booley/``,
                                follow a ``[project].dir`` override) or the fixed                 directory
                                falling back to the main checkout's copy.                         returns
Checkout ``booley.toml``        ``[project].dir`` routing walks up from the                       ``worktree:booley.toml``
                                worktree root.
legacy ``pipeline.toml``        Every reader above falls back to it beside                        the ``pipeline.toml`` sibling of
                                a missing ``booley.toml`` (``resolve_toml``).                     every ``booley.toml`` candidate
``hooks/``                      Runner hooks read the Main Booley Directory;                      ``project:hooks``,
                                worktree preparation reads the Worktree Booley Directory          ``worktree:.booley/hooks``,
                                or the directory ``resolve_checkout_booley_dir`` returns.        ``hooks`` in the directory
                                No Flow or Specialist reads it.                                   ``resolve_checkout_booley_dir``
                                                                                                  returns
``.managed/``                   The Git hook adapters run the worktree's bundle                   ``worktree:.booley/.managed``,
                                file when it exists, else the main checkout's;                    ``main:.booley/.managed``,
                                ``init`` writes the selected checkout's Booley Directory.         ``project:.managed``
``generators/``                 No Booley reader exists; FuseSoC generators                       ``project:generators``
                                are declared per ``.core``. Digested where it
                                is authored.
``mcp_tools/``                  Discovered once, at MCP server start, from                        ``project:mcp_tools``,
                                ``BOOLEY_DIR``, else the server's                         ``main:.booley/mcp_tools``
                                working directory (the main checkout). The
                                Worktree Booley Directory copy is never run.
``FUSESOC_IGNORE``              Core discovery scans the ``work_dir`` root.                       ``worktree:FUSESOC_IGNORE``
=============================== ================================================================= ========================================

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
- HEAD is that of the innermost repository containing the path, so a Paired Checkout nested in the worktree is read from its own HEAD. A
  tracked symlink's target is read through the HEAD view as well.
- A directory that cannot be listed fails the snapshot; it is never digested
  as empty.

Project declarations
====================

``[project].protected_inputs`` adds tracked files relative to the Goal worktree.
Entry validates declarations and eligibility; later snapshots retain violations
instead of applying entry-only eligibility checks. Their versioned capture has
independent disk and HEAD membership and includes executable state. This catches
committed additions absent on disk and committed mode edits with restored working
modes. Matched files join the ordinary protected paths, deduplicating coverage by
built-ins. Digest format 3 includes the declaration capture; an omitted or empty
list keeps the existing format 2 bytes unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from booley.config.protected_inputs import protected_input_declarations
from booley.core.booley_dir import BOOLEY_DIR_NAME, resolve_checkout_booley_dir
from booley.goals.checkout import CheckoutError, GoalCheckout, TreeEntry
from booley.goals.declared_inputs import (
    DeclaredCapture,
    DeclaredFile,
    capture_violations,
    head_matches,
    validate_entry_matches,
    working_matches,
)
from booley.goals.input_identity import capture_roots, require_bindings
from booley.runtime.git import git_common_dir
from booley.runtime.git_facts import repository_facts

# The protected set as the rules text names it (ADR 0067 "Protected inputs").
PROTECTED_INPUT_NAMES: tuple[str, ...] = (
    "booley.toml",
    f"{BOOLEY_DIR_NAME}/booley.toml",
    f"{BOOLEY_DIR_NAME}/hooks/",
    f"{BOOLEY_DIR_NAME}/.managed/",
    f"{BOOLEY_DIR_NAME}/generators/",
    f"{BOOLEY_DIR_NAME}/mcp_tools/",
    "FUSESOC_IGNORE",
)
# Every name a Project configuration file may have, newest first (resolve_toml).
CONFIG_FILE_NAMES: tuple[str, ...] = ("booley.toml", "pipeline.toml")
# The legacy Booley Directory layout resolve_booley_toml still reads.
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
    """Where the resolvers look: the Goal worktree and the Main Booley Directory."""

    worktree: Path
    booley_dir: Path
    logical_paths: tuple[tuple[Path, Path], ...] = ()
    checkout_label: tuple[RootKind, str] | None = None

    @repository_facts()
    def with_input_paths(self, saved: Mapping[str, Any] | None) -> ProtectedInputRoots:
        """Physical proof preserves D7 labels and resolves them through the current mount."""
        if saved is None:
            return self
        current = capture_roots(self.worktree, self.booley_dir)
        require_bindings(saved, current)
        return replace(
            self,
            logical_paths=tuple(
                (Path(current[key]["path"]), Path(saved[key]["path"])) for key in saved
            ),
            checkout_label=_entry_checkout_label(saved),
        )

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
            physical = Path(path.relative)
            for current, original in sorted(
                self.logical_paths,
                key=lambda pair: len(pair[1].parts),
                reverse=True,
            ):
                if physical.is_relative_to(original):
                    return current / physical.relative_to(original)
            return physical
        base = {
            RootKind.WORKTREE: self.worktree,
            RootKind.PROJECT: self.booley_dir,
            RootKind.MAIN: main,
        }[path.root]
        return None if base is None else base.joinpath(*PurePosixPath(path.relative).parts)


@dataclass(frozen=True)
class ProtectedSnapshot:
    """The protected paths and both digests over them."""

    paths: tuple[ProtectedPath, ...]
    working_digest: str
    head_digest: str
    declared: DeclaredCapture | None = None

    @property
    def encoded_paths(self) -> tuple[str, ...]:
        """The paths as ``record.json`` stores them."""
        return tuple(path.encode() for path in self.paths)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_protected_inputs(roots: ProtectedInputRoots) -> tuple[ProtectedPath, ...]:
    """Every candidate a consumer could read for the protected set (module docstring)."""
    paths = _builtin_inputs(roots)
    declared = _declared_capture(roots)
    return _with_declared_paths(paths, declared, roots)


def _builtin_inputs(roots: ProtectedInputRoots) -> tuple[ProtectedPath, ...]:
    worktree, project, main = RootKind.WORKTREE, RootKind.PROJECT, RootKind.MAIN
    config_dirs = [
        (project, ""),
        (worktree, BOOLEY_DIR_NAME),
        (main, BOOLEY_DIR_NAME),
        _checkout_booley_dir(roots),
        (worktree, ""),  # the checkout root's own booley.toml
    ]
    paths = [
        ProtectedPath(kind, _join(directory, name))
        for kind, directory in config_dirs
        for name in CONFIG_FILE_NAMES
    ]
    checkout_kind, checkout_dir = _checkout_booley_dir(roots)
    paths += [
        ProtectedPath(project, "hooks"),
        ProtectedPath(worktree, f"{BOOLEY_DIR_NAME}/hooks"),
        # Preparation reads the hooks of the directory Flow readers resolve,
        # which a `[project].dir` override moves away from the fixed path.
        ProtectedPath(checkout_kind, _join(checkout_dir, "hooks")),
        ProtectedPath(worktree, f"{BOOLEY_DIR_NAME}/.managed"),
        ProtectedPath(main, f"{BOOLEY_DIR_NAME}/.managed"),
        ProtectedPath(project, ".managed"),
        ProtectedPath(project, "generators"),
        ProtectedPath(project, "mcp_tools"),
        ProtectedPath(main, f"{BOOLEY_DIR_NAME}/mcp_tools"),
        ProtectedPath(worktree, "FUSESOC_IGNORE"),
    ]
    return tuple(dict.fromkeys(paths))


def _checkout_booley_dir(roots: ProtectedInputRoots) -> tuple[RootKind, str]:
    """The directory Flow readers resolve for the worktree, relative to its root."""
    if roots.checkout_label is not None:
        return roots.checkout_label
    try:
        resolved = resolve_checkout_booley_dir(roots.worktree)
    except FileNotFoundError:
        return RootKind.WORKTREE, BOOLEY_DIR_NAME  # nothing resolves: the fixed path
    main = roots.main_checkout()
    bases = [(RootKind.WORKTREE, roots.worktree), (RootKind.PROJECT, roots.booley_dir)]
    if main is not None:
        bases.append((RootKind.MAIN, main))
    for kind, base in bases:
        for spelling in (base, base.resolve()):
            try:
                relative = resolved.relative_to(spelling).as_posix()
            except ValueError:
                continue
            return kind, "" if relative == "." else relative
    for current, original in sorted(
        roots.logical_paths, key=lambda pair: len(pair[0].parts), reverse=True
    ):
        if resolved.is_relative_to(current):
            resolved = original / resolved.relative_to(current)
            break
    return RootKind.ABSOLUTE, resolved.as_posix()


def _entry_checkout_label(saved: Mapping[str, Any]) -> tuple[RootKind, str]:
    """Keep the exact D7 role label after physical roots have been proved equal."""
    project = Path(saved["project"]["path"])
    for key, kind in (
        ("rtl", RootKind.WORKTREE),
        ("control", RootKind.PROJECT),
        ("main", RootKind.MAIN),
    ):
        if key in saved and project.is_relative_to(Path(saved[key]["path"])):
            relative = project.relative_to(Path(saved[key]["path"])).as_posix()
            return kind, "" if relative == "." else relative
    return RootKind.ABSOLUTE, project.as_posix()


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


# ---------------------------------------------------------------------------
# Digests and violations
# ---------------------------------------------------------------------------


@repository_facts()
def snapshot_protected_inputs(
    roots: ProtectedInputRoots, *, entering: bool = False
) -> ProtectedSnapshot:
    """Resolve the protected set and digest both views as a run would read it now."""
    declared = _declared_capture(roots, entering=entering)
    paths = _with_declared_paths(_builtin_inputs(roots), declared, roots)
    return ProtectedSnapshot(
        paths,
        _digest_inputs(paths, roots, head=False, declared=declared),
        _digest_inputs(paths, roots, head=True, declared=declared),
        declared,
    )


@repository_facts()
def digest_protected_inputs(
    paths: Sequence[ProtectedPath], roots: ProtectedInputRoots, *, head: bool
) -> str:
    """One ``sha256:`` digest over *paths*: the working view, or with *head* the HEAD view."""
    return _digest_inputs(paths, roots, head=head, declared=_declared_capture(roots))


def _digest_inputs(
    paths: Sequence[ProtectedPath],
    roots: ProtectedInputRoots,
    *,
    head: bool,
    declared: DeclaredCapture | None,
) -> str:
    main = roots.main_checkout()
    head_view = _HeadView() if head else None
    locations = [roots.absolute(path, main) for path in paths]
    if head_view is not None:
        head_view.prefetch(
            location
            for path, location in zip(paths, locations, strict=True)
            if path.root is RootKind.WORKTREE and location is not None
        )
    entries: list[Entry] = []
    for path, location in zip(paths, locations, strict=True):
        tree = {} if location is None else _disk_tree(location)
        if head_view is not None and path.root is RootKind.WORKTREE and location is not None:
            tree = head_view.committed_tree(location, tree, frozenset())
        entries.append({"path": path.encode(), "tree": tree})
    value: dict[str, Any] = {"format": _DIGEST_FORMAT, "head": head, "inputs": entries}
    if declared is not None:
        value["format"] = "booley-protected-inputs/3"
        value["declared"] = declared.view_json(head=head)
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@repository_facts()
def protected_input_violations(
    recorded_paths: Sequence[str],
    working_digest: str,
    head_digest: str | None,
    roots: ProtectedInputRoots,
    declared: DeclaredCapture | None = None,
) -> list[str]:
    """Why the protected inputs no longer match the entry snapshot; empty when they do.

    Checks, in order, reporting the first that fails: the protected path list
    is unchanged; the working view equals the working view at entry; the HEAD
    view equals the HEAD view at entry.
    """
    recorded = tuple(ProtectedPath.decode(text) for text in recorded_paths)
    if declared is not None:
        try:
            current_rules = _declared_rules(roots)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProtectedInputError(f"cannot inspect declared protected inputs: {exc}") from exc
        if current_rules != declared.declarations:
            return capture_violations(declared, None)
    snapshot = snapshot_protected_inputs(roots)
    if declared is not None:
        detailed = capture_violations(declared, snapshot.declared)
        if detailed:
            return detailed
    current = snapshot.paths
    if current != recorded:
        changed = sorted({p.encode() for p in current} ^ {p.encode() for p in recorded})
        return [f"protected inputs now resolve to different files: {', '.join(changed)}"]
    if snapshot.working_digest != working_digest:
        return ["a protected input differs from its state at entry"]
    if head_digest is not None and snapshot.head_digest != head_digest:
        return ["a protected input differs from its state at entry in HEAD"]
    return []


def _declared_rules(roots: ProtectedInputRoots) -> tuple[str, ...]:
    kind, relative = _checkout_booley_dir(roots)
    selected = roots.absolute(ProtectedPath(kind, relative), roots.main_checkout())
    return protected_input_declarations(roots.worktree, selected)


def _declared_capture(
    roots: ProtectedInputRoots, *, entering: bool = False
) -> DeclaredCapture | None:
    try:
        kind, relative = _checkout_booley_dir(roots)
        selected = roots.absolute(ProtectedPath(kind, relative), roots.main_checkout())
        declarations = protected_input_declarations(roots.worktree, selected)
        if not declarations:
            return None
        storage = (
            (roots.booley_dir / "goals", selected / "goals")
            if selected
            else (roots.booley_dir / "goals",)
        )
        matched = working_matches(roots.worktree, declarations, storage)
        rows = GoalCheckout(roots.worktree).head_tree(".")
        if entering:
            validate_entry_matches(roots.worktree, declarations, matched, rows)
        working = tuple(_declared_working_file(roots.worktree, path) for path in matched)
        reader = _HeadView(include_mode=True)
        head = tuple(
            _declared_head_file(roots.worktree, row, reader)
            for row in head_matches(roots.worktree, rows, declarations, storage)
        )
        return DeclaredCapture(declarations, working, head)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProtectedInputError(f"cannot inspect declared protected inputs: {exc}") from exc


def _with_declared_paths(
    paths: tuple[ProtectedPath, ...], declared: DeclaredCapture | None, roots: ProtectedInputRoots
) -> tuple[ProtectedPath, ...]:
    if declared is None:
        return paths
    main = roots.main_checkout()
    covered = tuple(roots.absolute(path, main) for path in paths)
    extra = (
        ProtectedPath(RootKind.WORKTREE, row.path)
        for row in declared.working
        if not any(
            base is not None and (roots.worktree / row.path).is_relative_to(base)
            for base in covered
        )
    )
    return tuple(dict.fromkeys((*paths, *extra)))


def _declared_working_file(root: Path, relative: str) -> DeclaredFile:
    location = root / relative
    tree = _disk_tree(location)
    redirects = {
        path.relative_to(root).as_posix(): str(path.readlink())
        for path in location.parents
        if path != root and path.is_relative_to(root) and path.is_symlink()
    }
    value = {"tree": tree, "redirects": redirects}
    executable = location.is_file() and bool(location.stat().st_mode & 0o111)
    return DeclaredFile(
        relative,
        str(tree.get(".", {}).get("kind", "absent")),
        "sha256:" + _content_hash(json.dumps(value, sort_keys=True).encode()),
        executable,
    )


def _declared_head_file(root: Path, row: TreeEntry, reader: _HeadView) -> DeclaredFile:
    entry = reader.entry(GoalCheckout(root), row.mode, row.object_id, root / row.path, frozenset())
    return DeclaredFile(
        row.path,
        str(entry["kind"]),
        "sha256:" + _content_hash(json.dumps(entry, sort_keys=True).encode()),
        row.mode == "100755",
    )


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
    it: the Goal worktree's, or a Paired Checkout nested inside it.
    """

    def __init__(self, *, include_mode: bool = False) -> None:
        self.include_mode = include_mode
        self._repositories: dict[Path, tuple[GoalCheckout, str] | None] = {}
        self._listed: dict[tuple[Path, str], list[TreeEntry]] = {}
        self._blobs: dict[tuple[Path, str], bytes] = {}

    def prefetch(self, locations: Iterable[Path]) -> None:
        """List HEAD once per repository for all *locations*, instead of once per path.

        A listing that fails is left to :meth:`_tracked`, which asks again for
        the one location and reports it by name.
        """
        wanted: dict[Path, tuple[GoalCheckout, list[str]]] = {}
        for location in locations:
            try:
                found = self._repository(location)
            except ProtectedInputError:
                continue  # reported by the path's own read
            if found is not None and not found[1].startswith(":"):
                # A leading ":" is Git pathspec magic: that path is listed on its own.
                checkout, relative = found
                wanted.setdefault(checkout.root, (checkout, []))[1].append(relative)
        for checkout, relatives in wanted.values():
            try:
                rows = checkout.head_rows(relatives)
            except CheckoutError:
                continue
            for relative in relatives:
                self._listed[checkout.root, relative] = [
                    row for row in rows if _under(row.path, relative)
                ]

    def _repository(self, location: Path) -> tuple[GoalCheckout, str] | None:
        if location not in self._repositories:
            self._repositories[location] = _repository_of(location)
        return self._repositories[location]

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
        found = self._repository(location)
        if found is None:
            return {}  # outside every repository: nothing is committed
        checkout, relative = found
        try:
            rows = self._listed.get((checkout.root, relative))
            if rows is None:
                rows = checkout.head_tree(relative)
        except CheckoutError as exc:
            raise ProtectedInputError(f"cannot list HEAD for {location}: {exc}") from exc
        entries: Tree = {}
        for row in rows:
            key = "." if row.path == relative else row.path.removeprefix(f"{relative}/")
            if any(part in _SKIPPED_DIRECTORIES for part in key.split("/")):
                continue
            entries[key] = self.entry(checkout, row.mode, row.object_id, location / key, seen)
        return entries

    def entry(
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
            if (checkout.root, object_id) not in self._blobs:
                self._blobs[checkout.root, object_id] = checkout.blob(object_id)
            content = self._blobs[checkout.root, object_id]
        except CheckoutError as exc:
            raise ProtectedInputError(f"cannot read HEAD object {object_id}: {exc}") from exc
        if mode == "120000":
            target = content.decode("utf-8", "surrogateescape")
            resolves = self._follow_committed(location.parent / target, seen)
            return {"kind": "symlink", "target": target, "resolves": resolves, "tracked": True}
        entry: Entry = {"kind": "file", "sha256": _content_hash(content), "tracked": True}
        if self.include_mode:
            entry["executable"] = mode == "100755"
        return entry

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


def _under(path: str, relative: str) -> bool:
    """Whether the HEAD entry *path* is *relative* or lies below it (``.`` is everything)."""
    return relative in (".", path) or path.startswith(relative + "/")


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
