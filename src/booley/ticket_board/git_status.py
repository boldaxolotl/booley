"""Shared parsing for Git porcelain-v1 status output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GitStatusEntry:
    """One NUL-delimited porcelain-v1 status record."""

    status: str
    path: str
    source_path: str | None = None

    @property
    def staged(self) -> bool:
        """Whether the index contains a change for this path."""
        return self.status[0] not in {" ", "?"}

    @property
    def unstaged(self) -> bool:
        """Whether the worktree contains an unstaged or untracked change."""
        return self.status == "??" or self.status[1] != " "


def parse_porcelain_v1_z(output: str) -> tuple[GitStatusEntry, ...]:
    """Parse ``git status --porcelain -z`` without losing rename sources."""
    fields = [field for field in output.split("\0") if field]
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            raise ValueError(f"malformed Git porcelain record: {record!r}")
        status = record[:2]
        path = record[3:].replace("\\", "/").removeprefix("./")
        source_path = None
        if "R" in status or "C" in status:
            if index >= len(fields):
                raise ValueError(f"Git porcelain rename has no source: {record!r}")
            source_path = fields[index].replace("\\", "/").removeprefix("./")
            index += 1
        entries.append(GitStatusEntry(status, path, source_path))
    return tuple(entries)
