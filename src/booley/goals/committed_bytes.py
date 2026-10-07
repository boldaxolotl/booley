"""Pinned built-in Git working-byte projection, without executing external filters."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from booley.goals.lifecycle import LifecycleError
from booley.runtime.history_commit import FileCommitError
from booley.runtime.pinned_history import raw_git


@contextmanager
def shadow_repository(repository: Path) -> Generator[tuple[Path, dict[str, str]]]:
    """Object-only scratch repository; no live/global attributes, templates or filters."""
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
        }
        yield shadow, env


def attributes(
    repository: Path, commit: str, names: list[bytes], *, ambient: bool = False
) -> dict[bytes, dict[str, str]]:
    """Pinned index attributes, optionally compared with effective external policy."""
    with shadow_repository(repository) as (shadow, env):
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
    if attributes(repository, commit, list(rows), ambient=True) != attrs:
        raise LifecycleError(
            "unsupported ambient input attributes; commit equivalent .gitattributes policy before finish"
        )
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
    with shadow_repository(repository) as (shadow, env):
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
    }
