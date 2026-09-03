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
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

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
class AutocrlfSetting:
    """One resolved Git Boolean and whether that scope sets it explicitly."""

    value: bool
    is_set: bool


@dataclass(frozen=True)
class LineEndingRepository:
    """One distinct Git worktree whose files enter the Session Runtime."""

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
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


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


GITATTRIBUTES_RULE = "* text=auto eol=lf"


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

    Sampled *before* init touches anything, because the CRLF fix itself moves
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
        with suppress(FileNotFoundError):
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
    """Atomically publish one still-identical tracked-file replacement."""
    path = project_root / name
    try:
        replacement_bytes = replacement.read_bytes()
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
        with suppress(FileNotFoundError):
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
) -> LineEndingActionResult:
    return LineEndingActionResult(kind, state, count=count, detail=detail)


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


def _plan_repository(
    repository: LineEndingRepository,
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
    clean = _worktree_is_clean(repository.root) if crlf_paths else True
    needs_policy = bool(crlf_paths) or effective.value or not local.is_set or local.value
    owns_policy = _eol_policy_is_user_owned(repository.root)
    attributes: _FileIdentity | None = None
    attributes_error: str | None = None
    if needs_policy and not owns_policy:
        attributes, attributes_error = _optional_file_identity(repository.root / ".gitattributes")
        if attributes_error is not None:
            observations.append(
                _observation(LineEndingObservationCode.CANDIDATE_UNSAFE, detail=attributes_error)
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
    if needs_policy and not plan.owns_attributes_policy:
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
            )
        )
    return tuple(actions)


def _inspection_report(repository: LineEndingRepository) -> RepositoryLineEndingReport:
    plan, observations = _plan_repository(repository)
    actions = () if plan is None else _planned_actions(plan)
    status = LineEndingStatus.SAFE if not observations else LineEndingStatus.UNSAFE
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
            return error or ".gitattributes changed during line-ending repair"
        os.link(staged, path)
    except FileExistsError:
        return ".gitattributes changed during line-ending repair"
    except OSError as exc:
        return f"could not atomically publish .gitattributes: {exc}"
    finally:
        with suppress(FileNotFoundError):
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
            return error or ".gitattributes changed during line-ending repair"
        staged.replace(path)
    except OSError as exc:
        return f"could not atomically publish .gitattributes: {exc}"
    finally:
        with suppress(FileNotFoundError):
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
    if (plan.crlf_paths or needs_pin) and not plan.owns_attributes_policy:
        expected = plan.attributes
        if normalization is not None and ".gitattributes" in normalization.file_snapshots:
            expected = normalization.file_snapshots[".gitattributes"]
        if plan.attributes_error is not None:
            actions.append(
                _action(
                    LineEndingActionKind.PUBLISH_ATTRIBUTES,
                    LineEndingActionState.REFUSED,
                    detail=plan.attributes_error,
                )
            )
        else:
            actions.append(_publish_attributes(plan.repository.root, expected))
    return tuple(actions)


def _repair_repository(repository: LineEndingRepository) -> RepositoryLineEndingReport:
    plan, initial_observations = _plan_repository(repository)
    if plan is None:
        return RepositoryLineEndingReport(
            repository,
            LineEndingStatus.UNSAFE,
            initial_observations,
            (),
        )
    actions = _repair_actions(plan)
    final = _inspection_report(repository)
    incomplete = any(
        action.state in (LineEndingActionState.REFUSED, LineEndingActionState.FAILED)
        for action in actions
    )
    status = LineEndingStatus.UNSAFE if incomplete else final.status
    return RepositoryLineEndingReport(repository, status, final.observations, actions)


def reconcile_project_line_endings(
    project_root: Path,
    project_dir: Path | None = None,
    *,
    mode: LineEndingMode,
) -> LineEndingReport:
    """Inspect or reconcile every distinct repository supplying Project files."""
    discovery = discover_line_ending_repositories(project_root, project_dir)
    if not discovery.repositories and not discovery.failures:
        return LineEndingReport(LineEndingStatus.NOT_APPLICABLE, (), ())
    reports = tuple(
        _inspection_report(repository)
        if mode is LineEndingMode.INSPECT
        else _repair_repository(repository)
        for repository in discovery.repositories
    )
    unsafe = bool(discovery.failures) or any(
        report.status is not LineEndingStatus.SAFE for report in reports
    )
    status = LineEndingStatus.UNSAFE if unsafe else LineEndingStatus.SAFE
    return LineEndingReport(status, reports, discovery.failures)
