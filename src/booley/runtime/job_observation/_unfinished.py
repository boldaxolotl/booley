"""A per-root index of the unfinished Job records, kept across refreshes."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# A directory changed this recently can change again within the same timestamp tick, so an
# equal directory key would not prove an equal listing. Such a root is listed again next time.
QUIET_NS = 2_000_000_000

DirectoryKey = tuple[int, int, int, int]
# Per record name: inode and mtime of the file that was parsed, and whether it was unfinished.
RecordStamp = tuple[int, int, bool]


@dataclass(frozen=True)
class _Entry:
    """One root's records; ``key`` is set only when the listing may be reused whole."""

    key: DirectoryKey | None
    records: dict[str, RecordStamp]

    def unfinished(self, root: Path) -> tuple[Path, ...]:
        return tuple(root / name for name, stamp in self.records.items() if stamp[2])


class UnfinishedIndex:
    """Which records of a root are unfinished, parsing each record once.

    Every record write is a temporary file renamed into place and every removal an unlink,
    so any change to a root's records changes the directory's mtime and ctime. A root whose
    directory key is unchanged is not listed again; a changed root parses only the records
    whose inode or mtime is new. A refresh that raises commits nothing (``begin``/``complete``).
    """

    def __init__(self) -> None:
        self._entries: dict[Path, _Entry] = {}
        self._pending: dict[Path, _Entry] = {}
        self._now_ns = 0

    def begin(self, now_ns: int) -> None:
        """Start a refresh generation at wall-clock time *now_ns*."""
        self._pending = {}
        self._now_ns = now_ns

    def complete(self) -> None:
        """Commit the generation; roots it did not visit are forgotten."""
        self._entries = self._pending

    def paths(self, root: Path, is_unfinished: Callable[[Path], bool]) -> tuple[Path, ...]:
        """Record paths in *root* whose on-disk status is running.

        *is_unfinished* parses one record; it may raise ``OSError``, which leaves that record
        out and the root's listing unreused next time.
        """
        try:
            stat = root.lstat()
        except OSError:
            return ()
        key = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
        prior = self._entries.get(root)
        if prior is not None and prior.key == key:
            self._pending[root] = prior
            return prior.unfinished(root)
        records: dict[str, RecordStamp] = {}
        names, complete = _json_names(root)
        for name in names:
            path = root / name
            try:
                recorded = path.lstat()
                stamp = (recorded.st_ino, recorded.st_mtime_ns)
                old = None if prior is None else prior.records.get(path.name)
                if old is None or old[:2] != stamp:
                    old = (*stamp, is_unfinished(path))
            except OSError:
                complete = False
                continue
            records[path.name] = old
        quiet = stat.st_mtime_ns <= self._now_ns - QUIET_NS
        entry = _Entry(key if complete and quiet else None, records)
        self._pending[root] = entry
        return entry.unfinished(root)


def _json_names(root: Path) -> tuple[list[str], bool]:
    """The ``*.json`` names in *root* and whether the listing succeeded.

    ``Path.glob`` hides a failing ``scandir``, which would cache an empty root for good.
    """
    try:
        with os.scandir(root) as entries:
            return [e.name for e in entries if e.name.endswith(".json") and e.name[0] != "."], True
    except OSError:
        return [], False
