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
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple

from booley.runtime import runtime_context
from booley.runtime.git_environment import inherited_git_environment

LineEndingRole = Literal["project-checkout", "project-data"]


class LineEndingMode(StrEnum):
    """Whether to inspect only or reconcile safe forward changes."""

    INSPECT = "inspect"
    #: Apply only the Git settings pin; report tracked-content actions.
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


class LineEndingActionKind(StrEnum):
    """Stable reconciliation actions exposed without implementation state."""

    PIN_AUTOCRLF = "pin-autocrlf"
    NORMALIZE_FILES = "normalize-files"
    REFRESH_INDEX = "refresh-index"


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
class _RepositoryPlan:
    """Private guarded transformation plan for one repository."""

    repository: LineEndingRepository
    effective_autocrlf: AutocrlfSetting
    local_autocrlf: AutocrlfSetting
    local_eol: str
    crlf_paths: tuple[str, ...]
    phantom_paths: tuple[str, ...]
    clean: bool | None


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

    ``left`` pairs each tracked path the repair did not touch with its cause.
    """

    normalized: tuple[str, ...] = ()
    left: tuple[tuple[str, str], ...] = ()
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
    return _NormalizationResult(tuple(names), tuple(left.items()))


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
    reason: LineEndingRefusalReason | None = None,
) -> LineEndingActionResult:
    return LineEndingActionResult(kind, state, count=count, detail=detail, reason=reason)


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


def _plan_repository(
    repository: LineEndingRepository,
    baseline_clean: bool | None = None,
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
    return (
        _RepositoryPlan(
            repository=repository,
            effective_autocrlf=effective,
            local_autocrlf=local,
            local_eol=local_eol,
            crlf_paths=tuple(crlf_paths),
            phantom_paths=tuple(phantom_paths),
            clean=clean,
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
    return tuple(actions)


def _inspection_report(repository: LineEndingRepository) -> RepositoryLineEndingReport:
    plan, observations = _plan_repository(repository)
    actions = () if plan is None else _planned_actions(plan)
    status = LineEndingStatus.UNSAFE if observations else LineEndingStatus.SAFE
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


def _normalize_action(plan: _RepositoryPlan) -> LineEndingActionResult:
    kind = LineEndingActionKind.NORMALIZE_FILES
    total = len(plan.crlf_paths)
    refusal = _normalization_refusal(plan)
    if refusal is not None:
        return _action(
            kind,
            LineEndingActionState.REFUSED,
            count=total,
            detail=refusal.detail,
            reason=refusal.reason,
        )
    result = _eol_only_recheckout(plan.repository.root, plan.crlf_paths)
    if result.refusal is not None:
        return _action(
            kind,
            LineEndingActionState.REFUSED,
            count=total,
            detail=result.refusal.detail,
            reason=result.refusal.reason,
        )
    if result.error is not None:
        return _action(kind, LineEndingActionState.FAILED, count=total, detail=result.error)
    if result.left:
        return _action(
            kind,
            LineEndingActionState.REFUSED,
            count=total,
            detail=_left_detail(len(result.normalized), dict(result.left)),
            reason=LineEndingRefusalReason.NOT_LINE_ENDING_ONLY,
        )
    return _action(kind, LineEndingActionState.COMPLETED, count=len(result.normalized))


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


def _repair_settings_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    """Apply the settings-only subset of a repair and leave the rest planned.

    The ``core.autocrlf`` pin never rewrites a tracked file, but on a tree that
    already holds CRLF checkouts it would make Git report phantom modifications
    without normalizing anything, so it waits for a full repair there. Every
    other action is reported as planned.
    """
    crlf_present = bool(plan.crlf_paths)
    actions: list[LineEndingActionResult] = []
    for planned in _planned_actions(plan):
        if planned.kind is LineEndingActionKind.PIN_AUTOCRLF and not crlf_present:
            actions.append(_pin_line_ending_config(plan))
        else:
            actions.append(planned)
    return tuple(actions)


def _repair_actions(plan: _RepositoryPlan) -> tuple[LineEndingActionResult, ...]:
    """Normalize CRLF checkouts first; flip ``core.autocrlf`` only once none are left.

    Pinning ``core.autocrlf=false`` over a CRLF checkout that stays (a refused or
    failed normalization) makes Git report every such file as modified, and
    nothing masks that now that Booley writes no ``.gitattributes`` rule. The pin
    stays planned there, as in a settings-only repair, until a later run
    normalizes the files. A repository whose local ``core.autocrlf`` is already
    ``false`` has nothing to flip, so a ``core.eol`` pin still applies.
    """
    actions: list[LineEndingActionResult] = []
    normalized: LineEndingActionResult | None = None
    if plan.crlf_paths:
        normalized = _normalize_action(plan)
        actions.append(normalized)
    if _pin_needed(plan):
        flips_autocrlf = not plan.local_autocrlf.is_set or plan.local_autocrlf.value
        withheld = (
            normalized is not None
            and normalized.state is not LineEndingActionState.COMPLETED
            and flips_autocrlf
        )
        actions.append(
            _action(LineEndingActionKind.PIN_AUTOCRLF, LineEndingActionState.PLANNED)
            if withheld
            else _pin_line_ending_config(plan)
        )
    if not plan.crlf_paths and plan.phantom_paths:
        actions.append(_refresh_action(plan))
    return tuple(actions)


def _repair_repository(
    repository: LineEndingRepository,
    baseline_clean: bool | None = None,
    *,
    settings_only: bool = False,
) -> RepositoryLineEndingReport:
    plan, initial_observations = _plan_repository(repository, baseline_clean)
    if plan is None:
        return RepositoryLineEndingReport(
            repository,
            LineEndingStatus.UNSAFE,
            initial_observations,
            (),
        )
    actions = _repair_settings_actions(plan) if settings_only else _repair_actions(plan)
    final = _inspection_report(repository)
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
    reports = tuple(
        _inspection_report(repository)
        if mode is LineEndingMode.INSPECT
        else _repair_repository(
            repository,
            baseline.get(repository.root),
            settings_only=mode is LineEndingMode.REPAIR_SETTINGS,
        )
        for repository in discovery.repositories
    )
    unsafe = bool(discovery.failures) or any(
        report.status is not LineEndingStatus.SAFE for report in reports
    )
    status = LineEndingStatus.UNSAFE if unsafe else LineEndingStatus.SAFE
    return LineEndingReport(status, reports, discovery.failures)
