"""Repair Git worktree registrations whose pointers name a former Sandbox mount point.

A Sandbox mount point change leaves the pointers Git wrote under the old mount
(a checkout's ``.git`` file, a registration's ``gitdir``, ``core.worktree`` and
``core.hooksPath``) naming paths that no longer exist. ``repair_mount_registrations``
rebases each such pointer onto the current mount, keeping its absolute or
relative spelling, and drops only the registration of a disposable baseline
scratch checkout whose directory is gone. Everything else it cannot repair is
reported and left as found, because Git's own prune would delete it.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.core import sandbox_layout
from booley.runtime import mount_history, platform_paths
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.directory_migration_git import pointer_bytes
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_repositories import common_git_dir, resolve_inner_project_repo
from booley.runtime.worktree_paths import worktree_state_dir

_BASELINE_NAME = re.compile(r"\.baseline-wt-\d+-[0-9a-f]+")
_GITFILE_PREFIX = "gitdir:"
_CONFIG_KEYS = (("core.worktree", "admin"), ("core.hooksPath", "checkout"))
_GIT_TIMEOUT_S = 30


@dataclass(frozen=True)
class RegistrationProblem:
    """A registration naming a former mount that could not be repaired; it is left as found."""

    registration: Path
    pointer: Path
    reason: str

    def message(self) -> str:
        return (
            f"worktree registration {self.registration} ({self.pointer}): {self.reason}; "
            "restore the checkout, or remove the registration with "
            f"`git worktree remove --force --force <registered path>` once its work is safe"
        )


@dataclass(frozen=True)
class MountRepair:
    """What one repair run changed (administrative directories) and what it could not."""

    repaired: tuple[Path, ...] = ()
    pruned: tuple[Path, ...] = ()
    problems: tuple[RegistrationProblem, ...] = ()


def _candidates(raw: str, base: Path, former: tuple[str, ...]) -> list[Path]:
    """The meanings of pointer *raw* read from directory *base*, as Git reads it first."""
    found: list[Path] = []

    def add(path: Path | None) -> None:
        if path is not None and path not in found:
            found.append(path)

    pointer = Path(raw)
    first = pointer if pointer.is_absolute() else Path(os.path.normpath(base.resolve() / pointer))
    add(first)
    for mount in former:
        add(mount_history.rebase(first, (mount,)))
    if pointer.is_absolute():
        return found
    # A relative pointer written from a base under a former mount of another depth
    # is broken from the same checkout under the current mount, though its target
    # never moved.
    try:
        inside = base.resolve().relative_to(Path(sandbox_layout.BOOLEY_DIR_TARGET).resolve())
    except ValueError:
        return found
    for mount in former:
        guess = Path(os.path.normpath(Path(mount) / inside / pointer))
        if guess.exists():
            add(guess)
        for older in former:
            add(mount_history.rebase(guess, (older,)))
    return found


def _spelling(target: Path, base: Path, *, relative: bool) -> str:
    return os.path.relpath(target.resolve(), base.resolve()) if relative else str(target)


def _first_existing(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.exists()), None)


def _read_pointer(path: Path, prefix: str = "") -> str:
    text = path.read_text(encoding="utf-8").strip()
    if prefix:
        if not text.startswith(prefix):
            raise ValueError(f"not a gitfile: {text!r}")
        text = text[len(prefix) :].strip()
    return text


def _rewrite(path: Path, content: bytes, *, keep_time: bool) -> None:
    status = path.stat()
    atomic_replace_bytes(
        path,
        content,
        mode=stat.S_IMODE(status.st_mode),
        mtime_ns=status.st_mtime_ns if keep_time else None,
    )


def _git_config(config: Path, *args: str) -> str | None:
    result = platform_paths.run_captured_bounded(
        ["git", "config", "--file", str(config), *args], timeout=_GIT_TIMEOUT_S, errors="strict"
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise OSError(f"git config in {config}: {result.stderr.strip()}")
    return result.stdout.strip()


def _config_fixes(admin: Path, checkout: Path, former: tuple[str, ...]) -> list[tuple[str, str]]:
    """The ``(key, value)`` pairs of ``config.worktree`` that name a former mount."""
    config = admin / "config.worktree"
    fixes: list[tuple[str, str]] = []
    if not config.is_file():
        return fixes
    for key, owner in _CONFIG_KEYS:
        raw = _git_config(config, "--get", key)
        if not raw:
            continue
        base = admin if owner == "admin" else checkout
        candidates = _candidates(raw, base, former)
        # The hooks directory may not exist yet: a lone former-mount meaning is unambiguous.
        chosen = _first_existing(candidates)
        if chosen is None and len(candidates) == 2:
            chosen = candidates[1]
        if chosen is not None and chosen != candidates[0]:
            fixes.append((key, _spelling(chosen, base, relative=not Path(raw).is_absolute())))
    return fixes


def _pointer_fix(
    raw: str, base: Path, candidates: list[Path], target: Path, *, prefix: str
) -> bytes | None:
    """The replacement bytes of a pointer that does not name *target* as Git reads it."""
    if candidates[0] == target:
        return None
    if Path(raw).is_absolute():
        return pointer_bytes(target, base, False, prefix=prefix)
    return pointer_bytes(target.resolve(), base.resolve(), True, prefix=prefix)


def _is_disposable(admin: Path, checkout: Path, project_root: Path) -> bool:
    """A detached, unlocked registration of a baseline scratch checkout of this Booley Directory.

    The nested Paired Checkout baseline sits one level below its scratch checkout.
    """
    try:
        head = (admin / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if head.startswith("ref:") or (admin / "locked").exists():
        return False
    state_dir = worktree_state_dir(project_root).resolve()
    return any(
        _BASELINE_NAME.fullmatch(owner.name) is not None and owner.parent.resolve() == state_dir
        for owner in (checkout, checkout.parent)
    )


def _names(path: Path, admin: Path) -> bool:
    try:
        return path.exists() and path.samefile(admin)
    except OSError:
        return False


class _Registration:
    """One administrative directory under ``<common>/worktrees``."""

    def __init__(self, admin: Path, former: tuple[str, ...], project_root: Path) -> None:
        self.admin = admin
        self.former = former
        self.project_root = project_root

    def _gitfile_of(self, marker: Path) -> tuple[str, list[Path]] | None:
        """The checkout ``.git`` file's pointer and meanings, when it names this registration."""
        try:
            raw = _read_pointer(marker, _GITFILE_PREFIX)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        candidates = _candidates(raw, marker.parent, self.former)
        named = [path for path in candidates if _names(path, self.admin)]
        return (raw, candidates) if named else None

    def repair(self) -> tuple[str, RegistrationProblem | None]:
        """Return ``current``, ``repaired`` or ``pruned``, or a problem that left it as found."""
        pointer = self.admin / "gitdir"
        try:
            raw = _read_pointer(pointer)
        except FileNotFoundError:
            return "current", None
        except (OSError, UnicodeDecodeError) as exc:
            return "current", self._problem(pointer, f"cannot read the pointer: {exc}")
        candidates = _candidates(raw, self.admin, self.former)
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            return self._absent(pointer, candidates)
        for marker in existing:
            found = self._gitfile_of(marker)
            if found is not None:
                return self._write(pointer, raw, candidates, marker, found)
        return "current", self._problem(
            existing[0], "its checkout names another registration or cannot be read"
        )

    def _problem(self, pointer: Path, reason: str) -> RegistrationProblem:
        return RegistrationProblem(self.admin, pointer, reason)

    def _absent(
        self, pointer: Path, candidates: list[Path]
    ) -> tuple[str, RegistrationProblem | None]:
        if len(candidates) == 1:
            return "current", None  # Names no former mount: Git's own prune decides.
        checkout = candidates[-1].parent
        if any(path.parent.exists() for path in candidates[1:]):
            return "current", self._problem(
                pointer, "its checkout directory exists but has lost its .git file"
            )
        if not _is_disposable(self.admin, checkout, self.project_root):
            return "current", self._problem(pointer, f"its checkout {checkout} is missing")
        safe_rmtree(self.admin)
        return "pruned", None

    def _write(
        self,
        pointer: Path,
        raw: str,
        candidates: list[Path],
        marker: Path,
        found: tuple[str, list[Path]],
    ) -> tuple[str, RegistrationProblem | None]:
        checkout = marker.parent
        gitfile_raw, gitfile_candidates = found
        target = next(path for path in gitfile_candidates if _names(path, self.admin))
        gitfile = _pointer_fix(
            gitfile_raw, checkout, gitfile_candidates, target, prefix=_GITFILE_PREFIX + " "
        )
        back = _pointer_fix(raw, self.admin, candidates, marker, prefix="")
        try:
            fixes = _config_fixes(self.admin, checkout, self.former)
            if gitfile is not None:
                _rewrite(marker, gitfile, keep_time=True)
            if back is not None:
                _rewrite(pointer, back, keep_time=False)
            for key, value in fixes:
                _git_config(self.admin / "config.worktree", key, value.replace(os.sep, "/"))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return "current", self._problem(pointer, f"cannot rewrite its pointers: {exc}")
        return ("repaired" if (gitfile or back or fixes) else "current"), None


def _registrations(repository_root: Path) -> list[Path]:
    common = common_git_dir(repository_root)
    directory = None if common is None else common / "worktrees"
    if directory is None or not directory.is_dir():
        return []
    return sorted(
        child for child in directory.iterdir() if child.is_dir() and not child.is_symlink()
    )


def repair_mount_registrations(project_root: Path) -> MountRepair:
    """Repair the registrations of the Project Root's repository and its Versioned Booley Directory.

    Raises ``MountHistoryError`` when the mount history cannot be read. Without a
    history of former mounts (the host, or a Sandbox that never changed mount) it
    runs no Git and changes nothing.
    """
    former = mount_history.former_mounts()
    if not former:
        return MountRepair()
    inner = resolve_inner_project_repo(project_root)
    repaired: list[Path] = []
    pruned: list[Path] = []
    problems: list[RegistrationProblem] = []
    seen: set[Path] = set()
    for root in (project_root, inner):
        if root is None:
            continue
        for admin in _registrations(root):
            if admin in seen:
                continue
            seen.add(admin)
            status, problem = _Registration(admin, former, project_root).repair()
            if problem is not None:
                problems.append(problem)
            elif status == "repaired":
                repaired.append(admin)
            elif status == "pruned":
                pruned.append(admin)
    return MountRepair(tuple(repaired), tuple(pruned), tuple(problems))
