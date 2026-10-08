"""Durably identify operation-owned inodes before acquiring visible publication names."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from booley.runtime.atomic_files import fsync_directory
from booley.runtime.history_commit import FileCommitError


@dataclass(frozen=True)
class PublicationOwnership:
    """Caller seals logical operation ownership; names and inodes are never inferred."""

    directory: Path
    write: Callable[[str, dict[str, Any]], None]
    read: Callable[[str], dict[str, Any]]
    checkpoint: Callable[[str], None]

    def inode(self, name: str, parent: Path, content: bytes | None = None) -> Path:
        authority = self.directory / (name + ".authority.json")
        saved: dict[str, Any]
        if authority.exists():
            saved = self.read(name)
        else:
            path = parent / (".booley-owned-" + str(uuid4()))
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644 if content is not None else 0o600
            )
            try:
                if content is not None:
                    os.fchmod(descriptor, 0o644) if hasattr(os, "fchmod") else path.chmod(0o644)
                    with os.fdopen(os.dup(descriptor), "wb") as stream:
                        stream.write(content)
                        stream.flush()
                os.fsync(descriptor)
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            fsync_directory(parent)
            saved = {
                "path": str(path),
                "identity": [metadata.st_dev, metadata.st_ino],
                "parent_identity": [parent.stat().st_dev, parent.stat().st_ino],
            }
            self.write(name, saved)
            self.checkpoint(name + "-owned")
        if set(saved) != {"path", "identity", "parent_identity"}:
            raise FileCommitError("invalid operation-owned inode authority")
        path = parent / Path(saved["path"]).name
        if (
            [parent.stat().st_dev, parent.stat().st_ino] != saved["parent_identity"]
            or not path.name.startswith(".booley-owned-")
            or path.is_symlink()
        ):
            raise FileCommitError("operation-owned inode path was substituted")
        metadata = path.stat()
        if [metadata.st_dev, metadata.st_ino] != saved["identity"] or not path.is_file():
            raise FileCommitError("operation-owned inode identity was substituted")
        if content is not None and path.read_bytes() != content:
            raise FileCommitError("operation-owned staging bytes were substituted")
        return path

    def _summary_anchor(self, destination: Path, *, create: bool = True) -> Path:
        # Equal devices do not prove equal mounts: bind aliases can reject links.
        # Always stage through the destination's local managed Project hierarchy.
        root = destination.parents[3]
        parent = destination.parent.parent / (".publication-" + self.directory.name)
        _require_anchor(parent, root)
        relative = (parent / "staging").relative_to(root).as_posix()
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", relative],
            cwd=root,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise FileCommitError(
                "publication needs the managed goals/*/ transient ignore policy; commit the Project ignore policy before finish"
            )
        if create:
            parent.mkdir(parents=True, exist_ok=True)
        _require_anchor(parent, root)
        return parent

    def validate_summary_anchor(self, destination: Path) -> None:
        """Check containment and managed ignore policy before the finishing fence."""
        self._summary_anchor(destination, create=False)

    def materialize(self, destination: Path, content: bytes) -> None:
        """The staging inode lives in the destination's ignored managed hierarchy."""
        self.directory.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = self.inode("summary-staging", self._summary_anchor(destination), content)
        try:
            os.link(stage, destination)
        except FileExistsError:
            if (
                destination.is_symlink()
                or not destination.is_file()
                or destination.read_bytes() != content
            ):
                raise FileCommitError(
                    "publication destination differs from owned staging"
                ) from None
        fsync_directory(destination.parent)
        self.checkpoint("summary-linked")


def _require_anchor(path: Path, root: Path) -> None:
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise FileCommitError("publication staging anchor escapes its literal worktree")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if (
            ancestor.is_symlink()
            or getattr(ancestor, "is_junction", lambda: False)()
            or (ancestor.exists() and not ancestor.is_dir())
        ):
            raise FileCommitError(
                "publication staging anchor is linked/non-directory; foreign anchor retained"
            )
