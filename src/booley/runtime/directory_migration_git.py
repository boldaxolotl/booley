"""Capture Git's complete registration graph before a directory migration."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Any

from booley.core.booley_layout import BooleyLayoutError, git_common_directory
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.temporary_workspace import run_guarded, temporary_workspace
from booley.runtime.worktrees import list_worktrees


def git(root: Path, *args: str) -> str:
    """Run bounded, observational Git without index refresh or lazy fetch."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
    result = run_guarded(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def primary_checkout(repository: Path) -> Path:
    """A primary gitfile shares its admin/common directory; linked checkouts do not."""
    admin = Path(git(repository, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(
        git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    if admin == common:
        return repository.resolve()
    raw = git(repository, "worktree", "list", "--porcelain").splitlines()[0]
    candidate = Path(raw.removeprefix("worktree ")).resolve()
    if git_common_directory(candidate) == common:
        return candidate
    configured = run_guarded(
        ["git", "--git-dir", str(common), "config", "--get", "core.worktree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if configured.returncode == 0:
        candidate = (common / configured.stdout.strip()).resolve()
        if git_common_directory(candidate) == common:
            return candidate
    raise BooleyLayoutError(
        "cannot locate the primary Project Root from this linked checkout; "
        f"run `booley init` in the checkout owning Git metadata at {common}"
    )


def registrations(repository: Path) -> list[dict[str, Any]]:
    """Capture branch AND detached checkouts without changing administrative names."""
    rows = []
    is_primary = primary_checkout(repository) == repository.resolve()
    for position, entry in enumerate(list_worktrees(repository)):
        # Git lists a separate common directory as its primary worktree, even
        # when --show-toplevel proves this checkout owns the .git pointer file.
        path = repository.resolve() if position == 0 and is_primary else entry.path.resolve()
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
        edits.extend(_config_edits(checkout, admin, moves))
        if mapped(checkout, moves) == checkout and mapped(admin, moves) == admin:
            continue
        marker = checkout / ".git"
        if marker.is_file():
            old = marker.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
            content = _pointer(
                mapped(admin, moves),
                mapped(checkout, moves),
                not Path(old).is_absolute(),
                prefix="gitdir: ",
            )
            edits.append(edit(marker, content, moves))
            if admin != Path(row["common"]):
                edits.extend(_linked_admin_edits(marker, admin, Path(row["common"]), moves))
    return edits


def _linked_admin_edits(
    marker: Path, admin: Path, common_dir: Path, moves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result = []
    for name, target in (("gitdir", marker), ("commondir", common_dir)):
        path = admin / name
        previous = path.read_text(encoding="utf-8").strip()
        result.append(
            edit(
                path,
                _pointer(
                    mapped(target, moves), mapped(admin, moves), not Path(previous).is_absolute()
                ),
                moves,
            )
        )
    return result


def _config_edits(
    checkout: Path, admin: Path, moves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    edits = []
    for name in ("config", "config.worktree"):
        config = admin / name
        if not config.is_file():
            continue
        with temporary_workspace(prefix="booley-migration-config-") as temporary:
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
    result = run_guarded(
        ["git", "config", "--file", str(config), "--get", key],
        capture_output=True,
        text=True,
        encoding="utf-8",
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
    if Path(spelling) == previous:
        return
    git(draft.parent, "config", "--file", str(draft), key, spelling.replace(os.sep, "/"))


def verify_registrations(rows: list[dict[str, Any]], moves: list[dict[str, Any]]) -> None:
    """Prove paths, administrative identities, HEADs, branches and lock state."""
    actual = {}
    primaries = {
        Path(row["path"])
        for row in rows
        if not row.get("missing") and row["admin"] == row["common"]
    }
    for checkout in primaries:
        for row in registrations(mapped(checkout, moves)):
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
