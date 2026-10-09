"""Pinned built-in Git working-byte projection, without executing external filters."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from booley.goals.lifecycle import LifecycleError
from booley.runtime.git_attributes_policy import (
    GITATTRIBUTES_RULE,
    default_user_attributes,
    has_managed_attributes,
    system_attributes_disabled,
)
from booley.runtime.history_commit import FileCommitError
from booley.runtime.pinned_history import raw_git

AMBIENT_ATTRIBUTES_ERROR = (
    "unsupported ambient input attributes; Finish accepts Booley's managed info/attributes "
    "rule from booley init. Remove other local/user/system attributes or commit equivalent "
    ".gitattributes policy before finish"
)


@contextmanager
def shadow_repository(
    repository: Path, info_attributes: str = ""
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
            (info / "attributes").write_bytes(info_attributes.encode() + b"\n")
        yield shadow, env


def attributes(
    repository: Path,
    commit: str,
    names: list[bytes],
    *,
    ambient: bool = False,
    policy: dict[str, str] | None = None,
) -> dict[bytes, dict[str, str]]:
    """Pinned index attributes, optionally compared with effective external policy."""
    policy = policy if policy is not None else byte_policy(repository)
    with shadow_repository(repository, policy.get("info_attributes", "")) as (shadow, env):
        raw_git(shadow, "read-tree", commit, env=env)
        args = (
            "check-attr",
            "--cached",
            "-z",
            "--stdin",
            "text",
            "eol",
            "filter",
            "working-tree-encoding",
        )
        paths = b"".join(name + b"\0" for name in names)
        result = (
            raw_git(
                repository,
                *args,
                env={"GIT_INDEX_FILE": str(shadow / ".git/index")},
                input_bytes=paths,
            )
            if ambient
            else raw_git(
                shadow,
                "-c",
                "core.attributesFile=" + os.devnull,
                *args,
                env=env,
                input_bytes=paths,
            )
        )
    fields = result.rstrip(b"\0").split(b"\0") if result else []
    values: dict[bytes, dict[str, str]] = {}
    for i in range(0, len(fields), 3):
        name, key, value = fields[i : i + 3]
        values.setdefault(name, {})[key.decode("ascii")] = value.decode("utf-8")
    return values


def projected_blobs(
    repository: Path,
    commit: str,
    rows: dict[bytes, bytes],
    attrs: dict[bytes, dict[str, str]],
    policy: dict[str, str],
) -> dict[bytes, bytes]:
    """Git's built-in conversion, with exact pinned attributes and hermetic configuration."""
    if (
        byte_policy(repository) != policy
        or attributes(repository, commit, list(rows), ambient=True, policy=policy) != attrs
    ):
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
    with shadow_repository(repository, policy.get("info_attributes", "")) as (shadow, env):
        raw_git(shadow, "read-tree", commit, env=env)
        raw_git(
            shadow,
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "core.autocrlf=" + policy["autocrlf"],
            "-c",
            "core.eol=" + policy["eol"],
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


def byte_policy(repository: Path) -> dict[str, str]:
    return {
        "autocrlf": config(repository, "core.autocrlf", "false"),
        "eol": config(repository, "core.eol", "crlf" if os.name == "nt" else "lf"),
        "info_attributes": _info_attributes_policy(repository),
    }


def _info_attributes_policy(repository: Path) -> str:
    """Accept only the managed single line; unrelated ambient policy is not projected."""
    try:
        name = raw_git(
            repository, "rev-parse", "--path-format=absolute", "--git-path", "info/attributes"
        )
        path = Path(os.fsdecode(name).rstrip("\r\n"))
        if path.is_symlink() or path.parent.is_symlink() or (path.exists() and not path.is_file()):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
        content = path.read_bytes() if path.exists() else b""
        if content and not has_managed_attributes(content, exclusive=True):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
        _require_no_external_attributes(repository, managed=bool(content))
    except (OSError, ValueError) as exc:
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR) from exc
    return GITATTRIBUTES_RULE if content else ""


def _require_no_external_attributes(repository: Path, *, managed: bool) -> None:
    try:
        configured = raw_git(repository, "config", "--path", "--get", "core.attributesFile")
    except FileCommitError as exc:
        if str(exc):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR) from exc
        configured = b""
    configured = configured.removesuffix(b"\n")
    if configured and os.fsdecode(configured).lower() != os.devnull.lower():
        raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)
    # Ask Git for its platform-specific paths rather than guessing installation prefixes.
    for variable in ("GIT_ATTR_GLOBAL", "GIT_ATTR_SYSTEM"):
        if variable == "GIT_ATTR_GLOBAL" and configured:
            continue  # The only accepted explicit selection is the platform null device.
        if variable == "GIT_ATTR_SYSTEM" and system_attributes_disabled():
            continue
        path = _external_attributes_path(repository, variable, managed=managed)
        if path is not None and path.exists() and (not path.is_file() or path.read_bytes()):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR)


def _external_attributes_path(repository: Path, variable: str, *, managed: bool) -> Path | None:
    try:
        name = os.fsdecode(raw_git(repository, "var", variable)).removesuffix("\n")
    except FileCommitError as exc:
        if not str(exc):
            return None
        # Like Initialization, do not install/replay a managed rule over unknown system policy.
        if "usage: git var" not in str(exc) or (variable == "GIT_ATTR_SYSTEM" and managed):
            raise LifecycleError(AMBIENT_ATTRIBUTES_ERROR) from exc
        name = str(default_user_attributes() or "") if variable == "GIT_ATTR_GLOBAL" else ""
    path = Path(name)
    return (path if path.is_absolute() else repository / path) if name else None
