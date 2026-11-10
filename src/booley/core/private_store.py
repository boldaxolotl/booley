"""Secure persistence for host-private Booley state."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast

from booley.core.file_lock import acquire_file_lock, release_file_lock
from booley.core.sharing_retry import open_with_sharing_retry, replace_file


@dataclass(frozen=True, slots=True)
class PrivateStore:
    """Harden one host-private directory behind a small persistence interface."""

    root: Path
    anchor: Path
    subject: str
    error_type: type[RuntimeError]

    def validate_existing_directory(self) -> bool:
        """Validate an existing store and its anchor; return whether it exists."""
        if not self.anchor.exists():
            return False
        self._validate_ancestor(self.anchor)
        if not self.root.exists() and not self.root.is_symlink():
            return False
        for directory in self._private_directories():
            self._validate_private_directory(directory)
        return True

    def ensure_directory(self) -> Path:
        """Create and validate the store's private directory chain."""
        if not self.anchor.exists():
            self.anchor.mkdir(parents=True, mode=0o700)
            self._set_mode(self.anchor, 0o700)
        self._validate_ancestor(self.anchor)
        for directory in self._private_directories():
            if directory.is_symlink():
                self._raise(f"{self.subject} directory must not be a symlink: {directory}")
            if not directory.exists():
                directory.mkdir(mode=0o700)
                self._set_mode(directory, 0o700)
            self._validate_private_directory(directory)
        return self.root

    def read_json(self, filename: str) -> object:
        """Read one owned mode-600 JSON file without following symlinks."""
        path = self.root / filename
        if path.is_symlink():
            self._raise(f"{self.subject} file must not be a symlink: {path}")
        descriptor = open_with_sharing_retry(lambda: os.open(path, self._read_flags()), path)
        try:
            self._validate_private_file(path, os.fstat(descriptor))
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @contextmanager
    def locked(
        self, filename: str, *, busy_message: str, timeout_s: float = 10.0
    ) -> Iterator[None]:
        """Hold one secure lock file, waiting only until the explicit deadline."""
        lock = self._open_lock(self.root / filename)
        acquired = False
        try:
            self._wait_for_lock(lock, timeout_s, busy_message)
            acquired = True
            yield
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    release_file_lock(lock)
            lock.close()

    def atomic_write_text(self, filename: str, content: str) -> None:
        """Atomically replace one mode-600 file and persist its directory entry."""
        path = self.root / filename
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        temp_path = Path(temporary)
        try:
            self._set_descriptor_mode(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temp_path, path)
            self._fsync_directory()
        finally:
            temp_path.unlink(missing_ok=True)

    def _private_directories(self) -> tuple[Path, ...]:
        try:
            relative = self.root.relative_to(self.anchor)
        except ValueError as exc:
            raise ValueError(f"private store {self.root} is outside anchor {self.anchor}") from exc
        current = self.anchor
        directories = []
        for part in relative.parts:
            current /= part
            directories.append(current)
        return tuple(directories)

    def _validate_ancestor(self, path: Path) -> None:
        if path.is_symlink():
            self._raise(f"{self.subject} ancestor must not be a symlink: {path}")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            self._raise(f"unsafe {self.subject} ancestor: {path}")
        if os.name == "nt":
            return
        if info.st_uid != os.getuid():
            self._raise(f"unsafe {self.subject} ancestor not owned by current user: {path}")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            self._raise_unsafe_mode("ancestor", path, mode)

    def _validate_private_directory(self, path: Path) -> None:
        if path.is_symlink():
            self._raise(f"{self.subject} directory must not be a symlink: {path}")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            self._raise(f"{self.subject} directory must be an owned directory: {path}")
        if os.name == "nt":
            return
        if info.st_uid != os.getuid():
            self._raise(f"{self.subject} directory must be owned by current user: {path}")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o700:
            self._raise_unsafe_mode("directory", path, mode)

    def _raise_unsafe_mode(self, kind: str, path: Path, mode: int) -> None:
        command_path = shlex.quote(str(path))
        self._raise(
            f"unsafe {self.subject} {kind}: {path} has mode {mode:04o}; "
            f"run `chmod 700 {command_path}`"
        )

    def _validate_private_file(self, path: Path, info: os.stat_result) -> None:
        unsafe = os.name != "nt" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
        )
        if not stat.S_ISREG(info.st_mode) or unsafe:
            self._raise(f"{self.subject} file must be owned, regular, and mode 600: {path}")

    def _open_lock(self, path: Path) -> IO[str]:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            self._raise(f"cannot open {self.subject} lock securely: {exc}", cause=exc)
        try:
            self._set_descriptor_mode(descriptor, 0o600)
            self._validate_private_file(path, os.fstat(descriptor))
            return os.fdopen(descriptor, "r+")
        except (OSError, RuntimeError):
            os.close(descriptor)
            raise

    def _wait_for_lock(self, lock: IO[str], timeout_s: float, busy_message: str) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                acquire_file_lock(lock)
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    self._raise(busy_message, cause=exc)
                time.sleep(0.1)

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_flags() -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return flags

    @staticmethod
    def _set_descriptor_mode(descriptor: int, mode: int) -> None:
        if os.name != "nt":
            os.fchmod(descriptor, mode)

    @staticmethod
    def _set_mode(path: Path, mode: int) -> None:
        if os.name != "nt":
            path.chmod(mode)

    def _raise(self, message: str, *, cause: BaseException | None = None) -> None:
        error = self.error_type(message)
        if cause is None:
            raise error
        raise error from cause


_PROJECT_SUFFIX = ".json"


@dataclass(frozen=True, slots=True)
class ProjectStoreEntry:
    """One validated per-Project file found by `ProjectStore.scan`."""

    path: Path
    project_root: str
    document: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProjectStore:
    """Private store holding one JSON file per Project Root, named by its hash.

    Every file's name is the SHA-256 of the case-normalised Project Root it
    holds, so lookups and scans agree on one identity rule. `repair` is the
    caller's sentence for fixing a misnamed file; it may use the `{path}` and
    `{root}` placeholders, keeping runtime policy out of this module.
    """

    private: PrivateStore
    repair: str

    def filename(self, project_root: str) -> str:
        """Name the file for a Project Root; case folds only where the OS folds paths."""
        return _hashed_name(os.path.normcase(project_root))

    def path(self, project_root: str) -> Path:
        """Return the file path for a Project Root."""
        return self.private.root / self.filename(project_root)

    def paths(self, project_root: str) -> tuple[Path, ...]:
        """Return the current path plus the pre-normalisation path when it differs."""
        current = self.path(project_root)
        old = self.private.root / _hashed_name(project_root)
        return (current,) if old == current else (current, old)

    def locate(self, project_root: str) -> Path | None:
        """Return the existing file path, or None; refuse a file named by the old rule."""
        current = self.path(project_root)
        if current.exists() or current.is_symlink():
            return current
        for old in self.paths(project_root)[1:]:
            if old.exists() or old.is_symlink():
                raise self._mismatch(old, project_root)
        return None

    def read(self, project_root: str) -> dict[str, object] | None:
        """Read the Project's document, or None when the store or file is absent."""
        if not self.private.validate_existing_directory():
            return None
        path = self.locate(project_root)
        if path is None:
            return None
        return self._identity(path, self._load(path)).document

    def write(self, project_root: str, content: str) -> None:
        """Atomically write the Project's file, creating the private directory."""
        self.private.ensure_directory()
        self.private.atomic_write_text(self.filename(project_root), content)

    def scan(self) -> tuple[ProjectStoreEntry, ...]:
        """Validate every file without changing host state; raise on the first bad one."""
        if not self.private.validate_existing_directory():
            return ()
        entries = []
        for path in sorted(self.private.root.iterdir()):
            if path.name.startswith("."):  # temporary and lock files
                continue
            if path.is_symlink() or not path.is_file() or not path.name.endswith(_PROJECT_SUFFIX):
                raise self.private.error_type(f"unexpected {self.private.subject} entry: {path}")
            entries.append(self._identity(path, self._load(path)))
        return tuple(entries)

    def _load(self, path: Path) -> object:
        try:
            return self.private.read_json(path.name)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self.private.error_type(
                f"cannot read {self.private.subject} {path}: {exc}"
            ) from exc

    def _identity(self, path: Path, raw: object) -> ProjectStoreEntry:
        """Require the file to name a Project Root and to be named for it."""
        document = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
        project_root = document.get("project_root")
        if not isinstance(project_root, str) or not project_root:
            raise self.private.error_type(
                f"{self.private.subject} {path} does not name a Project Root"
            )
        if path.name != self.filename(project_root):
            raise self._mismatch(path, project_root)
        return ProjectStoreEntry(path, project_root, document)

    def _mismatch(self, path: Path, project_root: str) -> RuntimeError:
        repair = self.repair.format(path=path, root=project_root)
        return self.private.error_type(
            f"{self.private.subject} identity mismatch: {path} is not named for the "
            f"Project Root it holds ({project_root}); {repair}"
        )


def _hashed_name(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest() + _PROJECT_SUFFIX
