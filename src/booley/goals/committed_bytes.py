"""Pinned built-in Git working-byte projection, without executing external filters."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from booley.goals.lifecycle import LifecycleError
from booley.runtime.git_attributes_policy import (
    GITATTRIBUTES_RULE,
    fallback_user_attributes,
    has_attribute_policy,
    has_managed_attributes,
    is_null_attributes_path,
    native_attribute_path,
    system_attributes_disabled,
)
from booley.runtime.history_commit import FileCommitError
from booley.runtime.pinned_history import raw_git, tree_rows

AMBIENT_ATTRIBUTES_ERROR = (
    "unsupported ambient input attributes; Finish accepts Booley's managed info/attributes "
    "rule from booley init. Remove other local/user/system attributes or commit equivalent "
    ".gitattributes policy before finish"
)


CHECKOUT_ATTRIBUTES = ("text", "eol", "filter", "working-tree-encoding", "ident", "crlf")
CHECKOUT_ATTRIBUTES_MARKER = ",".join(CHECKOUT_ATTRIBUTES)


ATTRIBUTES_CHANGED_ERROR = "unsupported ambient input attributes changed during finish"


@dataclass(frozen=True)
class BytePolicy:
    """Private replay bytes and a public, content-free materialization record."""

    autocrlf: str
    eol: str
    info_attributes: bytes

    def record(self) -> dict[str, str]:
        return {
            "checkout_attributes": CHECKOUT_ATTRIBUTES_MARKER,
            "autocrlf": self.autocrlf,
            "eol": self.eol,
            "managed_rule": GITATTRIBUTES_RULE if self.info_attributes else "",
            "info_attributes_sha256": (
                hashlib.sha256(self.info_attributes).hexdigest() if self.info_attributes else ""
            ),
        }


@contextmanager
def shadow_repository(
    repository: Path, info_attributes: bytes
) -> Generator[tuple[Path, dict[str, str]]]:
    """Object-only scratch repository with only the explicitly captured local policy."""
    with tempfile.TemporaryDirectory(prefix="booley-pinned-bytes-") as scratch:
        shadow, template = Path(scratch) / "repository", Path(scratch) / "template"
        template.mkdir()
        algorithm = (
            raw_git(repository, "rev-parse", "--show-object-format").strip().decode("ascii")
        )
        raw_git(
            repository,
            "init",
            "-q",
            "--template=" + str(template),
            "--object-format=" + algorithm,
            str(shadow),
        )
        common = Path(os.fsdecode(raw_git(repository, "rev-parse", "--git-common-dir").strip()))
        common = common if common.is_absolute() else repository / common
        env = {
            "GIT_OBJECT_DIRECTORY": str(common.resolve() / "objects"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_PARAMETERS": "",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
        if info_attributes:
            info = shadow / ".git/info"
            info.mkdir()
            (info / "attributes").write_bytes(info_attributes)
        yield shadow, env


def attributes(
    repository: Path,
    commit: str,
    names: list[bytes],
    *,
    ambient: bool = False,
    policy: BytePolicy,
) -> dict[bytes, dict[str, str]]:
    """Pinned attributes, or external policy over the pin's own tracked attributes."""
    with shadow_repository(repository, policy.info_attributes) as (shadow, env):
        raw_git(shadow, "read-tree", commit, env=env)
        paths = b"".join(name + b"\0" for name in names)
        args = ("check-attr", "-z", "--stdin", *CHECKOUT_ATTRIBUTES)
        if ambient:
            comparison = shadow.parent / "ambient"
            comparison.mkdir()
            _comparison_attributes(repository, commit, names, comparison)
            result = raw_git(
                repository,
                *args,
                env={
                    "GIT_INDEX_FILE": str(shadow / ".git/index"),
                    "GIT_WORK_TREE": str(comparison),
                },
                input_bytes=paths,
            )
        else:
            result = raw_git(
                shadow,
                "-c",
                "core.attributesFile=" + os.devnull,
                *args,
                "--cached",
                env=env,
                input_bytes=paths,
            )
    fields = result.rstrip(b"\0").split(b"\0") if result else []
    values: dict[bytes, dict[str, str]] = {}
    for i in range(0, len(fields), 3):
        name, key, value = fields[i : i + 3]
        values.setdefault(name, {})[key.decode("ascii")] = value.decode("utf-8")
    return values


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


def projected_blobs(
    repository: Path,
    commit: str,
    rows: dict[bytes, bytes],
    attrs: dict[bytes, dict[str, str]],
    policy: BytePolicy,
) -> dict[bytes, bytes]:
    """Git's built-in conversion, with exact pinned attributes and hermetic configuration."""
    _require_current_policy(repository, policy)
    if attributes(repository, commit, list(rows), ambient=True, policy=policy) != attrs:
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
    for name, values in attrs.items():
        if any(
            values[key] not in {"unspecified", "unset"}
            for key in ("filter", "working-tree-encoding")
        ):
            raise LifecycleError(
                f"unsupported committed input transformation: {os.fsdecode(name)}; "
                "the whole selected versioned participant requires a hermetic working-byte proof, "
                "including unconsumed files; custom filters/encodings are unsupported"
            )
    with shadow_repository(repository, policy.info_attributes) as (
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


def _require_current_policy(repository: Path, policy: BytePolicy) -> None:
    try:
        current = byte_policy(repository)
    except LifecycleError as exc:
        raise LifecycleError(ATTRIBUTES_CHANGED_ERROR) from exc
    for name, previous, observed in (
        ("core.autocrlf", policy.autocrlf, current.autocrlf),
        ("core.eol", policy.eol, current.eol),
    ):
        if observed != previous:
            raise LifecycleError(f"{name} changed during finish")
    if current.info_attributes != policy.info_attributes:
        raise LifecycleError(ATTRIBUTES_CHANGED_ERROR)


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
        info_attributes=_info_attributes_policy(repository),
    )


def _info_attributes_policy(repository: Path) -> bytes:
    """Replay init's managed policy; without it retain the effective-attribute comparison."""
    try:
        name = raw_git(repository, "rev-parse", "--git-path", "info/attributes")
        path = Path(os.fsdecode(name).rstrip("\r\n"))
        path = path if path.is_absolute() else repository / path
        if path.is_symlink() or path.parent.is_symlink() or (path.exists() and not path.is_file()):
            return b""
        content = path.read_bytes() if path.exists() else b""
    except OSError:
        return b""  # Retain Git's own effective-policy decision when no rule can be captured.
    try:
        if not has_managed_attributes(content):
            return b""
        if not has_managed_attributes(content, replayable=True):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
        _require_no_external_attributes(repository)
    except (OSError, ValueError) as exc:
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR) from exc
    return content


def _require_no_external_attributes(repository: Path) -> None:
    # Ask Git which files it actually reads rather than re-deriving its selection:
    # `git config --get` can report a lower-scope `core.attributesFile` than the one
    # Git applies (e.g. a missing `:(optional)` path falls back to the XDG file).
    supported, user = native_attribute_path(repository, "GIT_ATTR_GLOBAL", _attribute_path_query)
    if not supported:
        user = fallback_user_attributes(repository, _attribute_path_query)
    if user is not None and not is_null_attributes_path(repository, user):
        _require_no_attribute_policy(user)
    if system_attributes_disabled():
        return
    supported, system = native_attribute_path(repository, "GIT_ATTR_SYSTEM", _attribute_path_query)
    if not supported:
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
    if system is not None:
        _require_no_attribute_policy(system)


def _require_no_attribute_policy(path: Path) -> None:
    if path.exists() and (not path.is_file() or has_attribute_policy(path.read_bytes())):
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)


def _attribute_path_query(repository: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Adapt pinned-history reads to the shared attribute resolver's process-result boundary."""
    try:
        output = raw_git(repository, *args)
    except FileCommitError as exc:
        return subprocess.CompletedProcess(args, 128 if str(exc) else 1, b"", str(exc).encode())
    return subprocess.CompletedProcess(args, 0, output, b"")
