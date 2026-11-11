"""Worktree identity: a Git worktree named the same from every path spelling (ADR 0067 D3).

MCP session attribution and Goal Mode both key off it. It depends only on Git's
own directories and one UUID file Booley stores in the common directory.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import BoundaryError, require_uuid4
from booley.runtime.atomic_files import WriteOnceConflictError, atomic_write_once
from booley.runtime.filesystem_utils import read_file_bytes
from booley.runtime.project_repositories import GitDirectoryInspectionError, git_directories

# The repository identity file inside Git's common directory (D3).
REPOSITORY_ID_FILE = Path("booley") / "repository-id"
MAIN_CHECKOUT = "main"
# A checkout name: ``main`` or ``worktrees/<admin name>``.
CHECKOUT_PATTERN = re.compile(r"main|worktrees/[^/\x00]+")


class WorktreeIdentityError(RuntimeError):
    """A path is not a Git checkout Booley can identify."""


@dataclass(frozen=True)
class WorktreeIdentity:
    """A worktree as Git knows it, the same from every path spelling (D3).

    ``repository`` identifies the Git common directory: a UUID Booley stores
    inside it, so two mount spellings of one repository agree. ``checkout`` is
    ``main`` for the primary checkout and ``worktrees/<admin name>`` for a
    linked worktree, the Git directory relative to the common directory.
    """

    repository: str
    checkout: str

    @property
    def key(self) -> str:
        """One string naming this worktree, used to name its lock."""
        return f"{self.repository}/{self.checkout}"

    def to_json(self) -> dict[str, str]:
        """The JSON form stored in ``record.json``."""
        return {"repository": self.repository, "checkout": self.checkout}


def resolve_worktree_identity(work_dir: Path, *, create: bool = False) -> WorktreeIdentity | None:
    """Identify the Git worktree containing *work_dir*, the same from every spelling.

    The repository half is a UUID stored in Git's common directory. Without
    *create*, a repository that has none yet returns ``None``. With *create*,
    the first caller writes it atomically and concurrent callers all read the
    winner's value.
    """
    try:
        directories = git_directories(work_dir)
    except GitDirectoryInspectionError as exc:
        raise WorktreeIdentityError(str(exc)) from exc
    checkout = _checkout_name(directories.git_dir, directories.common_dir)
    repository = _repository_id(directories.common_dir, create=create)
    return None if repository is None else WorktreeIdentity(repository, checkout)


def _checkout_name(git_dir: Path, common_dir: Path) -> str:
    """``main`` for the primary checkout, ``worktrees/<admin>`` for a linked one."""
    try:
        if git_dir.samefile(common_dir):
            return "main"
        candidates = common_dir / "worktrees"
        if candidates.is_dir():
            names = [
                child.name
                for child in candidates.iterdir()
                if child.is_dir() and child.samefile(git_dir)
            ]
            if len(names) == 1:
                return "worktrees/" + names[0]
    except OSError as exc:
        raise WorktreeIdentityError(f"cannot inspect Git directory identity: {exc}") from exc
    raise WorktreeIdentityError(f"Git directory {git_dir} is not a worktree of {common_dir}")


def _repository_id(common_dir: Path, *, create: bool) -> str | None:
    path = common_dir / REPOSITORY_ID_FILE
    if not path.exists():
        if not create:
            return None
        # A concurrent caller may win the creation; everyone reads the winner's id.
        with contextlib.suppress(WriteOnceConflictError):
            atomic_write_once(path, f"{uuid.uuid4()}\n".encode(), mode=0o644)
    try:
        return require_uuid4(read_file_bytes(path).decode("utf-8").strip(), field=str(path))
    except (OSError, UnicodeDecodeError, BoundaryError) as exc:
        raise WorktreeIdentityError(f"unreadable repository identity {path}: {exc}") from exc
