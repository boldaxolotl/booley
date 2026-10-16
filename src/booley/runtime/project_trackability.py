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

    @property
    def subject(self) -> str:
        """Stable waiver identity, independent of HOME and source line shifts."""
        source = Path(self.source)
        if source.is_absolute():
            source_key = (
                source.relative_to(self.owner).as_posix()
                if source.is_relative_to(self.owner)
                else "global-excludes"
            )
        else:
            source_key = source.as_posix()
        return f"{self.role}:{source_key}:{self.pattern}"

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


def query_ignores(owner: Path, paths: tuple[str, ...]) -> tuple[IgnoreMatch, ...]:
    """Query NUL-safe effective rules, respecting authenticated gitlink boundaries."""
    selected = []
    links = _gitlinks(owner)
    for path in paths:
        boundary = next((link for link in links if (owner / path).is_relative_to(link)), None)
        if boundary is None:
            if any(parent.is_symlink() for parent in (owner / path).parents if parent != owner):
                raise OSError(
                    f"trackability inspection incomplete at {owner / path}: symlink ancestor"
                )
            selected.append(path)
        elif not is_git_worktree_root(boundary):
            raise OSError(
                f"trackability inspection incomplete at unavailable submodule {boundary}"
            )
    if not selected:
        return ()
    payload = b"".join(os.fsencode(path) + b"\0" for path in selected)
    matches = _parse_matches(
        _git(owner, ["check-ignore", "--no-index", "-v", "-z", "--stdin"], payload)
    )
    if any(match.path not in selected for match in matches):
        raise OSError(f"malformed Git check-ignore paths at {owner}")
    return matches


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
            if entry.name == ".git" or project_transient_pattern(relative + "/probe"):
                continue
            if entry.is_dir(follow_symlinks=False):
                if (path / ".git").exists():
                    owners.add(_owner(path))
                paths.extend(_walk_inputs(path, selected, owners, errors))
            elif not project_transient_pattern(relative):
                paths.append(path)
    except OSError as exc:
        errors.append(f"trackability inspection incomplete at {directory}: {exc}")
    return paths


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
        elif directory.is_dir():
            if (directory / ".git").exists():
                owners.add(_owner(directory))
            paths.extend(_walk_inputs(directory, selected, owners, errors))
    return paths


def _groups(
    owner: Path, role: str, selected: Path, matches: tuple[IgnoreMatch, ...], embedded: bool
) -> list[IgnoredInputs]:
    grouped: dict[tuple[str, int, str, bool], list[str]] = {}
    for match in matches:
        path = owner / match.path
        history = embedded and (
            match.path == HISTORY_PROBE
            or any(
                path.is_relative_to(selected / family)
                for family in ("goals/history", "tickets/history")
            )
        )
        key = (match.source, match.line, match.pattern, history)
        grouped.setdefault(key, []).append(match.path)
    return [
        IgnoredInputs(owner, role, source, line, pattern, tuple(paths), history)
        for (source, line, pattern, history), paths in grouped.items()
    ]


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
        if embedded:
            paths.append(root / HISTORY_PROBE)
        batches: dict[Path, list[str]] = {}
        for path in sorted(set(paths)):
            owner = max((o for o in owners if path.is_relative_to(o)), key=lambda o: len(o.parts))
            batches.setdefault(owner, []).append(path.relative_to(owner).as_posix())
        for owner, candidates in batches.items():
            try:
                matches = query_ignores(owner, tuple(candidates))
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
    return TrackabilityInspection(tuple(ignored), tuple(errors))
