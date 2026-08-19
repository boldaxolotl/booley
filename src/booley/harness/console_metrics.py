"""Live worktree metrics shared by Console event producers."""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
_SANDBOX_WORKTREE = Path("/work")


class WorktreeLineCounter:
    """Return the current text-line delta from a ticket's fork base."""

    def __init__(
        self,
        worktree: Path,
        base_ref: str,
        *,
        reported_root: Path | None = None,
    ) -> None:
        self._worktree = worktree.resolve()
        self._reported_root = reported_root.resolve() if reported_root is not None else None
        self._base_sha = self._resolve_base(base_ref)

    def snapshot(self) -> tuple[int, int] | None:
        """Return ``(added, removed)``, including untracked text files."""
        files = self.snapshot_by_file()
        if files is None:
            return None
        return (
            sum(added for added, _removed in files.values()),
            sum(removed for _added, removed in files.values()),
        )

    def snapshot_by_file(self) -> dict[str, tuple[int, int]] | None:
        """Return the current fork-base line delta keyed by repository path."""
        if self._base_sha is None:
            return None
        diff = self._git("diff", "--numstat", "--no-renames", self._base_sha, "--")
        if diff is None:
            return None
        files = self._parse_numstat_by_file(diff)
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z")
        if untracked is not None:
            files.update(self._count_untracked_lines_by_file(untracked))
        return files

    def normalize_path(self, raw_path: str) -> str | None:
        """Convert an agent-reported path to a safe repository-relative path."""
        # The agent reports Session Runtime paths using POSIX syntax even when
        # the host-side Console runs on Windows, where Path('/work/...') is not
        # considered absolute.
        if raw_path == "/work":
            return None
        if raw_path.startswith("/work/"):
            raw_path = raw_path.removeprefix("/work/")
        elif os.name == "nt" and raw_path.startswith("/"):
            # A POSIX absolute path outside the Session Runtime workspace is
            # not a path in the native Windows worktree.
            return None
        path = Path(raw_path)
        if ".." in path.parts:
            return None
        try:
            if path.is_absolute():
                try:
                    path = path.relative_to(self._worktree)
                except ValueError:
                    path = path.relative_to(_SANDBOX_WORKTREE)
            elif self._reported_root is not None:
                with contextlib.suppress(ValueError):
                    path = (self._reported_root / path).resolve().relative_to(self._worktree)
            normalized = Path(path).as_posix()
        except (TypeError, ValueError):
            return None
        if normalized in ("", "."):
            return None
        return normalized.removeprefix("./")

    def _resolve_base(self, base_ref: str) -> str | None:
        merged = self._git("merge-base", "HEAD", base_ref)
        if merged and merged.strip():
            return merged.strip()
        resolved = self._git("rev-parse", base_ref)
        return resolved.strip() if resolved and resolved.strip() else None

    def _git(self, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self._worktree,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            logger.debug("Console git metric failed: git %s", " ".join(args))
            return None
        return result.stdout

    @staticmethod
    def _parse_numstat_by_file(output: str) -> dict[str, tuple[int, int]]:
        files: dict[str, tuple[int, int]] = {}
        for line in output.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            files[fields[2]] = (int(fields[0]), int(fields[1]))
        return files

    def _count_untracked_lines_by_file(self, output: str) -> dict[str, tuple[int, int]]:
        files: dict[str, tuple[int, int]] = {}
        for relative in output.rstrip("\0").split("\0"):
            if not relative:
                continue
            try:
                path = self._worktree / relative
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" not in data:
                files[relative] = (len(data.splitlines()), 0)
        return files
