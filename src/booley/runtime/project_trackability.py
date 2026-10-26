"""Read-only diagnostics for durable Project inputs hidden by effective Git rules."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.booley_dir import BOOLEY_DIR_NAME, resolve_checkout_booley_dir
from booley.runtime.git_attributes_policy import has_managed_attributes
from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.project_gitignore import project_transient_pattern
from booley.runtime.project_repositories import is_git_worktree_root, run_git

_SEEDS = (".gitignore", "booley.toml", "tests.toml", "FUSESOC_IGNORE")
_OPTIONAL = (
    "AGENTS.md",
    "criteria.toml",
    "doctor-waivers.toml",
    "SETUP-PLAN.md",
    "pipeline.toml",
    "configs.toml",
)
_INPUT_TREES = (
    "goalsets",
    "cores",
    "selftest",
    "hooks",
    "generators",
    "mcp_tools",
    "docker",
    "goals/history",
    "tickets/history",
)
FIX = (
    "edit the reported exclusion (including excluded ancestors) to allow the named paths, "
    "then rerun booley init or booley doctor"
)
HISTORY_PROBE = f"{BOOLEY_DIR_NAME}/goals/history/doctor-probe.md"


def history_probe(root: Path) -> str | None:
    """The RTL-relative Goal-history probe path, or ``None`` when RTL cannot publish it.

    Git never looks beyond a symlink, so a symlinked Booley Directory entry is probed
    at its physical target. A target outside the RTL checkout has no RTL path: the
    owning checkout (or its absence) is reported by Project trackability instead.
    """
    entry = root / BOOLEY_DIR_NAME
    if not entry.is_symlink():
        return HISTORY_PROBE
    try:
        physical = entry.resolve() / "goals/history/doctor-probe.md"
        return physical.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class IgnoreMatch:
    """A positive effective exclude, with Git's exact source spelling."""

    source: str
    line: int
    pattern: str
    path: str


@dataclass(frozen=True)
class IgnoredInputs:
    """Durable inputs hidden by one rule in one owning repository."""

    owner: Path
    role: str
    source: str
    line: int
    pattern: str
    paths: tuple[str, ...]
    history: bool
    source_identity: str

    @property
    def subject(self) -> str:
        """Stable waiver identity, independent of HOME and source line shifts."""
        return f"{self.role}:{self.source_identity}:{self.pattern}"

    @property
    def message(self) -> str:
        """A bounded preview retaining source coordinates and the literal rule."""
        preview = ", ".join(self.paths[:5])
        extra = f" (+{len(self.paths) - 5} more)" if len(self.paths) > 5 else ""
        return (
            f"Git exclusion hides new/untracked durable inputs in {self.role}: {preview}{extra} "
            f"({len(self.paths)} path(s)). Exclusion: {self.source}:{self.line}:{self.pattern}"
        )


@dataclass(frozen=True)
class TrackabilityInspection:
    """Hidden input groups and explicit incomplete-inspection locations."""

    ignored: tuple[IgnoredInputs, ...]
    errors: tuple[str, ...]


def _git(root: Path, arguments: list[str], payload: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=inherited_git_environment(),
            input=payload,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"cannot inspect exclusions at {root}: {exc}") from exc
    allowed = (0, 1) if arguments[0] == "check-ignore" else (0,)
    if result.returncode not in allowed:
        raise OSError(f"cannot inspect exclusions at {root}: {os.fsdecode(result.stderr).strip()}")
    return result.stdout


def _gitlinks(owner: Path) -> tuple[Path, ...]:
    rows = _git(owner, ["ls-files", "--stage", "-z"])
    try:
        return tuple(
            owner / os.fsdecode(row.split(b"\t", 1)[1])
            for row in rows.split(b"\0")
            if row.startswith(b"160000 ")
        )
    except IndexError as exc:
        raise OSError(f"malformed Git index records at {owner}") from exc


def _parse_matches(output: bytes) -> tuple[IgnoreMatch, ...]:
    fields = output.split(b"\0")
    if fields.pop() != b"" or len(fields) % 4:
        raise OSError("malformed Git check-ignore records")
    matches = []
    for offset in range(0, len(fields), 4):
        source, line, pattern, path = map(os.fsdecode, fields[offset : offset + 4])
        if not source or not line.isdecimal() or int(line) < 1 or not pattern or not path:
            raise OSError("malformed Git check-ignore record")
        if not pattern.startswith("!"):
            matches.append(IgnoreMatch(source, int(line), pattern, path))
    return tuple(matches)


def _query_paths(
    owner: Path, paths: tuple[str, ...], links: tuple[Path, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected = []
    errors = []
    for path in paths:
        boundary = next((link for link in links if (owner / path).is_relative_to(link)), None)
        if boundary is None:
            if any(parent.is_symlink() for parent in (owner / path).parents if parent != owner):
                errors.append(
                    f"trackability inspection incomplete at {owner / path}: symlink ancestor"
                )
            else:
                selected.append(path)
        elif not is_git_worktree_root(boundary):
            errors.append(
                f"trackability inspection incomplete at unavailable submodule {boundary}"
            )
    return tuple(selected), tuple(errors)


def _matches(owner: Path, selected: tuple[str, ...]) -> tuple[IgnoreMatch, ...]:
    if not selected:
        return ()
    payload = b"".join(os.fsencode(path) + b"\0" for path in selected)
    matches = _parse_matches(
        _git(owner, ["check-ignore", "--no-index", "-v", "-z", "--stdin"], payload)
    )
    if any(match.path not in selected for match in matches):
        raise OSError(f"malformed Git check-ignore paths at {owner}")
    return matches


def query_ignores(owner: Path, paths: tuple[str, ...]) -> tuple[IgnoreMatch, ...]:
    """Query NUL-safe effective rules, respecting authenticated gitlink boundaries."""
    selected, errors = _query_paths(owner, paths, _gitlinks(owner))
    if errors:
        raise OSError("; ".join(errors))
    return _matches(owner, selected)


def _owner(directory: Path) -> Path:
    existing = directory
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    result = run_git(existing, "rev-parse", "--show-toplevel")
    if result.returncode or not result.stdout.strip():
        raise OSError(f"trackability inspection incomplete at {directory}: no owning Git checkout")
    return Path(result.stdout.strip()).resolve()


def _walk_inputs(
    directory: Path, selected: Path, owners: set[Path], errors: list[str]
) -> list[Path]:
    """Walk only audited input trees; prune transient directories before recursion."""
    paths = []
    try:
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            relative = path.relative_to(selected).as_posix()
            if entry.name == ".git":
                continue
            if entry.is_dir(follow_symlinks=False):
                if project_transient_pattern(relative + "/probe"):
                    continue
                if not _discover_owner(path, owners, errors):
                    continue
                paths.extend(_walk_inputs(path, selected, owners, errors))
            elif not project_transient_pattern(relative):
                paths.append(path)
    except OSError as exc:
        errors.append(f"trackability inspection incomplete at {directory}: {exc}")
    return paths


def _discover_owner(directory: Path, owners: set[Path], errors: list[str]) -> bool:
    if not (directory / ".git").exists():
        return True
    try:
        owners.add(_owner(directory))
    except OSError as exc:
        errors.append(str(exc))
        return False
    return True


def _managed_attributes(owner: Path, errors: list[str]) -> list[Path]:
    path = owner / ".gitattributes"
    try:
        return [path] if path.is_file() and has_managed_attributes(path.read_bytes()) else []
    except OSError as exc:
        errors.append(f"trackability inspection incomplete at {path}: {exc}")
        return []


def _candidates(selected: Path, owners: set[Path], errors: list[str]) -> list[Path]:
    paths = [selected / name for name in _SEEDS]
    paths.extend(
        selected / name
        for name in _OPTIONAL
        if (selected / name).is_file() or (selected / name).is_symlink()
    )
    for name in _INPUT_TREES:
        directory = selected / name
        if directory.is_symlink():
            paths.append(directory)
        elif directory.is_dir() and _discover_owner(directory, owners, errors):
            paths.extend(_walk_inputs(directory, selected, owners, errors))
    return paths


def _groups(
    owner: Path, role: str, selected: Path, matches: tuple[IgnoreMatch, ...], embedded: bool
) -> list[IgnoredInputs]:
    if not matches:
        return []
    grouped: dict[tuple[str, int, str, str], list[str]] = {}
    info_exclude = Path(
        os.fsdecode(_git(owner, ["rev-parse", "--git-path", "info/exclude"])).removesuffix("\n")
    )
    info_exclude = (owner / info_exclude).resolve()
    for match in matches:
        path = owner / match.path
        family = ""
        if embedded:
            family = next(
                (
                    name
                    for name in ("goals/history", "tickets/history")
                    if path.is_relative_to(selected / name)
                ),
                "goals/history" if match.path == HISTORY_PROBE else "",
            )
        key = (match.source, match.line, match.pattern, family)
        grouped.setdefault(key, []).append(match.path)
    return [
        IgnoredInputs(
            owner,
            role,
            source,
            line,
            pattern,
            tuple(paths),
            bool(family),
            _source_identity(owner, source, info_exclude),
        )
        for (source, line, pattern, family), paths in grouped.items()
    ]


def _source_identity(owner: Path, source: str, info_exclude: Path) -> str:
    path = Path(source)
    if (owner / path).resolve() == info_exclude:
        return ".git/info/exclude"
    if path.is_absolute():
        return (
            path.relative_to(owner).as_posix() if path.is_relative_to(owner) else "global-excludes"
        )
    return path.as_posix()


def _inspect_owner(
    owner: Path, selected: Path, candidates: list[str], errors: list[str]
) -> tuple[IgnoreMatch, ...]:
    links = _gitlinks(owner)
    for link in links:
        if not any(link.is_relative_to(selected / name) for name in _INPUT_TREES):
            continue
        relative = link.relative_to(selected).as_posix()
        if not project_transient_pattern(relative + "/probe") and not is_git_worktree_root(link):
            errors.append(f"trackability inspection incomplete at unavailable submodule {link}")
    paths, query_errors = _query_paths(owner, tuple(candidates), links)
    errors.extend(query_errors)
    return _matches(owner, paths)


def inspect_project_trackability(
    root: Path, booley_dir: Path | None = None
) -> TrackabilityInspection:
    """Inspect durable inputs in their physical owners without changing repository policy."""
    selected = (
        booley_dir if booley_dir is not None else resolve_checkout_booley_dir(root)
    ).resolve()
    root = root.resolve()
    errors: list[str] = []
    ignored: list[IgnoredInputs] = []
    try:
        primary = _owner(selected)
        owners = {primary}
        paths = _candidates(selected, owners, errors)
        rtl_owner = _owner(root)
        owners.add(rtl_owner)
        for owner in tuple(owners):
            paths.extend(_managed_attributes(owner, errors))
        embedded = primary == rtl_owner
        probe = history_probe(root)
        if embedded and probe is not None:
            paths.append(root / probe)
        batches: dict[Path, list[str]] = {owner: [] for owner in sorted(owners)}
        for path in sorted(set(paths)):
            owner = max((o for o in owners if path.is_relative_to(o)), key=lambda o: len(o.parts))
            batches.setdefault(owner, []).append(path.relative_to(owner).as_posix())
        for owner, candidates in batches.items():
            try:
                matches = _inspect_owner(owner, selected, candidates, errors)
                role = "RTL repository" if owner == rtl_owner else "Booley repository"
                if owner not in {primary, rtl_owner}:
                    role += f" ({owner.relative_to(selected).as_posix()})"
                ignored.extend(
                    _groups(owner, role, selected, matches, embedded and owner == primary)
                )
            except OSError as exc:
                errors.append(str(exc))
    except (OSError, RuntimeError) as exc:
        errors.append(f"trackability inspection incomplete at {selected}: {exc}")
    return TrackabilityInspection(tuple(ignored), tuple(dict.fromkeys(errors)))
