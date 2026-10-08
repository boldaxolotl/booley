"""Owned VS Code Dashboard task reconciliation, CAS publication and conditional rollback."""

from __future__ import annotations

import contextlib
import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import require_bool, require_dict
from booley.core.file_lock import nonblocking_file_lock
from booley.goals.preview import goal_mode_preview_enabled
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.jsonc import Document, Node
from booley.runtime.project_repositories import git_directories
from booley.runtime.safe_storage import refuse_symlinks

logger = logging.getLogger(__name__)
LABEL = "Booley Dashboard"
TASK = {
    "label": LABEL,
    "type": "shell",
    "command": "booley dashboard",
    "options": {"env": {"BOOLEY_GOAL_MODE_PREVIEW": "1"}},
    "runOptions": {"runOn": "folderOpen", "instanceLimit": 1},
    "presentation": {"reveal": "always", "panel": "dedicated", "clear": False},
    "problemMatcher": [],
}
RAW_TASK = json.dumps(TASK, indent=2)
EXCLUDE = b"# Booley Dashboard (owned directory)\n/.vscode\n"


@dataclass(frozen=True)
class FileChange:
    """Exact predecessor and replacement; rollback never overwrites a concurrent edit."""

    path: Path
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True)
class TaskPlan:
    """An inspectable, file-write-free reconciliation plan."""

    changes: tuple[FileChange, ...] = ()
    diagnostics: tuple[str, ...] = ()
    created_vscode: bool = False

    @property
    def pending(self) -> bool:
        """Whether safe owned content needs reconciliation."""
        return bool(self.changes)


def enabled(project_dir: Path) -> bool:
    """Preview plus strict [sandbox].dashboard (default true)."""
    if not goal_mode_preview_enabled():
        return False
    path = project_dir / "booley.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return require_bool(
        require_dict(data.get("sandbox", {}), field="[sandbox]"), "dashboard", default=True
    )


def _bytes(path: Path) -> bytes | None:
    refuse_symlinks(path)
    if not path.exists():
        return None
    if path.stat().st_size > 2 * 1024 * 1024:
        raise OSError(f"Dashboard task storage exceeds 2 MiB: {path}")
    return path.read_bytes()


def _opt_out(folder: Path) -> bool:
    path = folder / "settings.json"
    content = _bytes(path)
    return (
        content is not None
        and Document(content.decode()).root.value.get("task.allowAutomaticTasks") == "off"
    )


def inspect(root: Path, project_dir: Path) -> TaskPlan:
    """Refuse conflicts safely; preserve malformed, linked, unowned or user-edited content."""
    try:
        return _inspect(root, project_dir)
    except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
        return TaskPlan(diagnostics=(f"Dashboard task unavailable: {exc}",))


def _inspect(root: Path, project_dir: Path) -> TaskPlan:
    folder = root / ".vscode"
    task_path = folder / "tasks.json"
    owner_path = project_dir / "runtime" / "dashboard-task.json"
    ownership = _bytes(owner_path)
    active = enabled(project_dir)
    if not active and ownership is None:
        return TaskPlan()
    before = _bytes(task_path)
    owner = (
        {}
        if ownership is None
        else require_dict(json.loads(ownership), field="Dashboard task ownership")
    )
    if owner and (owner.get("schema") != 1 or owner.get("root") != str(root.resolve())):
        raise ValueError("unsupported or foreign Dashboard task ownership")
    desired = active and not _opt_out(folder)
    if before is None and not desired:
        return (
            _plan_changes(root, task_path, owner_path, before, ownership, owner, "", False)
            if owner
            else TaskPlan()
        )
    if before is None and owner.get("enabled", False) and desired:
        raise ValueError("user-removed Dashboard task preserved; disable then enable to restore")
    document = Document(
        before.decode() if before is not None else '{"version": "2.0.0", "tasks": []}\n'
    )
    tasks, matching = _matching_tasks(document)
    if len(matching) > 1:
        raise ValueError("duplicate Dashboard task labels preserved")
    if matching:
        raw = document.source[matching[0].start : matching[0].end]
        if not owner or raw != owner.get("task"):
            raise ValueError("unowned or user-edited Dashboard task preserved")
        after = (
            document.source[: matching[0].start] + RAW_TASK + document.source[matching[0].end :]
            if desired
            else document.remove(tasks, matching[0])
        )
    elif desired:
        if owner.get("enabled", False):
            raise ValueError(
                "user-removed Dashboard task preserved; disable then enable to restore"
            )
        after = document.append(tasks, RAW_TASK) if tasks else document.add_tasks(RAW_TASK)
    else:
        return TaskPlan()
    return _plan_changes(root, task_path, owner_path, before, ownership, owner, after, desired)


def _matching_tasks(document: Document) -> tuple[Node | None, list[Node]]:
    tasks = document.root.members.get("tasks")
    if tasks is not None and not isinstance(tasks.value, list):
        raise ValueError("tasks must be an array")
    return tasks, (
        []
        if tasks is None
        else [
            node
            for node in tasks.children
            if isinstance(node.value, dict) and node.value.get("label") == LABEL
        ]
    )


def _plan_changes(
    root: Path,
    task_path: Path,
    owner_path: Path,
    before: bytes | None,
    ownership: bytes | None,
    owner: dict[str, Any],
    after: str,
    desired: bool,
) -> TaskPlan:
    folder = root / ".vscode"
    created = owner.get("created_vscode", not folder.exists())
    changes = []
    rendered = after.encode()
    if not desired and (before is None or before == owner.get("created_document", "").encode()):
        rendered = None
    if rendered != before:
        changes.append(FileChange(task_path, before, rendered))
    new_owner: dict[str, Any] = {
        "schema": 1,
        "enabled": desired,
        "root": str(root.resolve()),
        "task": RAW_TASK,
        "created_vscode": created,
        "created_document": (
            after
            if before is None or before == owner.get("created_document", "").encode()
            else owner.get("created_document", "")
        ),
    }
    new_owner["exclude_suffix"] = _exclude_change(root, bool(created), desired, owner, changes)
    new_owner["exclude_created"] = owner.get("exclude_created", False) or any(
        change.path.name == "exclude" and change.before is None for change in changes
    )
    owner_after = (
        json.dumps(new_owner if desired else {**owner, "enabled": False}, sort_keys=True) + "\n"
    ).encode()
    if owner_after != ownership:
        changes.append(FileChange(owner_path, ownership, owner_after))
    return TaskPlan(tuple(changes), created_vscode=bool(created and not folder.exists()))


def _exclude_change(
    root: Path, created: bool, desired: bool, owner: dict[str, Any], changes: list[FileChange]
) -> str:
    suffix = owner.get("exclude_suffix", "")
    if not created:
        return ""
    path = git_directories(root).common_dir / "info/exclude"
    before = _bytes(path)
    content = before or b""
    if desired and b"/.vscode" not in content.splitlines():
        addition = (b"\n" if content and not content.endswith(b"\n") else b"") + EXCLUDE
        after, suffix = content + addition, addition.decode()
    elif not desired and suffix and content.endswith(suffix.encode()):
        after = content[: -len(suffix.encode())]
        if not after and owner.get("exclude_created", False):
            after = None
    else:
        return suffix
    changes.append(FileChange(path, before, after))
    return suffix


class TaskTransaction:
    """Lock serializes Booley writers; byte comparisons protect intervening user edits."""

    def __init__(self, plan: TaskPlan) -> None:
        self.plan = plan
        self.applied: list[FileChange] = []
        self.directories: list[Path] = []

    def apply(self) -> bool:
        """Publish the plan or restore only the bytes this transaction still owns."""
        for diagnostic in self.plan.diagnostics:
            logger.warning(diagnostic)
        if not self.plan.changes:
            return False
        try:
            for change in self.plan.changes:
                if _bytes(change.path) != change.before:
                    raise OSError(f"Dashboard task changed since inspection: {change.path}")
            for change in self.plan.changes:
                self._publish(change)
        except Exception:
            self.rollback()
            raise
        return True

    def _publish(self, change: FileChange) -> None:
        missing = [parent for parent in change.path.parents if not parent.exists()]
        if _bytes(change.path) != change.before:
            raise OSError(f"Dashboard task concurrent edit: {change.path}")
        if change.after is None:
            change.path.unlink(missing_ok=True)
        else:
            atomic_replace_bytes(change.path, change.after, mode=0o644)
        self.applied.append(change)
        self.directories.extend(reversed(missing))

    def rollback(self) -> None:
        """Restore predecessors conditionally; concurrent user changes survive failure."""
        for change in reversed(self.applied):
            try:
                if _bytes(change.path) != change.after:
                    logger.warning("Dashboard rollback preserved concurrent edit: %s", change.path)
                    continue
                if change.before is None:
                    change.path.unlink(missing_ok=True)
                else:
                    atomic_replace_bytes(change.path, change.before, mode=0o644)
            except OSError:
                logger.warning("Dashboard rollback storage unavailable: %s", change.path)
        for directory in reversed(self.directories):
            with contextlib.suppress(OSError):
                directory.rmdir()
        self.applied.clear()


def reconcile(root: Path, project_dir: Path) -> TaskTransaction:
    """Inspect and apply within a bounded, nonblocking writer lock."""
    plan = inspect(root, project_dir)
    transaction = TaskTransaction(plan)
    if plan.pending:
        lock = project_dir / "runtime" / "dashboard-task.lock"
        refuse_symlinks(lock)
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
            transaction.apply()
    else:
        transaction.apply()  # Report conflicts even when no safe mutation is possible.
    return transaction
