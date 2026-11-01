"""Provider-independent workspace isolation for read-only Specialists."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from booley.agent_workspace.isolation import (
    WorkspaceSnapshot,
    hide_opposite_sources,
    owned_copy_roots,
)
from booley.core.models import AgentCallParams, AgentResult
from booley.runtime.owned_scratch import owned_tempdir

WorkspaceAccess = Literal["read_only", "read_write"]


def _run_git(args: list[str], cwd: Path, *, text: bool = True) -> str | bytes:
    """Run a bounded git plumbing command or fail with its diagnostic."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def _git_root(cwd: Path) -> Path:
    output = _run_git(["rev-parse", "--show-toplevel"], cwd)
    assert isinstance(output, str)
    return Path(output.strip()).resolve()


def _visible_paths(root: Path) -> list[Path]:
    """Return tracked and ordinary untracked paths in the current worktree."""
    output = _run_git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        root,
        text=False,
    )
    assert isinstance(output, bytes)
    return [Path(os.fsdecode(raw)) for raw in output.split(b"\0") if raw]


def _safe_symlink_target(
    source: Path,
    real_root: Path,
    snapshot_root: Path,
) -> Path | None:
    """Map an in-worktree symlink target into the snapshot."""
    try:
        relative = source.resolve(strict=False).relative_to(real_root)
    except ValueError:
        return None
    return snapshot_root / relative


def _copy_path(
    source: Path,
    destination: Path,
    real_root: Path,
    snapshot_root: Path,
) -> None:
    """Copy one git-visible path without following symlinks."""
    if not source.parent.resolve().is_relative_to(real_root):
        raise ValueError(f"source parent escapes Project Root: {source.relative_to(real_root)}")
    if not source.exists() and not source.is_symlink():
        return  # tracked deletion in the live worktree
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target = _safe_symlink_target(source, real_root, snapshot_root)
        if target is not None:
            destination.symlink_to(target, target_is_directory=source.is_dir())
    elif source.is_dir():
        destination.mkdir(exist_ok=True)
        for child in source.iterdir():
            if child.name != ".git":
                _copy_path(child, destination / child.name, real_root, snapshot_root)
        shutil.copystat(source, destination, follow_symlinks=False)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def _populate_snapshot(real_root: Path, snapshot_root: Path) -> None:
    for relative in _visible_paths(real_root):
        _copy_path(
            real_root / relative,
            snapshot_root / relative,
            real_root,
            snapshot_root,
        )


def _initialize_snapshot_git(snapshot_root: Path) -> None:
    """Record only the sources the agent is allowed to read."""
    _run_git(["init", "--quiet"], snapshot_root)
    _run_git(["add", "--all"], snapshot_root)
    _run_git(
        [
            "-c",
            "user.name=Booley",
            "-c",
            "user.email=booley@localhost",
            # Keep Git from spawning detached auto-maintenance that would still hold
            # files inside the snapshot (objects/maintenance.lock) while it is removed.
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "read-only specialist snapshot",
        ],
        snapshot_root,
    )


def _replace_path(value: Any, old: str, new: str) -> Any:
    """Replace workspace paths recursively in agent-facing data."""
    if isinstance(value, str):
        # Agent output can embed Windows paths inside serialized JSON.
        escaped_old = json.dumps(old)[1:-1]
        escaped_new = json.dumps(new)[1:-1]
        return value.replace(escaped_old, escaped_new).replace(old, new)
    if isinstance(value, list):
        return [_replace_path(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_path(item, old, new) for key, item in value.items()}
    return value


def restore_result_paths(result: AgentResult, snapshot: WorkspaceSnapshot) -> AgentResult:
    """Translate disposable absolute paths back to their real-worktree form."""
    old = str(snapshot.snapshot_root)
    new = str(snapshot.real_root)
    return replace(
        result,
        output=_replace_path(result.output, old, new),
        structured=_replace_path(result.structured, old, new),
        captured_agent_capability_calls=_replace_path(
            result.captured_agent_capability_calls, old, new
        ),
    )


def _project_category_paths(
    project_root: Path, repository_root: Path, scope: tuple[str, ...]
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Resolve Project-owned roles, then express them in the copied Git tree."""
    from booley.fusesoc.fusesoc_registry import canonical_project_path, classified_sources

    sources = classified_sources(project_root, strict=True)
    prefix = project_root.relative_to(repository_root)
    category_files = {
        category: tuple((prefix / path).as_posix() for path in paths)
        for category, paths in (
            ("rtl", sources.rtl_source_files),
            ("tb", sources.tb_files),
        )
    }
    return category_files, tuple(
        (prefix / canonical_project_path(project_root, path)).as_posix() for path in scope
    )


@contextmanager
def isolated_agent_workspace(
    params: AgentCallParams,
    access: WorkspaceAccess,
    category: str | None = None,
    *,
    scope: tuple[str, ...] = (),
    excluded_roots: tuple[Path, ...] = (),
) -> Iterator[tuple[AgentCallParams, WorkspaceSnapshot | None]]:
    """Point a read-only agent call at a category-isolated disposable copy."""
    if access == "read_write":
        yield params, None
        return
    if access != "read_only":
        raise ValueError(f"unknown Specialist workspace_access {access!r}")

    real_cwd = Path(params.cwd).resolve()
    real_root = _git_root(real_cwd)
    category_files = None
    if category is not None:
        category_files, scope = _project_category_paths(real_cwd, real_root, scope)
        excluded_roots = (*owned_copy_roots(real_cwd), *excluded_roots)
    relative_cwd = real_cwd.relative_to(real_root)
    with owned_tempdir("booley-specialist-ro-") as owned:
        # The owner marker sits beside the snapshot, invisible to the agent and Git.
        snapshot_root = owned.resolve() / "workspace"
        snapshot_root.mkdir()
        snapshot = WorkspaceSnapshot(real_root, snapshot_root)
        _populate_snapshot(real_root, snapshot.snapshot_root)
        if category is not None:
            assert category_files is not None
            hide_opposite_sources(
                snapshot,
                category,
                category_files=category_files,
                scope=scope,
                excluded_roots=excluded_roots,
                cwd=snapshot.snapshot_root / relative_cwd,
            )
        _initialize_snapshot_git(snapshot.snapshot_root)
        old = str(real_root)
        new = str(snapshot.snapshot_root)
        isolated = replace(
            params,
            cwd=snapshot.snapshot_root / relative_cwd,
            prompt=_replace_path(params.prompt, old, new),
            system_prompt=_replace_path(params.system_prompt, old, new),
        )
        yield isolated, snapshot
