"""Owned VS Code Dashboard task reconciliation, CAS publication and conditional rollback."""

from __future__ import annotations

import contextlib
import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.goals import parse_dashboard
from booley.core.boundary import require_dict
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
    remove_vscode: Path | None = None

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
    return parse_dashboard(data)


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


@dataclass(frozen=True)
class _TaskFiles:
    root: Path
    project_dir: Path
    before: bytes | None
    ownership: bytes | None
    owner: dict[str, Any]

    @property
    def task_path(self) -> Path:
        return self.root / ".vscode/tasks.json"

    @property
    def owner_path(self) -> Path:
        return self.project_dir / "runtime/dashboard-task.json"


def _inspect(root: Path, project_dir: Path) -> TaskPlan:
    folder = root / ".vscode"
    ownership = _bytes(project_dir / "runtime/dashboard-task.json")
    active = enabled(project_dir)
    if not active and ownership is None:
        return TaskPlan()
    owner = (
        {}
        if ownership is None
        else require_dict(json.loads(ownership), field="Dashboard task ownership")
    )
    if owner and (owner.get("schema") != 1 or owner.get("root") != str(root.resolve())):
        raise ValueError("unsupported or foreign Dashboard task ownership")
    files = _TaskFiles(root, project_dir, _bytes(folder / "tasks.json"), ownership, owner)
    desired = active and not _opt_out(folder)
    if files.before is None and not desired:
        return _plan_changes(files, "", False, RAW_TASK) if owner else TaskPlan()
    if files.before is None and owner.get("enabled", False) and desired:
        raise ValueError("user-removed Dashboard task preserved; disable then enable to restore")
    document = Document(
        files.before.decode()
        if files.before is not None
        else '{"version": "2.0.0", "tasks": []}\n'
    )
    return _edit_task(files, document, desired)


def _edit_task(files: _TaskFiles, document: Document, desired: bool) -> TaskPlan:
    tasks, matching = _matching_tasks(document)
    if len(matching) > 1:
        raise ValueError("duplicate Dashboard task labels preserved")
    formatted = document.task_text(TASK, tasks)
    raw_task = formatted.lstrip(" \t")
    if matching:
        raw = document.source[matching[0].start : matching[0].end]
        if not files.owner or raw != files.owner.get("task"):
            raise ValueError("unowned or user-edited Dashboard task preserved")
        if desired:
            if matching[0].value == TASK:
                raw_task = raw
            after = (
                document.source[: matching[0].start]
                + raw_task
                + document.source[matching[0].end :]
            )
        else:
            insertion = files.owner.get("insertion", "")
            if insertion and document.source.count(insertion) == 1:
                after = document.source.replace(insertion, "", 1)
            elif insertion:
                raise ValueError("user-edited Dashboard task insertion preserved")
            else:
                after = document.remove(tasks, matching[0])
    elif desired:
        if files.owner.get("enabled", False):
            raise ValueError(
                "user-removed Dashboard task preserved; disable then enable to restore"
            )
        after = document.append(tasks, formatted) if tasks else document.add_tasks(formatted)
    else:
        return TaskPlan()
    return _plan_changes(files, after, desired, raw_task)


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


def _inserted_span(before: str, after: str) -> str:
    start = next(
        (i for i, (a, b) in enumerate(zip(before, after, strict=False)) if a != b),
        min(len(before), len(after)),
    )
    suffix = 0
    for a, b in zip(reversed(before[start:]), reversed(after[start:]), strict=False):
        if a != b:
            break
        suffix += 1
    return after[start : len(after) - suffix if suffix else len(after)]


def _plan_changes(files: _TaskFiles, after: str, desired: bool, raw_task: str) -> TaskPlan:
    root, owner, before = files.root, files.owner, files.before
    folder = root / ".vscode"
    created = owner.get("created_vscode", not folder.exists())
    rendered: bytes | None = after.encode()
    if not desired and (before is None or before == owner.get("created_document", "").encode()):
        rendered = None
    changes = [] if rendered == before else [FileChange(files.task_path, before, rendered)]
    insertion = owner.get("insertion", "")
    if desired:
        insertion = (
            insertion.replace(owner.get("task", raw_task), raw_task)
            if insertion and owner.get("enabled")
            else _inserted_span(
                before.decode() if before is not None else '{"version": "2.0.0", "tasks": []}\n',
                after,
            )
        )
    new_owner: dict[str, Any] = {
        "schema": 1,
        "enabled": desired,
        "root": str(root.resolve()),
        "task": raw_task,
        "created_vscode": created,
        "insertion": insertion,
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
    if owner_after != files.ownership:
        changes.append(FileChange(files.owner_path, files.ownership, owner_after))
    return TaskPlan(
        tuple(changes),
        created_vscode=bool(created and not folder.exists()),
        remove_vscode=folder if created and not desired else None,
    )


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
            if self.plan.remove_vscode is not None:
                with contextlib.suppress(OSError):
                    self.plan.remove_vscode.rmdir()
        except (OSError, ValueError):
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
        try:
            with lock.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
                transaction.apply()
        except BlockingIOError:
            transaction = TaskTransaction(
                TaskPlan(diagnostics=("Dashboard task writer busy; reconciliation skipped",))
            )
            transaction.apply()
    else:
        transaction.apply()  # Report conflicts even when no safe mutation is possible.
    return transaction
