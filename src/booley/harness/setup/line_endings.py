"""Project-repository line-ending inspection and reconciliation.

Owns Git discovery, container-safety policy, guarded worktree normalization,
index reconciliation, and verification for every repository supplying Project
files. Project Initialization and Doctor are adapters over this module.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from booley.commit_policy.policy import stealth_enabled
from booley.runtime.git_attributes_policy import (
    GITATTRIBUTES_RULE,
    fallback_user_attributes,
    has_attribute_policy,
    has_managed_attributes,
    local_policy_owned,
    native_attribute_path,
    system_attributes_disabled,
)

LineEndingRole = Literal["project-checkout", "project-data"]


class LineEndingMode(StrEnum):
    """Whether to inspect only or reconcile safe forward changes."""

    INSPECT = "inspect"
    REPAIR = "repair"


class LineEndingStatus(StrEnum):
    """Aggregate or per-repository safety verdict."""

    SAFE = "safe"
    UNSAFE = "unsafe"
    NOT_APPLICABLE = "not-applicable"


class LineEndingObservationCode(StrEnum):
    """Stable facts consumed by Project Initialization and Doctor adapters."""

    AUTOCRLF_UNREADABLE = "autocrlf-unreadable"
    LOCAL_AUTOCRLF_UNREADABLE = "local-autocrlf-unreadable"
    EOL_SCAN_UNREADABLE = "eol-scan-unreadable"
    STATUS_UNREADABLE = "status-unreadable"
    AUTOCRLF_EFFECTIVE_TRUE = "autocrlf-effective-true"
    AUTOCRLF_NOT_PINNED = "autocrlf-not-pinned"
    CRLF_MISMATCH = "crlf-mismatch"
    STALE_INDEX = "stale-index"
    CANDIDATE_UNSAFE = "candidate-unsafe"
    LOCAL_POLICY_MISSING = "local-policy-missing"
    LEAKED_ROOT_POLICY = "leaked-root-policy"
    LOCAL_POLICY_CONFLICT = "local-policy-conflict"
    UPSTREAM_POLICY = "upstream-policy"


class LineEndingActionKind(StrEnum):
    """Stable reconciliation actions exposed without implementation state."""

    PIN_AUTOCRLF = "pin-autocrlf"
    NORMALIZE_FILES = "normalize-files"
    REFRESH_INDEX = "refresh-index"
    PUBLISH_ATTRIBUTES = "publish-attributes"


class LineEndingActionState(StrEnum):
    """Outcome of one planned or attempted action."""

    PLANNED = "planned"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class AttributesTarget:
    """Verified publication destination and its Git scope."""

    path: Path
    local: bool


@dataclass(frozen=True)
class AutocrlfSetting:
    """One resolved Git Boolean and whether that scope sets it explicitly."""

    value: bool
    is_set: bool


@dataclass(frozen=True)
class LineEndingRepository:
    """One distinct Git worktree whose files enter the Sandbox."""

    role: LineEndingRole
    root: Path


@dataclass(frozen=True)
class RepositoryDiscoveryFailure:
    """An expected repository candidate whose Git root could not be resolved."""

    role: LineEndingRole
    candidate: Path
    detail: str


@dataclass(frozen=True)
class RepositoryDiscovery:
    """Ordered distinct Git roots plus candidates that could not be inspected."""

    repositories: tuple[LineEndingRepository, ...]
    failures: tuple[RepositoryDiscoveryFailure, ...]


@dataclass(frozen=True)
class LineEndingObservation:
    """One stable repository fact with adapter-safe context."""

    code: LineEndingObservationCode
    count: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class LineEndingActionResult:
    """One public reconciliation outcome."""

    kind: LineEndingActionKind
    state: LineEndingActionState
    count: int | None = None
    detail: str | None = None
    target: AttributesTarget | None = None


@dataclass(frozen=True)
class RepositoryLineEndingReport:
    """Typed result for one distinct Project repository."""

    repository: LineEndingRepository
    status: LineEndingStatus
    observations: tuple[LineEndingObservation, ...]
    actions: tuple[LineEndingActionResult, ...]


@dataclass(frozen=True)
class LineEndingReport:
    """One aggregate inspection or reconciliation result."""

    status: LineEndingStatus
    repositories: tuple[RepositoryLineEndingReport, ...]
    discovery_failures: tuple[RepositoryDiscoveryFailure, ...]


@dataclass(frozen=True)
class _FileIdentity:
    """Content and filesystem identity used by guarded file publication."""

    digest: bytes
    device: int
    inode: int
    mode: int
    link_count: int


@dataclass(frozen=True)
class _CandidateSnapshot:
    """Private compare-before-write state for one tracked candidate."""

    file: _FileIdentity
    index_entry: bytes
    attributes: bytes
    index_flags: bytes


@dataclass(frozen=True)
class _IndexPathState:
    """Restorable content entry and extended flags for one index path."""

    entry: bytes
    flags: bytes


@dataclass(frozen=True)
class _RepositoryPlan:
    """Private guarded transformation plan for one repository."""

    repository: LineEndingRepository
    effective_autocrlf: AutocrlfSetting
    local_autocrlf: AutocrlfSetting
    crlf_paths: tuple[str, ...]
    phantom_paths: tuple[str, ...]
    candidates: dict[str, _CandidateSnapshot]
    clean: bool | None
    candidate_error: str | None
    attributes: _FileIdentity | None
    attributes_error: str | None
    owns_attributes_policy: bool
    target: AttributesTarget
    policy_inputs: dict[str, tuple[_FileIdentity | None, bytes]]
    local_missing: bool


@dataclass(frozen=True)
class _NormalizationResult:
    """Private partial-progress result for guarded worktree replacement."""

    rewritten: tuple[str, ...]
    file_snapshots: dict[str, _FileIdentity]
    error: str | None = None


def line_ending_repository_display(role: LineEndingRole, root: Path) -> str:
    """Render one repository role and path consistently across init and Doctor."""
    label = "project checkout" if role == "project-checkout" else "project data"
    return f"{label} ({root})"


def _read_only_git_env() -> dict[str, str]:
    """Prevent observational Git commands from opportunistically locking the index."""
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GIT_DIR",
                "GIT_COMMON_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_PREFIX",
                "GIT_CEILING_DIRECTORIES",
                "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            }
        },
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _output_bytes(output: bytes | str | None) -> bytes:
    if isinstance(output, str):
        return output.encode(errors="surrogateescape")
    return output or b""


def _error_text(output: bytes | str | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace").strip()
    return (output or "").strip()


def _run_git_worktree_probe(
    candidate: Path,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    try:
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(candidate),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            timeout=10,
            env=_read_only_git_env(),
        )
    except FileNotFoundError:
        return None, "git unavailable"
    except subprocess.SubprocessError as exc:
        return None, f"Git probe failed: {exc}"
    except OSError as exc:
        return None, f"Git probe failed: {exc}"
    return probe, None


def _probe_git_worktree(
    role: LineEndingRole, candidate: Path
) -> tuple[LineEndingRepository | None, RepositoryDiscoveryFailure | None]:
    if not candidate.is_dir():
        return None, RepositoryDiscoveryFailure(role, candidate, "directory does not exist")
    probe, error = _run_git_worktree_probe(candidate)
    if error is not None:
        return None, RepositoryDiscoveryFailure(role, candidate, error)
    assert probe is not None
    return _parse_git_worktree_probe(role, candidate, probe)


def _parse_git_worktree_probe(
    role: LineEndingRole,
    candidate: Path,
    probe: subprocess.CompletedProcess[str],
) -> tuple[LineEndingRepository | None, RepositoryDiscoveryFailure | None]:
    if probe.returncode != 0:
        detail = probe.stderr.strip() or "not a Git repository"
        if "not a git repository" in detail.casefold():
            return None, None
        return None, RepositoryDiscoveryFailure(role, candidate, detail)
    rendered = probe.stdout.rstrip("\r\n")
    if not rendered:
        return None, RepositoryDiscoveryFailure(role, candidate, "Git returned an empty top-level")
    root = Path(rendered)
    if not root.is_absolute():
        detail = f"Git returned a non-absolute top-level: {rendered!r}"
        return None, RepositoryDiscoveryFailure(role, candidate, detail)
    return LineEndingRepository(role, root.resolve()), None


def discover_line_ending_repositories(
    project_root: Path, project_dir: Path | None = None
) -> RepositoryDiscovery:
    """Resolve the distinct Git worktrees that supply one Project's files."""
    candidates: list[tuple[LineEndingRole, Path]] = [("project-checkout", project_root)]
    if project_dir is not None:
        candidates.append(("project-data", project_dir))
    repositories: list[LineEndingRepository] = []
    failures: list[RepositoryDiscoveryFailure] = []
    seen: set[str] = set()
    for role, candidate in candidates:
        repository, failure = _probe_git_worktree(role, candidate)
        if failure is not None:
            failures.append(failure)
            continue
        if repository is None:
            continue
        key = os.path.normcase(str(repository.root))
        if key in seen:
            continue
        seen.add(key)
        repositories.append(repository)
    return RepositoryDiscovery(tuple(repositories), tuple(failures))


def _parse_crlf_mismatches(output: str) -> list[str]:
    """Extract tracked paths whose index and worktree line endings differ."""
    paths: list[str] = []
    for record in output.split("\0"):
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or not path or len(fields) < 2:
            continue
        index_eol, worktree_eol = fields[0], fields[1]
        if worktree_eol not in ("w/crlf", "w/mixed"):
            continue
        if index_eol[2:] == worktree_eol[2:]:
            continue
        if any(field.lower() == "eol=crlf" for field in fields[2:]):
            continue
        paths.append(path)
    return paths


def _crlf_worktree_files(project_root: Path) -> list[str] | None:
    """Return tracked paths that Linux sees modified due only to checkout EOLs."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--eol", "-z"],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_crlf_mismatches(proc.stdout)


def pending_normalization_paths(
    project_root: Path, project_dir: Path | None = None
) -> tuple[Path, ...]:
    """Absolute tracked paths that a repair run would normalize to LF."""
    discovery = discover_line_ending_repositories(project_root, project_dir)
    paths: list[Path] = []
    for repository in discovery.repositories:
        names = _crlf_worktree_files(repository.root) or []
        paths.extend(repository.root / name for name in names)
    return tuple(paths)


def read_autocrlf_setting(project_root: Path, *, local: bool = False) -> AutocrlfSetting | None:
    """Read effective or repo-local ``core.autocrlf`` and its presence."""
    command = ["git", "-C", str(project_root), "config"]
    if local:
        command.append("--local")
    command.extend(["--bool", "--get", "core.autocrlf"])
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode == 1 and not proc.stdout.strip():
        return AutocrlfSetting(False, is_set=False)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip().lower()
    if value not in {"true", "false"}:
        return None
    return AutocrlfSetting(value == "true", is_set=True)


def _eol_policy_is_user_owned(project_root: Path) -> bool:
    """Does the root ``.gitattributes`` already set an all-files eol policy?

    A ``*``-pattern line carrying ``text``/``-text``/``eol=`` means the project
    has stated its own whole-tree policy. Whatever it says, it is a deliberate
    choice and init must not second-guess it — we neither add our rule nor
    argue with theirs.
    """
    path = project_root / ".gitattributes"
    try:
        lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2 or fields[0] != "*":
            continue
        if any(a in {"text", "-text"} or a.startswith(("text=", "eol=")) for a in fields[1:]):
            return True
    return False


def _worktree_is_clean(project_root: Path) -> bool | None:
    """Is the host-side working tree free of tracked uncommitted changes?

    Sampled *before* init touches anything (see
    :func:`sample_worktree_cleanliness`), because the CRLF fix itself moves
    this answer: with ``core.autocrlf=true`` the clean filter hides the CRLF
    from ``git status`` on the host, and flipping the knob can expose those
    same files as modified. Only the pre-fix reading tells us whether the user
    has real work in the tree. Untracked files are ignored because normalization
    rewrites only exact paths reported by ``git ls-files``. None = git could not
    answer.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return not proc.stdout.strip()


def _path_diff_code(
    project_root: Path, name: str, *, cached: bool
) -> tuple[int | None, str | None]:
    command = ["git", "--literal-pathspecs", "-C", str(project_root), "diff"]
    if cached:
        command.append("--cached")
    command.extend(["--quiet", "--ignore-submodules=none", "--", name])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"`{' '.join(command)}` failed: {exc}"
    if result.returncode not in (0, 1):
        detail = _error_text(result.stderr) or "no stderr"
        return None, f"`{' '.join(command)}` exited {result.returncode}: {detail}"
    return result.returncode, None


def _tracked_phantom_paths(project_root: Path) -> tuple[list[str] | None, str | None]:
    """Tracked status entries that have neither staged nor unstaged content diffs."""
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain",
                "-z",
                "--untracked-files=no",
            ],
            capture_output=True,
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"`git -C {project_root} status --porcelain` failed: {exc}"
    if status.returncode != 0:
        detail = _error_text(status.stderr) or "no stderr"
        return None, f"`git -C {project_root} status --porcelain` failed: {detail}"

    paths: list[str] = []
    for record in _output_bytes(status.stdout).split(b"\0"):
        if len(record) < 4 or record[:3] not in (b" M ", b"M  "):
            continue
        name = os.fsdecode(record[3:])
        unstaged, error = _path_diff_code(project_root, name, cached=False)
        if error is not None:
            return None, error
        staged, error = _path_diff_code(project_root, name, cached=True)
        if error is not None:
            return None, error
        if unstaged == staged == 0:
            paths.append(name)
    return paths, None


def _protected_index_paths(project_root: Path, paths: list[str]) -> list[str] | None:
    """Affected paths hidden from normal status by Git index flags."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-v", "-z"],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None

    affected = set(paths)
    protected: list[str] = []
    for record in proc.stdout.split("\0"):
        tag, separator, path = record.partition(" ")
        if not separator or path not in affected:
            continue
        if tag == "S" or tag.islower():
            protected.append(path)
    return protected


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _path_git_bytes(
    project_root: Path, command: tuple[str, ...], name: str
) -> tuple[bytes | None, str | None]:
    try:
        proc = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(project_root), *command, "--", name],
            capture_output=True,
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"could not inspect tracked path {name!r}: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip() or "Git probe failed"
        return None, f"could not inspect tracked path {name!r}: {detail}"
    return proc.stdout, None


def _candidate_git_state(
    project_root: Path, name: str
) -> tuple[tuple[bytes, bytes, bytes] | None, str | None]:
    probes = (
        ("ls-files", "--stage", "-z"),
        ("check-attr", "-z", "--all"),
        ("ls-files", "-v", "-z"),
    )
    outputs: list[bytes] = []
    for command in probes:
        output, error = _path_git_bytes(project_root, command, name)
        if error is not None:
            return None, error
        assert output is not None
        outputs.append(output)
    if not outputs[0]:
        return None, f"tracked path disappeared during line-ending repair: {name!r}"
    return (outputs[0], outputs[1], outputs[2]), None


def _candidate_snapshot(
    project_root: Path, name: str
) -> tuple[_CandidateSnapshot | None, str | None]:
    path = project_root / name
    identity, error = _optional_file_identity(path)
    if error is not None:
        return None, error
    if identity is None:
        return None, f"tracked path disappeared during line-ending repair: {name!r}"
    if identity.link_count > 1:
        return None, f"refusing to normalize hard-linked tracked path {name!r}"
    git_state, error = _candidate_git_state(project_root, name)
    if error is not None:
        return None, error
    assert git_state is not None
    index_entry, attributes, index_flags = git_state
    return (
        _CandidateSnapshot(
            file=identity,
            index_entry=index_entry,
            attributes=attributes,
            index_flags=index_flags,
        ),
        None,
    )


def _snapshot_candidates(
    project_root: Path, paths: list[str]
) -> tuple[dict[str, _CandidateSnapshot], str | None]:
    snapshots: dict[str, _CandidateSnapshot] = {}
    for name in paths:
        snapshot, error = _candidate_snapshot(project_root, name)
        if error is not None:
            return {}, error
        assert snapshot is not None
        snapshots[name] = snapshot
    return snapshots, None


def _optional_file_identity(path: Path) -> tuple[_FileIdentity | None, str | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not inspect {path.name!r}: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return None, f"refusing to update non-regular {path.name!r}"
    try:
        digest = _file_digest(path)
    except OSError as exc:
        return None, f"could not inspect {path.name!r}: {exc}"
    return (
        _FileIdentity(
            digest=digest,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            link_count=metadata.st_nlink,
        ),
        None,
    )


def _staged_paths(project_root: Path, output: bytes) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    for record in output.split(b"\0"):
        temporary, separator, original = record.partition(b"\t")
        if separator and temporary and original:
            staged[os.fsdecode(original)] = project_root / os.fsdecode(temporary)
    return staged


def _cleanup_staged_files(paths: dict[str, Path]) -> None:
    for path in paths.values():
        with suppress(FileNotFoundError, PermissionError):
            path.unlink()


def _stage_atomic_content(
    path: Path, content: bytes, *, mode: int
) -> tuple[Path | None, str | None]:
    """Write complete replacement content beside its destination."""
    staged: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".booley-eol-", dir=path.parent)
        staged = Path(temporary)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        staged.chmod(stat.S_IMODE(mode))
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if staged is not None:
            with suppress(OSError):
                staged.unlink()
        return None, f"could not stage {path.name!r}: {exc}"
    return staged, None


def _stage_lf_files(project_root: Path, paths: list[str]) -> tuple[dict[str, Path], str | None]:
    """Materialize checkout-filtered LF replacements without touching the worktree."""
    encoded_paths = b"\0".join(os.fsencode(name) for name in paths) + b"\0"
    try:
        proc = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(project_root),
                "checkout-index",
                "--temp",
                "-z",
                "--stdin",
            ],
            input=encoded_paths,
            capture_output=True,
            check=False,
            timeout=300,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {}, f"could not stage LF replacements: {exc}"
    staged = _staged_paths(project_root, proc.stdout)
    if proc.returncode != 0 or set(staged) != set(paths):
        _cleanup_staged_files(staged)
        detail = proc.stderr.decode(errors="replace").strip() or "incomplete Git output"
        return {}, f"could not stage LF replacements: {detail}"
    return staged, None


def _rewrite_from_stage(  # noqa: PLR0911 -- each refusal is a pre-write safety gate
    project_root: Path,
    name: str,
    replacement: Path,
    expected: _CandidateSnapshot,
) -> str | None:
    """Publish only a still-identical replacement that changes line endings alone."""
    path = project_root / name
    try:
        replacement_bytes = replacement.read_bytes()
        if path.read_bytes().replace(b"\r\n", b"\n") != replacement_bytes:
            return f"refusing to discard content edits during line-ending repair: {name!r}"
    except OSError as exc:
        return f"could not normalize {name!r}: {exc}"
    staged, error = _stage_atomic_content(path, replacement_bytes, mode=expected.file.mode)
    if error is not None:
        return error
    assert staged is not None
    try:
        current, error = _candidate_snapshot(project_root, name)
        if error is not None:
            return error
        if current != expected:
            return f"tracked path changed during line-ending repair: {name!r}"
        current_file, error = _optional_file_identity(path)
        if error is not None:
            return error
        if current_file != expected.file:
            return f"tracked path changed during line-ending repair: {name!r}"
        staged.replace(path)
        return None
    except OSError as exc:
        return f"could not atomically normalize {name!r}: {exc}"
    finally:
        with suppress(FileNotFoundError, PermissionError):
            staged.unlink()


def _index_path_states(
    project_root: Path, paths: list[str]
) -> tuple[dict[str, _IndexPathState] | None, str | None]:
    states: dict[str, _IndexPathState] = {}
    for name in paths:
        entry, error = _path_git_bytes(project_root, ("ls-files", "--stage", "-z"), name)
        if error is not None:
            return None, error
        flags, error = _path_git_bytes(project_root, ("ls-files", "-v", "-z"), name)
        if error is not None:
            return None, error
        assert entry is not None and flags is not None
        states[name] = _IndexPathState(entry, flags)
    return states, None


def _update_index_paths(project_root: Path, option: str, paths: list[str]) -> str | None:
    if not paths:
        return None
    payload = b"\0".join(os.fsencode(name) for name in paths) + b"\0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "update-index", option, "-z", "--stdin"],
            input=payload,
            capture_output=True,
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return str(exc)
    if proc.returncode == 0:
        return None
    return proc.stderr.decode(errors="replace").strip() or "Git update-index failed"


def _index_flag_groups(states: dict[str, _IndexPathState]) -> tuple[list[str], list[str]]:
    assume_unchanged: list[str] = []
    skip_worktree: list[str] = []
    for name, state in states.items():
        tag = state.flags[:1]
        if tag.upper() == b"S":
            skip_worktree.append(name)
        if tag.islower():
            assume_unchanged.append(name)
    return assume_unchanged, skip_worktree


def _restore_index_state(project_root: Path, before: dict[str, _IndexPathState]) -> str | None:
    payload = b"".join(state.entry for state in before.values())
    try:
        restored = subprocess.run(
            ["git", "-C", str(project_root), "update-index", "-z", "--index-info"],
            input=payload,
            capture_output=True,
            check=False,
            timeout=60,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return str(exc)
    if restored.returncode != 0:
        return restored.stderr.decode(errors="replace").strip() or "Git update-index failed"
    names = list(before)
    for option in ("--no-assume-unchanged", "--no-skip-worktree"):
        if error := _update_index_paths(project_root, option, names):
            return error
    assume_unchanged, skip_worktree = _index_flag_groups(before)
    if error := _update_index_paths(project_root, "--assume-unchanged", assume_unchanged):
        return error
    return _update_index_paths(project_root, "--skip-worktree", skip_worktree)


def _restore_and_verify_index_state(project_root: Path, before: dict[str, _IndexPathState]) -> str:
    error = _restore_index_state(project_root, before)
    if error is not None:
        return f"could not restore exact index state: {error}"
    restored, error = _index_path_states(project_root, list(before))
    if error is not None:
        return f"restored index state but could not verify it: {error}"
    if restored != before:
        return "index restoration did not restore exact entries and flags"
    return "restored exact index entries and flags"


def _refresh_normalized_index(project_root: Path, paths: list[str]) -> str | None:
    """Refresh path metadata and compensate any content or flag mutation."""
    before, error = _index_path_states(project_root, paths)
    if error is not None:
        return f"could not snapshot normalized Git index state: {error}"
    assert before is not None
    encoded_paths = b"\0".join(os.fsencode(name) for name in paths) + b"\0"
    try:
        proc = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(project_root),
                "add",
                "-u",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input=encoded_paths,
            capture_output=True,
            check=False,
            timeout=300,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        recovery = _restore_and_verify_index_state(project_root, before)
        return f"could not refresh normalized Git index entries: {exc}; {recovery}"
    after, error = _index_path_states(project_root, paths)
    if error is not None:
        recovery = _restore_and_verify_index_state(project_root, before)
        return f"could not verify normalized Git index state: {error}; {recovery}"
    assert after is not None
    command_error = None
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip() or "Git add failed"
        command_error = f"could not refresh normalized Git index entries: {detail}"
    if after == before:
        return command_error
    recovery = _restore_and_verify_index_state(project_root, before)
    return f"normalization unexpectedly changed index content or flags; {recovery}"


def _normalization_refusal_error(project_root: Path, paths: list[str]) -> str | None:
    protected = _protected_index_paths(project_root, paths)
    if protected is None:
        return "could not inspect Git index flags — refusing to normalize files"
    if not protected:
        return None
    names = ", ".join(repr(name) for name in protected[:3])
    suffix = " …" if len(protected) > 3 else ""
    return (
        f"refusing to normalize Git-protected path(s): {names}{suffix} "
        "(skip-worktree or assume-unchanged may hide local edits)"
    )


def _normalize_as_lf(
    project_root: Path,
    paths: list[str],
    snapshots: dict[str, _CandidateSnapshot],
) -> _NormalizationResult:
    """Stage replacements, then rewrite only still-identical candidates."""
    refusal = _normalization_refusal_error(project_root, paths)
    if refusal is not None:
        return _NormalizationResult((), {}, refusal)
    staged, error = _stage_lf_files(project_root, paths)
    if error is not None:
        return _NormalizationResult((), {}, error)
    rewritten: list[str] = []
    file_snapshots: dict[str, _FileIdentity] = {}
    try:
        for name in paths:
            error = _rewrite_from_stage(project_root, name, staged[name], snapshots[name])
            if error is not None:
                break
            rewritten.append(name)
            current, snapshot_error = _optional_file_identity(project_root / name)
            if snapshot_error is not None:
                error = snapshot_error
                break
            assert current is not None
            file_snapshots[name] = current
    finally:
        _cleanup_staged_files(staged)
    if rewritten:
        refresh_error = _refresh_normalized_index(project_root, rewritten)
        if refresh_error is not None:
            error = f"{error}; {refresh_error}" if error else refresh_error
    return _NormalizationResult(tuple(rewritten), file_snapshots, error)


def _observation(
    code: LineEndingObservationCode,
    *,
    count: int | None = None,
    detail: str | None = None,
) -> LineEndingObservation:
    return LineEndingObservation(code, count=count, detail=detail)


def _action(
    kind: LineEndingActionKind,
    state: LineEndingActionState,
    *,
    count: int | None = None,
    detail: str | None = None,
    target: AttributesTarget | None = None,
) -> LineEndingActionResult:
    return LineEndingActionResult(kind, state, count=count, detail=detail, target=target)


def _read_required_policy(
    repository: LineEndingRepository,
) -> tuple[AutocrlfSetting | None, AutocrlfSetting | None, list[LineEndingObservation]]:
    observations: list[LineEndingObservation] = []
    effective = read_autocrlf_setting(repository.root)
    if effective is None:
        observations.append(_observation(LineEndingObservationCode.AUTOCRLF_UNREADABLE))
        return None, None, observations
    local = read_autocrlf_setting(repository.root, local=True)
    if local is None:
        observations.append(_observation(LineEndingObservationCode.LOCAL_AUTOCRLF_UNREADABLE))
        return effective, None, observations
    if effective.value:
        observations.append(_observation(LineEndingObservationCode.AUTOCRLF_EFFECTIVE_TRUE))
    elif not local.is_set:
        observations.append(_observation(LineEndingObservationCode.AUTOCRLF_NOT_PINNED))
    return effective, local, observations


def _read_worktree_observations(
    repository: LineEndingRepository,
) -> tuple[list[str] | None, list[str] | None, list[LineEndingObservation]]:
    observations: list[LineEndingObservation] = []
    crlf_paths = _crlf_worktree_files(repository.root)
    if crlf_paths is None:
        observations.append(_observation(LineEndingObservationCode.EOL_SCAN_UNREADABLE))
    elif crlf_paths:
        observations.append(
            _observation(LineEndingObservationCode.CRLF_MISMATCH, count=len(crlf_paths))
        )
    phantom_paths, error = _tracked_phantom_paths(repository.root)
    if phantom_paths is None:
        observations.append(
            _observation(LineEndingObservationCode.STATUS_UNREADABLE, detail=error)
        )
    elif phantom_paths:
        observations.append(
            _observation(LineEndingObservationCode.STALE_INDEX, count=len(phantom_paths))
        )
    return crlf_paths, phantom_paths, observations


def _policy_git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(root), *args],
        capture_output=True,
        check=False,
        timeout=10,
        env=_read_only_git_env(),
    )
    if result.returncode != 0:
        raise ValueError(_error_text(result.stderr) or "Git attributes query failed")
    return _output_bytes(result.stdout)


def _common_attributes(root: Path) -> Path:
    output = _policy_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    value = os.fsdecode(output).rstrip("\r\n")
    if not value or "\n" in value or "\r" in value or not Path(value).is_absolute():
        raise ValueError("Git common directory did not resolve to one absolute path")
    common = Path(value)
    if not common.is_dir():
        raise ValueError("Git common directory is unavailable")
    info = common / "info"
    if info.is_symlink() or (info.exists() and not info.is_dir()):
        raise ValueError(f"unsafe Git attributes directory: {info}")
    return info / "attributes"


def _policy_content(path: Path) -> tuple[_FileIdentity | None, bytes]:
    identity, error = _optional_file_identity(path)
    if error:
        raise ValueError(f"{path}: {error}")
    content = path.read_bytes() if identity else b""
    after, error = _optional_file_identity(path)
    if error or after != identity:
        raise ValueError(f"attributes changed while reading {path}")
    return identity, content


def _attribute_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    seen = 0

    def failed(exc: OSError) -> None:
        raise exc

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        seen += 1 + len(files)
        if seen > 100_000:
            raise ValueError("attributes inventory exceeds 100000 entries")
        has_attributes = ".gitattributes" in files or ".gitattributes" in dirs
        dirs[:] = [
            name
            for name in dirs
            if name != ".git"
            and not (Path(directory) / name).is_symlink()
            and not (Path(directory) / name / ".git").exists()
        ]
        if has_attributes:
            paths.append(Path(directory) / ".gitattributes")
    return paths


def _upstream_attributes(root: Path) -> dict[str, tuple[_FileIdentity | None, bytes]]:
    inputs = {str(path): _policy_content(path) for path in _attribute_files(root)}
    records = _policy_git(root, "ls-files", "--stage", "-z").split(b"\0")
    if len(records) > 100_000:
        raise ValueError("attributes index inventory exceeds 100000 entries")
    for record in records:
        entry, separator, raw_name = record.partition(b"\t")
        if not separator or Path(os.fsdecode(raw_name)).name != ".gitattributes":
            continue
        name = os.fsdecode(raw_name)
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("unsafe index attributes path")
        path = root / name
        if any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
            raise ValueError(f"symlink parent of index attributes path: {name}")
        if path.is_absolute() and not path.is_relative_to(root):
            raise ValueError("unsafe index attributes path")
        mode, object_id, stage = entry.split()
        if stage != b"0" or mode not in (b"100644", b"100755"):
            raise ValueError(f"unsafe index attributes entry: {name}")
        # Keep the index policy snapshot even when the worktree file is present.
        content = _policy_git(root, "cat-file", "blob", object_id.decode("ascii"))
        if len(content) > 4_000_000:
            raise ValueError("attributes index blob exceeds 4 MB")
        inputs["index:" + name] = (None, entry + b"\0" + content)
        if str(path) not in inputs:
            inputs[str(path)] = _policy_content(path)
    return inputs


def _upstream_owned(inputs: dict[str, tuple[_FileIdentity | None, bytes]]) -> bool:
    for name, (_, raw_content) in inputs.items():
        if name.startswith(("selection:", "link:")):
            continue
        content = raw_content.partition(b"\0")[2] if name.startswith("index:") else raw_content
        if has_attribute_policy(content):
            return True
    return False


def _crlf_index_dirt(root: Path) -> bool:
    records = _policy_git(root, "ls-files", "--eol", "-z").split(b"\0")
    crlf = {record.partition(b"\t")[2] for record in records if record.startswith(b"i/crlf ")}
    if not crlf:
        return False
    dirty = set(_policy_git(root, "diff", "--name-only", "-z").split(b"\0"))
    staged = set(_policy_git(root, "diff", "--cached", "--name-only", "-z").split(b"\0"))
    return bool(crlf.intersection(dirty | staged))


def _attribute_path_result(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        timeout=10,
        env=_read_only_git_env(),
    )


def _native_attribute_path(root: Path, variable: str) -> tuple[bool, Path | None]:
    return native_attribute_path(root, variable, _attribute_path_result)


def _fallback_user_attributes(root: Path) -> Path | None:
    return fallback_user_attributes(root, _attribute_path_result)


def _user_file_snapshots(path: Path) -> dict[str, tuple[_FileIdentity | None, bytes]]:
    inputs: dict[str, tuple[_FileIdentity | None, bytes]] = {}
    pending = path
    for _ in range(40):
        links = [part for part in (*reversed(pending.parents), pending) if part.is_symlink()]
        if not links:
            inputs["user-file:" + str(pending)] = _policy_content(pending)
            return inputs
        link = links[0]
        metadata = link.lstat()
        value = os.fsencode(link.readlink())
        identity = _FileIdentity(
            hashlib.sha256(value).digest(),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
        )
        inputs["link:" + str(link)] = (identity, value)
        replacement = Path(os.fsdecode(value))
        if not replacement.is_absolute():
            replacement = link.parent / replacement
        pending = replacement / pending.relative_to(link)
    raise ValueError(f"attributes symlink chain exceeds 40 links: {path}")


def _worktree_user_inputs(root: Path) -> dict[str, tuple[_FileIdentity | None, bytes]]:
    inputs: dict[str, tuple[_FileIdentity | None, bytes]] = {}
    supported, path = _native_attribute_path(root, "GIT_ATTR_GLOBAL")
    if not supported:
        path = _fallback_user_attributes(root)
    inputs["selection:user:" + str(root)] = (None, os.fsencode(path) if path else b"")
    if path:
        inputs.update(_user_file_snapshots(path))
    if system_attributes_disabled():
        inputs["selection:system:" + str(root)] = (None, b"disabled")
        return inputs
    supported, system = _native_attribute_path(root, "GIT_ATTR_SYSTEM")
    inputs["selection:system:" + str(root)] = (None, os.fsencode(system) if system else b"")
    if not supported:
        inputs["policy-unknown:system:" + str(root)] = (
            None,
            b"system attributes path unresolved on this Git version; no local default installed",
        )
    elif system:
        inputs.update(_user_file_snapshots(system))
    return inputs


def _effective_user_inputs(root: Path) -> dict[str, tuple[_FileIdentity | None, bytes]]:
    listing = _policy_git(root, "worktree", "list", "--porcelain", "-z")
    roots = [
        Path(os.fsdecode(field.removeprefix(b"worktree ")))
        for field in listing.split(b"\0")
        if field.startswith(b"worktree ")
    ]
    if root not in roots or len(roots) > 128:
        raise ValueError("Git user-attributes inventory requires 1 to 128 worktrees")
    inputs: dict[str, tuple[_FileIdentity | None, bytes]] = {
        "selection:worktrees": (None, listing)
    }
    for sibling in roots:
        if not sibling.is_absolute():
            raise ValueError("Git worktree listing did not provide absolute paths")
        try:
            if not sibling.is_dir():
                raise ValueError("worktree directory is unavailable")
            inputs.update(_worktree_user_inputs(sibling))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if sibling == root:
                raise
            inputs["policy-unknown:sibling:" + str(sibling)] = (
                None,
                f"cannot inspect sibling worktree {sibling}: {exc}; no local default installed".encode(
                    errors="surrogateescape"
                ),
            )
    return inputs


def _nonlocal_attributes_plan(repository: LineEndingRepository, target: AttributesTarget):
    observations: list[LineEndingObservation] = []
    if repository.role == "project-checkout":
        try:
            common = _common_attributes(repository.root)
            _, content = _policy_content(common)
            if has_managed_attributes(content):
                observations.append(
                    _observation(
                        LineEndingObservationCode.LOCAL_POLICY_CONFLICT,
                        detail=f"{common}: existing local default conflicts with Stealth opt-out; inspect and remove the local rule if appropriate (Booley cannot prove ownership)",
                    )
                )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # This read-only migration check must not block the existing root writer.
            observations.append(
                _observation(
                    LineEndingObservationCode.UPSTREAM_POLICY,
                    detail=f"could not inspect repository-local attributes for Stealth opt-out: {exc}; root policy behavior is unchanged",
                )
            )
    return target, {}, _eol_policy_is_user_owned(repository.root), False, observations, None


def _attributes_plan(repository: LineEndingRepository, stealth: bool):
    local = stealth and repository.role == "project-checkout"
    target = AttributesTarget(repository.root / ".gitattributes", local=local)
    if not local:
        return _nonlocal_attributes_plan(repository, target)
    observations: list[LineEndingObservation] = []
    inputs: dict[str, tuple[_FileIdentity | None, bytes]] = {}
    try:
        common = _common_attributes(repository.root)
        target = AttributesTarget(common, local=True)
        common_identity, common_content = _policy_content(common)
        inputs = _upstream_attributes(repository.root)
        inputs.update(_effective_user_inputs(repository.root))
        upstream = _upstream_owned(inputs)
        default = has_managed_attributes(common_content)
        if default and upstream:
            observations.append(
                _observation(
                    LineEndingObservationCode.LOCAL_POLICY_CONFLICT,
                    detail=f"{common}: existing local default conflicts with attributes policy; inspect and remove the local rule if appropriate (Booley cannot prove ownership)",
                )
            )
        if default and _crlf_index_dirt(repository.root):
            observations.append(
                _observation(
                    LineEndingObservationCode.CANDIDATE_UNSAFE,
                    detail="tracked CRLF index blobs have meaningful changes; index content is preserved, inspect git diff before claiming a clean repair",
                )
            )
        _record_stealth_policy(
            repository.root, common, common_content, upstream, inputs, observations
        )
        inputs[str(common)] = (common_identity, common_content)
        owned = upstream or local_policy_owned(common_content)
        return target, inputs, owned, not owned, observations, None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return (
            target,
            inputs,
            False,
            local,
            observations,
            f"attributes policy inspection failed: {exc}",
        )


def _record_stealth_policy(
    root: Path, common: Path, content: bytes, upstream: bool, inputs, observations
) -> None:
    root_content = inputs.get(str(root / ".gitattributes"), (None, b""))[1]
    indexed = "index:.gitattributes" in inputs
    if not indexed and root_content.strip() == GITATTRIBUTES_RULE.encode():
        observations.append(
            _observation(
                LineEndingObservationCode.LEAKED_ROOT_POLICY,
                detail="untracked .gitattributes contains an old default; inspect and remove or migrate it manually if appropriate (Booley cannot prove ownership)",
            )
        )
    for name, (_, message) in inputs.items():
        if name.startswith("policy-unknown:"):
            observations.append(
                _observation(
                    LineEndingObservationCode.UPSTREAM_POLICY, detail=os.fsdecode(message)
                )
            )
    if upstream:
        observations.append(
            _observation(
                LineEndingObservationCode.UPSTREAM_POLICY,
                detail="existing upstream/user/system attributes policy is preserved; no local default installed",
            )
        )
    elif not local_policy_owned(content):
        observations.append(
            _observation(
                LineEndingObservationCode.LOCAL_POLICY_MISSING,
                detail=f"repository-local line-ending policy missing at {common}; re-run booley init",
            )
        )


def _publish_planned_attributes(
    plan: _RepositoryPlan,
    expected: _FileIdentity | None,
    normalization: _NormalizationResult | None,
) -> LineEndingActionResult:
    if not plan.target.local:
        result = _publish_attributes(plan.repository.root, expected)
        return LineEndingActionResult(
            result.kind, result.state, detail=result.detail, target=plan.target
        )
    error = _revalidate_local_policy(plan, normalization)
    if error:
        return _action(
            LineEndingActionKind.PUBLISH_ATTRIBUTES,
            LineEndingActionState.REFUSED,
            detail=error,
            target=plan.target,
        )
    path = plan.target.path
    try:
        path.parent.mkdir(exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise ValueError(f"unsafe attributes directory: {path.parent}")
        current, error = _optional_file_identity(path)
        if error or current != expected:
            return _action(
                LineEndingActionKind.PUBLISH_ATTRIBUTES,
                LineEndingActionState.REFUSED,
                detail=f"{path}: attributes changed during line-ending repair",
                target=plan.target,
            )
        content = _attributes_replacement(path)
        error = (
            _update_attributes(path, expected, content)
            if expected
            else _create_attributes(path, content)
        )
    except (OSError, ValueError) as exc:
        error = str(exc)
    state = LineEndingActionState.FAILED if error else LineEndingActionState.COMPLETED
    return _action(
        LineEndingActionKind.PUBLISH_ATTRIBUTES,
        state,
        detail=f"{path}: {error}" if error else None,
        target=plan.target,
    )


def _revalidate_local_policy(
    plan: _RepositoryPlan, normalization: _NormalizationResult | None
) -> str | None:
    try:
        if _common_attributes(plan.repository.root) != plan.target.path:
            return "Git common attributes destination changed during line-ending repair"
        current = _upstream_attributes(plan.repository.root)
        current.update(_effective_user_inputs(plan.repository.root))
        current[str(plan.target.path)] = _policy_content(plan.target.path)
        expected = dict(plan.policy_inputs)
        if normalization:
            for name, identity in normalization.file_snapshots.items():
                path = str(plan.repository.root / name)
                content = (plan.repository.root / name).read_bytes()
                before = plan.candidates[name].file
                for key, (snapshot, _) in tuple(expected.items()):
                    if key == path or (key.startswith("user-file:") and snapshot == before):
                        expected[key] = (identity, content)
        if current != expected:
            return (
                f"{plan.target.path}: attributes policy inputs changed during line-ending repair"
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"{plan.target.path}: could not revalidate attributes policy: {exc}"
    return None


def _observe_attributes_snapshot(
    target: AttributesTarget, error: str | None, observations: list[LineEndingObservation]
) -> tuple[_FileIdentity | None, str | None]:
    attributes = None
    if error is None:
        attributes, error = _optional_file_identity(target.path)
    if error is not None:
        observations.append(_observation(LineEndingObservationCode.CANDIDATE_UNSAFE, detail=error))
    return attributes, error


def _plan_repository(
    repository: LineEndingRepository,
    baseline_clean: bool | None = None,
    *,
    stealth: bool = False,
) -> tuple[_RepositoryPlan | None, tuple[LineEndingObservation, ...]]:
    effective, local, observations = _read_required_policy(repository)
    crlf_paths, phantom_paths, worktree_observations = _read_worktree_observations(repository)
    observations.extend(worktree_observations)
    if effective is None or local is None or crlf_paths is None or phantom_paths is None:
        return None, tuple(observations)

    candidates, candidate_error = _snapshot_candidates(repository.root, crlf_paths)
    if candidate_error is not None:
        observations.append(
            _observation(LineEndingObservationCode.CANDIDATE_UNSAFE, detail=candidate_error)
        )
    if not crlf_paths:
        clean: bool | None = True
    elif baseline_clean is not None:
        clean = baseline_clean
    else:
        clean = _worktree_is_clean(repository.root)
    target, inputs, owns_policy, local_missing, policy_observations, attributes_error = (
        _attributes_plan(repository, stealth)
    )
    observations.extend(policy_observations)
    attributes, attributes_error = _observe_attributes_snapshot(
        target, attributes_error, observations
    )
    return (
        _RepositoryPlan(
            repository=repository,
            effective_autocrlf=effective,
            local_autocrlf=local,
            crlf_paths=tuple(crlf_paths),
            phantom_paths=tuple(phantom_paths),
            candidates=candidates,
            clean=clean,
            candidate_error=candidate_error,
            attributes=attributes,
            attributes_error=attributes_error,
            owns_attributes_policy=owns_policy,
            target=target,
            policy_inputs=inputs,
            local_missing=local_missing,
        ),
        tuple(observations),
    )


def _normalization_refusal(plan: _RepositoryPlan) -> str | None:
    if plan.candidate_error is not None:
        return plan.candidate_error
    if plan.clean is None:
        return "could not read `git status` — refusing to normalize tracked files"
    if not plan.clean:
        return (
            "working tree has uncommitted changes — refusing to normalize it "
            "(normalization rewrites the affected tracked files). Commit or stash first, "
            "then re-run `booley init`."
        )
    return None


def _planned_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    actions: list[LineEndingActionResult] = []
    if not plan.local_autocrlf.is_set or plan.local_autocrlf.value:
        actions.append(_action(LineEndingActionKind.PIN_AUTOCRLF, LineEndingActionState.PLANNED))
    if plan.crlf_paths:
        refusal = _normalization_refusal(plan)
        state = LineEndingActionState.REFUSED if refusal else LineEndingActionState.PLANNED
        actions.append(
            _action(
                LineEndingActionKind.NORMALIZE_FILES,
                state,
                count=len(plan.crlf_paths),
                detail=refusal,
            )
        )
    elif plan.phantom_paths:
        actions.append(
            _action(
                LineEndingActionKind.REFRESH_INDEX,
                LineEndingActionState.PLANNED,
                count=len(plan.phantom_paths),
            )
        )
    needs_policy = bool(plan.crlf_paths) or any(
        action.kind is LineEndingActionKind.PIN_AUTOCRLF for action in actions
    )
    if (needs_policy or plan.local_missing) and not plan.owns_attributes_policy:
        state = (
            LineEndingActionState.REFUSED
            if plan.attributes_error
            else LineEndingActionState.PLANNED
        )
        actions.append(
            _action(
                LineEndingActionKind.PUBLISH_ATTRIBUTES,
                state,
                detail=plan.attributes_error,
                target=plan.target,
            )
        )
    return tuple(actions)


def _inspection_report(
    repository: LineEndingRepository, *, stealth: bool = False
) -> RepositoryLineEndingReport:
    plan, observations = _plan_repository(repository, stealth=stealth)
    actions = () if plan is None else _planned_actions(plan)
    unsafe = any(
        item.code is not LineEndingObservationCode.UPSTREAM_POLICY for item in observations
    )
    status = LineEndingStatus.UNSAFE if unsafe else LineEndingStatus.SAFE
    return RepositoryLineEndingReport(repository, status, observations, actions)


def _pin_autocrlf(plan: _RepositoryPlan) -> LineEndingActionResult:
    reason = "effective true" if plan.effective_autocrlf.value else "not pinned"
    current_effective = read_autocrlf_setting(plan.repository.root)
    current_local = read_autocrlf_setting(plan.repository.root, local=True)
    if (current_effective, current_local) != (
        plan.effective_autocrlf,
        plan.local_autocrlf,
    ):
        return _action(
            LineEndingActionKind.PIN_AUTOCRLF,
            LineEndingActionState.REFUSED,
            detail="core.autocrlf changed during line-ending repair",
        )
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(plan.repository.root),
                "config",
                "--local",
                "core.autocrlf",
                "false",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return _action(
            LineEndingActionKind.PIN_AUTOCRLF,
            LineEndingActionState.FAILED,
            detail=f"could not set core.autocrlf: {exc}",
        )
    verified = read_autocrlf_setting(plan.repository.root, local=True)
    if proc.returncode != 0 or verified != AutocrlfSetting(False, is_set=True):
        detail = proc.stderr.strip() or "repo-local value did not verify as false"
        return _action(
            LineEndingActionKind.PIN_AUTOCRLF,
            LineEndingActionState.FAILED,
            detail=f"could not set core.autocrlf: {detail}",
        )
    return _action(
        LineEndingActionKind.PIN_AUTOCRLF,
        LineEndingActionState.COMPLETED,
        detail=reason,
    )


def _normalize_action(
    plan: _RepositoryPlan,
) -> tuple[LineEndingActionResult, _NormalizationResult | None]:
    refusal = _normalization_refusal(plan)
    if refusal is not None:
        return (
            _action(
                LineEndingActionKind.NORMALIZE_FILES,
                LineEndingActionState.REFUSED,
                count=len(plan.crlf_paths),
                detail=refusal,
            ),
            None,
        )
    result = _normalize_as_lf(plan.repository.root, list(plan.crlf_paths), plan.candidates)
    state = (
        LineEndingActionState.COMPLETED if result.error is None else LineEndingActionState.FAILED
    )
    return (
        _action(
            LineEndingActionKind.NORMALIZE_FILES,
            state,
            count=len(plan.crlf_paths),
            detail=result.error,
        ),
        result,
    )


def _refresh_action(plan: _RepositoryPlan) -> LineEndingActionResult:
    error = _refresh_normalized_index(plan.repository.root, list(plan.phantom_paths))
    state = LineEndingActionState.COMPLETED if error is None else LineEndingActionState.FAILED
    return _action(
        LineEndingActionKind.REFRESH_INDEX,
        state,
        count=len(plan.phantom_paths),
        detail=error,
    )


def _attributes_replacement(path: Path) -> bytes:
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    return GITATTRIBUTES_RULE.encode() + b"\n" + existing


def _create_attributes(path: Path, content: bytes) -> str | None:
    staged, error = _stage_atomic_content(path, content, mode=0o644)
    if error is not None:
        return error
    assert staged is not None
    try:
        current, error = _optional_file_identity(path)
        if error is not None or current is not None:
            return error or f"{path}: attributes changed during line-ending repair"
        os.link(staged, path)
    except FileExistsError:
        return f"{path}: attributes changed during line-ending repair"
    except OSError as exc:
        return f"could not atomically publish {path}: {exc}"
    finally:
        with suppress(FileNotFoundError, PermissionError):
            staged.unlink()
    return None


def _update_attributes(path: Path, expected: _FileIdentity, content: bytes) -> str | None:
    staged, error = _stage_atomic_content(path, content, mode=expected.mode)
    if error is not None:
        return error
    assert staged is not None
    try:
        current, error = _optional_file_identity(path)
        if error is not None or current != expected:
            return error or f"{path}: attributes changed during line-ending repair"
        staged.replace(path)
    except OSError as exc:
        return f"could not atomically publish {path}: {exc}"
    finally:
        with suppress(FileNotFoundError, PermissionError):
            staged.unlink()
    return None


def _publish_attributes(
    project_root: Path, expected: _FileIdentity | None
) -> LineEndingActionResult:
    path = project_root / ".gitattributes"
    current, error = _optional_file_identity(path)
    if error is not None or current != expected:
        return _action(
            LineEndingActionKind.PUBLISH_ATTRIBUTES,
            LineEndingActionState.REFUSED,
            detail=error or ".gitattributes changed during line-ending repair",
        )
    if _eol_policy_is_user_owned(project_root):
        return _action(
            LineEndingActionKind.PUBLISH_ATTRIBUTES,
            LineEndingActionState.REFUSED,
            detail=".gitattributes policy changed during line-ending repair",
        )
    try:
        content = _attributes_replacement(path)
    except OSError as exc:
        error = f"could not read .gitattributes: {exc}"
    else:
        error = (
            _update_attributes(path, expected, content)
            if expected is not None
            else _create_attributes(path, content)
        )
    state = LineEndingActionState.COMPLETED if error is None else LineEndingActionState.FAILED
    return _action(LineEndingActionKind.PUBLISH_ATTRIBUTES, state, detail=error)


def _repair_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    actions: list[LineEndingActionResult] = []
    needs_pin = not plan.local_autocrlf.is_set or plan.local_autocrlf.value
    if needs_pin:
        actions.append(_pin_autocrlf(plan))
    normalization: _NormalizationResult | None = None
    if plan.crlf_paths:
        action, normalization = _normalize_action(plan)
        actions.append(action)
    elif plan.phantom_paths:
        actions.append(_refresh_action(plan))
    if (plan.crlf_paths or needs_pin or plan.local_missing) and not plan.owns_attributes_policy:
        expected = plan.attributes
        if (
            not plan.target.local
            and normalization is not None
            and ".gitattributes" in normalization.file_snapshots
        ):
            expected = normalization.file_snapshots[".gitattributes"]
        if plan.attributes_error is not None:
            actions.append(
                _action(
                    LineEndingActionKind.PUBLISH_ATTRIBUTES,
                    LineEndingActionState.REFUSED,
                    detail=plan.attributes_error,
                    target=plan.target,
                )
            )
        else:
            actions.append(_publish_planned_attributes(plan, expected, normalization))
    return tuple(actions)


def _repair_repository(
    repository: LineEndingRepository, baseline_clean: bool | None = None, *, stealth: bool = False
) -> RepositoryLineEndingReport:
    plan, initial_observations = _plan_repository(repository, baseline_clean, stealth=stealth)
    if plan is None:
        return RepositoryLineEndingReport(
            repository,
            LineEndingStatus.UNSAFE,
            initial_observations,
            (),
        )
    actions = _repair_actions(plan)
    final = _inspection_report(repository, stealth=stealth)
    incomplete = any(
        action.state in (LineEndingActionState.REFUSED, LineEndingActionState.FAILED)
        for action in actions
    )
    status = LineEndingStatus.UNSAFE if incomplete else final.status
    return RepositoryLineEndingReport(repository, status, final.observations, actions)


def sample_worktree_cleanliness(
    project_root: Path, project_dir: Path | None = None
) -> dict[Path, bool | None]:
    """Record whether each Project repository is clean before init writes anything.

    Init itself edits tracked Project data (for example the ``[agent]``
    selection in ``booley.toml``) before the line-ending repair runs. Those LF
    edits are Booley's own, not user work, so the repair judges "uncommitted
    changes" against this pre-write baseline. A repository dirty here still
    refuses normalization. None = Git could not answer for that repository.
    """
    discovery = discover_line_ending_repositories(project_root, project_dir)
    return {
        repository.root: _worktree_is_clean(repository.root)
        for repository in discovery.repositories
    }


def reconcile_project_line_endings(
    project_root: Path,
    project_dir: Path | None = None,
    *,
    mode: LineEndingMode,
    clean_baseline: Mapping[Path, bool | None] | None = None,
) -> LineEndingReport:
    """Inspect or reconcile every distinct repository supplying Project files.

    ``clean_baseline`` comes from :func:`sample_worktree_cleanliness` taken
    before the caller wrote Project data. Repair uses it instead of the
    current ``git status`` for each repository it covers.
    """
    discovery = discover_line_ending_repositories(project_root, project_dir)
    if not discovery.repositories and not discovery.failures:
        return LineEndingReport(LineEndingStatus.NOT_APPLICABLE, (), ())
    baseline = clean_baseline or {}
    stealth = stealth_enabled(project_root, project_dir=project_dir)
    reports = tuple(
        _inspection_report(repository, stealth=stealth)
        if mode is LineEndingMode.INSPECT
        else _repair_repository(repository, baseline.get(repository.root), stealth=stealth)
        for repository in discovery.repositories
    )
    unsafe = bool(discovery.failures) or any(
        report.status is not LineEndingStatus.SAFE for report in reports
    )
    status = LineEndingStatus.UNSAFE if unsafe else LineEndingStatus.SAFE
    return LineEndingReport(status, reports, discovery.failures)
