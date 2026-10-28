"""Bounded read-only indexes of exact Job artifacts, reused by Dashboard refreshes."""

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from booley.core.booley_dir_links import is_booley_dir_entry
from booley.core.boundary import require_dict
from booley.flows.progress_lifecycle import validate_progress_shape
from booley.runtime.filesystem_utils import is_transient_read_error, read_file_bytes

MAX_ARTIFACTS = 4096
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
Artifact = tuple[dict[str, Any], Path]
FileEntry = tuple[int, int, dict[str, Any] | None]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))  # noqa: PTH100 — preserve symlinks


class JobArtifactCache:
    """Reuse parsed artifacts present in successful refreshes."""

    def __init__(self, project: Path) -> None:
        self._project = _absolute(project)
        self._committed: dict[Path, FileEntry] = {}
        self.begin()

    def begin(self) -> None:
        self._next: dict[Path, FileEntry] = {}
        self._indexes: dict[tuple[Path, str], dict[tuple[str, str], Artifact]] = {}
        self._overflow: dict[tuple[Path, str], Iterator[tuple[int, int, Path]]] = {}
        self._directories: set[Path] = set()
        self.diagnostics: list[str] = []

    def complete(self) -> None:
        self._committed = self._next

    def find(self, root: Path, endpoint: str, run_id: str, kind: str) -> Artifact | None:
        root = _absolute(root)
        key = root, endpoint
        if key not in self._indexes:
            self._indexes[key] = self._index(root, endpoint)
        index = self._indexes[key]
        if (kind, run_id) not in index and kind == "progress":
            for entry in self._overflow.get(key, ()):
                if entry[2].name == "progress.json":
                    self._load(entry, endpoint, index)
                    if (kind, run_id) in index:
                        break
        return index.get((kind, run_id))

    def _inspect(self, path: Path, *, directory: bool = False) -> os.stat_result | None:
        path = _absolute(path)
        if not path.is_relative_to(self._project):
            raise OSError(f"Job artifact path outside Project: {path}")
        for parent in reversed((path, *path.parents)):
            if not parent.is_relative_to(self._project) or parent in self._directories:
                continue
            # self._project is the Booley Directory (the cache is built from it): the one
            # component that may be a link.
            info = parent.stat() if is_booley_dir_entry(parent, self._project) else parent.lstat()
            if stat.S_ISDIR(info.st_mode):
                self._directories.add(parent)
            elif parent != path or directory or path == self._project:
                raise OSError(f"Job artifact directory unavailable: {parent}")
            elif stat.S_ISLNK(info.st_mode):
                raise OSError(f"Job artifact symlink unavailable: {path}")
            if parent == path:
                return info
        return None

    def _candidates(self, root: Path, endpoint: str) -> list[tuple[int, int, Path]]:
        candidates = []
        roots = (root.parent / "flow-reports", root.parent / "mcp-tool-reports")
        for reports in roots:
            endpoint_directory = reports / endpoint
            try:
                self._inspect(reports, directory=True)
                if reports == roots[0]:
                    self._candidate(reports / (endpoint + ".json"), candidates)
                self._inspect(endpoint_directory, directory=True)
                invocations = tuple(endpoint_directory.iterdir())
            except OSError:
                continue
            for invocation in invocations:
                self._candidate(invocation / "report.json", candidates)
                if invocation.name.isdigit():
                    self._candidate(invocation / "progress.json", candidates)
        return sorted(candidates, key=lambda item: item[0], reverse=True)

    def _candidate(self, path: Path, candidates: list[tuple[int, int, Path]]) -> None:
        try:
            info = self._inspect(path)
            if info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= 2 * 1024 * 1024:
                entry = info.st_mtime_ns, info.st_size, path
                candidates.append(entry)
                cached = self._committed.get(path)
                if path.name == "progress.json" and cached and cached[:2] == entry[:2]:
                    self._next[path] = cached
        except OSError:
            pass

    def _index(self, root: Path, endpoint: str) -> dict[tuple[str, str], Artifact]:
        result: dict[tuple[str, str], Artifact] = {}
        if not root.is_relative_to(self._project):
            self.diagnostics.append(f"Job artifact root unavailable: {root}")
            return result
        total = 0
        candidates = self._candidates(root, endpoint)
        for index, entry in enumerate(candidates):
            total += entry[1]
            if index >= MAX_ARTIFACTS or total > MAX_ARTIFACT_BYTES:
                self._overflow[root, endpoint] = iter(candidates[index:])
                self.diagnostics.append(
                    f"Job artifact history exceeds scan bound: {root}/{endpoint}"
                )
                break
            self._load(entry, endpoint, result)
        return result

    def _load(
        self,
        entry: tuple[int, int, Path],
        endpoint: str,
        result: dict[tuple[str, str], Artifact],
    ) -> None:
        stamp, size, path = entry
        cached = self._next.get(path, self._committed.get(path))
        try:
            if cached is None or cached[:2] != (stamp, size):
                cached = stamp, size, self._read(path)
        except OSError as exc:
            if is_transient_read_error(exc):
                # Surface a lasting sharing error per item instead of as absent.
                self.diagnostics.append(f"Job artifact unavailable: {path}: {exc}")
            return
        self._next[path] = cached
        data = cached[2]
        if data is None or not isinstance(data.get("run_id"), str):
            return
        kind = "progress" if path.name == "progress.json" else "report"
        if kind != "progress" or data.get("flow") == endpoint:
            result.setdefault((kind, data["run_id"]), (data, path))

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = require_dict(json.loads(read_file_bytes(path)), field="Job artifact")
            if path.name == "progress.json":
                validate_progress_shape(data)
            return data
        except (ValueError, UnicodeError):
            return None
