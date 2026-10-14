"""Capture Git's complete registration graph before a directory migration."""

from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from booley.core.booley_layout import BooleyLayoutError
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.worktrees import list_worktrees


def git(root: Path, *args: str) -> str:
    """Run bounded, observational Git without index refresh or lazy fetch."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise BooleyLayoutError(f"git {' '.join(args)} in {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def mapped(path: Path, moves: list[dict[str, Any]]) -> Path:
    """Apply the most specific original root mapping, on component boundaries."""
    for move in sorted(moves, key=lambda row: len(Path(row["source"]).parts), reverse=True):
        old = Path(move["source"])
        if path.is_relative_to(old):
            return Path(move["destination"]) / path.relative_to(old)
    return path


def registrations(repository: Path) -> list[dict[str, Any]]:
    """Capture branch AND detached checkouts without changing administrative names."""
    rows = []
    for entry in list_worktrees(repository):
        path = entry.path.resolve()
        if not path.is_dir():
            rows.append({"path": str(path), "missing": True})
            continue
        admin = Path(git(path, "rev-parse", "--absolute-git-dir")).resolve()
        common = Path(
            git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        rows.append(
            {
                "path": str(path),
                "admin": str(admin),
                "common": str(common),
                "head": entry.head,
                "branch": entry.branch,
                "locked": entry.locked,
            }
        )
    return rows


def edit(path: Path, content: bytes, moves: list[dict[str, Any]]) -> dict[str, Any]:
    """Retain exact before/after bytes for interruption-safe pointer publication."""
    refuse_symlinks(path)
    if not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.W_OK):
        raise BooleyLayoutError(f"managed file is not writable: {path}")
    if not path.stat().st_mode & 0o222 or not path.parent.stat().st_mode & 0o222:
        raise BooleyLayoutError(f"managed file is read-only: {path}")
    return {
        "path": str(mapped(path, moves)),
        "before": base64.b64encode(path.read_bytes()).decode(),
        "after": base64.b64encode(content).decode(),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


def _pointer(target: Path, parent: Path, relative: bool, *, prefix: str = "") -> bytes:
    spelling = os.path.relpath(target, parent) if relative else str(target)
    return (prefix + spelling.replace(os.sep, "/") + "\n").encode()


def pointer_edits(rows: list[dict[str, Any]], moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Capture both ends of every affected linked-worktree registration."""
    edits = []
    for row in rows:
        if row.get("missing"):
            if mapped(Path(row["path"]), moves) != Path(row["path"]):
                raise BooleyLayoutError(f"affected registered worktree is missing: {row['path']}")
            continue
        checkout, admin = Path(row["path"]), Path(row["admin"])
        if mapped(checkout, moves) == checkout and mapped(admin, moves) == admin:
            continue
        marker = checkout / ".git"
        if marker.is_file():
            old = marker.read_text().strip().removeprefix("gitdir: ")
            content = _pointer(
                mapped(admin, moves),
                mapped(checkout, moves),
                not Path(old).is_absolute(),
                prefix="gitdir: ",
            )
            edits.append(edit(marker, content, moves))
            backlink = admin / "gitdir"
            previous = backlink.read_text().strip()
            edits.append(
                edit(
                    backlink,
                    _pointer(
                        mapped(marker, moves),
                        mapped(admin, moves),
                        not Path(previous).is_absolute(),
                    ),
                    moves,
                )
            )
            common = admin / "commondir"
            previous = common.read_text().strip()
            edits.append(
                edit(
                    common,
                    _pointer(
                        mapped(Path(row["common"]), moves),
                        mapped(admin, moves),
                        not Path(previous).is_absolute(),
                    ),
                    moves,
                )
            )
        edits.extend(_config_edits(checkout, admin, moves))
    return edits


def _config_edits(
    checkout: Path, admin: Path, moves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    edits = []
    for name in ("config", "config.worktree"):
        config = admin / name
        if not config.is_file():
            continue
        with tempfile.TemporaryDirectory(prefix="booley-migration-config-") as temporary:
            draft = Path(temporary) / "config"
            draft.write_bytes(config.read_bytes())
            for key, relative_base in (("core.worktree", admin), ("core.hooksPath", checkout)):
                _map_config(config, draft, key, relative_base, moves)
            content = draft.read_bytes()
        if content != config.read_bytes():
            edits.append(edit(config, content, moves))
    return edits


def _map_config(
    config: Path, draft: Path, key: str, base: Path, moves: list[dict[str, Any]]
) -> None:
    result = subprocess.run(
        ["git", "config", "--file", str(config), "--get", key],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 1:
        return
    if result.returncode:
        raise BooleyLayoutError(f"cannot inspect {key} in {config}: {result.stderr}")
    previous = Path(result.stdout.strip())
    absolute = previous if previous.is_absolute() else (base / previous).resolve()
    new = mapped(absolute, moves)
    spelling = str(new) if previous.is_absolute() else os.path.relpath(new, mapped(base, moves))
    git(draft.parent, "config", "--file", str(draft), key, spelling.replace(os.sep, "/"))


def verify_registrations(rows: list[dict[str, Any]], moves: list[dict[str, Any]]) -> None:
    """Prove paths, administrative identities, HEADs, branches and lock state."""
    actual = {}
    commons = {Path(row["common"]) for row in rows if not row.get("missing")}
    for common in commons:
        for row in registrations(mapped(common.parent, moves)):
            actual[row["path"]] = row
    for row in rows:
        path = mapped(Path(row["path"]), moves)
        current = actual.get(str(path))
        if row.get("missing"):
            if current is None or not current.get("missing"):
                raise BooleyLayoutError(f"missing registration changed during migration: {path}")
            continue
        expected = {
            **row,
            **{key: str(mapped(Path(row[key]), moves)) for key in ("path", "admin", "common")},
        }
        if current != expected:
            raise BooleyLayoutError(f"worktree registration changed during migration: {path}")
