"""Init-only, journaled migration of the Booley Directory and its Git graph."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from booley.core.booley_layout import (
    BOOLEY_DIR_NAME,
    RETIRED_DIR_NAME,
    BooleyLayoutError,
    Layout,
    classify_layout,
    ensure_layout_metadata,
    git_common_directory,
    layout_metadata,
    validate_environment,
)
from booley.core.checkout_role import require_project_checkout
from booley.core.project_identity import matches_project_label
from booley.runtime.atomic_files import atomic_replace_bytes, fsync_directory
from booley.runtime.directory_migration_git import (
    edit,
    mapped,
    pointer_edits,
    primary_checkout,
    registrations,
    verify_registrations,
)
from booley.runtime.file_lock import release_file_lock, wait_for_file_lock

_INDEX = ".directory-relocation.json"
_JOURNAL = "directory-migration.json"
_SCHEMA = "booley-directory-migration/1"
IntegrationPlanner = Callable[
    [Path, list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]
]


@dataclass(frozen=True)
class Migration:
    """Read-only preflight facts, sufficient to resume every persisted transition."""

    root: Path
    journal: Path
    document: dict[str, Any]
    observe: Callable[[Path], None]
    integrations: IntegrationPlanner


def metadata_root(root: Path) -> Path:
    """Stable storage shared with consumer admission and pending-layout checks."""
    return layout_metadata(root)


def _require_host_migration() -> None:
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        raise BooleyLayoutError(
            "directory migration must run on the host; run `booley init` from the primary Project Root there"
        )


def _identity(path: Path) -> list[int]:
    value = path.stat()
    return [value.st_dev, value.st_ino]


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _move(source: Path, destination: Path) -> dict[str, Any]:
    _unlinked(source)
    _unlinked(destination.parent)
    if source.is_symlink() or not source.is_dir():
        raise BooleyLayoutError(f"unsafe migration source: {source}")
    if _exists(destination):
        raise BooleyLayoutError(f"both migration paths exist: {source} and {destination}")
    ancestor = destination.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    for parent in (source.parent, ancestor):
        if not parent.stat().st_mode & 0o222 or not os.access(parent, os.W_OK):
            raise BooleyLayoutError(f"migration parent is not writable: {parent}")
    if source.stat().st_dev != ancestor.stat().st_dev:
        raise BooleyLayoutError(f"migration must stay on one filesystem: {source}")
    return {"source": str(source), "destination": str(destination), "identity": _identity(source)}


def _capture(
    root: Path, observe: Callable[[Path], None], integrations: IntegrationPlanner
) -> dict[str, Any]:
    old = root / RETIRED_DIR_NAME
    observe(old)
    _require_host_migration()
    moves, rows = _participants(root, observe)
    from booley.runtime.worktree_relocation import WorktreeRelocationError, _validate_source

    for row in rows:
        if not row.get("missing") and Path(row["path"]) != root:
            try:
                _validate_source(Path(row["path"]))
            except WorktreeRelocationError as exc:
                raise BooleyLayoutError(
                    f"cannot move {row['path']}: {str(exc).replace('deinit -f --all', 'deinit --all')}. "
                    "Preserve local submodule changes; "
                    "use non-forced `git submodule deinit --all` only after reviewing them"
                ) from exc
    _refuse_live_sandboxes(root, moves, rows)
    edits = pointer_edits(rows, moves) + _integration_edits(root, rows, moves)
    edits += integrations(root, rows, moves)
    return {
        "schema": _SCHEMA,
        "root": str(root),
        "identity": _identity(root),
        "moves": moves,
        "registrations": rows,
        "edits": _deduplicate(edits),
        "records": _record_hashes(old),
        "phase": "prepared",
    }


def _participants(
    root: Path, observe: Callable[[Path], None]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    old, new = root / RETIRED_DIR_NAME, root / BOOLEY_DIR_NAME
    common = git_common_directory(root)
    if common is not None and common.is_relative_to(old):
        raise BooleyLayoutError(
            "RTL Git metadata would move; preserve and relocate it outside the Booley Directory before init"
        )
    moves = [_move(old, new)]
    rows = registrations(root) if (root / ".git").exists() else []
    if (root / ".git").is_file():
        primary = primary_checkout(root)
        if primary != root:
            raise BooleyLayoutError(f"run `booley init` from the primary Project Root: {primary}")
    if (old / ".git").exists():
        rows += registrations(old)
    for row in rows:
        checkout = Path(row["path"])
        nested = checkout / RETIRED_DIR_NAME
        if checkout == root or not _exists(nested):
            continue
        classify_layout(checkout)
        observe(nested)
        destination = mapped(checkout, moves) / BOOLEY_DIR_NAME
        moves.append(_move(nested, destination))
    return moves, rows


def _deduplicate(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = {}
    for row in edits:
        if row["path"] in result and row != result[row["path"]]:
            raise BooleyLayoutError(f"conflicting migration edits: {row['path']}")
        result[row["path"]] = row
    return list(result.values())


def _record_hashes(directory: Path) -> dict[str, str]:
    return {
        path.parent.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (directory / "goals").glob("*/record.json")
    }


def _integration_edits(
    root: Path, rows: list[dict[str, Any]], moves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rewrite only generated integrations and exact managed exclude entries."""
    paths = {root, *(Path(row["path"]) for row in rows if not row.get("missing"))}
    edits = []
    ignores = {checkout / ".gitignore" for checkout in paths}
    ignores.update(Path(row["common"]) / "info/exclude" for row in rows if not row.get("missing"))
    for path in ignores:
        if path.is_file():
            before = path.read_bytes()
            old, new = RETIRED_DIR_NAME.encode(), BOOLEY_DIR_NAME.encode()
            after = b"".join(
                line.replace(old, new) if line.strip().rstrip(b"/") in {old, b"/" + old} else line
                for line in before.splitlines(keepends=True)
            )
            if after != before:
                edits.append(edit(path, after, moves))
    for checkout in paths:
        for name in ("AGENTS.md", "CLAUDE.md"):
            link = checkout / name
            if link.is_symlink():
                # Link edits carry no file bytes and preserve the link itself.
                target = str(link.readlink())
                if RETIRED_DIR_NAME in Path(target).parts:
                    absolute = (checkout / target).absolute()
                    new_target = os.path.relpath(mapped(absolute, moves), mapped(checkout, moves))
                    edits.append(
                        {
                            "path": str(mapped(link, moves)),
                            "link_before": target,
                            "link_after": new_target,
                        }
                    )
    return edits


def inspect_migration(
    root: Path, *, observe: Callable[[Path], None], integrations: IntegrationPlanner
) -> Migration | None:
    """Plan without creating files; resume only an identity-bound existing journal."""
    validate_environment()
    root = require_project_checkout(root).resolve()
    journal = metadata_root(root) / _JOURNAL
    if journal.is_file():
        document = _read_journal(journal, root)
        observe(_record_directory(root, document))
        _require_host_migration()
        _refuse_live_sandboxes(root, document["moves"], document["registrations"])
        return Migration(root, journal, document, observe, integrations)
    layout = classify_layout(root)
    if layout is not Layout.RETIRED:
        return None
    if (root / ".git").is_file():
        primary = primary_checkout(root)
        if (primary / RETIRED_DIR_NAME).is_dir():
            observe(primary / RETIRED_DIR_NAME)
    return Migration(root, journal, _capture(root, observe, integrations), observe, integrations)


def _record_directory(root: Path, document: dict[str, Any]) -> Path:
    first = document["moves"][0]
    old, new = Path(first["source"]), Path(first["destination"])
    return old if old.is_dir() else new


def _read_journal(path: Path, root: Path) -> dict[str, Any]:
    try:
        _unlinked(path)
        data = json.loads(path.read_bytes())
        if (
            data["schema"] != _SCHEMA
            or data["root"] != str(root)
            or data["identity"] != _identity(root)
        ):
            raise ValueError("Project identity differs")
        first = data["moves"][0]
        if first["source"] != str(root / RETIRED_DIR_NAME) or first["destination"] != str(
            root / BOOLEY_DIR_NAME
        ):
            raise ValueError("migration endpoints differ")
        _validate_moves(data["moves"], root, data["registrations"])
        if data["phase"] not in {"prepared", "renamed", "complete"}:
            raise ValueError("unknown migration phase")
        _validate_records(data["records"])
        _validate_edits(data)
        return data
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise BooleyLayoutError(f"invalid migration journal {path}: {exc}") from exc


def _unlinked(path: Path) -> None:
    if any(
        part.is_symlink() or getattr(part, "is_junction", lambda: False)()
        for part in (path, *path.parents)
    ):
        raise BooleyLayoutError(f"migration storage has a linked component: {path}")


def _validate_moves(moves: list[dict[str, Any]], root: Path, rows: list[dict[str, Any]]) -> None:
    sources = {root / RETIRED_DIR_NAME}
    commons = {git_common_directory(root), root / RETIRED_DIR_NAME / ".git"}
    data_directory = root / RETIRED_DIR_NAME
    if not data_directory.is_dir():
        data_directory = root / BOOLEY_DIR_NAME
    if (data_directory / ".git").exists():
        commons.add(git_common_directory(data_directory))
    for row in rows:
        checkout = Path(row["path"])
        if not checkout.is_absolute() or ".." in checkout.parts:
            raise ValueError("invalid checkout path")
        sources.add(checkout / RETIRED_DIR_NAME)
        if row.get("missing"):
            continue
        common, admin = Path(row["common"]), Path(row["admin"])
        if common not in commons or not admin.is_relative_to(common):
            raise ValueError("unbound Git administration directory")
    completed = []
    for move in moves:
        source, destination = Path(move["source"]), Path(move["destination"])
        if (
            source not in sources
            or destination != mapped(source.parent, completed) / BOOLEY_DIR_NAME
        ):
            raise ValueError("unbound migration endpoints")
        if len(move["identity"]) != 2 or any(type(i) is not int for i in move["identity"]):
            raise ValueError("invalid migration identity")
        completed.append(move)


def _validate_records(records: dict[str, str]) -> None:
    if not isinstance(records, dict) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key)
        or not isinstance(value, str)
        or not re.fullmatch(r"[a-f0-9]{64}", value)
        for key, value in records.items()
    ):
        raise ValueError("invalid archival record hashes")


def _validate_edits(data: dict[str, Any]) -> None:
    allowed = set()
    checkouts = {Path(data["root"])}
    for row in data["registrations"]:
        if row.get("missing"):
            continue
        checkout, admin = Path(row["path"]), Path(row["admin"])
        checkouts.add(checkout)
        allowed.add(Path(row["common"]) / "info/exclude")
        allowed.update(
            admin / name
            for name in (
                "gitdir",
                "commondir",
                "config",
                "config.worktree",
                "hooks/commit-msg",
                "hooks/pre-push",
            )
        )
    for checkout in checkouts:
        allowed.update(
            checkout / name
            for name in (
                ".git",
                ".git/info/exclude",
                ".gitignore",
                "AGENTS.md",
                "CLAUDE.md",
                ".devcontainer/devcontainer.json",
            )
        )
        directory = checkout if checkout.name == RETIRED_DIR_NAME else checkout / RETIRED_DIR_NAME
        allowed.add(directory / ".managed/project-git-hooks.pyz")
        allowed.add(directory / "runtime/doctor_stamp.json")
    allowed = {mapped(path, data["moves"]) for path in allowed}
    for row in data["edits"]:
        path = Path(row["path"])
        if path not in allowed and not _projected_path(path, checkouts, data["moves"]):
            raise ValueError("unbound migration edit")
        _unlinked(path.parent)
        _validate_edit(row)


def _validate_edit(row: dict[str, Any]) -> None:
    if "link_before" in row:
        if set(row) != {"path", "link_before", "link_after"} or any(
            not isinstance(row.get(key), str) or not row[key] or "\x00" in row[key]
            for key in ("link_before", "link_after")
        ):
            raise ValueError("invalid managed link edit")
    else:
        if set(row) != {"path", "before", "after", "mode"}:
            raise ValueError("invalid managed file edit")
        base64.b64decode(row["before"], validate=True)
        base64.b64decode(row["after"], validate=True)
        if type(row["mode"]) is not int or not 0 <= row["mode"] <= 0o777:
            raise ValueError("invalid managed file mode")


def _projected_path(path: Path, checkouts: set[Path], moves: list[dict[str, Any]]) -> bool:
    return any(
        (path.parent == mapped(checkout, moves) and path.match(".booley-projected-*.core"))
        or (
            path.parent
            == mapped(checkout / RETIRED_DIR_NAME / "tmp/fusesoc-isolated-cores", moves)
            and path.match("booley-isolated-*.core")
        )
        for checkout in checkouts
    )


def _invalidate_issuance(document: dict[str, Any]) -> None:
    from booley.runtime.session_issuance import invalidate_project

    roots = {Path(document["root"]), *(Path(row["path"]) for row in document["registrations"])}
    for root in roots:
        invalidate_project(str(root))
        relocated = mapped(root, document["moves"])
        if relocated != root:
            invalidate_project(str(relocated))


def _save(migration: Migration) -> None:
    atomic_replace_bytes(migration.journal, json.dumps(migration.document, indent=2).encode())


def apply_migration(plan: Migration) -> None:
    """Hold a stable layout lock, then rename and roll forward idempotently."""
    ensure_layout_metadata(plan.root)
    with (plan.journal.parent / "directory-layout.lock").open("a+") as lock:
        wait_for_file_lock(lock, timeout_s=30)
        try:
            current = inspect_migration(
                plan.root, observe=plan.observe, integrations=plan.integrations
            )
            if current is None:
                return
            if current.document != plan.document:
                raise BooleyLayoutError("Project changed after migration planning; run init again")
            _validate_edits(current.document)
            _save(current)
            _apply(current)
        finally:
            release_file_lock(lock)


def _apply(plan: Migration) -> None:
    document = plan.document
    completed = []
    for move in document["moves"]:
        source = mapped(Path(move["source"]), completed)
        destination = Path(move["destination"])
        if _exists(source):
            if (
                _exists(destination)
                or _identity(source) != move["identity"]
                or source.is_symlink()
            ):
                raise BooleyLayoutError(f"migration endpoints changed: {source} -> {destination}")
            source.rename(destination)
            fsync_directory(destination.parent)
        elif not destination.is_dir() or _identity(destination) != move["identity"]:
            raise BooleyLayoutError(f"cannot recover migration endpoint: {destination}")
        completed.append(move)
    document["phase"] = "renamed"
    _save(plan)
    for row in document["edits"]:
        _apply_edit(row)
    verify_registrations(document["registrations"], document["moves"])
    _invalidate_issuance(document)
    index = plan.root / BOOLEY_DIR_NAME / _INDEX
    atomic_replace_bytes(
        index,
        json.dumps(
            {"moves": document["moves"], "records": document["records"]}, indent=2
        ).encode(),
    )
    document["phase"] = "complete"
    _save(plan)
    plan.journal.unlink()
    fsync_directory(plan.journal.parent)


def _apply_edit(row: dict[str, Any]) -> None:
    path = Path(row["path"])
    _unlinked(path.parent)
    if "link_before" in row:
        if not path.is_symlink():
            raise BooleyLayoutError(f"managed link changed during migration: {path}")
        actual = str(path.readlink())
        if actual == row["link_after"]:
            return
        if actual != row["link_before"]:
            raise BooleyLayoutError(f"managed link target changed: {path}")
        temporary = path.with_name(path.name + ".booley-migrating")
        if _exists(temporary):
            if not temporary.is_symlink() or str(temporary.readlink()) != row["link_after"]:
                raise BooleyLayoutError(f"migration link staging collision: {temporary}")
        else:
            temporary.symlink_to(row["link_after"])
        temporary.replace(path)
        return
    before, after = base64.b64decode(row["before"]), base64.b64decode(row["after"])
    if path.is_symlink() or path.read_bytes() not in (before, after):
        raise BooleyLayoutError(f"managed file changed during migration: {path}")
    if path.read_bytes() != after:
        atomic_replace_bytes(path, after, mode=row["mode"])


def _running_sandboxes() -> list[dict[str, Any]]:
    """Inventory Docker on every migration; a missing local spec proves nothing."""
    if shutil.which("docker") is None:
        raise BooleyLayoutError(
            "cannot inventory live Sandboxes; install/restore Docker before migration"
        )
    result = subprocess.run(
        ["docker", "ps", "-q"], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        raise BooleyLayoutError(
            "cannot inventory live Sandboxes; stop consumers and restore Docker before migration"
        )
    documents = []
    for container in result.stdout.split():
        inspected = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if inspected.returncode:
            raise BooleyLayoutError(f"cannot inspect live Sandbox {container}")
        try:
            values = json.loads(inspected.stdout)
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                raise ValueError("expected one container object")
            _validate_container_inventory(values[0])
            documents.append(values[0])
        except (ValueError, TypeError, IndexError) as exc:
            raise BooleyLayoutError(
                f"cannot inspect live Sandbox {container}: invalid inventory"
            ) from exc
    return documents


def _validate_container_inventory(document: dict[str, Any]) -> None:
    config, mounts = document.get("Config"), document.get("Mounts")
    if not isinstance(config, dict) or "Labels" not in config or not isinstance(mounts, list):
        raise ValueError("container labels/mount inventory is incomplete")
    labels = config["Labels"]
    if labels is not None and (
        not isinstance(labels, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
        )
    ):
        raise ValueError("invalid container labels")
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") not in {
            "bind",
            "volume",
            "tmpfs",
            "npipe",
            "cluster",
        }:
            raise ValueError("invalid container mount inventory")
        if mount["Type"] in {"bind", "volume"} and (
            not isinstance(mount.get("Source"), str) or not mount["Source"]
        ):
            raise ValueError("container bind/volume source is unavailable")
        if mount["Type"] in {"bind", "volume"} and (
            "\x00" in mount["Source"]
            or not (
                PurePosixPath(mount["Source"]).is_absolute()
                or PureWindowsPath(mount["Source"]).is_absolute()
            )
        ):
            raise ValueError("container source is not an absolute path")


def _refuse_live_sandboxes(
    root: Path, moves: list[dict[str, Any]], rows: list[dict[str, Any]] | None = None
) -> None:
    """Refuse active affected mounts and canonical/legacy Project labels."""
    roots = {root}
    for row in rows or []:
        checkout = Path(row["path"])
        roots.add(checkout.parent if checkout.name == RETIRED_DIR_NAME else checkout)
        relocated = mapped(checkout, moves)
        roots.add(relocated.parent if relocated.name == BOOLEY_DIR_NAME else relocated)
    for document in _running_sandboxes():
        labels = document.get("Config", {}).get("Labels", {}) or {}
        if any(
            matches_project_label(labels.get("booley.project-id"), str(path)) for path in roots
        ):
            raise BooleyLayoutError("stop live Sandbox before running `booley init`")
        for mount in document.get("Mounts", []):
            source = Path(mount.get("Source", "/__unavailable__"))
            if source in roots or any(
                affected.is_relative_to(source) or source.is_relative_to(affected)
                for move in moves
                for affected in (Path(move["source"]), Path(move["destination"]))
            ):
                raise BooleyLayoutError("stop live Sandbox before running `booley init`")
