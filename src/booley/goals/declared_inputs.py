"""Match Project declarations and retain a versioned, immutable protection capture."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from booley.config.protected_inputs import validate_declaration, validate_input_path
from booley.core.boundary import (
    require_bool_value,
    require_dict,
    require_list,
    require_sha256_digest,
    require_str_value,
)
from booley.goals.checkout import TreeEntry

CAPTURE_SCHEMA = "booley-declared-protected-inputs/1"


@dataclass(frozen=True)
class DeclaredFile:
    """One logical path's content/type/mode in one captured view."""

    path: str
    kind: str
    digest: str
    executable: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "digest": self.digest,
            "executable": self.executable,
        }

    @classmethod
    def from_json(cls, raw: object) -> DeclaredFile:
        row = require_dict(raw, field="declared file")
        if set(row) != {"path", "kind", "digest", "executable"}:
            raise ValueError("declared file has missing or unknown fields")
        path = validate_input_path(require_str_value(row["path"], field="declared file path"))
        if path.endswith("/"):
            raise ValueError("declared file path must be a file")
        kind = require_str_value(row["kind"], field="declared file kind")
        if kind not in {"file", "symlink", "special", "directory", "absent", "submodule"}:
            raise ValueError(f"unknown declared file kind {kind!r}")
        return cls(
            path,
            kind,
            require_sha256_digest(row["digest"], field="declared file digest"),
            require_bool_value(row["executable"], field="declared file executable"),
        )


@dataclass(frozen=True)
class DeclaredCapture:
    """Frozen rules and independent working/HEAD membership and properties."""

    declarations: tuple[str, ...]
    working: tuple[DeclaredFile, ...]
    head: tuple[DeclaredFile, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": CAPTURE_SCHEMA,
            "declarations": list(self.declarations),
            "working": [row.to_json() for row in self.working],
            "head": [row.to_json() for row in self.head],
        }

    def view_json(self, *, head: bool) -> dict[str, Any]:
        return {
            "schema": CAPTURE_SCHEMA,
            "declarations": list(self.declarations),
            "files": [row.to_json() for row in (self.head if head else self.working)],
        }

    @classmethod
    def from_json(cls, raw: object) -> DeclaredCapture:
        value = require_dict(raw, field="declared protected inputs")
        if set(value) != {"schema", "declarations", "working", "head"}:
            raise ValueError("declared protected inputs have missing or unknown fields")
        if value["schema"] != CAPTURE_SCHEMA:
            raise ValueError("declared protected inputs schema is not supported")
        rules = tuple(
            validate_declaration(require_str_value(item, field="declared input declaration"))
            for item in require_list(value["declarations"], field="declared input declarations")
        )
        if not rules or len(set(rules)) != len(rules):
            raise ValueError("declared input declarations must be nonempty and unique")
        views = [_parse_files(value[key], rules) for key in ("working", "head")]
        return cls(rules, *views)


def empty_view_json() -> dict[str, Any]:
    """The digest view of a Project that declares no protected files."""
    return {"schema": CAPTURE_SCHEMA, "declarations": [], "files": []}


def _parse_files(raw: object, rules: Sequence[str]) -> tuple[DeclaredFile, ...]:
    files = tuple(
        DeclaredFile.from_json(item) for item in require_list(raw, field="declared files")
    )
    paths = tuple(row.path for row in files)
    if paths != tuple(sorted(set(paths))) or any(
        not declarations_for(path, rules) for path in paths
    ):
        raise ValueError("declared files must be sorted, unique, and matched by a declaration")
    if any(not any(declaration_matches(rule, path) for path in paths) for rule in rules):
        raise ValueError("every saved declaration must match captured files")
    return files


def declaration_matches(declaration: str, path: str) -> bool:
    """Segment-local wildcards; ** matches zero or more complete path components."""
    pattern = declaration + "**" if declaration.endswith("/") else declaration
    parts, candidate = pattern.split("/"), path.split("/")

    @cache
    def match(i: int, j: int) -> bool:
        if i == len(parts):
            return j == len(candidate)
        if parts[i] == "**":
            return match(i + 1, j) or (j < len(candidate) and match(i, j + 1))
        return (
            j < len(candidate)
            and fnmatch.fnmatchcase(candidate[j], parts[i])
            and match(i + 1, j + 1)
        )

    return match(0, 0)


def declarations_for(path: str, declarations: Sequence[str]) -> tuple[str, ...]:
    return tuple(rule for rule in declarations if declaration_matches(rule, path))


def working_matches(
    root: Path, declarations: Sequence[str], storage: Sequence[Path]
) -> tuple[str, ...]:
    """Enumerate live leaves, including ignored/untracked files, and reject reserved matches."""
    matches: set[str] = set()
    for declaration in declarations:
        start = _literal_prefix(root, declaration)
        _require_storage_allowed(root, start, declaration, storage)
        if (
            not any(char in declaration for char in "*?")
            and not declaration.endswith("/")
            and start.is_dir()
            and not start.is_symlink()
        ):
            raise ValueError(
                f"protected input {declaration!r} is a directory; use {declaration + '/'!r}"
            )
        for path in _leaves(start):
            relative = path.relative_to(root).as_posix()
            if declaration_matches(declaration, relative):
                _require_storage_allowed(root, path, declaration, storage)
                matches.add(relative)
    return tuple(sorted(matches))


def head_matches(
    root: Path, rows: Sequence[TreeEntry], declarations: Sequence[str], storage: Sequence[Path]
) -> tuple[TreeEntry, ...]:
    """Committed membership is independent of the working filesystem."""
    selected: list[TreeEntry] = []
    for row in sorted(rows, key=lambda row: row.path):
        rules = declarations_for(row.path, declarations)
        if rules:
            _require_storage_allowed(root, root / row.path, rules[0], storage)
            selected.append(row)
    return tuple(selected)


def _literal_prefix(root: Path, declaration: str) -> Path:
    prefix: list[str] = []
    for part in declaration.rstrip("/").split("/"):
        if any(char in part for char in "*?"):
            break
        prefix.append(part)
    return root.joinpath(*prefix)


def _leaves(start: Path) -> Iterator[Path]:
    if not os.path.lexists(start):
        return
    if start.is_symlink() or not start.is_dir():
        yield start
        return
    for current, directories, files in os.walk(
        start, followlinks=False, onerror=_raise_walk_error
    ):
        directories[:] = sorted(name for name in directories if name != ".git")
        base = Path(current)
        for name in (*directories, *sorted(name for name in files if name != ".git")):
            path = base / name
            if path.is_symlink() or not path.is_dir():
                yield path


def _raise_walk_error(error: OSError) -> None:
    raise ValueError(f"cannot list declared protected input {error.filename}: {error}") from error


def _require_storage_allowed(
    root: Path, path: Path, declaration: str, storage: Sequence[Path]
) -> None:
    relative = path.relative_to(root)
    if ".git" in relative.parts or any(
        path.resolve().is_relative_to(base.resolve()) for base in storage
    ):
        raise ValueError(
            f"protected input {declaration!r} matches reserved Goal Record or Git storage: {relative}"
        )


def validate_entry_matches(
    root: Path, declarations: Sequence[str], matches: Sequence[str], rows: Sequence[TreeEntry]
) -> None:
    """Require every declaration to match tracked files owned by the design repository."""
    tracked = {row.path: row for row in rows}
    for row in rows:
        rules = declarations_for(row.path, declarations)
        if row.mode == "160000" and rules:
            raise ValueError(f"protected input {rules[0]!r} matches a submodule: {row.path}")
    for declaration in declarations:
        selected = [path for path in matches if declaration_matches(declaration, path)]
        if not selected:
            raise ValueError(f"protected input {declaration!r} matches no files")
        for relative in selected:
            path = root / relative
            _require_outer_repository(root, path, declaration)
            if relative not in tracked or tracked[relative].mode == "160000":
                raise ValueError(
                    f"protected input {declaration!r} matches untracked or ignored file {relative!r}"
                )
            if path.is_symlink():
                _require_link_target(root, path, declaration, tracked)
            elif not path.is_file():
                raise ValueError(
                    f"protected input {declaration!r} is not a regular file: {relative}"
                )


def _require_outer_repository(root: Path, path: Path, declaration: str) -> None:
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_dir() and (parent / ".git").exists():
            raise ValueError(
                f"protected input {declaration!r} matches nested repository or submodule: {path.relative_to(root)}"
            )
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"protected input {declaration!r} escapes the Project Root: {path}")


def _require_link_target(
    root: Path, path: Path, declaration: str, tracked: Mapping[str, TreeEntry]
) -> None:
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"protected input {declaration!r} has an invalid symlink: {path}"
        ) from exc
    _require_outer_repository(root.resolve(), target, declaration)
    relative = target.relative_to(root.resolve()).as_posix()
    if not target.is_file() or relative not in tracked:
        raise ValueError(
            f"protected input {declaration!r} symlink target must be a tracked file: {relative}"
        )


def capture_violations(saved: DeclaredCapture, current: DeclaredCapture | None) -> list[str]:
    """One diagnostic per affected path, with all declarations responsible for it."""
    if current is None or saved.declarations != current.declarations:
        return [
            f"protected input declarations changed: {', '.join(repr(rule) for rule in saved.declarations)}; abandon and re-enter to change them"
        ]
    violations: dict[str, str] = {}
    for head in (False, True):
        before = {row.path: row for row in (saved.head if head else saved.working)}
        after = {row.path: row for row in (current.head if head else current.working)}
        for path in sorted(before.keys() | after.keys()):
            reason = _file_change(before.get(path), after.get(path))
            if reason and path not in violations:
                rules = ", ".join(
                    repr(rule) for rule in declarations_for(path, saved.declarations)
                )
                violations[path] = (
                    f"protected input {rules}: {path} {reason}{' in HEAD' if head else ''}"
                )
    return list(violations.values())


def _file_change(before: DeclaredFile | None, after: DeclaredFile | None) -> str:
    if before is None:
        return "added"
    if after is None:
        return "removed"
    if before.kind != after.kind:
        return "file type changed"
    if before.executable != after.executable:
        return "executable bit changed"
    return "contents or symlink changed" if before.digest != after.digest else ""
