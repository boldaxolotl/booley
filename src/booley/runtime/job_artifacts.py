"""Bounded read-only indexes of exact Job artifacts, reused by Dashboard refreshes."""

from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.flows.progress_lifecycle import read_progress_document, read_progress_for_run

MAX_ARTIFACTS = 4096
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
Artifact = tuple[dict[str, Any], Path]
FileEntry = tuple[int, int, dict[str, Any] | None]
ShardKey = tuple[Path, str]


def _absolute(path: Path) -> Path:
    # Resolving would erase the symlink evidence required by the lexical guard.
    return Path(os.path.abspath(path))  # noqa: PTH100 — preserve symlinks


@dataclass
class _Shard:
    files: dict[Path, FileEntry] = field(default_factory=dict)
    directories: dict[Path, bool] = field(default_factory=dict)


class JobArtifactCache:
    """Retain each admitted endpoint's bounded parsed history across refreshes.

    Each lazy shard retains at most 4096 files / 16 MiB of serialized source.
    Production capacity derives from MAX_JOBS: at most 16,777,216 entries and
    64 GiB source bytes, not Python heap bytes. Exact progress beyond the scan
    bound keeps the released complete lookup. No history is repaired or deleted.
    Standalone callers define a fresh safety-check boundary with begin().
    Their released complete progress fallback stays fresh without begin();
    configured fallback results retain at most the endpoint capacity per refresh.
    """

    def __init__(self) -> None:
        self._project: Path | None = None
        self._max_endpoints = 4096
        self._shards: OrderedDict[ShardKey, _Shard] = OrderedDict()
        self._indexes: dict[ShardKey, dict[tuple[str, str], Artifact]] = {}
        self._truncated: set[ShardKey] = set()
        self._fallbacks: dict[tuple[ShardKey, str], Artifact | None] = {}
        self._shared_directories: dict[Path, bool] = {}
        self._touched: set[ShardKey] = set()
        self._completed: set[ShardKey] | None = None
        self._expected: set[ShardKey] = set()
        self._active: _Shard | None = None
        self._local_directories: tuple[Path, ...] = ()
        self.diagnostics: list[str] = []

    def configure_project(self, project_dir: Path, *, max_endpoints: int = 4096) -> None:
        """Set an inclusive lexical Project boundary and projection-derived capacity."""
        if max_endpoints < 1:
            raise ValueError("Job artifact endpoint capacity must be positive")
        project = _absolute(project_dir)
        if (project, max_endpoints) != (self._project, self._max_endpoints):
            self._shards.clear()
            self._completed = None
            self._shared_directories.clear()
            self._indexes.clear()
            self._truncated.clear()
            self._fallbacks.clear()
            self._touched.clear()
            self._active = None
            self._expected.clear()
        self._project, self._max_endpoints = project, max_endpoints

    def begin(self) -> None:
        """Start fresh indexes/safety checks, preserving the last completed generation."""
        if self._completed is not None:
            for key in tuple(self._shards):
                if key not in self._completed:
                    del self._shards[key]
        for shard in self._shards.values():
            shard.directories.clear()
        self._indexes.clear()
        self._truncated.clear()
        self._fallbacks.clear()
        self._shared_directories.clear()
        self._touched.clear()
        self._expected.clear()
        self.diagnostics.clear()

    def prepare_endpoints(self, endpoints: Iterable[ShardKey]) -> None:
        """Protect the bounded current projection from eviction by newly admitted shards."""
        expected = {(_absolute(root), endpoint) for root, endpoint in endpoints}
        if len(expected) > self._max_endpoints:
            raise ValueError("Job artifact projection exceeds endpoint capacity")
        self._expected = expected

    def complete(self) -> None:
        """Commit the touch generation only after a successful snapshot projection."""
        self._completed = self._touched.copy()

    def find(self, root: Path, endpoint: str, run_id: str, kind: str) -> Artifact | None:
        """Exact run identity; unrelated latest files never satisfy another Job."""
        root = _absolute(root)
        key = root, endpoint
        self._local_directories = tuple(
            root.parent / namespace / endpoint
            for namespace in ("flow-reports", "mcp-tool-reports")
        )
        self._touched.add(key)
        if key not in self._indexes:
            self._shards.setdefault(key, _Shard())
            self._shards.move_to_end(key)
            self._active = self._shards[key]
            self._indexes[key] = self._index(root, endpoint)
            self._trim_shards()
        found = self._indexes[key].get((kind, run_id))
        if found is None and kind == "progress":
            if self._project is None:
                return self._progress_fallback(root, endpoint, run_id)
            if key in self._truncated:
                fallback_key = key, run_id
                if fallback_key not in self._fallbacks:
                    self._active = self._shards.get(key)
                    self._fallbacks[fallback_key] = self._progress_fallback(root, endpoint, run_id)
                    if len(self._fallbacks) > self._max_endpoints:
                        del self._fallbacks[next(iter(self._fallbacks))]
                found = self._fallbacks[fallback_key]
        return found

    def _progress_fallback(self, root: Path, endpoint: str, run_id: str) -> Artifact | None:
        roots = (root.parent / "flow-reports", root.parent / "mcp-tool-reports")
        progress = read_progress_for_run(
            roots,
            endpoint,
            run_id,
            trusted_path_validator=self._validate if self._project is not None else None,
        )
        return None if progress is None else (progress[1], progress[0])

    def _trim_shards(self) -> None:
        if len(self._shards) <= self._max_endpoints:
            return
        while len(self._shards) > self._max_endpoints:
            key = next(
                (key for key in self._shards if key not in self._expected),
                next(iter(self._shards)),
            )
            del self._shards[key]
            self._indexes.pop(key, None)
            self._truncated.discard(key)
            self._touched.discard(key)
            self._fallbacks = {k: v for k, v in self._fallbacks.items() if k[0] != key}
        # Standalone shared ancestors are bounded by the surviving root set.
        ancestors = {parent for root, _ in self._shards for parent in root.parents}
        ancestors.update(
            root.parent / namespace
            for root, _ in self._shards
            for namespace in ("flow-reports", "mcp-tool-reports")
        )
        self._shared_directories = {
            path: value for path, value in self._shared_directories.items() if path in ancestors
        }

    def _directory_memo(self, path: Path) -> dict[Path, bool]:
        if self._active is not None and any(
            path.is_relative_to(local) for local in self._local_directories
        ):
            return self._active.directories
        return self._shared_directories

    def _remember_directory(self, memo: dict[Path, bool], path: Path, valid: bool) -> None:
        memo[path] = valid
        if self._active is None or memo is not self._active.directories:
            return
        if len(memo) > MAX_ARTIFACTS + 2:
            oldest = next(
                candidate for candidate in memo if candidate not in self._local_directories
            )
            del memo[oldest]

    def _directory(self, path: Path) -> None:
        memo = self._directory_memo(path)
        if path not in memo:
            try:
                self._remember_directory(memo, path, stat.S_ISDIR(path.lstat().st_mode))
            except OSError:
                self._remember_directory(memo, path, False)
        if not memo[path]:
            raise OSError(f"Job artifact directory unavailable: {path}")

    def _validate(self, path: Path) -> None:
        self._inspect(path)

    def _inspect(self, path: Path, *, directory: bool = False) -> os.stat_result | None:
        path = _absolute(path)
        boundary = self._project or Path(path.anchor)
        if not path.is_relative_to(boundary):
            raise OSError(f"Job artifact path outside Project: {path}")
        # Check parents before the candidate; a linked parent must never be followed.
        parents = list(path.parent.parents)
        parents.insert(0, path.parent)
        for parent in reversed(parents):
            if parent.is_relative_to(boundary):
                self._directory(parent)
        if directory:
            self._directory(path)
            return None
        memo = self._directory_memo(path)
        if path in memo:
            self._directory(path)
            return None
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise OSError(f"Job artifact symlink unavailable: {path}")
        if stat.S_ISDIR(info.st_mode):
            self._remember_directory(memo, path, True)
        elif path == boundary:
            raise OSError(f"Job artifact Project unavailable: {path}")
        return info

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
                try:
                    self._inspect(invocation, directory=True)
                    self._candidate(invocation / "report.json", candidates)
                    if invocation.name.isdigit():
                        self._candidate(invocation / "progress.json", candidates)
                except OSError:
                    continue
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
        assert self._active is not None
        previous, selected = self._active.files, {}
        if self._project is not None and not root.is_relative_to(self._project):
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
            cached = previous.get(path)
            if cached is not None and cached[:2] == (stamp, size):
                data = cached[2]
                selected[path] = cached
            else:
                try:
                    data = self._read(path)
                except OSError:
                    continue
                selected[path] = stamp, size, data
            if data is None or not isinstance(data.get("run_id"), str):
                continue
            kind = "progress" if path.name == "progress.json" else "report"
            if kind == "progress" and data.get("flow") != endpoint:
                continue
            result.setdefault((kind, data["run_id"]), (data, path))
        selected_directories = {path.parent for path in selected}
        self._active.directories = {
            path: valid
            for path, valid in self._active.directories.items()
            if path in selected_directories or path.name == endpoint
        }
        self._active.files = selected
        return result

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            return (
                read_progress_document(
                    path,
                    (path.parents[2],),
                    trusted_path_validator=self._validate,
                    raise_io_errors=True,
                )
                if path.name == "progress.json"
                else require_dict(json.loads(path.read_bytes()), field="Job report")
            )
        except (ValueError, UnicodeError):
            return None
