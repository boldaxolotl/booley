"""Bounded read-only indexes of exact Job artifacts, reused by Dashboard refreshes."""

import json
import os
import stat
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.flows.progress_lifecycle import read_progress_document, read_progress_for_run

MAX_ARTIFACTS = 4096
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
Artifact = tuple[dict[str, Any], Path]
FileEntry = tuple[int, int, dict[str, Any] | None]


def _absolute(path: Path) -> Path:
    # Resolving would erase the symlink evidence required by the lexical guard.
    return Path(os.path.abspath(path))  # noqa: PTH100 — preserve symlinks


class JobArtifactCache:
    """Retain the files read by the last successful refresh."""

    def __init__(self, project: Path) -> None:
        self._project = _absolute(project)
        self._committed: dict[Path, FileEntry] = {}
        self.begin()

    def begin(self) -> None:
        self._next: dict[Path, FileEntry] = {}
        self._indexes: dict[tuple[Path, str], dict[tuple[str, str], Artifact]] = {}
        self._truncated: set[tuple[Path, str]] = set()
        self._fallbacks: dict[tuple[Path, str, str], Artifact | None] = {}
        self._directories: set[Path] = set()
        self.diagnostics: list[str] = []

    def complete(self) -> None:
        self._committed = self._next

    def find(self, root: Path, endpoint: str, run_id: str, kind: str) -> Artifact | None:
        root = _absolute(root)
        key = root, endpoint
        if key not in self._indexes:
            self._indexes[key] = self._index(root, endpoint)
        found = self._indexes[key].get((kind, run_id))
        if found is None and kind == "progress" and key in self._truncated:
            fallback_key = root, endpoint, run_id
            if fallback_key not in self._fallbacks:
                roots = (root.parent / "flow-reports", root.parent / "mcp-tool-reports")
                progress = read_progress_for_run(
                    roots, endpoint, run_id, trusted_path_validator=self._validate
                )
                self._fallbacks[fallback_key] = (
                    None if progress is None else (progress[1], progress[0])
                )
            found = self._fallbacks[fallback_key]
        return found

    def _validate(self, path: Path) -> None:
        self._inspect(path)

    def _inspect(self, path: Path, *, directory: bool = False) -> os.stat_result | None:
        path = _absolute(path)
        if not path.is_relative_to(self._project):
            raise OSError(f"Job artifact path outside Project: {path}")
        for parent in reversed((path, *path.parents)):
            if not parent.is_relative_to(self._project) or parent in self._directories:
                continue
            info = parent.lstat()
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
                candidates.append((info.st_mtime_ns, info.st_size, path))
        except OSError:
            pass

    def _index(self, root: Path, endpoint: str) -> dict[tuple[str, str], Artifact]:
        result: dict[tuple[str, str], Artifact] = {}
        if not root.is_relative_to(self._project):
            self.diagnostics.append(f"Job artifact root unavailable: {root}")
            return result
        total = 0
        for index, (stamp, size, path) in enumerate(self._candidates(root, endpoint)):
            total += size
            if index >= MAX_ARTIFACTS or total > MAX_ARTIFACT_BYTES:
                self._truncated.add((root, endpoint))
                self.diagnostics.append(
                    f"Job artifact history exceeds scan bound: {root}/{endpoint}"
                )
                break
            cached = self._committed.get(path)
            try:
                if cached is None or cached[:2] != (stamp, size):
                    cached = stamp, size, self._read(path)
            except OSError:
                continue
            self._next[path] = cached
            data = cached[2]
            if data is None or not isinstance(data.get("run_id"), str):
                continue
            kind = "progress" if path.name == "progress.json" else "report"
            if kind == "progress" and data.get("flow") != endpoint:
                continue
            result.setdefault((kind, data["run_id"]), (data, path))
        return result

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            if path.name == "progress.json":
                return read_progress_document(
                    path,
                    (path.parents[2],),
                    trusted_path_validator=self._validate,
                    raise_io_errors=True,
                )
            return require_dict(json.loads(path.read_bytes()), field="Job report")
        except (ValueError, UnicodeError):
            return None
