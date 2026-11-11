"""Pinned built-in Git working-byte projection, without executing external filters."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from booley.goals.lifecycle import LifecycleError
from booley.runtime.commit_export import (
    CHECKOUT_ATTRIBUTES,
    BytePolicy,
    parse_attributes,
    pinned_attributes,
    shadow_repository,
    transformed_path,
)
from booley.runtime.git_attributes_policy import has_managed_attributes
from booley.runtime.history_commit import FileCommitError
from booley.runtime.pinned_history import raw_git, tree_rows

AMBIENT_ATTRIBUTES_ERROR = (
    "unsupported ambient input attributes; Finish accepts Booley's managed info/attributes "
    "rule from booley init. Remove other local/user/system attributes or commit equivalent "
    ".gitattributes policy before finish"
)

ATTRIBUTES_CHANGED_ERROR = "unsupported ambient input attributes changed during finish"


def attributes(
    repository: Path,
    commit: str,
    names: list[bytes],
    *,
    ambient: bool = False,
    policy: BytePolicy,
) -> dict[bytes, dict[str, str]]:
    """Pinned attributes, or external policy over the pin's own tracked attributes."""
    if not ambient:
        return pinned_attributes(repository, commit, names, policy)
    with shadow_repository(repository, policy.managed) as (shadow, env):
        raw_git(shadow, "read-tree", commit, env=env)
        comparison = shadow.parent / "ambient"
        comparison.mkdir()
        _comparison_attributes(repository, commit, names, comparison)
        result = raw_git(
            repository,
            "check-attr",
            "-z",
            "--stdin",
            *CHECKOUT_ATTRIBUTES,
            env={
                "GIT_INDEX_FILE": str(shadow / ".git/index"),
                "GIT_WORK_TREE": str(comparison),
            },
            input_bytes=b"".join(name + b"\0" for name in names),
        )
    return parse_attributes(result)


def _attribute_ancestors(names: list[bytes]) -> set[Path]:
    ancestors = {Path(".gitattributes")}
    for name in names:
        parent = Path(os.fsdecode(name)).parent
        ancestors.update(path / ".gitattributes" for path in (parent, *parent.parents))
    return ancestors


def _comparison_attributes(
    repository: Path, commit: str, names: list[bytes], target: Path
) -> None:
    """Keep baseline tracked rules; overlay only current-index-untracked ancestor rules."""
    for name, metadata in tree_rows(repository, commit).items():
        mode, kind, oid = metadata.split()
        path = Path(os.fsdecode(name))
        if path.name != ".gitattributes" or kind != b"blob" or mode not in {b"100644", b"100755"}:
            continue
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw_git(repository, "cat-file", "blob", oid.decode("ascii")))
    tracked = set(raw_git(repository, "ls-files", "-z").split(b"\0"))
    for path in _attribute_ancestors(names):
        if any(
            os.fsencode(candidate.as_posix()) in tracked for candidate in (path, *path.parents)
        ):
            continue
        content = _untracked_attribute_bytes(repository, path)
        if content is not None:
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)


def _untracked_attribute_bytes(repository: Path, relative: Path) -> bytes | None:
    """Native literal lookup preserves case behavior and never follows attribute links."""
    try:
        source = repository / relative
        for parent in relative.parents:
            if parent == Path():
                continue
            candidate = repository / parent
            if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
        try:
            mode = source.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if stat.S_ISLNK(mode):
            return None  # Git ignores symlink attribute files.
        if not stat.S_ISREG(mode):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
        return source.read_bytes()
    except OSError as exc:
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR) from exc


def require_ambient_matches(
    repository: Path,
    commit: str,
    names: list[bytes],
    attrs: dict[bytes, dict[str, str]],
    policy: BytePolicy,
) -> None:
    """Refuse ambient attribute policy that would change the pinned attributes."""
    if attributes(repository, commit, names, ambient=True, policy=policy) != attrs:
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)


def refuse_goal_transformations(attrs: dict[bytes, dict[str, str]]) -> None:
    """Goal Finish refuses any pinned filter or working-tree-encoding."""
    name = transformed_path(attrs)
    if name is not None:
        raise LifecycleError(
            f"unsupported committed input transformation: {os.fsdecode(name)}; "
            "the whole selected versioned participant requires a hermetic working-byte proof, "
            "including unconsumed files; custom filters/encodings are unsupported"
        )


def projected_blobs(
    repository: Path,
    commit: str,
    rows: dict[bytes, bytes],
    attrs: dict[bytes, dict[str, str]],
    policy: BytePolicy,
) -> dict[bytes, bytes]:
    """Git's built-in conversion, with exact pinned attributes and hermetic configuration."""
    require_ambient_matches(repository, commit, list(rows), attrs, policy)
    refuse_goal_transformations(attrs)
    with shadow_repository(repository, policy.managed) as (
        shadow,
        env,
    ):
        raw_git(shadow, "read-tree", commit, env=env)
        raw_git(
            shadow,
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "core.autocrlf=" + policy.autocrlf,
            "-c",
            "core.eol=" + policy.eol,
            "checkout-index",
            "--all",
            "--force",
            env=env,
        )
        return {
            name: (shadow / os.fsdecode(name)).read_bytes()
            for name, value in rows.items()
            if value.startswith((b"100644 blob ", b"100755 blob "))
        }


def config(repository: Path, name: str, default: str) -> str:
    try:
        return raw_git(repository, "config", "--get", name).strip().decode("ascii").lower()
    except FileCommitError as exc:
        if str(exc):
            raise
        return default


def byte_policy(repository: Path) -> BytePolicy:
    return BytePolicy(
        autocrlf=config(repository, "core.autocrlf", "false"),
        eol=config(repository, "core.eol", "crlf" if os.name == "nt" else "lf"),
        managed=_info_attributes_policy(repository),
    )


def _info_attributes_policy(repository: Path) -> bool:
    """Replay init's managed policy; without it retain the effective-attribute comparison."""
    try:
        name = raw_git(repository, "rev-parse", "--git-path", "info/attributes")
        path = Path(os.fsdecode(name).rstrip("\r\n"))
        path = path if path.is_absolute() else repository / path
        if path.is_symlink() or path.parent.is_symlink() or (path.exists() and not path.is_file()):
            return False
        content = path.read_bytes() if path.exists() else b""
    except OSError:
        return False  # Retain Git's own effective-policy decision when no rule can be captured.
    if not has_managed_attributes(content):
        return False
    if not has_managed_attributes(content, replayable=True):
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
    return True
