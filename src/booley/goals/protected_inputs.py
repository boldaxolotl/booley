"""Protected inputs: the files that decide how evidence is produced (ADR 0067 D7).

At entry the Goal Record stores one ``protected_digest`` over the protected set
and the absolute paths it covered (``protected_paths``). Later calls compare
three things against that snapshot (:func:`protected_input_violations`): the
working files, HEAD for the inputs tracked in the Goal worktree, and the path
list itself (a resolver that now picks a different file is a violation).

Resolver contract
=================

A Booley run with an explicit ``work_dir`` (a linked worktree carrying a copied
``.booley_project/`` snapshot) does not read every protected input through one
resolver. The digest covers the file each consumer actually reads, so one
protected name can resolve to more than one path. Below, *control* is the
Project directory the caller resolved for the session
(:func:`booley.runtime.project_dir.resolve_project_dir`; inside a Sandbox the
shared ``BOOLEY_PROJECT_DIR``), *snapshot* is
:func:`~booley.runtime.project_dir.resolve_checkout_project_dir` of the
worktree (its copied ``.booley_project/``), and *root* is the worktree root.

=============================== =============================================== ==============================
Protected input                 Consumer and the resolver it uses               Paths digested
=============================== =============================================== ==============================
``.booley_project/booley.toml`` Session-global readers (``project_config``      ``control/booley.toml``
                                lazy globals, EDA requests, endpoint config,
                                Specialist ``[agent]``/``[models]``) use
                                control. Flow readers given ``work_dir``
                                (Flow enablement, Flow options, project name,   ``snapshot/booley.toml`` and
                                stealth projection, compiler cache) use the     ``root/.booley_project/
                                snapshot or the fixed                           booley.toml``; when both are
                                ``root/.booley_project/booley.toml``, falling   missing, the main checkout's
                                back to the main checkout's copy when it is     ``.booley_project/booley.toml``
                                missing.
``booley.toml`` (checkout root) ``[project].dir`` routing walks up from the     ``root/booley.toml``
                                worktree root (D7: the checkout root).
``hooks/``                      No Booley Flow or Specialist reads it; Runner   ``control/hooks/``
                                hooks read control, worktree preparation reads
                                the snapshot. Digested where it is authored.
``.managed/``                   The Git hook adapters run                       ``root/.booley_project/
                                ``<root>/.booley_project/.managed`` first,      .managed/`` if present, else
                                then the main checkout's copy.                  ``control/.managed/``
``generators/``                 No Booley reader exists; FuseSoC generators     ``control/generators/``
                                are declared per ``.core``. Digested where it
                                is authored.
``mcp_tools/``                  Custom MCP tools are discovered once, at MCP    ``control/mcp_tools/``
                                server start, from ``BOOLEY_PROJECT_DIR`` (the
                                caller passes the same directory as control);
                                the snapshot copy is never run.
``FUSESOC_IGNORE``              Core discovery scans the ``work_dir`` root      ``root/FUSESOC_IGNORE``
                                (D7: the checkout root).
=============================== =============================================== ==============================

Rules shared by every entry:

- A missing path is digested as absent, so creating it later is a change.
  Nothing here creates a file or directory.
- A directory is digested recursively: each regular file by content, each
  symlink by its target text (not followed, so a link target outside the
  protected set is not protected), and each subdirectory by name, so adding
  or deleting a file or an empty directory is a change. ``__pycache__``
  directories are skipped: Python rewrites them when it imports a custom MCP
  tool, which is not a change to the input.
- Paths are stored as given by the resolvers, made absolute without resolving
  symlinks. A later path list counts as the same when each path is the same
  string or names the same existing file (bind-mount aliases of the shared
  Project directory are one directory).
- The HEAD view replaces the content of every path tracked in the worktree's
  HEAD with the committed content, so a committed edit is a violation even
  after the working copy is reverted. An untracked input has no HEAD and is
  compared with the entry digest only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.core.project_dir import PROJECT_DIR_NAME, resolve_checkout_project_dir
from booley.goals.checkout import GIT_TIMEOUT_S, GoalCheckout

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
_SKIPPED_DIRECTORIES = frozenset({"__pycache__"})
_DIGEST_FORMAT = "booley-protected-inputs/1"


class ProtectedInputError(RuntimeError):
    """A protected input could not be read to digest it."""


@dataclass(frozen=True)
class ProtectedInputRoots:
    """Where the resolvers look: the Goal worktree and the session's Project directory."""

    worktree: Path
    project_dir: Path


def resolve_protected_inputs(roots: ProtectedInputRoots) -> tuple[Path, ...]:
    """The absolute paths a run in *roots.worktree* reads for the protected set.

    Follows the resolver contract in the module docstring; duplicates (one
    file reached through two resolvers under the same spelling) appear once.
    The order is fixed, so equal inputs give equal lists.
    """
    root = _absolute(roots.worktree)
    control = _absolute(roots.project_dir)
    fixed_snapshot = root / PROJECT_DIR_NAME
    try:
        snapshot = _absolute(resolve_checkout_project_dir(root))
    except FileNotFoundError:
        # No snapshot and no session Project directory: Flow readers find nothing
        # beyond the fixed path, which is digested as absent.
        snapshot = fixed_snapshot
    paths = [control / "booley.toml", snapshot / "booley.toml", fixed_snapshot / "booley.toml"]
    if not (snapshot / "booley.toml").exists() and not (fixed_snapshot / "booley.toml").exists():
        main_checkout = _main_checkout(root)
        if main_checkout is not None:
            paths.append(main_checkout / PROJECT_DIR_NAME / "booley.toml")
    paths.append(root / "booley.toml")
    paths.append(control / "hooks")
    managed = fixed_snapshot / ".managed"
    paths.append(managed if managed.exists() else control / ".managed")
    paths.append(control / "generators")
    paths.append(control / "mcp_tools")
    paths.append(root / "FUSESOC_IGNORE")
    return tuple(dict.fromkeys(paths))


def digest_protected_inputs(paths: Sequence[Path], *, head_of: Path | None = None) -> str:
    """One SHA-256 digest over *paths* in the documented encoding.

    With *head_of* (a worktree root), every path tracked in that worktree's
    HEAD contributes its committed content instead of the working copy.
    """
    head = _HeadView(GoalCheckout(head_of)) if head_of is not None else None
    entries = [_path_entry(path, head) for path in paths]
    payload = json.dumps(
        {"format": _DIGEST_FORMAT, "inputs": entries}, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProtectedSnapshot:
    """The entry snapshot: resolved paths and the digest over them."""

    paths: tuple[Path, ...]
    digest: str


def snapshot_protected_inputs(roots: ProtectedInputRoots) -> ProtectedSnapshot:
    """Resolve and digest the protected set as a run would read it now."""
    paths = resolve_protected_inputs(roots)
    return ProtectedSnapshot(paths, digest_protected_inputs(paths))


def protected_input_violations(
    recorded_paths: Sequence[str], recorded_digest: str, roots: ProtectedInputRoots
) -> list[str]:
    """Why the protected inputs no longer match the entry snapshot; empty when they do.

    Checks, in order, reporting the first that fails: the resolvers still pick
    the recorded paths; the working files digest to the recorded digest; HEAD
    (for tracked inputs) digests to it.
    """
    current = resolve_protected_inputs(roots)
    if not _same_paths(current, [Path(path) for path in recorded_paths]):
        return [
            "protected inputs now resolve to different files: "
            f"{[str(path) for path in current]} instead of {list(recorded_paths)}"
        ]
    recorded = tuple(Path(path) for path in recorded_paths)
    if digest_protected_inputs(recorded) != recorded_digest:
        return ["a protected input differs from its state at entry"]
    # The working files match; HEAD can still hold a committed edit that was
    # reverted only in the working copy, and Finish would publish HEAD.
    if digest_protected_inputs(recorded, head_of=roots.worktree) != recorded_digest:
        return ["a protected input differs from its state at entry in HEAD"]
    return []


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _path_entry(path: Path, head: _HeadView | None) -> dict[str, object]:
    """The typed record of one protected path: its tree of entries, keyed relative to it.

    The path itself is ``.``: absent paths have an empty tree, a file or
    symlink is one leaf, and a directory is ``.`` plus every file and symlink
    below it. Directories below the root are not entries of their own, so an
    empty subdirectory never counts (Git cannot track one either).
    """
    tree = _disk_tree(path)
    if head is not None:
        tree = head.committed_tree(path, tree)
    return {"path": str(path), "tree": tree}


def _disk_tree(path: Path) -> dict[str, dict[str, object]]:
    """The working-copy entries of *path* (see :func:`_path_entry`)."""
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_dir():
        return {".": _leaf(path)}
    return {".": {"kind": "directory"}, **dict(_walk(path))}


def _walk(directory: Path) -> Iterator[tuple[str, dict[str, object]]]:
    """Every file and symlink under *directory*, keyed by its POSIX path relative to it."""
    for current, subdirs, files in os.walk(directory, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if name not in _SKIPPED_DIRECTORIES)
        base = Path(current)
        for name in subdirs:
            entry = base / name
            if entry.is_symlink():  # os.walk lists a directory symlink as a subdirectory
                yield entry.relative_to(directory).as_posix(), _leaf(entry)
        for name in sorted(files):
            entry = base / name
            yield entry.relative_to(directory).as_posix(), _leaf(entry)


def _leaf(path: Path) -> dict[str, object]:
    """A symlink by its target, a regular file by its content hash."""
    try:
        if path.is_symlink():
            return {"kind": "symlink", "target": str(path.readlink())}
        if not path.is_file():
            return {"kind": "special"}
        return {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except OSError as exc:
        raise ProtectedInputError(f"cannot read protected input {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# HEAD view
# ---------------------------------------------------------------------------


class _HeadView:
    """Committed content of protected paths inside one worktree.

    The committed tree of a path is HEAD's entry for every path HEAD tracks,
    and the working entry for every path it does not: an untracked input has
    no committed state, so it is compared with the entry digest only (the
    module docstring). Paths outside the worktree have no HEAD at all.
    """

    def __init__(self, checkout: GoalCheckout) -> None:
        self._root = _absolute(checkout.root)

    def committed_tree(
        self, path: Path, disk: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        """*disk* with every HEAD-tracked entry replaced by its committed state."""
        try:
            relative = _absolute(path).relative_to(self._root).as_posix()
        except ValueError:
            return disk  # outside the worktree: no HEAD
        tracked = self._tracked(relative)
        tree = {**disk, **tracked}
        if "." not in tree and tracked:
            tree["."] = {"kind": "directory"}  # deleted from disk, still in HEAD
        return tree

    def _tracked(self, relative: str) -> dict[str, dict[str, object]]:
        entries: dict[str, dict[str, object]] = {}
        for mode, object_id, name in self._ls_tree(relative):
            key = "." if name == relative else name.removeprefix(f"{relative}/")
            if any(part in _SKIPPED_DIRECTORIES for part in key.split("/")):
                continue
            entries[key] = self._entry(mode, object_id)
        return entries

    def _ls_tree(self, relative: str) -> list[tuple[str, str, str]]:
        result = self._git(["ls-tree", "-r", "-z", "HEAD", "--", relative], b"")
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise ProtectedInputError(f"cannot list HEAD for {relative}: {detail}")
        rows = []
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, _kind, object_id = meta.decode("ascii").split(" ")
            rows.append((mode, object_id, name.decode("utf-8", "surrogateescape")))
        return rows

    def _entry(self, mode: str, object_id: str) -> dict[str, object]:
        if mode == "160000":
            return {"kind": "submodule", "commit": object_id}
        result = self._git(["cat-file", "blob", object_id], b"")
        if result.returncode != 0:
            raise ProtectedInputError(f"cannot read HEAD object {object_id}")
        if mode == "120000":
            return {"kind": "symlink", "target": result.stdout.decode("utf-8", "surrogateescape")}
        return {"kind": "file", "sha256": hashlib.sha256(result.stdout).hexdigest()}

    def _git(self, args: list[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self._root,
                input=stdin,
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtectedInputError(
                f"git {' '.join(args)} failed in {self._root}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _absolute(path: Path) -> Path:
    """*path* made absolute without resolving symlinks or mount aliases."""
    return path.absolute()


def _same_paths(current: Sequence[Path], recorded: Sequence[Path]) -> bool:
    """Equal lists, where two spellings of one existing file count as equal."""
    if len(current) != len(recorded):
        return False
    return all(_same_path(left, right) for left, right in zip(current, recorded, strict=True))


def _same_path(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _main_checkout(worktree: Path) -> Path | None:
    """The primary checkout of *worktree*'s repository, or ``None`` when unknown."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if common.returncode != 0 or not common.stdout.strip():
        return None
    common_dir = Path(common.stdout.strip())
    return common_dir.parent if common_dir.name == ".git" else None
