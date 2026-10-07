"""Read removed baseline gitlinks from their original contained local object caches."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from booley.goals.lifecycle import LifecycleError
from booley.runtime.history_commit import FileCommitError
from booley.runtime.pinned_history import raw_git, validate_pin


def cached_administration(
    repository: Path, commit: str, relative: str, parent: Path | None
) -> Path:
    """Only the original pinned mapping may select a retained local Git administration."""
    try:
        mappings = raw_git(
            repository,
            "config",
            "--blob",
            commit + ":.gitmodules",
            "--null",
            "--get-regexp",
            r"^submodule\..*\.path$",
        )
    except FileCommitError as exc:
        raise LifecycleError(
            "baseline submodule has no usable original .gitmodules mapping"
        ) from exc
    names: list[str] = []
    for row in mappings.split(b"\0"):
        if row:
            key, value = row.split(b"\n", 1)
            if os.fsdecode(value) == relative:
                names.append(os.fsdecode(key)[len("submodule.") : -len(".path")])
    if len(names) != 1:
        raise LifecycleError("baseline submodule mapping is missing or ambiguous: " + relative)
    name = names[0]
    if (
        not name
        or not name.isprintable()
        or name != name.strip()
        or "\\" in name
        or PureWindowsPath(name).drive
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise LifecycleError("unsafe original baseline submodule name: " + name)
    if parent is not None:
        return contained_cache(parent, name)
    directory = Path(os.fsdecode(raw_git(repository, "rev-parse", "--git-dir").strip()))
    directory = directory if directory.is_absolute() else repository / directory
    return contained_cache(directory, name)


def contained_cache(parent: Path, name: str) -> Path:
    """A retained cache must remain inside the original common administration without links."""
    cache = parent / "modules" / PurePosixPath(name)
    for path in (cache, *cache.parents):
        if _is_link(path):
            raise LifecycleError("baseline submodule cache crosses a link: " + str(path))
        if path == parent:
            break
    if not cache.is_dir() or not (cache / "objects").is_dir():
        raise LifecycleError("original baseline submodule objects unavailable: " + str(cache))
    for path in (
        cache / "objects",
        cache / "config",
        cache / "objects/info",
        cache / "objects/info/alternates",
    ):
        if _is_link(path):
            raise LifecycleError("baseline submodule object/config cache crosses a link")
    alternates = cache / "objects/info/alternates"
    if alternates.exists() and alternates.read_bytes().strip():
        raise LifecycleError("baseline submodule cache has unsupported alternate object storage")
    if not cache.resolve().is_relative_to(parent.resolve() / "modules"):
        raise LifecycleError("baseline submodule object cache escaped original administration")
    return cache


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


@contextmanager
def baseline_repository(
    repository: Path, commit: str, relative: str, oid: str, scratch: Path, parent: Path | None
) -> Generator[tuple[Path, Path]]:
    """Empty-template object-only view: no fetch, checkout, external filter or live-byte fallback."""
    admin = cached_administration(repository, commit, relative, parent)
    if not str(admin).isprintable():
        raise LifecycleError(
            "baseline object cache path is not safe for literal local object lookup"
        )
    with tempfile.TemporaryDirectory(prefix="baseline-objects-", dir=scratch) as directory:
        root = Path(directory) / "repository"
        template = Path(directory) / "template"
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
            str(root),
        )
        (root / ".git/objects/info/alternates").write_bytes(
            os.fsencode(str((admin / "objects").resolve())) + b"\n"
        )
        raw_git(root, "config", "core.attributesFile", os.devnull)
        for key, default in (
            ("core.autocrlf", "false"),
            ("core.eol", "crlf" if os.name == "nt" else "lf"),
        ):
            try:
                value = (
                    raw_git(repository, "config", "--file", str(admin / "config"), "--get", key)
                    .strip()
                    .decode("ascii")
                )
            except FileCommitError as exc:
                if str(exc):
                    raise
                value = default
            raw_git(root, "config", key, value)
        try:
            validate_pin(root, oid)
        except FileCommitError as exc:
            raise LifecycleError(
                "original baseline submodule commit objects unavailable: " + oid
            ) from exc
        yield root, admin
