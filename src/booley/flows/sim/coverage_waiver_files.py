"""Race-safe, no-follow filesystem access for approved coverage waivers."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

PathProblemKind = Literal["missing", "symlink", "invalid", "unreadable"]
PathEntryKind = Literal["directory", "file"]


@dataclass(frozen=True)
class SecureFile:
    """One regular file read while its verified ancestor handles were retained."""

    relative_path: str
    raw: bytes


@dataclass(frozen=True)
class SecurePathProblem:
    """One entry that could not be traversed without following links."""

    relative_path: str
    kind: PathProblemKind
    entry_kind: PathEntryKind


@dataclass(frozen=True)
class SecureFileScan:
    """Files and problems from one contained directory traversal."""

    files: tuple[SecureFile, ...]
    problems: tuple[SecurePathProblem, ...]


class SecurePathError(OSError):
    """A requested path could not be opened through verified ancestor handles."""

    def __init__(
        self, relative_path: str, kind: PathProblemKind, entry_kind: PathEntryKind
    ) -> None:
        self.relative_path = relative_path
        self.kind = kind
        self.entry_kind = entry_kind
        super().__init__(f"{entry_kind} {relative_path!r} is {kind}")


class _SecureTreeImplementation(Protocol):
    def close(self) -> None: ...

    def scan_files(self, relative: str) -> SecureFileScan: ...

    def read_file(self, relative: str) -> bytes: ...


def _parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or path.as_posix() != relative:
        raise SecurePathError(relative, "invalid", "file")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SecurePathError(relative, "invalid", "file")
    return path.parts


def _problem_kind(exc: OSError) -> PathProblemKind:
    if exc.errno in {errno.ENOENT}:
        return "missing"
    if exc.errno in {errno.ELOOP}:
        return "symlink"
    if exc.errno in {errno.ENOTDIR, errno.EISDIR}:
        return "invalid"
    return "unreadable"


def _entry_problem_kind(parent: int, name: str, exc: OSError) -> PathProblemKind:
    try:
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except OSError:
        return _problem_kind(exc)
    return "symlink" if stat.S_ISLNK(mode) else _problem_kind(exc)


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


class _DescriptorSecureTree:
    """POSIX adapter using directory descriptors for every lookup."""

    def __init__(self, root: Path) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self._root_descriptor = os.open(root, flags)
        except OSError as exc:
            raise SecurePathError(".", _problem_kind(exc), "directory") from exc

    def close(self) -> None:
        os.close(self._root_descriptor)

    def _open_directory_chain(self, relative: str, stack: ExitStack) -> int:
        current = self._root_descriptor
        traversed: list[str] = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for part in _parts(relative):
            traversed.append(part)
            try:
                current = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                joined = PurePosixPath(*traversed).as_posix()
                kind = _entry_problem_kind(current, part, exc)
                raise SecurePathError(joined, kind, "directory") from exc
            stack.callback(os.close, current)
        return current

    def _read_regular_at(self, parent: int, name: str, relative: str) -> bytes:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            raise SecurePathError(relative, _problem_kind(exc), "file") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SecurePathError(relative, "invalid", "file")
            return _read_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def read_file(self, relative: str) -> bytes:
        parts = _parts(relative)
        with ExitStack() as stack:
            parent = self._root_descriptor
            if len(parts) > 1:
                parent = self._open_directory_chain(PurePosixPath(*parts[:-1]).as_posix(), stack)
            return self._read_regular_at(parent, parts[-1], relative)

    def _scan_directory(
        self,
        descriptor: int,
        prefix: PurePosixPath,
        files: list[SecureFile],
        problems: list[SecurePathProblem],
    ) -> None:
        try:
            names = sorted(os.listdir(descriptor))
        except OSError:
            relative = prefix.as_posix() if prefix.parts else "."
            problems.append(SecurePathProblem(relative, "unreadable", "directory"))
            return
        for name in names:
            self._scan_entry(descriptor, name, prefix, files, problems)

    def _scan_entry(
        self,
        parent: int,
        name: str,
        prefix: PurePosixPath,
        files: list[SecureFile],
        problems: list[SecurePathProblem],
    ) -> None:
        relative = (prefix / name).as_posix()
        try:
            mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
        except OSError as exc:
            problems.append(SecurePathProblem(relative, _problem_kind(exc), "file"))
            return
        if stat.S_ISLNK(mode):
            entry_kind: PathEntryKind = "file" if name.endswith(".toml") else "directory"
            problems.append(SecurePathProblem(relative, "symlink", entry_kind))
        elif stat.S_ISDIR(mode):
            self._scan_child_directory(parent, name, prefix, files, problems)
        elif stat.S_ISREG(mode):
            self._scan_regular_file(parent, name, relative, files, problems)
        else:
            problems.append(SecurePathProblem(relative, "invalid", "file"))

    def _scan_child_directory(
        self,
        parent: int,
        name: str,
        prefix: PurePosixPath,
        files: list[SecureFile],
        problems: list[SecurePathProblem],
    ) -> None:
        relative = (prefix / name).as_posix()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            problems.append(SecurePathProblem(relative, _problem_kind(exc), "directory"))
            return
        try:
            self._scan_directory(descriptor, prefix / name, files, problems)
        finally:
            os.close(descriptor)

    def _scan_regular_file(
        self,
        parent: int,
        name: str,
        relative: str,
        files: list[SecureFile],
        problems: list[SecurePathProblem],
    ) -> None:
        try:
            raw = self._read_regular_at(parent, name, relative)
        except SecurePathError as exc:
            problems.append(SecurePathProblem(relative, exc.kind, "file"))
        else:
            files.append(SecureFile(relative, raw))

    def scan_files(self, relative: str) -> SecureFileScan:
        files: list[SecureFile] = []
        problems: list[SecurePathProblem] = []
        with ExitStack() as stack:
            directory = self._open_directory_chain(relative, stack)
            self._scan_directory(directory, PurePosixPath(), files, problems)
        return SecureFileScan(
            files=tuple(sorted(files, key=lambda item: item.relative_path)),
            problems=tuple(sorted(problems, key=lambda item: item.relative_path)),
        )


def _windows_error_kind(exc: OSError) -> PathProblemKind:
    winerror = getattr(exc, "winerror", None)
    return "missing" if winerror in {2, 3} else "unreadable"


class _WindowsSecureTree:
    """Windows adapter retaining no-delete handles for every ancestor."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root_handle = self._open(root, expected_directory=True)

    @staticmethod
    def _open(path: Path, *, expected_directory: bool) -> Any:
        import win32con
        import win32file

        share = win32con.FILE_SHARE_READ
        if expected_directory:
            share |= win32con.FILE_SHARE_WRITE
        flags = win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OPEN_REPARSE_POINT
        try:
            handle = win32file.CreateFile(
                str(path),
                win32con.GENERIC_READ,
                share,
                None,
                win32con.OPEN_EXISTING,
                flags,
                None,
            )
        except OSError as exc:
            kind: PathEntryKind = "directory" if expected_directory else "file"
            raise SecurePathError(str(path), _windows_error_kind(exc), kind) from exc
        attributes = win32file.GetFileInformationByHandle(handle)[0]
        if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            handle.Close()
            kind = "directory" if expected_directory else "file"
            raise SecurePathError(str(path), "symlink", kind)
        is_directory = bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != expected_directory:
            handle.Close()
            kind = "directory" if expected_directory else "file"
            raise SecurePathError(str(path), "invalid", kind)
        return handle

    def close(self) -> None:
        self._root_handle.Close()

    def _open_directory_chain(self, relative: str, stack: ExitStack) -> Path:
        current = self._root
        for part in _parts(relative):
            current /= part
            handle = self._open(current, expected_directory=True)
            stack.callback(handle.Close)
        return current

    @staticmethod
    def _read_handle(handle: Any) -> bytes:
        import pywintypes
        import win32file

        chunks: list[bytes] = []
        while True:
            try:
                _, chunk = win32file.ReadFile(handle, 1024 * 1024)
            except pywintypes.error as exc:
                if exc.winerror == 38:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def read_file(self, relative: str) -> bytes:
        parts = _parts(relative)
        with ExitStack() as stack:
            parent = self._root
            if len(parts) > 1:
                parent = self._open_directory_chain(PurePosixPath(*parts[:-1]).as_posix(), stack)
            path = parent / parts[-1]
            handle = self._open(path, expected_directory=False)
            stack.callback(handle.Close)
            return self._read_handle(handle)

    def _scan_directory(
        self,
        directory: Path,
        prefix: PurePosixPath,
        files: list[SecureFile],
        problems: list[SecurePathProblem],
    ) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            relative = prefix.as_posix() if prefix.parts else "."
            problems.append(SecurePathProblem(relative, "unreadable", "directory"))
            return
        for entry in entries:
            is_directory = entry.is_dir(follow_symlinks=False)
            self._scan_entry(Path(entry.path), entry.name, is_directory, prefix, files, problems)

    def _scan_entry(
        self,
        path: Path,
        name: str,
        expected_directory: bool,
        prefix: PurePosixPath,
        files: list[SecureFile],
        problems: list[SecurePathProblem],
    ) -> None:
        relative = (prefix / name).as_posix()
        try:
            handle = self._open(path, expected_directory=expected_directory)
        except SecurePathError as exc:
            if exc.kind == "symlink":
                entry_kind: PathEntryKind = "file" if name.endswith(".toml") else "directory"
            else:
                entry_kind = "directory" if expected_directory else "file"
            problems.append(SecurePathProblem(relative, exc.kind, entry_kind))
            return
        try:
            if expected_directory:
                self._scan_directory(path, prefix / name, files, problems)
            else:
                files.append(SecureFile(relative, self._read_handle(handle)))
        finally:
            handle.Close()

    def scan_files(self, relative: str) -> SecureFileScan:
        files: list[SecureFile] = []
        problems: list[SecurePathProblem] = []
        with ExitStack() as stack:
            directory = self._open_directory_chain(relative, stack)
            self._scan_directory(directory, PurePosixPath(), files, problems)
        return SecureFileScan(
            files=tuple(sorted(files, key=lambda item: item.relative_path)),
            problems=tuple(sorted(problems, key=lambda item: item.relative_path)),
        )


class SecureTree:
    """Select the platform adapter and expose one contained-read interface."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._implementation: _SecureTreeImplementation | None = None

    def __enter__(self) -> SecureTree:
        if os.name == "nt":
            self._implementation = _WindowsSecureTree(self._root)
        else:
            self._implementation = _DescriptorSecureTree(self._root)
        return self

    def __exit__(self, *_args: object) -> None:
        assert self._implementation is not None
        self._implementation.close()

    def scan_files(self, relative: str) -> SecureFileScan:
        assert self._implementation is not None
        return self._implementation.scan_files(relative)

    def read_file(self, relative: str) -> bytes:
        assert self._implementation is not None
        return self._implementation.read_file(relative)
