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

import ast
import contextlib
import gzip
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from enum import Enum
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

# Inputs which determine the Python wheel.  Native B-Wave sources and image
# recipes deliberately live in substrate fingerprints instead: changing either
# must not make a Python-only overlay look incompatible, and changing Python
# must not invalidate the EDA/toolchain substrates below it.
_WHEEL_SOURCE_TREES = ("src/booley",)
_WHEEL_SOURCE_FILES = ("pyproject.toml", "VERSION")

# Keep every non-generated tree read by the B-Wave Cargo build. Cargo validates
# declared bench paths while parsing the manifest, and the binary embeds docs
# and the schema at compile time.
_DEVELOPMENT_CONTEXT_TREES = (
    "src/booley",
    "crates/bwave/src",
    "crates/bwave/benches",
    "crates/bwave/docs",
    "crates/bwave/schema",
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


class BuildProfile(Enum):
    """Artifact purpose controlling release attestation and embedded inputs."""

    DEVELOPMENT_WHEEL = "development-wheel"
    OFFICIAL_RELEASE = "official-release"
    RUNTIME_IMAGE = "runtime-image"


def stamp_path(booley_root: Path) -> Path:
    """Where :func:`write_build_stamp` writes the generated stamp module."""
    return booley_root / STAMP_RELPATH


def development_context_path(booley_root: Path) -> Path:
    """Return the generated local-build context path for *booley_root*."""
    return booley_root / DEVELOPMENT_CONTEXT_RELPATH


def embedded_development_context_path() -> Path:
    """Return the development context shipped beside this installed module."""
    package_root = Path(__file__).resolve().parents[1]
    return package_root / "data" / Path(DEVELOPMENT_CONTEXT_RELPATH).name


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
    """Yield the canonical source inputs baked into a Sandbox Image payload."""
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
    """Return a path-and-content SHA-256 for every Sandbox Image payload input."""
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


def iter_wheel_source_files(booley_root: Path) -> Iterator[Path]:
    """Yield exactly the checkout inputs which determine the Booley wheel."""
    for relative in _WHEEL_SOURCE_TREES:
        root = booley_root / relative
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.relative_to(booley_root).as_posix() in _PAYLOAD_EXCLUDED:
                continue
            yield path
    for relative in _WHEEL_SOURCE_FILES:
        path = booley_root / relative
        if path.is_file():
            yield path


def resolve_wheel_source_fingerprint(booley_root: Path) -> str | None:
    """Return the deterministic compatibility identity of a wheel's sources."""
    files = sorted(set(iter_wheel_source_files(booley_root)))
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(booley_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def wheel_embedded_source_fingerprint(wheel: Path) -> str:
    """Read the wheel-source identity from one built wheel without importing it."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            source = archive.read("booley/_build_commit.py").decode("utf-8")
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError(f"could not read wheel build provenance from {wheel}: {exc}") from exc
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"wheel build provenance is invalid in {wheel}") from exc
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id == "WHEEL_SOURCE_FINGERPRINT":
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and len(value) == 64:
                return value
            break
    raise ValueError(f"wheel build provenance has no valid wheel-source fingerprint: {wheel}")


def embedded_payload_fingerprint() -> str | None:
    """Return the canonical fingerprint embedded in an installed wheel, if any."""
    try:
        from booley._build_commit import PAYLOAD_FINGERPRINT
    except (ImportError, AttributeError):
        return None
    return PAYLOAD_FINGERPRINT or None


def embedded_wheel_source_fingerprint() -> str | None:
    """Return the wheel-source identity embedded in an installed wheel."""
    try:
        from booley._build_commit import WHEEL_SOURCE_FINGERPRINT
    except (ImportError, AttributeError):
        return None
    return WHEEL_SOURCE_FINGERPRINT or None


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
    completed = False
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
        completed = True
    finally:
        if not completed:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def write_build_stamp(
    booley_root: Path,
    *,
    profile: BuildProfile = BuildProfile.DEVELOPMENT_WHEEL,
) -> str:
    """Generate the stamp module for *booley_root*; return the stamped commit.

    An unknown commit is still stamped (as ``COMMIT = ""``) so the runtime
    reader has a module to import either way.
    """
    if not isinstance(profile, BuildProfile):
        raise TypeError("build profile must be a BuildProfile")
    target = stamp_path(booley_root)
    context = development_context_path(booley_root)
    target.unlink(missing_ok=True)
    context.unlink(missing_ok=True)
    commit = resolve_build_commit(booley_root)
    from booley.runtime.image_build_contracts import source_image_build_contracts

    image_contracts = source_image_build_contracts(booley_root)
    payload_fingerprint = resolve_payload_fingerprint(booley_root) or ""
    wheel_source_fingerprint = resolve_wheel_source_fingerprint(booley_root) or ""
    official_release = profile is BuildProfile.OFFICIAL_RELEASE
    include_context = profile is BuildProfile.DEVELOPMENT_WHEEL
    completed = False
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
            f'WHEEL_SOURCE_FINGERPRINT = "{wheel_source_fingerprint}"\n'
            f'RUNTIME_BASE_CONTRACT = "{image_contracts.runtime_base}"\n'
            f'STANDARD_SUBSTRATE_CONTRACT = "{image_contracts.standard_substrate}"\n'
            f"OFFICIAL_RELEASE = {official_release!r}\n"
            f'DEVELOPMENT_CONTEXT_SHA256 = "{context_sha256}"\n',
            encoding="utf-8",
        )
        completed = True
    finally:
        if not completed:
            target.unlink(missing_ok=True)
            context.unlink(missing_ok=True)
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


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@contextlib.contextmanager
def extracted_development_context() -> Iterator[Path]:
    """Yield a verified repository-shaped build context from an installed wheel."""
    try:
        from booley._build_commit import DEVELOPMENT_CONTEXT_SHA256, PAYLOAD_FINGERPRINT
    except (ImportError, AttributeError) as exc:
        raise ValueError("development wheel has no embedded Sandbox Image build context") from exc
    if not _is_sha256(DEVELOPMENT_CONTEXT_SHA256) or not _is_sha256(PAYLOAD_FINGERPRINT):
        raise ValueError("development wheel has invalid build-context provenance")
    package_root = Path(__file__).resolve().parents[1]
    archive_path = embedded_development_context_path()
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
def build_stamp(booley_root: Path) -> Iterator[str]:
    """Stamp for the duration of a wheel build, then remove the stamp.

    The stamp only has to survive until the wheel is built. Leaving it behind
    makes the checkout report a baked commit it does not have (and fails
    ``test_absent_stamp_module_yields_none``), so removal is unconditional.
    """
    commit = write_build_stamp(booley_root, profile=BuildProfile.RUNTIME_IMAGE)
    try:
        yield commit
    finally:
        stamp_path(booley_root).unlink(missing_ok=True)
        development_context_path(booley_root).unlink(missing_ok=True)
