"""Recursively materialize pinned local objects and prove their working-byte policy."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from booley.core.boundary import is_str_list, require_dict
from booley.core.config_paths import resolve_booley_toml
from booley.goals.baseline_objects import baseline_repository, cached_administration
from booley.goals.committed_bytes import (
    AMBIENT_ATTRIBUTES_ERROR,
    attributes,
    byte_policy,
    projected_blobs,
)
from booley.goals.input_identity import directory_identity, rebase_path
from booley.goals.lifecycle import LifecycleError
from booley.goals.proposals import digest
from booley.runtime.pinned_history import raw_git, tree_rows, validate_pin


def confined(path: Path, scratch: Path, *, leaf_link: bool = False) -> Path:
    resolved = path.parent.resolve() if leaf_link else path.resolve()
    if not path.is_relative_to(scratch) or not resolved.is_relative_to(scratch.resolve()):
        raise LifecycleError(f"materialized input escapes committed scratch view: {path}")
    return path


def export_tree(
    repository: Path,
    commit: str,
    destination: Path,
    *,
    scratch: Path,
    baseline: bool = False,
    excluded_top_level: frozenset[str] = frozenset(),
    project_config: Path | None = None,
    _baseline_admin: Path | None = None,
) -> list[dict[str, Any]]:
    """No fetch, registered checkout or current source bytes: every file is a pinned blob."""
    validate_pin(repository, commit)
    confined(destination, scratch).mkdir(parents=True, exist_ok=True)
    rows = tree_rows(repository, commit)
    policy = byte_policy(repository)
    attrs = attributes(repository, commit, list(rows), policy=policy)
    _export_blobs(repository, commit, destination, scratch, rows, attrs, policy)
    proofs = [
        {
            "path": str(repository),
            "pin": commit,
            "policy": policy,
            "attributes_digest": digest(attrs_json(attrs)),
            "submodule": False,
        }
    ]
    config = confined(project_config or resolve_booley_toml(destination), scratch)
    selected = _configured_paths(config)
    for name, metadata in rows.items():
        _mode, kind, oid = metadata.split()
        relative = os.fsdecode(name)
        if kind != b"commit" or relative in excluded_top_level:
            continue
        if selected is not None and relative not in selected:
            continue
        path = confined(destination / relative, scratch)
        absent = not (repository / relative / ".git").exists()
        if baseline and (_baseline_admin is not None or absent):
            nested = _export_cached_submodule(
                repository, commit, relative, oid, path, scratch, _baseline_admin
            )
        else:
            nested = _export_live_submodule(
                repository, commit, relative, oid, path, scratch, baseline
            )
        proofs.extend(nested)
    return proofs


def _export_cached_submodule(
    repository: Path,
    commit: str,
    relative: str,
    oid: bytes,
    path: Path,
    scratch: Path,
    admin: Path | None,
) -> list[dict[str, Any]]:
    with baseline_repository(
        repository, commit, relative, oid.decode("ascii"), scratch, admin
    ) as (source, cache):
        nested = export_tree(
            source,
            oid.decode("ascii"),
            path,
            scratch=scratch,
            baseline=True,
            _baseline_admin=cache,
        )
        nested[0].update(path=str(cache), submodule=True, baseline_only=True)
        return nested


def _export_live_submodule(
    repository: Path,
    commit: str,
    relative: str,
    oid: bytes,
    path: Path,
    scratch: Path,
    baseline: bool,
) -> list[dict[str, Any]]:
    source = submodule_source(repository, relative)
    if baseline:
        expected = cached_administration(repository, commit, relative, None)
        admin = Path(os.fsdecode(raw_git(source, "rev-parse", "--git-dir").strip()))
        admin = admin if admin.is_absolute() else source / admin
        if directory_identity(admin) != directory_identity(expected):
            raise LifecycleError(
                "baseline submodule checkout differs from original cached administration"
            )
    nested = export_tree(source, oid.decode("ascii"), path, scratch=scratch, baseline=baseline)
    current_pin = raw_git(source, "rev-parse", "HEAD").strip()
    if not baseline and current_pin != oid:
        raise LifecycleError(f"submodule HEAD differs from pinned gitlink: {source}")
    nested[0].update(
        submodule=True,
        identity=directory_identity(source),
        current_pin=current_pin.decode("ascii"),
        git_admin=git_administration(source),
    )
    return nested


def _configured_paths(config: Path) -> tuple[str, ...] | None:
    """Selection comes only from the already materialized pinned Project, never live policy."""
    if not config.exists():
        return None
    raw = require_dict(
        tomllib.loads(config.read_text(encoding="utf-8")), field="pinned Project config"
    )
    section = require_dict(raw.get("submodules", {}), field="[submodules]")
    if "paths" not in section:
        return None
    paths = section["paths"]
    if not is_str_list(paths):
        raise LifecycleError("[submodules].paths must be an array of strings")
    for value in paths:
        if (
            not value
            or value != value.strip()
            or not value.isprintable()
            or value.startswith(("-", "/"))
            or "\\" in value
            or PureWindowsPath(value).drive
            or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
        ):
            raise LifecycleError("unsafe pinned submodule selection path: " + value)
    return tuple(paths)


def _export_blobs(
    repository: Path,
    commit: str,
    destination: Path,
    scratch: Path,
    rows: dict[bytes, bytes],
    attrs: dict[bytes, dict[str, str]],
    policy: dict[str, str],
) -> None:
    blobs = projected_blobs(repository, commit, rows, attrs, policy)
    for name, metadata in rows.items():
        mode, kind, oid = metadata.split()
        if kind != b"blob":
            continue
        path = confined(destination / os.fsdecode(name), scratch)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == b"120000":
            content = raw_git(repository, "cat-file", "blob", oid.decode("ascii"))
            path.symlink_to(os.fsdecode(content))
        else:
            path.write_bytes(blobs[name])
            path.chmod(0o755 if mode == b"100755" else 0o644)


def attrs_json(attrs: dict[bytes, dict[str, str]]) -> dict[str, Any]:
    return {os.fsdecode(name): values for name, values in attrs.items()}


def submodule_source(repository: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise LifecycleError("unsafe pinned submodule path")
    source = repository / relative
    for path in (source, *source.parents):
        if path == repository:
            break
        if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
            raise LifecycleError(f"submodule source crosses a link: {path}")
    if not source.is_dir() or not (source / ".git").exists():
        raise LifecycleError(
            f"pinned local submodule unavailable: {source}; initialize its local objects before finish"
        )
    top = Path(os.fsdecode(raw_git(source, "rev-parse", "--show-toplevel").strip()))
    if not top.samefile(source):
        raise LifecycleError(f"submodule source is not an independent checkout: {source}")
    if raw_git(source, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise LifecycleError(f"commit submodule changes before finish: {source}")
    return source


def materializations_unchanged(proof: dict[str, Any], current_roots: dict[str, Any]) -> bool:
    for row in proof.get("committed_materializations", []):
        path = rebase_path(Path(row["path"]), proof["path_roots"], current_roots)
        if row["submodule"]:
            submodule_source(path.parent, path.name)
            if (
                directory_identity(path) != row["identity"]
                or git_administration(path) != row["git_admin"]
            ) or raw_git(path, "rev-parse", "HEAD").strip().decode("ascii") != row["current_pin"]:
                return False
        names = list(tree_rows(path, row["pin"]))
        policy = byte_policy(path)
        pinned = attributes(path, row["pin"], names, policy=policy)
        if attributes(path, row["pin"], names, ambient=True, policy=policy) != pinned:
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
        recorded = {"info_attributes": "", "info_attributes_hex": "", **row["policy"]}
        if policy != recorded or digest(attrs_json(pinned)) != row["attributes_digest"]:
            return False
    return True


def git_administration(repository: Path) -> list[list[int]]:
    directories: list[list[int]] = []
    for option in ("--git-dir", "--git-common-dir"):
        path = Path(os.fsdecode(raw_git(repository, "rev-parse", option).strip()))
        directories.append(directory_identity(path if path.is_absolute() else repository / path))
    return directories


@dataclass(frozen=True)
class PinnedWorkingBytes:
    """One read-only working-byte projection shared by a cleanliness observation."""

    rows: dict[bytes, bytes]
    blobs: dict[bytes, bytes]


def pinned_working_bytes(repository: Path, pin: str) -> PinnedWorkingBytes:
    rows = tree_rows(repository, pin)
    policy = byte_policy(repository)
    attrs = attributes(repository, pin, list(rows), policy=policy)
    return PinnedWorkingBytes(rows, projected_blobs(repository, pin, rows, attrs, policy))


def tracked_matches_pin(
    repository: Path, pin: str, path: Path, *, projection: PinnedWorkingBytes | None = None
) -> bool:
    """A stale stat/EOL cache cannot make clean built-in working bytes look edited."""
    if path.is_symlink() or not path.is_file():
        return False
    projection = projection or pinned_working_bytes(repository, pin)
    name = os.fsencode(path.relative_to(repository).as_posix())
    metadata = projection.rows.get(name, b"")
    if not metadata.startswith((b"100644 blob ", b"100755 blob ")):
        return False
    if os.name != "nt" and bool(stat.S_IMODE(path.stat().st_mode) & 0o111) != metadata.startswith(
        b"100755"
    ):
        return False
    return projection.blobs[name] == path.read_bytes()
