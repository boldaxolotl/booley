"""Observational retained Goal Record inventory, independent of live status/recovery."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from booley.core.booley_layout import BooleyLayoutError
from booley.goals.model import GoalRecord, GoalState
from booley.goals.paths import GOAL_ID_PATTERN, goals_root, record_paths, validate_goal_id
from booley.goals.presentation import goal_presentation
from booley.goals.proposals import digest
from booley.goals.state_store import GoalStateError, load_goal_state
from booley.goals.store import GoalStore, GoalStoreError, resolve_worktree_identity
from booley.review.goal_package import GoalCompletionPackage
from booley.runtime.booley_dir_migration import archival_path
from booley.runtime.worktrees import list_worktrees

SCAN_ENTRIES = 20_000
SCAN_SECONDS = 2.0
GIT_TIMEOUT = 5


def unlinked(path: Path, root: Path) -> None:
    """Reject links/junctions in a confined metadata path before reading it."""
    if not path.is_relative_to(root):
        raise GoalStoreError(f"path outside Goal Record: {path}")
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise GoalStoreError(f"linked Goal storage is unavailable: {item}")
        if item == root:
            break


def logical_bytes(
    root: Path, *, entries: int = SCAN_ENTRIES, seconds: float = SCAN_SECONDS
) -> int | None:
    """Exact logical regular-file bytes, or unavailable when the finite scan is incomplete.

    Child links are never followed or included as regular-file bytes. The cleanup
    preflight uses a larger finite 200,000-entry/20-second budget.
    """
    pending = [root]
    total = 0
    remaining = entries
    deadline = time.monotonic() + seconds
    try:
        unlinked(root, root)
        while pending and remaining > 0 and time.monotonic() < deadline:
            path = pending.pop()
            mode = path.lstat()
            remaining -= 1
            if stat.S_ISREG(mode.st_mode):
                total += mode.st_size
            elif stat.S_ISDIR(mode.st_mode):
                with os.scandir(path) as children:
                    for child in children:
                        if len(pending) >= remaining or time.monotonic() >= deadline:
                            return None
                        if (
                            not child.is_symlink()
                            and not getattr(Path(child.path), "is_junction", lambda: False)()
                        ):
                            pending.append(Path(child.path))
        return total if not pending else None
    except (OSError, GoalStoreError):
        return None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )


def presence(record: GoalRecord, project_root: Path) -> tuple[str, str]:
    """Observe repository/admin identity and the exact local ref; uncertainty is explicit."""
    try:
        identity = resolve_worktree_identity(project_root)
        if identity is None or identity.repository != record.worktree.repository:
            return "unavailable", "unavailable"
        ref = (
            record.branch
            if record.branch.startswith("refs/heads/")
            else "refs/heads/" + record.branch
        )
        if _git(project_root, "check-ref-format", ref).returncode:
            return "unavailable", "unavailable"
        found = _git(project_root, "show-ref", "--verify", "--quiet", ref).returncode
        branch = {0: "present", 1: "missing"}.get(found, "unavailable")
        worktree = _worktree_presence(record, project_root)
        return worktree, branch
    except (OSError, ValueError, GoalStoreError, subprocess.SubprocessError):
        return "unavailable", "unavailable"


def _worktree_presence(record: GoalRecord, project_root: Path) -> str:
    # Git registration, not the captured pathname, owns checkout identity.
    entries = list_worktrees(project_root)
    if len(entries) > 1000:
        return "unavailable"
    unknown = False
    for entry in entries:
        try:
            if resolve_worktree_identity(entry.path) == record.worktree:
                return "present"
        except (OSError, ValueError, GoalStoreError):
            unknown = True
    saved = Path(record.worktree_path)
    try:
        saved.lstat()
    except FileNotFoundError:
        return "unavailable" if unknown else "missing"
    except OSError:
        return "unavailable"
    return "unavailable"


def load_record(store: GoalStore, record_id: str) -> GoalRecord:
    """Validate ID and metadata confinement before decoding existing storage."""
    paths = record_paths(store.booley_dir, validate_goal_id(record_id))
    for path in (paths.root, paths.record_file, paths.root / "goal-names.json"):
        unlinked(path, store.booley_dir)
    return store.load(record_id)


def _display_worktree(store: GoalStore, record: GoalRecord) -> Path:
    try:
        return archival_path(store.booley_dir, Path(record.worktree_path), record_id=record.id)
    except BooleyLayoutError as exc:
        raise GoalStoreError(f"unavailable migrated record paths: {exc}") from exc


def _outcome_metadata(root: Path) -> None:
    acceptance = root / "logs" / "acceptance"
    unlinked(acceptance, root)
    deadline = time.monotonic() + SCAN_SECONDS
    remaining = SCAN_ENTRIES
    for directory, dirs, files in os.walk(acceptance, followlinks=False):
        remaining -= 1 + len(dirs) + len(files)
        if remaining < 0 or time.monotonic() >= deadline:
            raise GoalStoreError("retained evidence scan unavailable/incomplete")
        for name in [*dirs, *files]:
            unlinked(Path(directory) / name, root)


def summary(store: GoalStore, root: Path, record: GoalRecord) -> dict[str, Any]:
    """Project the stable machine envelope without asserting current evidence freshness."""
    worktree, branch = presence(record, root)
    paths = record_paths(store.booley_dir, record.id)
    return {
        "record_id": record.id,
        "state": record.state.value,
        "entered_at": record.entered_at,
        "ended_at": record.ended_at,
        "worktree_path": str(_display_worktree(store, record)),
        "branch": record.branch,
        "worktree_presence": worktree,
        "branch_presence": branch,
        "size_bytes": logical_bytes(paths.root),
    }


def _inventory_entries(directory: Path, diagnostics: list[str]) -> list[Path]:
    entries: list[Path] = []
    deadline = time.monotonic() + SCAN_SECONDS
    with os.scandir(directory) as children:
        for child in children:
            if len(entries) >= SCAN_ENTRIES or time.monotonic() >= deadline:
                diagnostics.append("Goal inventory scan incomplete: entry/time budget exceeded")
                break
            entries.append(Path(child.path))
    return entries


def list_records(store: GoalStore, root: Path) -> dict[str, Any]:
    """List every retained lifecycle state, with corrupt/pending entries named separately."""
    directory = goals_root(store.booley_dir)
    records: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    if directory.exists():
        entries = _inventory_entries(directory, diagnostics)
        for entry in sorted(entries):
            if GOAL_ID_PATTERN.fullmatch(entry.name):
                try:
                    records.append(summary(store, root, load_record(store, entry.name)))
                except (OSError, ValueError, GoalStoreError) as exc:
                    diagnostics.append(f"corrupt Goal Record {entry.name}: {exc}")
            elif entry.name.startswith(".pruning-"):
                size = logical_bytes(entry) if entry.is_dir() else None
                record_id = entry.name.removeprefix(".pruning-").removesuffix(".json")
                diagnostics.append(
                    f"pending cleanup {record_id}: retained path {entry}, remaining bytes {size if size is not None else 'unavailable'}; resume booley goal prune {record_id} --confirm"
                )
    return {"records": records, "diagnostics": diagnostics}


def _package(store: GoalStore, record: GoalRecord) -> tuple[Path, dict[str, Any]]:
    paths = record_paths(store.booley_dir, record.id)
    path = (
        paths.review_package_file
        if record.finish_operation is None
        else paths.root / "operations" / record.finish_operation / "review-package.json"
    )
    unlinked(path, paths.root)
    data = GoalCompletionPackage.from_json(json.loads(path.read_bytes())).to_json()
    if (
        data["record"]["id"] != record.id
        or data["record"]["worktree"] != record.worktree.to_json()
        or data["branch"] != record.branch
        or not record.package_digest
        or record.package_digest != "sha256:" + digest(data)
    ):
        raise GoalStoreError("completion package differs from frozen Goal facts")
    if record.finish_operation and data["record"].get("finish_operation") not in {
        None,
        record.finish_operation,
    }:
        raise GoalStoreError("completion package differs from successful operation")
    return path, data


def _goals(
    store: GoalStore, record: GoalRecord
) -> tuple[list[dict[str, Any]], Path | None, list[str]]:
    names = {row.spec.key: goal_presentation(row.spec).name for row in record.goals}
    if record.state is GoalState.FINISHED:
        path, package = _package(store, record)
        return (
            [
                dict(
                    row,
                    goal_key=row["key"],
                    name=names.get(row["key"], row["key"]),
                    status="passing",
                    freshness_asserted=False,
                )
                for row in package["goals"]
            ],
            path,
            [],
        )
    paths = record_paths(store.booley_dir, record.id)
    unlinked(paths.state_file, paths.root)
    unlinked(paths.logs_dir, paths.root)
    _outcome_metadata(paths.root)
    state = load_goal_state(store, record)
    diagnostics = (
        [] if paths.state_file.exists() else ["no retained evidence; declared unmet Goals"]
    )
    rows = [
        {
            "goal_key": row.spec.key,
            "name": names[row.spec.key],
            "status": "passing" if state.criteria[row.spec.key].met else "unmet",
            "evidence": state.criteria[row.spec.key].detail,
            "freshness_asserted": False,
        }
        for row in record.goals
    ]
    return rows, None, diagnostics


def show_record(store: GoalStore, root: Path, record_id: str) -> dict[str, Any]:
    """Read a selected retained snapshot, never scheduling or reconciling evidence."""
    record = load_record(store, record_id)
    paths = record_paths(store.booley_dir, record.id)
    try:
        goals, package, diagnostics = _goals(store, record)
    except (OSError, ValueError, KeyError, GoalStateError) as exc:
        raise GoalStoreError(f"unavailable retained Goal outcomes for {record.id}: {exc}") from exc
    operation_id = (
        record.finish_operation if record.state is GoalState.FINISHED else record.abandon_operation
    )
    operation = (
        None
        if operation_id is None
        else {
            "kind": "finish" if record.state is GoalState.FINISHED else "abandon",
            "operation_id": operation_id,
            "path": str(paths.root / "operations" / operation_id),
        }
    )
    artifacts = {
        name: {"path": str(path), "exists": path.exists()}
        for name, path in {
            "logs": paths.logs_dir,
            "raw_transcripts": paths.runtime_dir / "transcripts",
            "rendered_transcripts": paths.logs_dir / "transcripts",
        }.items()
    }
    if package is not None:
        artifacts["review_package"] = {"path": str(package), "exists": True}
    if record.end_instruction_quote:
        diagnostics.append("Abandonment instruction: " + record.end_instruction_quote)
    diagnostics.insert(0, "recorded outcomes; current checkout freshness is not asserted")
    return {
        "record": summary(store, root, record),
        "goals": goals,
        "operation": operation,
        "artifacts": artifacts,
        "diagnostics": diagnostics,
    }
