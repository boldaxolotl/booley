"""The wheel's baked-in build commit — one implementation, two build paths.

A wheel install has no adjacent ``.git``, so ``booley --version`` inside the
sandbox can only name the commit it was built from if the build bakes the answer
in. It does that by generating ``src/booley/_build_commit.py`` (gitignored,
transient) just before ``python -m build`` runs, which
``booley.harness.booley._packaged_commit`` reads back at runtime.

Both wheel builders go through this module — ``src/booley/data/docker/build.sh``
and ``booley init``'s in-process build
(``init_docker_image._docker_build_wheel``). init used to build unstamped, so
every init-driven image reported a bare ``booley <version>`` and the prescribed
"does the wheel match the commit?" freshness check was unanswerable exactly
where it matters (F-3).

Kept dependency-free (stdlib only, no imports from the rest of the harness) so
``build.sh`` can call it with nothing but ``PYTHONPATH=src``.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

#: Generated stamp module, relative to the Booley repo root. Gitignored.
STAMP_RELPATH = "src/booley/_build_commit.py"
DEVELOPMENT_CONTEXT_RELPATH = "src/booley/data/development-build-context.tar.gz"

_PAYLOAD_TREES = (
    "src/booley",
    "crates/bwave/src",
)
_PAYLOAD_FILES = (
    ".dockerignore",
    "pyproject.toml",
    "VERSION",
    "crates/bwave/Cargo.toml",
    "crates/bwave/Cargo.lock",
)
_PAYLOAD_EXCLUDED = frozenset({STAMP_RELPATH, DEVELOPMENT_CONTEXT_RELPATH})

_DEVELOPMENT_CONTEXT_TREES = (
    "src/booley",
    "crates/bwave/src",
    "crates/bwave/vendor",
)
_DEVELOPMENT_CONTEXT_FILES = (
    ".dockerignore",
    ".github/scripts/validate_installed_artifact.py",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "crates/bwave/Cargo.lock",
    "crates/bwave/Cargo.toml",
    "pyproject.toml",
)
_MAX_CONTEXT_MEMBERS = 5_000
_MAX_CONTEXT_BYTES = 64 * 1024 * 1024

_GIT_TIMEOUT_S = 15


def stamp_path(booley_root: Path) -> Path:
    """Where :func:`write_build_stamp` writes the generated stamp module."""
    return booley_root / STAMP_RELPATH


def development_context_path(booley_root: Path) -> Path:
    """Return the generated local-build context path for *booley_root*."""
    return booley_root / DEVELOPMENT_CONTEXT_RELPATH


def _git_output(booley_root: Path, *args: str) -> str:
    """``git -C <root> <args>`` stdout, or ``""`` when git can't answer.

    Not a git checkout, no git on PATH, a hung index — all mean the same thing
    to the caller (no commit to stamp), and none of them may abort a build that
    is otherwise fine, so they collapse to the empty string.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(booley_root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def resolve_build_commit(booley_root: Path) -> str:
    """Short HEAD of *booley_root*, ``+dirty`` when the tree has local changes.

    Returns ``""`` when there is no commit to report (not a checkout, no git).
    A dirty tree produces a wheel that matches no commit, so it says so rather
    than claiming the HEAD it almost-but-not-quite is.
    """
    commit = _git_output(booley_root, "rev-parse", "--short", "HEAD").strip()
    if not commit:
        return ""
    if _git_output(booley_root, "status", "--porcelain").strip():
        commit += "+dirty"
    return commit


def resolve_source_updated_at(booley_root: Path) -> str:
    """ISO-8601 commit time of HEAD, or ``""`` when git cannot answer."""
    return _git_output(booley_root, "log", "-1", "--format=%cI", "HEAD").strip()


def iter_payload_files(booley_root: Path) -> Iterator[Path]:
    """Yield the canonical source inputs baked into a Runtime Image payload."""
    for relative in _PAYLOAD_TREES:
        root = booley_root / relative
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.relative_to(booley_root).as_posix() in _PAYLOAD_EXCLUDED:
                continue
            yield path
    for relative in _PAYLOAD_FILES:
        path = booley_root / relative
        if path.is_file():
            yield path


def resolve_payload_fingerprint(booley_root: Path) -> str | None:
    """Return a path-and-content SHA-256 for every Runtime Image payload input."""
    files = sorted(set(iter_payload_files(booley_root)))
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(booley_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def embedded_payload_fingerprint() -> str | None:
    """Return the canonical fingerprint embedded in an installed wheel, if any."""
    try:
        from booley._build_commit import PAYLOAD_FINGERPRINT
    except (ImportError, AttributeError):
        return None
    return PAYLOAD_FINGERPRINT or None


def embedded_official_release() -> bool:
    """Return whether the installed wheel was built for official publication."""
    try:
        from booley._build_commit import OFFICIAL_RELEASE
    except (ImportError, AttributeError):
        return False
    return OFFICIAL_RELEASE is True


def _iter_development_context_files(booley_root: Path) -> Iterator[Path]:
    for relative in _DEVELOPMENT_CONTEXT_TREES:
        root = booley_root / relative
        if not root.is_dir():
            raise FileNotFoundError(f"development build context is missing {relative}")
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.relative_to(booley_root).as_posix()
            if name in _PAYLOAD_EXCLUDED:
                continue
            if path.is_symlink():
                raise ValueError(f"development build context contains a symlink: {name}")
            yield path
    for relative in _DEVELOPMENT_CONTEXT_FILES:
        path = booley_root / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"development build context is missing {relative}")
        yield path


def _write_development_context(booley_root: Path, target: Path) -> str:
    files = sorted(set(_iter_development_context_files(booley_root)))
    if len(files) > _MAX_CONTEXT_MEMBERS:
        raise ValueError("development build context has too many files")
    total = sum(path.stat().st_size for path in files)
    if total > _MAX_CONTEXT_BYTES:
        raise ValueError("development build context is too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with (
            temporary.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as archive,
        ):
            for path in files:
                name = path.relative_to(booley_root).as_posix()
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mtime = 0
                info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with path.open("rb") as source:
                    archive.addfile(info, source)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(target.read_bytes()).hexdigest()


def write_build_stamp(
    booley_root: Path,
    *,
    official_release: bool = False,
    include_development_context: bool | None = None,
) -> str:
    """Generate the stamp module for *booley_root*; return the stamped commit.

    An unknown commit is still stamped (as ``COMMIT = ""``) so the runtime
    reader has a module to import either way.
    """
    if type(official_release) is not bool or (
        include_development_context is not None and type(include_development_context) is not bool
    ):
        raise TypeError("build-stamp flags must be literal booleans")
    target = stamp_path(booley_root)
    context = development_context_path(booley_root)
    target.unlink(missing_ok=True)
    context.unlink(missing_ok=True)
    commit = resolve_build_commit(booley_root)
    payload_fingerprint = resolve_payload_fingerprint(booley_root) or ""
    include_context = (
        not official_release
        if include_development_context is None
        else include_development_context
    )
    try:
        context_sha256 = (
            _write_development_context(booley_root, context) if include_context else ""
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '"""Source commit baked in at wheel-build time (generated — do not edit)."""\n'
            "\n"
            f'COMMIT = "{commit}"\n'
            f'PAYLOAD_FINGERPRINT = "{payload_fingerprint}"\n'
            f"OFFICIAL_RELEASE = {official_release!r}\n"
            f'DEVELOPMENT_CONTEXT_SHA256 = "{context_sha256}"\n',
            encoding="utf-8",
        )
    except BaseException:
        target.unlink(missing_ok=True)
        context.unlink(missing_ok=True)
        raise
    return commit


def _validate_context_members(members: list[tarfile.TarInfo]) -> tuple[tarfile.TarInfo, ...]:
    if len(members) > _MAX_CONTEXT_MEMBERS:
        raise ValueError("development build context has too many members")
    validated: list[tarfile.TarInfo] = []
    names: set[str] = set()
    total = 0
    for member in members:
        name = member.name
        pure = PurePosixPath(name)
        canonical = pure.as_posix()
        if (
            not name
            or name in {".", ".."}
            or "\\" in name
            or pure.is_absolute()
            or ".." in pure.parts
            or canonical != name
            or name in names
            or not member.isfile()
            or member.size < 0
        ):
            raise ValueError(f"unsafe development build context member: {name!r}")
        names.add(name)
        total += member.size
        if total > _MAX_CONTEXT_BYTES:
            raise ValueError("development build context is too large")
        validated.append(member)
    if not validated:
        raise ValueError("development build context is empty")
    return tuple(validated)


def _extract_development_context(
    archive_path: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_payload_fingerprint: str,
    stamp_source: Path,
) -> None:
    if archive_path.stat().st_size > _MAX_CONTEXT_BYTES:
        raise ValueError("development build context archive is too large")
    actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("development build context hash does not match the wheel stamp")
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = _validate_context_members(archive.getmembers())
        for member in members:
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read development build context member {member.name}")
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    actual_payload = resolve_payload_fingerprint(destination)
    if actual_payload != expected_payload_fingerprint:
        raise ValueError("development build context payload does not match the wheel stamp")
    target_stamp = stamp_path(destination)
    target_stamp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(stamp_source, target_stamp)
    shutil.copyfile(archive_path, development_context_path(destination))


@contextlib.contextmanager
def extracted_development_context() -> Iterator[Path]:
    """Yield a verified repository-shaped build context from an installed wheel."""
    try:
        from booley._build_commit import DEVELOPMENT_CONTEXT_SHA256, PAYLOAD_FINGERPRINT
    except (ImportError, AttributeError) as exc:
        raise ValueError("development wheel has no embedded Runtime Image build context") from exc
    if (
        not isinstance(DEVELOPMENT_CONTEXT_SHA256, str)
        or len(DEVELOPMENT_CONTEXT_SHA256) != 64
        or any(character not in "0123456789abcdef" for character in DEVELOPMENT_CONTEXT_SHA256)
        or not isinstance(PAYLOAD_FINGERPRINT, str)
        or len(PAYLOAD_FINGERPRINT) != 64
        or any(character not in "0123456789abcdef" for character in PAYLOAD_FINGERPRINT)
    ):
        raise ValueError("development wheel has invalid build-context provenance")
    package_root = Path(__file__).resolve().parents[1]
    archive_path = package_root / "data" / Path(DEVELOPMENT_CONTEXT_RELPATH).name
    stamp_source = package_root / "_build_commit.py"
    with tempfile.TemporaryDirectory(prefix="booley-image-source-") as temporary:
        root = Path(temporary)
        _extract_development_context(
            archive_path,
            root,
            expected_sha256=DEVELOPMENT_CONTEXT_SHA256,
            expected_payload_fingerprint=PAYLOAD_FINGERPRINT,
            stamp_source=stamp_source,
        )
        yield root


@contextlib.contextmanager
def build_stamp(booley_root: Path, *, include_development_context: bool = False) -> Iterator[str]:
    """Stamp for the duration of a wheel build, then remove the stamp.

    The stamp only has to survive until the wheel is built. Leaving it behind
    makes the checkout report a baked commit it does not have (and fails
    ``test_absent_stamp_module_yields_none``), so removal is unconditional.
    """
    commit = write_build_stamp(
        booley_root,
        include_development_context=include_development_context,
    )
    try:
        yield commit
    finally:
        stamp_path(booley_root).unlink(missing_ok=True)
        development_context_path(booley_root).unlink(missing_ok=True)
