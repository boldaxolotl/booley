"""Bounded read-only indexes of exact Job artifacts, reused by Dashboard refreshes."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.flows.progress_lifecycle import read_progress_document, read_progress_for_run
from booley.runtime.safe_storage import refuse_symlinks

MAX_ARTIFACTS = 4096
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
Artifact = tuple[dict[str, Any], Path]


class JobArtifactCache:
    """Index each endpoint once per snapshot; cache parsed files by path/mtime/size.

    File metadata is sorted before indexing at most 4096 files / 16 MiB per
    endpoint. Exact progress beyond that index uses the released complete
    lookup. The persistent parse cache has the same bounds. Excess history
    is diagnosed, never adopted, repaired or deleted.
    """

    def __init__(self) -> None:
        self._files: OrderedDict[Path, tuple[int, int, dict[str, Any] | None]] = OrderedDict()
        self._bytes = 0
        self._indexes: dict[tuple[Path, str], dict[tuple[str, str], Artifact]] = {}
        self.diagnostics: list[str] = []

    def begin(self) -> None:
        """Start a snapshot, preserving only the bounded parse cache across refreshes."""
        self._indexes.clear()
        self.diagnostics.clear()

    def find(self, root: Path, endpoint: str, run_id: str, kind: str) -> Artifact | None:
        """Exact run identity; unrelated latest files never satisfy another Job."""
        key = root, endpoint
        if key not in self._indexes:
            self._indexes[key] = self._index(root, endpoint)
        found = self._indexes[key].get((kind, run_id))
        if found is None and kind == "progress":
            roots = (root.parent / "flow-reports", root.parent / "mcp-tool-reports")
            progress = read_progress_for_run(roots, endpoint, run_id)
            if progress is not None:
                path, data = progress
                return data, path
        return found

    def _candidates(self, root: Path, endpoint: str) -> list[tuple[int, int, Path]]:
        roots = (root.parent / "flow-reports", root.parent / "mcp-tool-reports")
        paths = [roots[0] / (endpoint + ".json")]
        for reports in roots:
            paths.extend((reports / endpoint).glob("*/*.json"))
        candidates = []
        for path in paths:
            if path.name not in {endpoint + ".json", "report.json", "progress.json"}:
                continue
            if path.name == "progress.json" and not path.parent.name.isdigit():
                continue
            try:
                refuse_symlinks(path)
                stat = path.stat()
                if stat.st_size <= 2 * 1024 * 1024:
                    candidates.append((stat.st_mtime_ns, stat.st_size, path))
            except OSError:
                continue
        return sorted(candidates, key=lambda item: item[0], reverse=True)

    def _index(self, root: Path, endpoint: str) -> dict[tuple[str, str], Artifact]:
        result: dict[tuple[str, str], Artifact] = {}
        total = 0
        candidates = self._candidates(root, endpoint)
        for index, (stamp, size, path) in enumerate(candidates):
            total += size
            if index >= MAX_ARTIFACTS or total > MAX_ARTIFACT_BYTES:
                self.diagnostics.append(
                    f"Job artifact history exceeds scan bound: {root}/{endpoint}"
                )
                break
            data = self._read(path, stamp, size)
            if data is None or not isinstance(data.get("run_id"), str):
                continue
            kind = "progress" if path.name == "progress.json" else "report"
            if kind == "progress" and data.get("flow") != endpoint:
                continue
            result.setdefault((kind, data["run_id"]), (data, path))
        return result

    def _read(self, path: Path, stamp: int, size: int) -> dict[str, Any] | None:
        cached = self._files.get(path)
        if cached is not None and cached[:2] == (stamp, size):
            self._files.move_to_end(path)
            return cached[2]
        try:
            data = (
                read_progress_document(path, (path.parents[2],))
                if path.name == "progress.json"
                else require_dict(json.loads(path.read_bytes()), field="Job report")
            )
        except (OSError, ValueError, UnicodeError):
            data = None
        if cached is not None:
            self._bytes -= cached[1]
        self._files[path] = stamp, size, data
        self._files.move_to_end(path)
        self._bytes += size
        while self._files and (
            self._bytes > MAX_ARTIFACT_BYTES or len(self._files) > MAX_ARTIFACTS
        ):
            _, old = self._files.popitem(last=False)
            self._bytes -= old[1]
        return data
