"""Project line-ending inspection and reconciliation.

Owns Git discovery, container-safety policy, Git re-checkout of line-ending-only
files, stale-index refresh, and verification for every repository supplying Project
files. Project Initialization and Doctor are adapters over this module.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple

from booley.commit_policy.policy import stealth_enabled
from booley.runtime import runtime_context
from booley.runtime.git_attributes_policy import (
    GITATTRIBUTES_RULE,
    fallback_user_attributes,
    has_attribute_policy,
    has_managed_attributes,
    local_policy_owned,
    native_attribute_path,
    system_attributes_disabled,
)
from booley.runtime.git_environment import inherited_git_environment

LineEndingRole = Literal["project-checkout", "project-data"]


class LineEndingMode(StrEnum):
    """Whether to inspect only or reconcile safe forward changes."""

    INSPECT = "inspect"
    #: Apply only Git settings and untracked attributes; report tracked-content actions.
    REPAIR_SETTINGS = "repair-settings"
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
    ATTRIBUTES_POLICY_UNKNOWN = "attributes-policy-unknown"


class LineEndingActionKind(StrEnum):
    """Stable reconciliation actions exposed without implementation state."""

    PIN_AUTOCRLF = "pin-autocrlf"
    NORMALIZE_FILES = "normalize-files"
    REFRESH_INDEX = "refresh-index"
    PUBLISH_ATTRIBUTES = "publish-attributes"
    MIGRATE_ATTRIBUTES = "migrate-attributes"


class LineEndingActionState(StrEnum):
    """Outcome of one planned or attempted action."""

    PLANNED = "planned"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


class LineEndingRefusalReason(StrEnum):
    """Why an action was refused, so adapters never parse the refusal text."""

    DIRTY_TREE = "dirty-tree"
    STATUS_UNREADABLE = "status-unreadable"
    PROTECTED_PATHS = "protected-paths"
    NOT_LINE_ENDING_ONLY = "not-line-ending-only"
    SETTING_CHANGED = "setting-changed"
    ATTRIBUTES_POLICY = "attributes-policy"


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
    reason: LineEndingRefusalReason | None = None


@dataclass(frozen=True)
class RepositoryLineEndingReport:
    """Typed result for one inspected RTL or Booley Directory's repository."""

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
class _PolicyFacts:
    """Fresh Stealth authorization: rule ownership, unknown sources and worktree topology."""

    owned: bool
    unknown: tuple[str, ...]
    topology: bytes


@dataclass(frozen=True)
class _AttributesMigration:
    """Replayable shared default, root snapshots and worktree topology for recoverable opt-out."""

    source: Path
    topology: bytes
    source_snapshot: tuple[_FileIdentity | None, bytes]
    root_snapshot: tuple[_FileIdentity | None, bytes]
    root_owned: bool


@dataclass(frozen=True)
class _RepositoryPlan:
    """Private guarded transformation plan for one repository."""

    repository: LineEndingRepository
    effective_autocrlf: AutocrlfSetting
    local_autocrlf: AutocrlfSetting
    local_eol: str
    crlf_paths: tuple[str, ...]
    phantom_paths: tuple[str, ...]
    clean: bool | None
    attributes: _FileIdentity | None
    attributes_error: str | None
    owns_attributes_policy: bool
    target: AttributesTarget
    facts: _PolicyFacts | None
    local_missing: bool
    migration: _AttributesMigration | None


class _Refusal(NamedTuple):
    """A typed refusal: the machine-readable reason and its user-facing text."""

    reason: LineEndingRefusalReason
    detail: str


@dataclass(frozen=True)
class _IndexEntry:
    """One stage-annotated ``git ls-files -v -s --eol`` record."""

    path: str
    tag: str
    mode: str
    oid: str
    stage: str
    index_eol: str
    worktree_eol: str
    eol_crlf_attr: bool

    @property
    def crlf_mismatch(self) -> bool:
        """Checked out with CRLF where the index holds a different ending, by no ``eol=crlf`` rule."""
        return (
            self.worktree_eol in ("w/crlf", "w/mixed")
            and self.index_eol[2:] != self.worktree_eol[2:]
            and not self.eol_crlf_attr
        )

    @property
    def git_protected(self) -> bool:
        """Skip-worktree or assume-unchanged: Git hides local edits of this path."""
        return self.tag == "S" or self.tag.islower()


@dataclass(frozen=True)
class _NormalizationResult:
    """What one Git re-checkout of the line-ending-only files did.

    ``left`` pairs each tracked path the repair did not touch with its cause;
    ``attributes_identity`` is the new identity of a root ``.gitattributes`` that
    was itself normalized.
    """

    normalized: tuple[str, ...] = ()
    left: tuple[tuple[str, str], ...] = ()
    attributes_identity: _FileIdentity | None = None
    error: str | None = None
    refusal: _Refusal | None = None


def line_ending_repository_display(role: LineEndingRole, root: Path) -> str:
    """Render one repository role and path consistently across init and Doctor."""
    label = "Project Root" if role == "project-checkout" else "Booley Directory"
    return f"{label} ({root})"


def _read_only_git_env() -> dict[str, str]:
    """Keep explicit-checkout observations independent of inherited Git selectors."""
    return {**inherited_git_environment(), "GIT_OPTIONAL_LOCKS": "0"}


def _untranslated_git_env() -> dict[str, str]:
    """Read-only Git environment whose messages are English, whatever the user's locale.

    Discovery recognizes a non-repository by Git's message, which a localized
    Git translates.
    """
    environment = {**_read_only_git_env(), "LC_ALL": "C"}
    environment.pop("LANGUAGE", None)
    return environment


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
            env=_untranslated_git_env(),
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
    project_root: Path, booley_dir: Path | None = None
) -> RepositoryDiscovery:
    """Resolve the distinct Git worktrees that supply one Project's files."""
    candidates: list[tuple[LineEndingRole, Path]] = [("project-checkout", project_root)]
    if booley_dir is not None:
        candidates.append(("project-data", booley_dir))
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


def _git_output(
    root: Path, args: tuple[str, ...], *, stdin: bytes | None = None, timeout: int = 60
) -> tuple[bytes | None, str | None]:
    """Run one Git command under the read-only selector environment: its stdout, or None on failure."""
    command = f"git -C {root} {' '.join(args)}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"`{command}` failed: {exc}"
    if proc.returncode != 0:
        detail = _error_text(proc.stderr) or "no stderr"
        return None, f"`{command}` exited {proc.returncode}: {detail}"
    return _output_bytes(proc.stdout), None


def _parse_index_record(record: str) -> _IndexEntry | None:
    """Parse ``<tag> <mode> <oid> <stage>\\t<i/..> <w/..> <attr/..>\\t<path>``.

    ``-s`` adds a tab before the EOL fields and a POSIX path may itself hold a
    tab, so only the first two tabs split.
    """
    parts = record.split("\t", 2)
    if len(parts) != 3:
        return None
    header, eol_fields = parts[0].split(), parts[1].split()
    if len(header) != 4 or len(eol_fields) < 2:
        return None
    tag, mode, oid, stage = header
    return _IndexEntry(
        path=parts[2],
        tag=tag,
        mode=mode,
        oid=oid,
        stage=stage,
        index_eol=eol_fields[0],
        worktree_eol=eol_fields[1],
        eol_crlf_attr=any(field.lower() == "eol=crlf" for field in eol_fields[2:]),
    )


def _scan_index(root: Path) -> tuple[tuple[_IndexEntry, ...] | None, str | None]:
    """Every index entry with its flag tag, object id and line-ending state, in one Git process."""
    output, error = _git_output(root, ("ls-files", "-z", "-v", "-s", "--eol"))
    if output is None:
        return None, error
    entries = (_parse_index_record(os.fsdecode(record)) for record in output.split(b"\0"))
    return tuple(entry for entry in entries if entry is not None), None


def _crlf_worktree_files(project_root: Path) -> list[str] | None:
    """Return tracked paths that Linux sees modified due only to checkout EOLs."""
    entries, _error = _scan_index(project_root)
    if entries is None:
        return None
    return [entry.path for entry in entries if entry.crlf_mismatch]


def pending_normalization_paths(
    project_root: Path, booley_dir: Path | None = None
) -> tuple[Path, ...]:
    """Absolute tracked paths that a repair run would normalize to LF."""
    discovery = discover_line_ending_repositories(project_root, booley_dir)
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


def read_local_eol(project_root: Path) -> str | None:
    """Repo-local ``core.eol`` (empty string when unset); None when Git could not read it."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "config", "--local", "--get", "core.eol"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode == 1 and not proc.stdout.strip():
        return ""
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _eol_policy_is_user_owned(project_root: Path) -> bool:
    """Whether the root attributes file states a deliberate whole-tree policy."""
    try:
        return _root_policy_owned((project_root / ".gitattributes").read_bytes())
    except OSError:
        return False


def _root_policy_owned(content: bytes) -> bool:
    for line in content.decode("utf-8", errors="surrogateescape").splitlines():
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
    re-checks out only exact tracked paths. None = git could not answer.
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


def _name_set(root: Path, *args: str) -> tuple[set[bytes] | None, str | None]:
    output, error = _git_output(
        root, ("diff", "--name-only", "-z", "--ignore-submodules=none", *args)
    )
    if output is None:
        return None, error
    return set(output.split(b"\0")), None


def _tracked_phantom_paths(project_root: Path) -> tuple[list[str] | None, str | None]:
    """Tracked status entries that have neither staged nor unstaged content diffs.

    A constant three Git processes: one status, plus the unstaged and staged
    name-only diffs when status lists a modified path at all.
    """
    status, error = _git_output(
        project_root, ("status", "--porcelain", "-z", "--untracked-files=no")
    )
    if status is None:
        return None, error
    modified = [record[3:] for record in status.split(b"\0") if record[:3] in (b" M ", b"M  ")]
    if not modified:
        return [], None
    unstaged, error = _name_set(project_root)
    if unstaged is None:
        return None, error
    staged, error = _name_set(project_root, "--cached")
    if staged is None:
        return None, error
    return [os.fsdecode(name) for name in modified if name not in unstaged | staged], None


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


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


def _cleanup_staged_files(paths: dict[str, Path]) -> list[Path]:
    """Remove Git's temporary files; return those that could not be removed."""
    stuck: list[Path] = []
    for path in paths.values():
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except PermissionError:
            stuck.append(path)
    return stuck


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


def _c_quote(raw: bytes) -> bytes:
    """C-quote one path for ``hash-object --stdin-paths``, which reads one path per line."""
    quoted = bytearray(b'"')
    for byte in raw:
        if byte in (0x22, 0x5C) or not 0x20 <= byte < 0x7F:
            quoted += b"\\%03o" % byte
        else:
            quoted.append(byte)
    return bytes(quoted) + b'"'


def _nul_joined(names: list[str]) -> bytes:
    return b"".join(os.fsencode(name) + b"\0" for name in names)


def _converted_attributes(
    root: Path, names: list[str]
) -> tuple[dict[str, set[str]] | None, str | None]:
    """Per path, which of ``filter``/``ident``/``working-tree-encoding`` Git applies on checkout."""
    attributes = ("filter", "ident", "working-tree-encoding")
    output, error = _git_output(
        root, ("check-attr", "-z", "--stdin", *attributes), stdin=_nul_joined(names)
    )
    if output is None:
        return None, error
    fields = output.split(b"\0")
    converted: dict[str, set[str]] = {}
    for index in range(0, len(fields) - 2, 3):
        path, attribute, value = fields[index : index + 3]
        if value not in (b"unspecified", b"unset"):
            converted.setdefault(os.fsdecode(path), set()).add(attribute.decode("ascii"))
    return converted, None


def _mode_changed_paths(root: Path) -> tuple[set[str] | None, str | None]:
    """Tracked paths whose file mode or type differs from the index (``core.fileMode`` applies)."""
    output, error = _git_output(root, ("-c", "core.fsmonitor=false", "diff-files", "-z"))
    if output is None:
        return None, error
    # Raw ``-z`` output is strictly ``<header> NUL <path> NUL`` per record, so walk it in pairs: a
    # path that itself starts with ``:`` is never mistaken for a header.
    fields = output.split(b"\0")
    changed: set[str] = set()
    for header, path in zip(fields[0::2], fields[1::2], strict=False):
        modes = header[1:].split(b" ")
        if len(modes) >= 2 and modes[0] != modes[1]:
            changed.add(os.fsdecode(path))
    return changed, None


def _blob_oid(content: bytes, object_id_length: int) -> str:
    """The Git object id of ``content`` as a blob; 40 hex digits mean SHA-1, 64 mean SHA-256."""
    algorithm = "sha256" if object_id_length == 64 else "sha1"
    header = b"blob %d\0" % len(content)
    return hashlib.new(algorithm, header + content).hexdigest()


def _regular_file_cause(root: Path, name: str) -> str | None:
    """Why a candidate is not a file to rewrite (a symlink or a directory now); None for a regular file."""
    try:
        is_regular = stat.S_ISREG((root / name).lstat().st_mode)
    except OSError as exc:
        return f"could not read: {exc}"
    return None if is_regular else "not a regular file"


def _plain_path_cause(root: Path, entry: _IndexEntry) -> str | None:
    """Why one unconverted candidate must stay as it is; None when only its line endings differ."""
    try:
        content = (root / entry.path).read_bytes()
    except OSError as exc:
        return f"could not read: {exc}"
    if _blob_oid(content.replace(b"\r\n", b"\n"), len(entry.oid)) != entry.oid:
        return "changed"
    return None


def _clean_conversion_oids(
    root: Path, names: list[str]
) -> tuple[dict[str, str] | None, str | None]:
    """Object ids of converted paths hashed through Git's own clean conversion in one process."""
    payload = b"".join(_c_quote(os.fsencode(name)) + b"\n" for name in names)
    output, error = _git_output(
        root, ("-c", "core.autocrlf=input", "hash-object", "--stdin-paths"), stdin=payload
    )
    if output is None:
        return None, error
    oids = output.decode("ascii", errors="replace").split()
    if len(oids) != len(names):
        return None, "Git returned an unexpected number of object ids"
    return dict(zip(names, oids, strict=True)), None


def _probe_smudge(root: Path, names: list[str]) -> tuple[dict[str, bytes], list[Path], str | None]:
    """Materialize Git's checkout output for ``names`` beside the tree, touching no tracked file.

    A required smudge filter that fails makes the real checkout unlink the path
    first, so this probe runs before anything is replaced. Its temporary files
    are removed even if Git is killed: those it listed, and any ``.merge_file_*``
    that did not exist before. Returns each path's output, the temporary files
    that could not be removed, and an error.
    """
    existing = {path.name for path in root.glob(".merge_file_*")}
    staged: dict[str, Path] = {}
    outputs: dict[str, bytes] = {}
    error: str | None = None
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "-C",
                str(root),
                "checkout-index",
                "--temp",
                "-z",
                "--stdin",
            ],
            input=_nul_joined(names),
            capture_output=True,
            check=False,
            timeout=300,
            env=_read_only_git_env(),
        )
        staged = _staged_paths(root, _output_bytes(proc.stdout))
        if proc.returncode != 0:
            error = _error_text(proc.stderr) or "git checkout-index failed"
        outputs = {name: path.read_bytes() for name, path in staged.items()}
    except subprocess.TimeoutExpired as exc:
        staged = _staged_paths(root, _output_bytes(exc.stdout))
        error = f"timed out after {exc.timeout} s"
    except (subprocess.SubprocessError, OSError) as exc:
        error = str(exc)
    finally:
        leftovers = {path.name: path for path in staged.values()}
        for path in root.glob(".merge_file_*"):
            if path.name not in existing:
                leftovers[path.name] = path
        stuck = _cleanup_staged_files(leftovers)
    return outputs, stuck, error


def _classify_filtered(root: Path, names: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split paths with a ``filter`` attribute into those a checkout would reproduce as LF."""
    outputs, stuck, error = _probe_smudge(root, names)
    left: dict[str, str] = {}
    if error is not None:
        left = dict.fromkeys(names, f"filter failed: {error}")
    eligible: list[str] = []
    for name in names:
        if name in left:
            continue
        if name not in outputs:
            left[name] = "filter failed: incomplete Git output"
            continue
        try:
            worktree = (root / name).read_bytes()
        except OSError as exc:
            left[name] = f"could not read: {exc}"
            continue
        if outputs[name] == worktree.replace(b"\r\n", b"\n"):
            eligible.append(name)
        elif outputs[name] == worktree:
            left[name] = "checkout reproduces the file; its line endings come from the filter"
        else:
            left[name] = "checkout output differs from the file beyond line endings"
    for path in stuck:
        left[path.name] = f"temporary file could not be removed: {path.name}"
    return eligible, left


def _classify_converted(
    root: Path, entries: dict[str, _IndexEntry], filtered: set[str]
) -> tuple[list[str], dict[str, str]]:
    """Judge paths whose checkout bytes differ from the blob by more than line endings."""
    names = list(entries)
    oids, error = _clean_conversion_oids(root, names)
    if oids is None:
        return [], dict.fromkeys(names, f"clean conversion failed: {error}")
    candidates = [name for name in names if oids[name] == entries[name].oid]
    left = {name: "changed" for name in names if name not in candidates}
    to_probe = [name for name in candidates if name in filtered]
    eligible = [name for name in candidates if name not in filtered]
    if to_probe:
        probed, probe_left = _classify_filtered(root, to_probe)
        eligible += probed
        left.update(probe_left)
    return eligible, left


def _classify_candidates(
    root: Path, candidates: dict[str, _IndexEntry]
) -> tuple[list[str], dict[str, str], str | None]:
    """Which candidates differ from Git's copy by line endings alone, and why the rest do not.

    Runs a constant number of Git processes whatever the number of paths.
    """
    names = list(candidates)
    attributes, error = _converted_attributes(root, names)
    mode_changed, mode_error = _mode_changed_paths(root)
    if attributes is None or mode_changed is None:
        return [], {}, error or mode_error
    left = {name: "changed" for name in names if name in mode_changed}
    for name in names:
        if name not in left and (cause := _regular_file_cause(root, name)) is not None:
            left[name] = cause
    converted = {
        name: candidates[name] for name in names if name in attributes and name not in left
    }
    eligible: list[str] = []
    for name in names:
        if name in left or name in converted:
            continue
        cause = _plain_path_cause(root, candidates[name])
        if cause is None:
            eligible.append(name)
        else:
            left[name] = cause
    if converted:
        filtered = {name for name in converted if "filter" in attributes[name]}
        converted_eligible, converted_left = _classify_converted(root, converted, filtered)
        eligible += converted_eligible
        left.update(converted_left)
    return eligible, left, None


def _git_protected_refusal(candidates: dict[str, _IndexEntry]) -> _Refusal | None:
    protected = [entry.path for entry in candidates.values() if entry.git_protected]
    if not protected:
        return None
    names = ", ".join(repr(name) for name in protected[:3])
    suffix = " …" if len(protected) > 3 else ""
    return _Refusal(
        LineEndingRefusalReason.PROTECTED_PATHS,
        f"refusing to normalize Git-protected path(s): {names}{suffix} "
        "(skip-worktree or assume-unchanged may hide local edits)",
    )


def _shift_mtimes(
    root: Path, names: list[str]
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    """Move each file's mtime two seconds on so Git treats even a stat-clean entry as changed.

    Returns ``{name: (original_ns, shifted_ns)}``, where ``shifted_ns`` is what the
    filesystem stored (coarse timestamps round it), and the paths that could not be shifted.
    """
    shifted: dict[str, tuple[int, int]] = {}
    failed: dict[str, str] = {}
    for name in names:
        path = root / name
        try:
            metadata = path.stat()
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000))
            moved = path.stat().st_mtime_ns
        except OSError as exc:
            failed[name] = f"could not update mtime: {exc}"
            continue
        shifted[name] = (metadata.st_mtime_ns, moved)
    return shifted, failed


def _restore_mtimes(
    root: Path, shifted: dict[str, tuple[int, int]]
) -> tuple[list[str], list[str]]:
    """Undo the shift for every path Git did not rewrite (its mtime is still the shifted one).

    Returns the paths Git did rewrite and the paths whose mtime could not be restored.
    """
    rewritten: list[str] = []
    unrestored: list[str] = []
    for name, (original, moved) in shifted.items():
        path = root / name
        try:
            metadata = path.stat()
            if metadata.st_mtime_ns != moved:
                rewritten.append(name)
                continue
            os.utime(path, ns=(metadata.st_atime_ns, original))
        except FileNotFoundError:
            continue  # Git removed it and could not write it back; the failure hint says how to restore
        except OSError:
            unrestored.append(name)
    return rewritten, unrestored


def _checkout_index(root: Path, names: list[str]) -> str | None:
    """Re-check the listed paths out of the index as LF; the error text on failure."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                "checkout-index",
                "-f",
                "-u",
                "-z",
                "--stdin",
            ],
            input=_nul_joined(names),
            capture_output=True,
            check=False,
            timeout=300,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        detail = str(exc)
    else:
        if proc.returncode == 0:
            return None
        detail = _error_text(proc.stderr) or "git checkout-index failed"
    return (
        f"could not re-check out line-ending-only files: {detail}; a file Git removed and "
        "could not write is restored by `git checkout -- <path>`, otherwise re-run "
        f"{runtime_context.booley_init_hint(code=True)}"
    )


def _left_detail(normalized: int, left: dict[str, str]) -> str:
    shown = ", ".join(f"{name!r} ({cause})" for name, cause in list(left.items())[:3])
    more = f", and {len(left) - 3} more" if len(left) > 3 else ""
    prefix = f"normalized {normalized} file(s) to LF; " if normalized else ""
    return f"{prefix}left {len(left)} tracked file(s) untouched: {shown}{more}"


def _settle_failed_checkout(root: Path, shifted: dict[str, tuple[int, int]], error: str) -> str:
    """Leave the tree as a failed ``checkout-index`` left it, without phantom modifications.

    Git rewrites every path it can and writes no index when one fails. The rewritten
    files are LF with a stale index stat, which ``git status`` reports as modified, so
    their entries are re-recorded for the next refresh to compare by content.
    """
    rewritten, unrestored = _restore_mtimes(root, shifted)
    notes: list[str] = []
    stat_error = _reset_index_stat(root, tuple(rewritten)) if rewritten else None
    # Every shifted path, rewritten or not, now has a changed ctime. autocrlf=input compares an
    # unrewritten CRLF file by its LF content, so its entry is refreshed as clean even when no
    # text rule covers it; the file stays CRLF.
    _output, refresh_error = _git_output(
        root,
        (
            "-c",
            "core.autocrlf=input",
            "-c",
            "core.fsmonitor=false",
            "update-index",
            "-q",
            "--unmerged",
            "--refresh",
        ),
        timeout=300,
    )
    if stat_error := stat_error or refresh_error:
        notes.append(
            f"could not refresh the index after the failure: {stat_error}; once that is fixed "
            "run `git -c core.autocrlf=input update-index --refresh`"
        )
    if unrestored:
        notes.append(f"could not restore the mtime of {', '.join(map(repr, unrestored[:3]))}")
    return "; ".join([error, *notes])


def _recheckout_eligible(
    root: Path, eligible: list[str], left: dict[str, str]
) -> _NormalizationResult:
    """Shift, then re-check out the eligible paths in one Git process."""
    shifted, failed = _shift_mtimes(root, eligible)
    left.update(failed)
    names = list(shifted)
    if names:
        error = _checkout_index(root, names)
        if error is not None:
            error = _settle_failed_checkout(root, shifted, error)
            return _NormalizationResult(left=tuple(left.items()), error=error)
    identity = None
    if ".gitattributes" in shifted:
        identity, error = _optional_file_identity(root / ".gitattributes")
        if error is not None:
            error = f"normalized {len(names)} file(s) to LF, but could not read .gitattributes: {error}"
            return _NormalizationResult(tuple(names), tuple(left.items()), error=error)
    return _NormalizationResult(tuple(names), tuple(left.items()), identity)


def _eol_only_recheckout(root: Path, crlf_paths: tuple[str, ...]) -> _NormalizationResult:
    """Re-check out, as LF, the CRLF paths that differ from Git's copy by line endings alone.

    Classification happens now, from a fresh index scan, so an edit or an index
    change made after planning keeps its file untouched. Files Git must not
    rewrite (content or mode edits, ``eol=crlf`` rules, filters that emit CRLF) are
    reported in ``left`` with their cause.
    """
    entries, error = _scan_index(root)
    if entries is None:
        return _NormalizationResult(error=error)
    wanted = set(crlf_paths)
    candidates = {
        entry.path: entry
        for entry in entries
        if entry.path in wanted and entry.stage == "0" and entry.crlf_mismatch
    }
    refusal = _git_protected_refusal(candidates)
    if refusal is not None or not candidates:
        return _NormalizationResult(refusal=refusal)
    eligible, left, error = _classify_candidates(root, candidates)
    if error is not None:
        return _NormalizationResult(error=error)
    return _recheckout_eligible(root, eligible, left)


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
    reason: LineEndingRefusalReason | None = None,
) -> LineEndingActionResult:
    return LineEndingActionResult(
        kind, state, count=count, detail=detail, target=target, reason=reason
    )


def _read_required_policy(
    repository: LineEndingRepository,
) -> tuple[
    AutocrlfSetting | None, AutocrlfSetting | None, str | None, list[LineEndingObservation]
]:
    """Effective and repo-local ``core.autocrlf``, repo-local ``core.eol``, and what they imply."""
    observations: list[LineEndingObservation] = []
    effective = read_autocrlf_setting(repository.root)
    if effective is None:
        observations.append(_observation(LineEndingObservationCode.AUTOCRLF_UNREADABLE))
        return None, None, None, observations
    local = read_autocrlf_setting(repository.root, local=True)
    if local is None:
        observations.append(_observation(LineEndingObservationCode.LOCAL_AUTOCRLF_UNREADABLE))
        return effective, None, None, observations
    local_eol = read_local_eol(repository.root)
    if local_eol is None:
        observations.append(
            _observation(LineEndingObservationCode.LOCAL_AUTOCRLF_UNREADABLE, detail="core.eol")
        )
        return effective, local, None, observations
    if effective.value:
        observations.append(_observation(LineEndingObservationCode.AUTOCRLF_EFFECTIVE_TRUE))
    elif not local.is_set:
        observations.append(_observation(LineEndingObservationCode.AUTOCRLF_NOT_PINNED))
    return effective, local, local_eol, observations


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


def _upstream_attributes(root: Path) -> tuple[bool, bool]:
    """Whether worktree or index attributes set policy; whether an untracked root default leaked."""
    contents = {path: _policy_content(path)[1] for path in _attribute_files(root)}
    tracked_root = False
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
        # The index blob is policy even when the worktree file differs or is absent.
        content = _policy_git(root, "cat-file", "blob", object_id.decode("ascii"))
        if len(content) > 4_000_000:
            raise ValueError("attributes index blob exceeds 4 MB")
        contents[Path(name)] = content
        contents.setdefault(path, _policy_content(path)[1])
        tracked_root = tracked_root or name == ".gitattributes"
    root_content = contents.get(root / ".gitattributes", b"").strip()
    owned = any(has_attribute_policy(content) for content in contents.values())
    return owned, not tracked_root and root_content == GITATTRIBUTES_RULE.encode()


def unknown_attributes_policy_details(report: RepositoryLineEndingReport) -> tuple[str, ...]:
    """Stable inspection limitations, independent of confirmed policy and repair results."""
    return tuple(
        sorted(
            {
                observation.detail or "attributes policy inspection incomplete"
                for observation in report.observations
                if observation.code is LineEndingObservationCode.ATTRIBUTES_POLICY_UNKNOWN
            }
        )
    )


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


def _selected_policy(path: Path | None) -> bool:
    # The OS bounds symlink chains; a loop leaves a link that _policy_content refuses.
    return path is not None and has_attribute_policy(
        _policy_content(Path(os.path.realpath(path)))[1]
    )


def _worktree_user_inputs(root: Path) -> tuple[bool, set[str]]:
    """Whether one checkout's selected user or system file sets policy, and unknown sources."""
    supported, path = _native_attribute_path(root, "GIT_ATTR_GLOBAL")
    if not supported:
        path = _fallback_user_attributes(root)
    owned = _selected_policy(path)
    if system_attributes_disabled():
        return owned, set()
    supported, system = _native_attribute_path(root, "GIT_ATTR_SYSTEM")
    if not supported:
        return owned, {
            "system attributes path unresolved on this Git version; inspect system policy with a Git build that supports attributes path discovery; existing local attributes are preserved and new defaults are deferred"
        }
    return _selected_policy(system) or owned, set()


def _effective_user_inputs(root: Path) -> tuple[bool, set[str], bytes]:
    """User/system ownership and unknown sources across every registered worktree."""
    listing = _policy_git(root, "worktree", "list", "--porcelain", "-z")
    roots = [
        Path(os.fsdecode(field.removeprefix(b"worktree ")))
        for field in listing.split(b"\0")
        if field.startswith(b"worktree ")
    ]
    if root not in roots or len(roots) > 128:
        raise ValueError("Git user-attributes inventory requires 1 to 128 worktrees")
    owned, unknown = False, set()
    for sibling in roots:
        if not sibling.is_absolute():
            raise ValueError("Git worktree listing did not provide absolute paths")
        available = False
        try:
            available = sibling.is_dir()
            if not available:
                raise ValueError("worktree directory is unavailable")
            sibling_owned, sibling_unknown = _worktree_user_inputs(sibling)
            owned, unknown = owned or sibling_owned, unknown | sibling_unknown
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if sibling == root:
                raise
            guidance = (
                "inspect this sibling's attributes selection, including the selected file's type and access"
                if available
                else "inspect its registered location from an environment where it is accessible (host if needed)"
            )
            unknown.add(
                f"cannot inspect sibling worktree {sibling}: {exc}; {guidance}; existing local attributes and registrations are preserved and new defaults are deferred".encode(
                    errors="surrogateescape"
                ).decode(errors="backslashreplace")
            )
    return owned, unknown, listing


def _policy_facts(root: Path) -> tuple[_PolicyFacts, bool]:
    """Fresh ownership, unknown-policy and topology facts, plus the leaked-root-default flag."""
    upstream, leaked = _upstream_attributes(root)
    user, unknown, topology = _effective_user_inputs(root)
    return _PolicyFacts(upstream or user, tuple(sorted(unknown)), topology), leaked


def _nonlocal_attributes_plan(repository: LineEndingRepository, target: AttributesTarget):
    observations: list[LineEndingObservation] = []
    migration = None
    error = None
    owned = _eol_policy_is_user_owned(repository.root)
    if repository.role == "project-checkout":
        try:
            common = _common_attributes(repository.root)
            snapshot = _policy_content(common)
            if has_managed_attributes(snapshot[1], replayable=True):
                root_snapshot = _policy_content(target.path)
                owned = _root_policy_owned(root_snapshot[1])
                topology = _policy_git(repository.root, "worktree", "list", "--porcelain", "-z")
                migration = _AttributesMigration(common, topology, snapshot, root_snapshot, owned)
            elif has_managed_attributes(snapshot[1]):
                observations.append(
                    _observation(
                        LineEndingObservationCode.LOCAL_POLICY_CONFLICT,
                        detail=f"{common}: existing local default conflicts with Stealth opt-out; inspect and remove the local rule if appropriate (Booley cannot prove ownership)",
                    )
                )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = f"attributes policy inspection failed: {exc}"
    return target, None, owned, False, observations, error, migration


def _attributes_plan(repository: LineEndingRepository, stealth: bool):
    local = stealth and repository.role == "project-checkout"
    target = AttributesTarget(repository.root / ".gitattributes", local=local)
    if not local:
        return _nonlocal_attributes_plan(repository, target)
    observations: list[LineEndingObservation] = []
    try:
        common = _common_attributes(repository.root)
        target = AttributesTarget(common, local=True)
        common_content = _policy_content(common)[1]
        facts, leaked = _policy_facts(repository.root)
        observations.extend(
            _observation(LineEndingObservationCode.ATTRIBUTES_POLICY_UNKNOWN, detail=message)
            for message in facts.unknown
        )
        upstream = facts.owned
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
        _record_stealth_policy(common, common_content, facts, leaked, observations)
        owned = upstream or local_policy_owned(common_content)
        missing = not owned and not facts.unknown
        return target, facts, owned, missing, observations, None, None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return (
            target,
            None,
            False,
            local,
            observations,
            f"attributes policy inspection failed: {exc}",
            None,
        )


def _record_stealth_policy(
    common: Path, content: bytes, facts: _PolicyFacts, leaked: bool, observations
) -> None:
    if leaked:
        observations.append(
            _observation(
                LineEndingObservationCode.LEAKED_ROOT_POLICY,
                detail="untracked .gitattributes contains an old default; inspect and remove or migrate it manually if appropriate (Booley cannot prove ownership)",
            )
        )
    if facts.owned:
        observations.append(
            _observation(
                LineEndingObservationCode.UPSTREAM_POLICY,
                detail="existing upstream/user/system attributes policy is preserved; no local default installed",
            )
        )
    elif not local_policy_owned(content) and not facts.unknown:
        observations.append(
            _observation(
                LineEndingObservationCode.LOCAL_POLICY_MISSING,
                detail=(
                    f"repository-local line-ending policy missing at {common}; "
                    f"re-run {runtime_context.booley_init_hint()}"
                ),
            )
        )


def _publish_planned_attributes(
    plan: _RepositoryPlan, expected: _FileIdentity | None
) -> LineEndingActionResult:
    error = plan.attributes_error
    if error is None and plan.target.local:
        error = _revalidate_local_policy(plan)
    if error:
        return _action(
            LineEndingActionKind.PUBLISH_ATTRIBUTES,
            LineEndingActionState.REFUSED,
            detail=error,
            target=plan.target,
            reason=LineEndingRefusalReason.ATTRIBUTES_POLICY,
        )
    if plan.target.local:
        path = plan.target.path
        try:
            path.parent.mkdir(exist_ok=True)
            if path.parent.is_symlink() or not path.parent.is_dir():
                raise ValueError(f"unsafe attributes directory: {path.parent}")
        except (OSError, ValueError) as exc:
            return _action(
                LineEndingActionKind.PUBLISH_ATTRIBUTES,
                LineEndingActionState.FAILED,
                detail=f"{path}: {exc}",
                target=plan.target,
            )
    return _publish_attributes(plan.target, expected)


def _revalidate_local_policy(plan: _RepositoryPlan) -> str | None:
    # Ownership and unknown policy are re-decided; the target keeps its narrow identity check.
    try:
        if _policy_facts(plan.repository.root)[0] != plan.facts:
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
    effective, local, local_eol, observations = _read_required_policy(repository)
    crlf_paths, phantom_paths, worktree_observations = _read_worktree_observations(repository)
    observations.extend(worktree_observations)
    if (
        effective is None
        or local is None
        or local_eol is None
        or crlf_paths is None
        or phantom_paths is None
    ):
        return None, tuple(observations)

    if not crlf_paths:
        clean: bool | None = True
    elif baseline_clean is not None:
        clean = baseline_clean
    else:
        clean = _worktree_is_clean(repository.root)
    target, facts, owned, missing, policy_obs, error, migration = _attributes_plan(
        repository, stealth
    )
    observations.extend(policy_obs)
    attributes, error = _observe_attributes_snapshot(target, error, observations)
    return (
        _RepositoryPlan(
            repository=repository,
            effective_autocrlf=effective,
            local_autocrlf=local,
            local_eol=local_eol,
            crlf_paths=tuple(crlf_paths),
            phantom_paths=tuple(phantom_paths),
            clean=clean,
            attributes=attributes,
            attributes_error=error,
            owns_attributes_policy=owned,
            target=target,
            facts=facts,
            local_missing=missing,
            migration=migration,
        ),
        tuple(observations),
    )


def _normalization_refusal(plan: _RepositoryPlan) -> _Refusal | None:
    if plan.clean is None:
        return _Refusal(
            LineEndingRefusalReason.STATUS_UNREADABLE,
            "could not read `git status` — refusing to normalize tracked files",
        )
    if not plan.clean:
        return _Refusal(
            LineEndingRefusalReason.DIRTY_TREE,
            "working tree has uncommitted changes — refusing to normalize it "
            "(normalization re-checks out the affected tracked files). Commit or stash first, "
            f"then re-run {runtime_context.booley_init_hint(code=True)}.",
        )
    return None


def _pin_needed(plan: _RepositoryPlan) -> bool:
    """Whether the repo-local line-ending pin must be written.

    ``core.autocrlf`` unset or true needs it, and so does a ``core.eol`` that is
    not ``lf`` (Git for Windows' native value is ``crlf``): both decide what the
    next checkout writes, including in a repository an earlier Booley pinned.
    """
    autocrlf_needed = not plan.local_autocrlf.is_set or plan.local_autocrlf.value
    return autocrlf_needed or plan.local_eol.casefold() != "lf"


def _planned_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    actions: list[LineEndingActionResult] = []
    if _pin_needed(plan):
        actions.append(_action(LineEndingActionKind.PIN_AUTOCRLF, LineEndingActionState.PLANNED))
    if plan.crlf_paths:
        refusal = _normalization_refusal(plan)
        state = LineEndingActionState.REFUSED if refusal else LineEndingActionState.PLANNED
        actions.append(
            _action(
                LineEndingActionKind.NORMALIZE_FILES,
                state,
                count=len(plan.crlf_paths),
                detail=refusal.detail if refusal else None,
                reason=refusal.reason if refusal else None,
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
    if plan.migration:
        actions.append(_migration_action(plan, LineEndingActionState.PLANNED))
    elif _attributes_publication_needed(plan):
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
                reason=LineEndingRefusalReason.ATTRIBUTES_POLICY
                if plan.attributes_error
                else None,
            )
        )
    return tuple(actions)


def _attributes_publication_needed(plan: _RepositoryPlan) -> bool:
    needs_policy = (
        bool(plan.crlf_paths)
        or not plan.local_autocrlf.is_set
        or plan.local_autocrlf.value
        or plan.local_missing
    )
    return (
        needs_policy
        and not plan.owns_attributes_policy
        and (plan.attributes_error is not None or not (plan.facts and plan.facts.unknown))
    )


def _inspection_report(
    repository: LineEndingRepository, *, stealth: bool = False
) -> RepositoryLineEndingReport:
    plan, observations = _plan_repository(repository, stealth=stealth)
    actions = () if plan is None else _planned_actions(plan)
    unsafe = any(
        item.code
        not in (
            LineEndingObservationCode.UPSTREAM_POLICY,
            LineEndingObservationCode.ATTRIBUTES_POLICY_UNKNOWN,
        )
        for item in observations
    )
    status = LineEndingStatus.UNSAFE if unsafe else LineEndingStatus.SAFE
    return RepositoryLineEndingReport(repository, status, observations, actions)


def _set_local_config(root: Path, key: str, value: str) -> str | None:
    """Write one repo-local Git setting; the failure text, or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--local", key, value],
            capture_output=True,
            check=False,
            timeout=10,
            env=_read_only_git_env(),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"could not set {key}: {exc}"
    if proc.returncode != 0:
        return f"could not set {key}: {_error_text(proc.stderr) or 'git config failed'}"
    return None


def _pin_changed_setting(plan: _RepositoryPlan) -> str | None:
    """The key whose value moved since planning, if any."""
    root = plan.repository.root
    if (read_autocrlf_setting(root), read_autocrlf_setting(root, local=True)) != (
        plan.effective_autocrlf,
        plan.local_autocrlf,
    ):
        return "core.autocrlf"
    if read_local_eol(root) != plan.local_eol:
        return "core.eol"
    return None


def _pin_line_ending_config(plan: _RepositoryPlan) -> LineEndingActionResult:
    """Pin ``core.autocrlf=false`` and ``core.eol=lf`` repo-locally, writing only what is not already set."""
    kind = LineEndingActionKind.PIN_AUTOCRLF
    root = plan.repository.root
    changed = _pin_changed_setting(plan)
    if changed is not None:
        return _action(
            kind,
            LineEndingActionState.REFUSED,
            detail=f"{changed} changed during line-ending repair",
            reason=LineEndingRefusalReason.SETTING_CHANGED,
        )
    writes: list[tuple[str, str]] = []
    if not plan.local_autocrlf.is_set or plan.local_autocrlf.value:
        writes.append(("core.autocrlf", "false"))
    if plan.local_eol.casefold() != "lf":
        writes.append(("core.eol", "lf"))
    for key, value in writes:
        if error := _set_local_config(root, key, value):
            return _action(kind, LineEndingActionState.FAILED, detail=error)
    if read_autocrlf_setting(root, local=True) != AutocrlfSetting(False, is_set=True) or (
        (read_local_eol(root) or "").casefold() != "lf"
    ):
        return _action(
            kind,
            LineEndingActionState.FAILED,
            detail="could not set core.autocrlf/core.eol: repo-local values did not verify",
        )
    reason = "effective true" if plan.effective_autocrlf.value else "not pinned"
    return _action(kind, LineEndingActionState.COMPLETED, detail=reason)


def _normalize_action(
    plan: _RepositoryPlan,
) -> tuple[LineEndingActionResult, _NormalizationResult | None]:
    kind = LineEndingActionKind.NORMALIZE_FILES
    total = len(plan.crlf_paths)
    refusal = _normalization_refusal(plan)
    if refusal is not None:
        refused = _action(
            kind,
            LineEndingActionState.REFUSED,
            count=total,
            detail=refusal.detail,
            reason=refusal.reason,
        )
        return refused, None
    result = _eol_only_recheckout(plan.repository.root, plan.crlf_paths)
    if result.refusal is not None:
        refused = _action(
            kind,
            LineEndingActionState.REFUSED,
            count=total,
            detail=result.refusal.detail,
            reason=result.refusal.reason,
        )
        return refused, result
    if result.error is not None:
        failed = _action(kind, LineEndingActionState.FAILED, count=total, detail=result.error)
        return failed, result
    if result.left:
        left = _action(
            kind,
            LineEndingActionState.REFUSED,
            count=total,
            detail=_left_detail(len(result.normalized), dict(result.left)),
            reason=LineEndingRefusalReason.NOT_LINE_ENDING_ONLY,
        )
        return left, result
    done = _action(kind, LineEndingActionState.COMPLETED, count=len(result.normalized))
    return done, result


def _reset_index_stat(root: Path, names: tuple[str, ...]) -> str | None:
    """Re-record each path's own index entry so Git must re-compare it with the file.

    ``update-index --refresh`` trusts a changed file size without reading the
    file, so a stale entry from a CRLF checkout never refreshes. An entry written
    by ``--index-info`` has no recorded size, which makes the next refresh compare
    content. The object id and mode stay as they are; no file content is staged.
    """
    entries, error = _scan_index(root)
    if entries is None:
        return error
    wanted = set(names)
    payload = b"".join(
        f"{entry.mode} {entry.oid}\t".encode("ascii") + os.fsencode(entry.path) + b"\0"
        for entry in entries
        if entry.path in wanted and entry.stage == "0"
    )
    _output, error = _git_output(root, ("update-index", "-z", "--index-info"), stdin=payload)
    return error


def _refresh_action(plan: _RepositoryPlan) -> LineEndingActionResult:
    """Refresh stale stat data of paths that show as modified with no content change.

    Only stat data changes: a path whose content really differs stays modified.
    """
    root = plan.repository.root
    error = _reset_index_stat(root, plan.phantom_paths)
    if error is None:
        _output, error = _git_output(
            root,
            ("-c", "core.fsmonitor=false", "update-index", "-q", "--unmerged", "--refresh"),
            timeout=300,
        )
    state = LineEndingActionState.COMPLETED if error is None else LineEndingActionState.FAILED
    return _action(
        LineEndingActionKind.REFRESH_INDEX,
        state,
        count=len(plan.phantom_paths),
        detail=error,
    )


def _attributes_replacement(existing: bytes) -> bytes:
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    return GITATTRIBUTES_RULE.encode() + b"\n" + existing


def _write_attributes(
    path: Path,
    expected: _FileIdentity | None,
    content: bytes,
    validate: Callable[[], str | None] | None = None,
) -> str | None:
    staged, error = _stage_atomic_content(path, content, mode=expected.mode if expected else 0o644)
    if error is not None:
        return error
    assert staged is not None
    try:
        current, error = _optional_file_identity(path)
        if error is not None or current != expected:
            return error or f"{path}: attributes changed during line-ending repair"
        if validate and (error := validate()):
            return error
        if expected is None:
            try:
                os.link(staged, path)
            except FileExistsError:
                return f"{path}: attributes changed during line-ending repair"
        else:
            staged.replace(path)
    except OSError as exc:
        return f"could not atomically publish {path}: {exc}"
    finally:
        with suppress(FileNotFoundError, PermissionError):
            staged.unlink()
    return None


def _publish_attributes(
    target: AttributesTarget,
    expected: _FileIdentity | None,
    validate: Callable[[], str | None] | None = None,
) -> LineEndingActionResult:
    path = target.path
    current, error = _optional_file_identity(path)
    if error is not None or current != expected:
        return _action(
            LineEndingActionKind.PUBLISH_ATTRIBUTES,
            LineEndingActionState.REFUSED,
            detail=(
                f"{path}: attributes changed during line-ending repair"
                if target.local
                else error or ".gitattributes changed during line-ending repair"
            ),
            target=target,
            reason=LineEndingRefusalReason.ATTRIBUTES_POLICY,
        )
    try:
        existing = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        error = str(exc) if target.local else f"could not read .gitattributes: {exc}"
    else:
        # Ownership was decided from an earlier read; the planned identity may postdate it.
        if (local_policy_owned if target.local else _root_policy_owned)(existing):
            return _action(
                LineEndingActionKind.PUBLISH_ATTRIBUTES,
                LineEndingActionState.REFUSED,
                detail=f"{path}: attributes policy changed during line-ending repair",
                target=target,
                reason=LineEndingRefusalReason.ATTRIBUTES_POLICY,
            )
        content = _attributes_replacement(existing)
        try:
            error = _write_attributes(path, expected, content, validate)
        except OSError as exc:
            if not target.local:
                raise
            error = str(exc)
    state = LineEndingActionState.COMPLETED if error is None else LineEndingActionState.FAILED
    return _action(
        LineEndingActionKind.PUBLISH_ATTRIBUTES,
        state,
        detail=f"{path}: {error}" if target.local and error else error,
        target=target,
    )


def _migration_action(
    plan: _RepositoryPlan, state: LineEndingActionState, error: str | None = None
) -> LineEndingActionResult:
    migration = plan.migration
    assert migration is not None
    destination = (
        "preserving existing root policy" if migration.root_owned else "publishing root default"
    )
    return _action(
        LineEndingActionKind.MIGRATE_ATTRIBUTES,
        state,
        detail=error or f"{migration.source} -> {plan.target.path} ({destination})",
        target=plan.target,
        reason=LineEndingRefusalReason.ATTRIBUTES_POLICY
        if state is LineEndingActionState.REFUSED
        else None,
    )


def _migration_revalidation(
    plan: _RepositoryPlan, root_snapshot: tuple[_FileIdentity | None, bytes]
) -> str | None:
    migration = plan.migration
    assert migration is not None
    try:
        if _policy_git(plan.repository.root, "worktree", "list", "--porcelain", "-z") != (
            migration.topology
        ):
            return "Git worktree topology changed during line-ending repair"
        if _policy_content(migration.source) != migration.source_snapshot:
            return f"{migration.source}: attributes changed during line-ending repair"
        if _policy_content(plan.target.path) != root_snapshot:
            return f"{plan.target.path}: attributes changed during line-ending repair"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"could not revalidate attributes migration: {exc}"
    return None


def _raw_attribute_lines(content: bytes):
    parts = content.split(b"\n")
    return [line + b"\n" for line in parts[:-1]] + [parts[-1]]


def _remove_migrated_default(plan: _RepositoryPlan, root_snapshot) -> str | None:
    migration = plan.migration
    assert migration is not None
    identity, content = migration.source_snapshot
    assert identity is not None
    # Split only at Git's newline boundary, retaining every other byte verbatim.
    remaining = b"".join(
        line
        for line in _raw_attribute_lines(content)
        if line.strip(b" \t\r\n") != GITATTRIBUTES_RULE.encode()
    )
    error = _migration_revalidation(plan, root_snapshot)
    if error:
        return error
    if remaining:
        return _write_attributes(
            migration.source,
            identity,
            remaining,
            lambda: _migration_revalidation(plan, root_snapshot),
        )
    try:
        migration.source.unlink()
    except OSError as exc:
        return f"could not remove {migration.source}: {exc}"
    return None


def _migrate_attributes(
    plan: _RepositoryPlan, normalization: _NormalizationResult | None
) -> LineEndingActionResult:
    migration = plan.migration
    assert migration is not None
    root_snapshot = migration.root_snapshot
    if normalization and normalization.attributes_identity is not None:
        root_snapshot = (
            normalization.attributes_identity,
            root_snapshot[1].replace(b"\r\n", b"\n"),
        )
    error = _migration_revalidation(plan, root_snapshot)
    if error:
        return _migration_action(plan, LineEndingActionState.REFUSED, error)
    if not migration.root_owned:
        publication = _publish_attributes(
            plan.target,
            root_snapshot[0],
            lambda: _migration_revalidation(plan, root_snapshot),
        )
        if publication.state is not LineEndingActionState.COMPLETED:
            return _migration_action(plan, publication.state, publication.detail)
        try:
            published = _policy_content(plan.target.path)
            expected_content = GITATTRIBUTES_RULE.encode() + b"\n" + root_snapshot[1]
            if root_snapshot[1] and not root_snapshot[1].endswith(b"\n"):
                expected_content += b"\n"
            if published[1] != expected_content:
                return _migration_action(
                    plan, LineEndingActionState.REFUSED, "root policy changed after publication"
                )
            root_snapshot = published
        except (OSError, ValueError) as exc:
            return _migration_action(plan, LineEndingActionState.FAILED, str(exc))
    error = _remove_migrated_default(plan, root_snapshot)
    state = LineEndingActionState.FAILED if error else LineEndingActionState.COMPLETED
    return _migration_action(plan, state, error)


def _repair_settings_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    """Apply the settings-only subset of a repair and leave the rest planned.

    ``core.autocrlf`` and the repository-local attributes file never rewrite a
    tracked file, but on a tree that already holds CRLF checkouts either one would
    make Git report phantom modifications without normalizing anything, so both
    wait for a full repair there. Every other action is reported as planned.
    """
    crlf_present = bool(plan.crlf_paths)
    actions: list[LineEndingActionResult] = []
    for planned in _planned_actions(plan):
        if planned.kind is LineEndingActionKind.PIN_AUTOCRLF and not crlf_present:
            actions.append(_pin_line_ending_config(plan))
        elif (
            planned.kind is LineEndingActionKind.PUBLISH_ATTRIBUTES
            and planned.state is LineEndingActionState.PLANNED
            and plan.target.local
            and not crlf_present
        ):
            actions.append(_publish_planned_attributes(plan, plan.attributes))
        else:
            actions.append(planned)
    return tuple(actions)


def _repair_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    actions: list[LineEndingActionResult] = []
    if _pin_needed(plan):
        actions.append(_pin_line_ending_config(plan))
    normalization: _NormalizationResult | None = None
    if plan.crlf_paths:
        action, normalization = _normalize_action(plan)
        actions.append(action)
    elif plan.phantom_paths:
        actions.append(_refresh_action(plan))
    if plan.migration:
        actions.append(_migrate_attributes(plan, normalization))
    elif _attributes_publication_needed(plan):
        expected = plan.attributes
        if (
            not plan.target.local
            and normalization is not None
            and normalization.attributes_identity is not None
        ):
            expected = normalization.attributes_identity
        actions.append(_publish_planned_attributes(plan, expected))
    return tuple(actions)


def _repair_repository(
    repository: LineEndingRepository,
    baseline_clean: bool | None = None,
    *,
    stealth: bool = False,
    settings_only: bool = False,
) -> RepositoryLineEndingReport:
    plan, initial_observations = _plan_repository(repository, baseline_clean, stealth=stealth)
    if plan is None:
        return RepositoryLineEndingReport(
            repository,
            LineEndingStatus.UNSAFE,
            initial_observations,
            (),
        )
    actions = _repair_settings_actions(plan) if settings_only else _repair_actions(plan)
    final = _inspection_report(repository, stealth=stealth)
    incomplete = any(
        action.state in (LineEndingActionState.REFUSED, LineEndingActionState.FAILED)
        for action in actions
    )
    status = LineEndingStatus.UNSAFE if incomplete else final.status
    return RepositoryLineEndingReport(repository, status, final.observations, actions)


def sample_worktree_cleanliness(
    project_root: Path, booley_dir: Path | None = None
) -> dict[Path, bool | None]:
    """Record whether each RTL or Booley Directory's repository is clean before init writes anything.

    Init itself edits tracked files in the Booley Directory (for example the ``[agent]``
    selection in ``booley.toml``) before the line-ending repair runs. Those LF
    edits are Booley's own, not user work, so the repair judges "uncommitted
    changes" against this pre-write baseline. A repository dirty here still
    refuses normalization. None = Git could not answer for that repository.
    """
    discovery = discover_line_ending_repositories(project_root, booley_dir)
    return {
        repository.root: _worktree_is_clean(repository.root)
        for repository in discovery.repositories
    }


def reconcile_project_line_endings(
    project_root: Path,
    booley_dir: Path | None = None,
    *,
    mode: LineEndingMode,
    clean_baseline: Mapping[Path, bool | None] | None = None,
) -> LineEndingReport:
    """Inspect or reconcile every distinct repository supplying Project files.

    ``clean_baseline`` comes from :func:`sample_worktree_cleanliness` taken
    before the caller wrote files in the Booley Directory. Repair uses it instead of the
    current ``git status`` for each repository it covers.
    """
    discovery = discover_line_ending_repositories(project_root, booley_dir)
    if not discovery.repositories and not discovery.failures:
        return LineEndingReport(LineEndingStatus.NOT_APPLICABLE, (), ())
    baseline = clean_baseline or {}
    stealth = stealth_enabled(project_root, booley_dir=booley_dir)
    reports = tuple(
        _inspection_report(repository, stealth=stealth)
        if mode is LineEndingMode.INSPECT
        else _repair_repository(
            repository,
            baseline.get(repository.root),
            stealth=stealth,
            settings_only=mode is LineEndingMode.REPAIR_SETTINGS,
        )
        for repository in discovery.repositories
    )
    unsafe = bool(discovery.failures) or any(
        report.status is not LineEndingStatus.SAFE for report in reports
    )
    status = LineEndingStatus.UNSAFE if unsafe else LineEndingStatus.SAFE
    return LineEndingReport(status, reports, discovery.failures)
