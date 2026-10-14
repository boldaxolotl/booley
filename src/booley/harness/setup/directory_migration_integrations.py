"""Plan exact rewrites of previously generated integrations before the layout moves."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from booley.core.booley_layout import (
    BOOLEY_DIR_NAME,
    RETIRED_DIR_NAME,
    RETIRED_ENV,
    BooleyLayoutError,
)
from booley.harness.setup.project_git_hook_bundle import BUNDLE_NAME, build_project_git_hook_bundle
from booley.runtime.directory_migration_git import edit, git


def _tracked(checkout: Path, path: Path) -> bool:
    if not (checkout / ".git").exists():
        return False
    return bool(git(checkout, "ls-files", "--", str(path.relative_to(checkout))))


def _rewrite(path: Path, moves: list[dict[str, Any]]) -> dict[str, Any] | None:
    before = path.read_bytes()
    after = re.sub(
        rb"(?<![A-Za-z0-9_.-])" + re.escape(RETIRED_DIR_NAME.encode()) + rb"(?=$|[/\\\"'\s])",
        BOOLEY_DIR_NAME.encode(),
        before,
    )
    after = re.sub(
        rb"(?<![A-Za-z0-9_])" + RETIRED_ENV.encode() + rb"(?![A-Za-z0-9_])", b"BOOLEY_DIR", after
    )
    return edit(path, after, moves) if after != before else None


def _sandbox(checkout: Path, moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = checkout / ".devcontainer/devcontainer.json"
    if not path.is_file():
        return []
    document = json.loads(path.read_bytes())
    row = _rewrite(path, moves)
    if row is None:
        return []
    if not str(document.get("name", "")).startswith("Booley Interactive (") or _tracked(
        checkout, path
    ):
        raise BooleyLayoutError(
            f"cannot regenerate user-owned/tracked Sandbox specification: {path}"
        )
    if path.is_symlink():
        raise BooleyLayoutError(f"Sandbox specification is linked: {path}")
    return [row]


def _bundle(directory: Path, moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = directory / ".managed" / BUNDLE_NAME
    if not path.exists():
        return []
    if path.is_symlink():
        raise BooleyLayoutError(f"managed bundle is linked: {path}")
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != 1 or "booley_commit_policy.py" not in archive.namelist():
            raise BooleyLayoutError(f"unrecognized Project policy bundle: {path}")
    content = build_project_git_hook_bundle().content
    if content == path.read_bytes():
        return []
    if _tracked(directory, path):
        raise BooleyLayoutError(f"tracked managed bundle requires human reconciliation: {path}")
    return [edit(path, content, moves)]


def _outputs(checkout: Path, directory: Path, moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    stamp = directory / "runtime/doctor_stamp.json"
    if stamp.is_file():
        if stamp.is_symlink() or _tracked(checkout, stamp):
            raise BooleyLayoutError(f"Doctor stamp is not private regular storage: {stamp}")
        result.append(edit(stamp, b"{}\n", moves))
    projections = [*checkout.glob(".booley-projected-*.core")]
    projections.extend((directory / "tmp/fusesoc-isolated-cores").glob("booley-isolated-*.core"))
    for path in projections:
        if not path.is_file():
            continue
        if (
            not path.read_bytes().startswith(b"CAPI=2:")
            or b"# Booley stealth core projection:" not in path.read_bytes()
        ):
            continue
        proposed = _rewrite(path, moves)
        if proposed:
            if path.is_symlink() or _tracked(checkout, path):
                raise BooleyLayoutError(f"projected core is linked or tracked: {path}")
            result.append(proposed)
    return result


def plan_generated_integrations(
    root: Path, rows: list[dict[str, Any]], moves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Capture old owned output bytes; neither planning nor recovery runs user hooks."""
    result = []
    checkouts = {root, *(Path(row["path"]) for row in rows if not row.get("missing"))}
    for checkout in checkouts:
        result.extend(_sandbox(checkout, moves))
        directory = checkout if checkout.name == RETIRED_DIR_NAME else checkout / RETIRED_DIR_NAME
        result.extend(_bundle(directory, moves))
        result.extend(_outputs(checkout, directory, moves))
    for row in rows:
        if row.get("missing"):
            continue
        for name in ("commit-msg", "pre-push"):
            path = Path(row["admin"]) / "hooks" / name
            if not path.is_file():
                continue
            content = path.read_bytes()
            if b"# Managed bundle: project-git-hooks.pyz;" in content and b"# Booley " in content:
                if path.is_symlink():
                    raise BooleyLayoutError(f"managed hook is linked: {path}")
                proposed = _rewrite(path, moves)
                if proposed:
                    result.append(proposed)
    return result
