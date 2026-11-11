"""Export a pinned Git commit as a clean checkout: its tracked files and nothing else.

The export holds the commit's tracked files (and its pinned submodules and paired
Versioned Booley Directory), never the live working tree, untracked or ignored files,
or ``.git``. Git's built-in checkout machinery writes each blob straight to its final
path, so no file is held in memory and none is written twice. Git LFS and other
external filters are refused rather than approximated.

Goal Mode shares the byte-projection pieces (:class:`BytePolicy`,
:func:`shadow_repository`, :func:`pinned_attributes`, :func:`materialize_tree`,
:func:`configured_submodule_paths`); :func:`export_commit` is the Goal-neutral entry
point for the measurement environment.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from booley.core.boundary import is_str_list, require_dict
from booley.core.config_paths import resolve_toml
from booley.runtime.booley_dir import (
    BooleyDirOutsideCheckoutError,
    in_checkout_booley_dir,
    own_booley_dir,
    own_booley_dir_relative,
)
from booley.runtime.git_attributes_policy import GITATTRIBUTES_RULE
from booley.runtime.history_commit import FileCommitError
from booley.runtime.pinned_history import raw_git, tree_rows, validate_pin
from booley.runtime.project_repositories import (
    RepositoryCheckoutError,
    is_standalone_git_repository,
    paired_project_repository,
)

CHECKOUT_ATTRIBUTES = ("text", "eol", "filter", "working-tree-encoding", "ident", "crlf")
CHECKOUT_ATTRIBUTES_MARKER = ",".join(CHECKOUT_ATTRIBUTES)

# Blob modes that become regular files; ``120000`` is a symlink, ``160000`` a gitlink.
_REGULAR_MODES = {b"100644", b"100755"}

_FILE_TRANSFORM_ATTRIBUTES = ("filter", "working-tree-encoding")


class CommitExportError(ValueError):
    """The commit cannot be exported as a clean checkout."""


@dataclass(frozen=True)
class BytePolicy:
    """Checkout settings and canonical managed-rule replay applied to exported bytes."""

    autocrlf: str
    eol: str
    managed: bool

    def record(self) -> dict[str, str]:
        return {
            "checkout_attributes": CHECKOUT_ATTRIBUTES_MARKER,
            "autocrlf": self.autocrlf,
            "eol": self.eol,
            "managed_rule": GITATTRIBUTES_RULE if self.managed else "",
        }


# The measurement export depends only on the commit: committed ``.gitattributes`` and
# Git built-ins, never repository-local ``core.autocrlf``/``core.eol``/``info/attributes``.
HERMETIC_POLICY = BytePolicy(autocrlf="false", eol="lf", managed=False)


@dataclass(frozen=True)
class SubmoduleDrift:
    """A live submodule checkout that differs from the gitlink the export used."""

    path: PurePosixPath
    pinned: str
    head: str
    dirty: bool


@dataclass(frozen=True)
class CommitExport:
    """The exported checkout, drifted submodules, and the paired Booley Directory commit."""

    root: Path
    drift: tuple[SubmoduleDrift, ...]
    paired_commit: str | None


@contextmanager
def shadow_repository(repository: Path, managed: bool) -> Generator[tuple[Path, dict[str, str]]]:
    """Object-only scratch repository with only the explicitly captured local policy."""
    with tempfile.TemporaryDirectory(prefix="booley-pinned-bytes-") as scratch:
        shadow, template = Path(scratch) / "repository", Path(scratch) / "template"
        template.mkdir()
        identity = raw_git(repository, "rev-parse", "--show-object-format", "--git-common-dir")
        algorithm, common_text = os.fsdecode(identity).split("\n")[:2]
        raw_git(
            repository,
            "init",
            "-q",
            "--template=" + str(template),
            "--object-format=" + algorithm,
            str(shadow),
        )
        common = Path(common_text)
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
        if managed:
            info = shadow / ".git/info"
            info.mkdir()
            (info / "attributes").write_bytes(GITATTRIBUTES_RULE.encode() + b"\n")
        yield shadow, env


def pinned_attributes(
    repository: Path, commit: str, names: list[bytes], policy: BytePolicy
) -> dict[bytes, dict[str, str]]:
    """Checkout attributes of *names* from the commit's own committed ``.gitattributes``."""
    with shadow_repository(repository, policy.managed) as (shadow, env):
        raw_git(shadow, "read-tree", commit, env=env)
        result = raw_git(
            shadow,
            "-c",
            "core.attributesFile=" + os.devnull,
            "check-attr",
            "-z",
            "--stdin",
            *CHECKOUT_ATTRIBUTES,
            "--cached",
            env=env,
            input_bytes=b"".join(name + b"\0" for name in names),
        )
    return parse_attributes(result)


def parse_attributes(result: bytes) -> dict[bytes, dict[str, str]]:
    """Decode ``check-attr -z`` output into ``{path: {attribute: value}}``."""
    fields = result.rstrip(b"\0").split(b"\0") if result else []
    values: dict[bytes, dict[str, str]] = {}
    for i in range(0, len(fields), 3):
        name, key, value = fields[i : i + 3]
        values.setdefault(name, {})[key.decode("ascii")] = value.decode("utf-8")
    return values


def transformed_path(attrs: Mapping[bytes, Mapping[str, str]]) -> bytes | None:
    """First path whose ``filter`` or ``working-tree-encoding`` transforms its bytes."""
    for name, values in attrs.items():
        if any(values[key] not in {"unspecified", "unset"} for key in _FILE_TRANSFORM_ATTRIBUTES):
            return name
    return None


def materialize_tree(
    repository: Path,
    commit: str,
    destination: Path,
    rows: Mapping[bytes, bytes],
    policy: BytePolicy,
) -> None:
    """Write the commit's blobs under *destination* with Git's built-in conversion.

    Callers refuse transformations first, so nothing is written before a refusal. One
    ``checkout-index`` streams every regular blob to its final path; nothing is read
    back, and gitlinks are left to the caller.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with shadow_repository(repository, policy.managed) as (shadow, env):
        raw_git(shadow, "read-tree", commit, env=env)
        regular = b"".join(
            name + b"\0"
            for name, metadata in rows.items()
            if metadata.split()[0] in _REGULAR_MODES
        )
        if regular:
            raw_git(
                shadow,
                "-c",
                "core.attributesFile=" + os.devnull,
                "-c",
                "core.autocrlf=" + policy.autocrlf,
                "-c",
                "core.eol=" + policy.eol,
                "--work-tree=" + str(destination),
                "checkout-index",
                "--force",
                "-z",
                "--stdin",
                env=env,
                input_bytes=regular,
            )
    for name, metadata in rows.items():
        mode, _kind, oid = metadata.split()
        path = destination / os.fsdecode(name)
        if mode == b"120000":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(os.fsdecode(raw_git(repository, "cat-file", "blob", oid.decode())))
        elif mode in _REGULAR_MODES:
            path.chmod(0o755 if mode == b"100755" else 0o644)


def configured_submodule_paths(config: Path) -> tuple[str, ...] | None:
    """``[submodules].paths`` of an already exported Project config, never live policy."""
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
        raise CommitExportError("[submodules].paths must be an array of strings")
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
            raise CommitExportError("unsafe pinned submodule selection path: " + value)
    return tuple(paths)


def export_commit(repository: Path, commit: str, destination: Path) -> CommitExport:
    """Export *commit* of *repository* to a new *destination* directory, atomically.

    The destination is staged beside itself and renamed into place on success, so a
    failed or killed export never leaves a directory that looks complete.

    Raises:
        CommitExportError: the commit cannot be exported as a clean checkout.
    """
    try:
        return _export_commit(repository, commit, destination.absolute())
    except (FileCommitError, RepositoryCheckoutError) as exc:
        raise CommitExportError(str(exc) or "Git could not read the commit") from exc


def _export_commit(repository: Path, commit: str, destination: Path) -> CommitExport:
    validate_pin(repository, commit)
    bd_repository = _versioned_booley_dir(repository)
    _require_new_destination(repository, bd_repository, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=f".{destination.name}.export-"))
    try:
        drift, paired_commit = _populate(repository, commit, bd_repository, staging)
        staging.chmod(0o755)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return CommitExport(destination, drift, paired_commit)


def _versioned_booley_dir(repository: Path) -> Path | None:
    """The checkout's own Versioned Booley Directory, from its structure only.

    A linked (Paired Checkout) or standalone Git repository at the checkout's own
    Booley Directory; never ``BOOLEY_DIR`` or an ancestor search.
    """
    paired = paired_project_repository(repository)
    if paired is not None:
        return paired.worktree
    own = own_booley_dir(repository)
    return own if is_standalone_git_repository(own) else None


def _require_new_destination(
    repository: Path, bd_repository: Path | None, destination: Path
) -> None:
    if os.path.lexists(destination):
        raise CommitExportError(f"export destination already exists: {destination}")
    target = destination.parent.resolve() / destination.name
    live = [repository, own_booley_dir(repository)]
    if bd_repository is not None:
        live.append(bd_repository)
    for path in live:
        resolved = path.resolve()
        if target == resolved or target.is_relative_to(resolved):
            raise CommitExportError(
                f"export destination {destination} lies inside the live checkout path {path}"
            )


def _populate(
    repository: Path, commit: str, bd_repository: Path | None, root: Path
) -> tuple[tuple[SubmoduleDrift, ...], str | None]:
    rows = _export_repository(repository, commit, root, HERMETIC_POLICY)
    try:
        place = root / own_booley_dir_relative(root)
    except BooleyDirOutsideCheckoutError as exc:
        raise CommitExportError(
            f"{exc}; the export would resolve configuration from the live directory"
        ) from exc
    paired_commit = None
    excluded: frozenset[str] = frozenset()
    if bd_repository is not None:
        paired_commit = raw_git(bd_repository, "rev-parse", "HEAD").strip().decode("ascii")
        _export_repository(bd_repository, paired_commit, place, HERMETIC_POLICY)
        excluded = frozenset({own_booley_dir_relative(root).as_posix()})
    selected = configured_submodule_paths(resolve_toml(in_checkout_booley_dir(root)))
    drift: list[SubmoduleDrift] = []
    _export_gitlinks(repository, rows, root, PurePosixPath(), selected, excluded, drift)
    return tuple(drift), paired_commit


def _export_repository(
    repository: Path, commit: str, destination: Path, policy: BytePolicy
) -> dict[bytes, bytes]:
    """One repository's tree at *commit*, refusing collisions and transformations first."""
    rows = tree_rows(repository, commit)
    _refuse_case_collisions(rows)
    attrs = pinned_attributes(repository, commit, list(rows), policy)
    transformed = transformed_path(attrs)
    if transformed is not None:
        raise CommitExportError(
            f"unsupported committed input transformation: {os.fsdecode(transformed)}; "
            "Git LFS and other Git filters or working-tree-encoding attributes are not supported"
        )
    materialize_tree(repository, commit, destination, rows, policy)
    return rows


def _refuse_case_collisions(rows: Mapping[bytes, bytes]) -> None:
    """Refuse paths that a case-insensitive filesystem would merge or overwrite."""
    spellings: dict[str, str] = {}
    for name in rows:
        parts = PurePosixPath(os.fsdecode(name)).parts
        for end in range(1, len(parts) + 1):
            spelling = PurePosixPath(*parts[:end]).as_posix()
            earlier = spellings.setdefault(spelling.casefold(), spelling)
            if earlier != spelling:
                raise CommitExportError(
                    f"case-colliding paths cannot be exported: {earlier} and {spelling}"
                )


def _export_gitlinks(
    repository: Path,
    rows: Mapping[bytes, bytes],
    root: Path,
    prefix: PurePosixPath,
    selected: tuple[str, ...] | None,
    excluded: frozenset[str],
    drift: list[SubmoduleDrift],
) -> None:
    """Export each selected gitlink of *rows* at its pinned commit, recursively."""
    for name, metadata in rows.items():
        _mode, kind, oid = metadata.split()
        relative = os.fsdecode(name)
        if kind != b"commit" or relative in excluded:
            continue
        if selected is not None and relative not in selected:
            continue
        source = repository / relative
        shown = prefix / relative
        pinned = oid.decode("ascii")
        if not (source / ".git").exists() or not _has_commit(source, pinned):
            raise CommitExportError(
                f"submodule objects missing for {shown}: pinned commit {pinned} is not in "
                "the local objects; run git submodule update --init"
            )
        nested = _export_repository(source, pinned, root / relative, HERMETIC_POLICY)
        _record_drift(source, shown, pinned, drift)
        _export_gitlinks(source, nested, root / relative, shown, None, frozenset(), drift)


def _has_commit(repository: Path, oid: str) -> bool:
    try:
        raw_git(repository, "cat-file", "-e", f"{oid}^{{commit}}")
    except FileCommitError:
        return False
    return True


def _record_drift(
    source: Path, shown: PurePosixPath, pinned: str, drift: list[SubmoduleDrift]
) -> None:
    """Report, never raise, a live submodule that differs from its exported gitlink."""
    head = raw_git(source, "rev-parse", "HEAD").strip().decode("ascii")
    changes = raw_git(
        source,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--ignore-submodules=all",
    )
    if head != pinned or changes:
        drift.append(SubmoduleDrift(shown, pinned, head, dirty=bool(changes)))
