"""Secure persistence for host-private Booley state."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from booley.runtime.file_lock import acquire_file_lock, release_file_lock


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
        descriptor = os.open(path, self._read_flags())
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
            temp_path.replace(path)
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
        unsafe = os.name != "nt" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022
        )
        if not stat.S_ISDIR(info.st_mode) or unsafe:
            self._raise(f"unsafe {self.subject} ancestor: {path}")

    def _validate_private_directory(self, path: Path) -> None:
        if path.is_symlink():
            self._raise(f"{self.subject} directory must not be a symlink: {path}")
        info = path.stat()
        unsafe = os.name != "nt" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
        )
        if not stat.S_ISDIR(info.st_mode) or unsafe:
            self._raise(f"{self.subject} directory must be owned with mode 700: {path}")

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
