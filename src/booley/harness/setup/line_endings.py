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
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal

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
class _CandidateSnapshot:
    """Private compare-before-write state for one tracked candidate."""

    digest: bytes
    device: int
    inode: int
    mode: int
    link_count: int
    index_entry: bytes
    attributes: bytes
    index_flags: bytes


@dataclass(frozen=True)
class _FileSnapshot:
    """Private compare-before-write state for an optional regular file."""

    exists: bool
    digest: bytes | None = None
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    link_count: int | None = None


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
    attributes: _FileSnapshot | None
    attributes_error: str | None
    owns_attributes_policy: bool


@dataclass(frozen=True)
class _NormalizationResult:
    """Private partial-progress result for guarded worktree replacement."""

    rewritten: tuple[str, ...]
    file_snapshots: dict[str, _FileSnapshot]
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


def _crlf_worktree_files(project_root: Path) -> list[str] | None:
    """Tracked paths that will read as modified in the container.

    ``git ls-files --eol`` prints one ``i/<index-eol> w/<worktree-eol>
    attr/<text-attr>`` line per tracked file. A CRLF worktree file is only a
    *phantom diff* when its line endings differ from the blob in the index —
    that mismatch is what a Linux container's git (which does no CRLF
    conversion) reports as a modification.

    A file whose ``.gitattributes`` marks it ``-text`` (or which git detected as
    binary) is stored CRLF and checked out CRLF: ``i/crlf w/crlf``. It matches
    the index byte for byte, in the container as much as on the host, so it
    cannot produce a phantom diff and must not be counted. Upstream repos
    legitimately do this for CRLF-native payloads carried as text — Windows
    ``.bat`` scripts, vendor register dumps — and counting them made the check
    fire on projects with nothing to fix (8 files on taxi, every one exempt).

    Comparing the two eol fields (rather than sniffing ``attr/-text``) tests the
    condition directly, so it also catches the inverse case ``-text`` never
    covers: an ``i/lf w/crlf`` file, which IS a phantom diff.
    """
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
    paths: list[str] = []
    for record in proc.stdout.split("\0"):
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or not path:
            continue
        if len(fields) < 2:
            continue
        index_eol, worktree_eol = fields[0], fields[1]
        if worktree_eol not in ("w/crlf", "w/mixed"):
            continue
        if index_eol[2:] == worktree_eol[2:]:
            continue  # index already holds CRLF — no diff for git to see
        if any(field.lower() == "eol=crlf" for field in fields[2:]):
            continue  # an explicit checkout policy the Session Runtime also honors
        paths.append(path)
    return paths


def _count_crlf_worktree_files(project_root: Path) -> int | None:
    """Number of tracked files that will read as modified in the container."""
    paths = _crlf_worktree_files(project_root)
    return None if paths is None else len(paths)


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


def _tracked_status_is_phantom(project_root: Path) -> tuple[bool | None, str | None]:
    paths, error = _tracked_phantom_paths(project_root)
    return (None, error) if paths is None else (bool(paths), None)


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
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return None, f"refusing to normalize non-regular tracked path {name!r}"
        if metadata.st_nlink > 1:
            return None, f"refusing to normalize hard-linked tracked path {name!r}"
        digest = _file_digest(path)
    except OSError as exc:
        return None, f"could not inspect tracked path {name!r}: {exc}"
    git_state, error = _candidate_git_state(project_root, name)
    if error is not None:
        return None, error
    assert git_state is not None
    index_entry, attributes, index_flags = git_state
    return (
        _CandidateSnapshot(
            digest=digest,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            link_count=metadata.st_nlink,
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


def _optional_file_snapshot(path: Path) -> tuple[_FileSnapshot | None, str | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _FileSnapshot(False), None
    except OSError as exc:
        return None, f"could not inspect {path.name!r}: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return None, f"refusing to update non-regular {path.name!r}"
    try:
        digest = _file_digest(path)
    except OSError as exc:
        return None, f"could not inspect {path.name!r}: {exc}"
    return (
        _FileSnapshot(
            exists=True,
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


def _restore_after_write_failure(
    target: BinaryIO, original: bytes, path: Path, cause: OSError
) -> str:
    try:
        target.seek(0)
        target.write(original)
        target.truncate()
        target.flush()
    except OSError as restore_exc:
        return (
            f"could not normalize {path.name!r}: {cause}; restoring the original "
            f"content also failed: {restore_exc}"
        )
    return f"could not normalize {path.name!r}: {cause}; original content restored"


def _opened_candidate_matches(target: BinaryIO, expected: _CandidateSnapshot) -> bool:
    metadata = os.fstat(target.fileno())
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
        and metadata.st_mode == expected.mode
        and metadata.st_nlink == expected.link_count == 1
    )


def _git_state_matches(
    project_root: Path, name: str, expected: _CandidateSnapshot
) -> tuple[bool, str | None]:
    state, error = _candidate_git_state(project_root, name)
    if error is not None:
        return False, error
    assert state is not None
    return state == (expected.index_entry, expected.attributes, expected.index_flags), None


def _rewrite_from_stage(  # noqa: PLR0911 -- each refusal is a pre-write safety gate
    project_root: Path,
    name: str,
    replacement: Path,
    expected: _CandidateSnapshot,
) -> str | None:
    """Update one still-identical regular file in place."""
    current, error = _candidate_snapshot(project_root, name)
    if error is not None:
        return error
    if current != expected:
        return f"tracked path changed during line-ending repair: {name!r}"
    path = project_root / name
    try:
        replacement_bytes = replacement.read_bytes()
        with path.open("r+b") as target:
            if not _opened_candidate_matches(target, expected):
                return f"tracked path changed during line-ending repair: {name!r}"
            original = target.read()
            if hashlib.sha256(original).digest() != expected.digest:
                return f"tracked path changed during line-ending repair: {name!r}"
            matches, git_error = _git_state_matches(project_root, name, expected)
            if git_error is not None:
                return git_error
            if not matches:
                return f"Git index or attributes changed during line-ending repair: {name!r}"
            try:
                target.seek(0)
                target.write(replacement_bytes)
                target.truncate()
                target.flush()
            except OSError as exc:
                return _restore_after_write_failure(target, original, path, exc)
    except OSError as exc:
        return f"could not normalize {name!r}: {exc}"
    return None


def _index_entries(
    project_root: Path, paths: list[str]
) -> tuple[dict[str, bytes] | None, str | None]:
    entries: dict[str, bytes] = {}
    for name in paths:
        output, error = _path_git_bytes(project_root, ("ls-files", "--stage", "-z"), name)
        if error is not None:
            return None, error
        assert output is not None
        entries[name] = output
    return entries, None


def _restore_index_entries(project_root: Path, entries: dict[str, bytes]) -> str | None:
    payload = b"".join(entries.values())
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "update-index", "-z", "--index-info"],
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


def _refresh_normalized_index(  # noqa: PLR0911 -- every return identifies one recovery gate
    project_root: Path, paths: list[str]
) -> str | None:
    """Refresh path metadata while proving index content entries stay exact."""
    before, error = _index_entries(project_root, paths)
    if error is not None:
        return f"could not snapshot normalized Git index entries: {error}"
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
        return f"could not refresh normalized Git index entries: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip() or "Git add failed"
        return f"could not refresh normalized Git index entries: {detail}"
    after, error = _index_entries(project_root, paths)
    if error is not None:
        return f"could not verify normalized Git index entries: {error}"
    if after == before:
        return None
    assert before is not None
    restore_error = _restore_index_entries(project_root, before)
    if restore_error is None:
        return "normalization unexpectedly changed staged content; restored exact index entries"
    return (
        "normalization unexpectedly changed staged content and could not restore exact "
        f"index entries: {restore_error}"
    )


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
    file_snapshots: dict[str, _FileSnapshot] = {}
    try:
        for name in paths:
            error = _rewrite_from_stage(project_root, name, staged[name], snapshots[name])
            if error is not None:
                break
            rewritten.append(name)
            current, snapshot_error = _optional_file_snapshot(project_root / name)
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
    attributes: _FileSnapshot | None = _FileSnapshot(False)
    attributes_error: str | None = None
    if needs_policy and not owns_policy:
        attributes, attributes_error = _optional_file_snapshot(repository.root / ".gitattributes")
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


def _opened_file_matches(target: BinaryIO, expected: _FileSnapshot) -> bool:
    metadata = os.fstat(target.fileno())
    return (
        expected.exists
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
        and metadata.st_mode == expected.mode
        and metadata.st_nlink == expected.link_count == 1
    )


def _attributes_replacement(path: Path) -> bytes:
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    return GITATTRIBUTES_RULE.encode() + b"\n" + existing


def _create_attributes(path: Path, content: bytes) -> str | None:
    created_identity: tuple[int, int] | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as target:
            metadata = os.fstat(target.fileno())
            created_identity = (metadata.st_dev, metadata.st_ino)
            target.write(content)
            target.flush()
    except FileExistsError:
        return ".gitattributes changed during line-ending repair"
    except OSError as exc:
        try:
            current = path.lstat()
            if created_identity == (current.st_dev, current.st_ino):
                path.unlink()
        except OSError:
            pass
        return f"could not write .gitattributes: {exc}"
    return None


def _update_attributes(path: Path, expected: _FileSnapshot, content: bytes) -> str | None:
    try:
        with path.open("r+b") as target:
            if not _opened_file_matches(target, expected):
                return ".gitattributes changed during line-ending repair"
            original = target.read()
            if hashlib.sha256(original).digest() != expected.digest:
                return ".gitattributes changed during line-ending repair"
            try:
                target.seek(0)
                target.write(content)
                target.truncate()
                target.flush()
            except OSError as exc:
                return _restore_after_write_failure(target, original, path, exc)
    except OSError as exc:
        return f"could not write .gitattributes: {exc}"
    return None


def _publish_attributes(
    project_root: Path, expected: _FileSnapshot | None
) -> LineEndingActionResult:
    if expected is None:
        return _action(
            LineEndingActionKind.PUBLISH_ATTRIBUTES,
            LineEndingActionState.REFUSED,
            detail="could not establish a .gitattributes precondition",
        )
    path = project_root / ".gitattributes"
    current, error = _optional_file_snapshot(path)
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
            if expected.exists
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
