"""Plan and apply narrow Target deletion to an accepted merge candidate.

The finalizer deliberately edits source spans instead of serializing parsed
YAML/TOML.  Acceptance may remove only a selected ``targets.<name>`` definition
and its unambiguously-owned ``tests.toml`` tables; every other byte remains as
authored.
"""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode

from booley.config.project_config import TEST_LISTS_TABLE, normalize_tests_toml
from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle, UnknownTargetError

from .acceptance_targets import AcceptanceTargetBinding


class TargetFinalizationError(ValueError):
    """A requested Target cannot be removed without broad or ambiguous edits."""


def _bare_target(target: str) -> str:
    return target.rsplit("#", 1)[-1]


@dataclass(frozen=True, order=True)
class PlannedTargetRemoval:
    """One canonical Target definition and optional test registry entry."""

    canonical: str
    name: str
    core_path: str
    tests_key: str = ""


@dataclass(frozen=True, order=True)
class PlannedFilesetRemoval:
    """One newly-authored fileset orphaned by a planned Target removal."""

    core_path: str
    name: str


@dataclass(frozen=True)
class TargetRemovalPlan:
    """A deterministic, basis-validated set of acceptance-time removals."""

    targets: tuple[PlannedTargetRemoval, ...]
    filesets: tuple[PlannedFilesetRemoval, ...] = ()

    @property
    def canonical_targets(self) -> tuple[str, ...]:
        return tuple(item.canonical for item in self.targets)


def _bound_targets(bindings: Iterable[AcceptanceTargetBinding]) -> set[str]:
    return {target for row in bindings for target in (row.baseline, row.candidate)}


def _tests_key(root: Path, handle: TargetHandle, catalog: TargetCatalog) -> str:
    try:
        tests_path = resolve_checkout_project_dir(root) / "tests.toml"
    except FileNotFoundError:
        return ""
    if not tests_path.is_file():
        return ""
    try:
        with tests_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TargetFinalizationError(f"cannot inspect tests.toml: {exc}") from exc
    canonical = handle.identity
    if canonical in raw:
        return canonical
    if handle.name not in raw:
        return ""
    declarations = catalog.declaration_count(handle.name, include_private=True)
    if declarations > 1:
        raise TargetFinalizationError(
            f"ambiguous bare tests.toml section [{handle.name}] is shared by "
            f"{declarations} cores; use a VLNV-qualified table before enqueue"
        )
    return handle.name


def _require_participant_owned_target(
    root: Path,
    handle: TargetHandle,
    canonical: str,
) -> None:
    from booley.ticket_board.ticket_repositories import (
        TicketWorkspaceError,
        ticket_repositories,
    )

    owner = handle.core_file.resolve().parent
    while owner != root and owner.is_relative_to(root):
        if (owner / ".git").exists():
            break
        owner = owner.parent
    try:
        participants = {row.worktree.resolve() for row in ticket_repositories(root)}
    except TicketWorkspaceError as exc:
        raise TargetFinalizationError(str(exc)) from exc
    if owner in participants:
        return
    relative = owner.relative_to(root).as_posix()
    raise TargetFinalizationError(
        f"Acceptance Basis removal Target {canonical!r} is declared in nested "
        f"repository {relative!r}; only outer and paired project participants can be finalized"
    )


def plan_target_removals(
    project_root: Path | str,
    selectors: Iterable[str],
    bindings: Iterable[AcceptanceTargetBinding],
) -> TargetRemovalPlan:
    """Resolve selectors and prove every edit is criterion-bound and unambiguous."""
    root = Path(project_root).resolve()
    allowed = _bound_targets(bindings)
    try:
        catalog = TargetCatalog.build(root)
    except FuseSocError as exc:
        raise TargetFinalizationError(str(exc)) from exc
    removals: list[PlannedTargetRemoval] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            handle = catalog.select(selector)
        except FuseSocError as exc:
            raise TargetFinalizationError(str(exc)) from exc
        canonical = handle.identity
        if canonical not in allowed:
            raise TargetFinalizationError(
                f"Acceptance Basis removal Target {canonical!r} is not bound by this "
                "Ticket's criteria"
            )
        if canonical in seen:
            raise TargetFinalizationError(
                f"Acceptance Basis removal resolves {canonical!r} more than once"
            )
        seen.add(canonical)
        try:
            core_path = handle.core_file.relative_to(root).as_posix()
        except ValueError as exc:
            raise TargetFinalizationError(
                f"Target {canonical!r} is declared outside the project checkout"
            ) from exc
        _require_participant_owned_target(root, handle, canonical)
        removals.append(
            PlannedTargetRemoval(
                canonical,
                handle.name,
                core_path,
                _tests_key(root, handle, catalog),
            )
        )
    plan = TargetRemovalPlan(tuple(sorted(removals)))
    _validate_plan_spans(root, plan)
    return plan


def _mapping_value(node: MappingNode, key: str) -> MappingNode | None:
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node if isinstance(value_node, MappingNode) else None
    return None


def _read_core_mapping(text: str | bytes, path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TargetFinalizationError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise TargetFinalizationError(f".core {path} is not a YAML mapping")
    return value


def _target_fileset_references(targets: Mapping[str, Any]) -> set[str]:
    return {
        name
        for body in targets.values()
        if isinstance(body, Mapping)
        for name in fusesoc_registry.possible_target_fileset_names(body)
    }


def _plan_orphaned_filesets(
    root: Path,
    removals: Iterable[PlannedTargetRemoval],
    baseline_cores: Mapping[str, bytes | None],
) -> list[PlannedFilesetRemoval]:
    if not baseline_cores:
        return []
    by_core: dict[str, set[str]] = defaultdict(set)
    for removal in removals:
        by_core[removal.core_path].add(removal.name)
    planned: list[PlannedFilesetRemoval] = []
    for relative, removed_names in by_core.items():
        core_path = root / relative
        current = _read_core_mapping(core_path.read_text(encoding="utf-8"), core_path)
        if relative not in baseline_cores:
            continue
        baseline_content = baseline_cores[relative]
        baseline = (
            _read_core_mapping(baseline_content, core_path) if baseline_content is not None else {}
        )
        current_filesets = current.get("filesets")
        baseline_filesets = baseline.get("filesets")
        current_names = set(current_filesets) if isinstance(current_filesets, Mapping) else set()
        baseline_names = (
            set(baseline_filesets) if isinstance(baseline_filesets, Mapping) else set()
        )
        targets = current.get("targets")
        if not isinstance(targets, Mapping):
            continue
        removed_targets = {name: body for name, body in targets.items() if name in removed_names}
        retained_targets = {
            name: body for name, body in targets.items() if name not in removed_names
        }
        removed_references = _target_fileset_references(removed_targets)
        retained_references = _target_fileset_references(retained_targets)
        for name in sorted(
            (current_names - baseline_names) & removed_references - retained_references
        ):
            planned.append(PlannedFilesetRemoval(relative, name))
    return planned


def plan_orphaned_fileset_removals(
    project_root: Path | str,
    plan: TargetRemovalPlan,
    baseline_cores: Mapping[str, bytes | None],
) -> TargetRemovalPlan:
    """Add removals for newly-authored filesets orphaned by Target removal."""
    root = Path(project_root).resolve()
    filesets = _plan_orphaned_filesets(root, plan.targets, baseline_cores)
    result = TargetRemovalPlan(plan.targets, tuple(sorted(filesets)))
    _validate_plan_spans(root, result)
    return result


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline + 1


def _node_block_end(text: str, start: int, end: int) -> int:
    """Return the exclusive line boundary without consuming the next YAML key."""
    end_line = _line_start(text, end)
    return end_line if end_line > start else _line_end(text, end)


def _flow_mapping_replacements(
    mapping: MappingNode,
    entries: dict[str, tuple[ScalarNode, object]],
    names: set[str],
) -> list[tuple[int, int, str]]:
    if names == set(entries):
        return [(mapping.start_mark.index, mapping.end_mark.index, "{}")]
    ordered = [
        (key.value, key, value)
        for key, value in mapping.value
        if isinstance(key, ScalarNode) and key.value in entries
    ]
    selected = [index for index, (name, _key, _value) in enumerate(ordered) if name in names]
    groups: list[tuple[int, int]] = []
    for index in selected:
        if groups and index == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], index)
        else:
            groups.append((index, index))
    replacements = []
    for first, last in groups:
        start = ordered[first][1].start_mark.index
        end = ordered[last][2].end_mark.index
        if first:
            start = ordered[first - 1][2].end_mark.index
        elif last + 1 < len(ordered):
            end = ordered[last + 1][1].start_mark.index
        replacements.append((start, end, ""))
    return replacements


def _core_replacements(
    text: str, names: set[str], path: Path, *, section: str = "targets"
) -> list[tuple[int, int, str]]:
    try:
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise TargetFinalizationError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, MappingNode):
        raise TargetFinalizationError(f".core {path} is not a YAML mapping")
    targets = _mapping_value(document, section)
    if targets is None:
        raise TargetFinalizationError(f".core {path} has no mapping-valued {section} block")
    entries = {
        key.value: (key, value) for key, value in targets.value if isinstance(key, ScalarNode)
    }
    missing = sorted(names - entries.keys())
    if missing:
        raise TargetFinalizationError(
            f".core {path} no longer declares {section} entries: {', '.join(missing)}"
        )
    if targets.flow_style:
        return _flow_mapping_replacements(targets, entries, names)
    if names == set(entries):
        first = min(_line_start(text, entries[name][0].start_mark.index) for name in names)
        last = max(
            _node_block_end(
                text,
                _line_start(text, entries[name][0].start_mark.index),
                entries[name][1].end_mark.index,
            )
            for name in names
        )
        indent = " " * min(entries[name][0].start_mark.column for name in names)
        return [(first, last, f"{indent}{{}}\n")]
    return [
        (
            _line_start(text, entries[name][0].start_mark.index),
            _node_block_end(
                text,
                _line_start(text, entries[name][0].start_mark.index),
                entries[name][1].end_mark.index,
            ),
            "",
        )
        for name in sorted(names)
    ]


_TOML_HEADER_RE = re.compile(r"^\s*(\[\[?[^\]\r\n]+\]\]?)\s*(?:#.*)?$")


def _single_toml_path(value: Mapping[str, Any]) -> tuple[str, ...]:
    path: list[str] = []
    current = value
    while current:
        if len(current) != 1:
            raise TargetFinalizationError("tests.toml table header is not uniquely addressable")
        key, child = next(iter(current.items()))
        path.append(key)
        if not isinstance(child, Mapping):
            break
        current = child
    return tuple(path)


def _toml_headers(text: str) -> list[tuple[int, tuple[str, ...]]]:
    headers: list[tuple[int, tuple[str, ...]]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _TOML_HEADER_RE.match(line.rstrip("\r\n"))
        if match:
            try:
                parsed = tomllib.loads(match.group(1) + "\n")
            except tomllib.TOMLDecodeError as exc:
                raise TargetFinalizationError(
                    f"unsupported tests.toml table header: {exc}"
                ) from exc
            headers.append((offset, _single_toml_path(parsed)))
        offset += len(line)
    return headers


def _toml_table_end(text: str, start: int, next_header: int) -> int:
    """Exclude trailing blank/comment lines that may document the next table."""
    end = next_header
    segment = text[start:next_header]
    for line in reversed(segment.splitlines(keepends=True)):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        end -= len(line)
    return end


def _tests_replacements(text: str, keys: set[str]) -> list[tuple[int, int, str]]:
    headers = _toml_headers(text)
    replacements: list[tuple[int, int, str]] = []
    found: set[str] = set()
    for index, (start, path) in enumerate(headers):
        if not path or path[0] not in keys:
            continue
        found.add(path[0])
        next_header = headers[index + 1][0] if index + 1 < len(headers) else len(text)
        end = _toml_table_end(text, start, next_header)
        replacements.append((start, end, ""))
    missing = sorted(keys - found)
    if missing:
        raise TargetFinalizationError(
            "tests.toml no longer contains planned section(s): " + ", ".join(missing)
        )
    return replacements


def _apply_replacements(text: str, replacements: Iterable[tuple[int, int, str]]) -> str:
    result = text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _validate_plan_spans(root: Path, plan: TargetRemovalPlan) -> None:
    by_core: dict[str, set[str]] = defaultdict(set)
    filesets_by_core: dict[str, set[str]] = defaultdict(set)
    tests_keys: set[str] = set()
    for removal in plan.targets:
        by_core[removal.core_path].add(removal.name)
        if removal.tests_key:
            tests_keys.add(removal.tests_key)
    for removal in plan.filesets:
        filesets_by_core[removal.core_path].add(removal.name)
    for relative, names in by_core.items():
        path = root / relative
        text = path.read_text(encoding="utf-8")
        _core_replacements(text, names, path)
        if filesets_by_core[relative]:
            _core_replacements(text, filesets_by_core[relative], path, section="filesets")
    if tests_keys:
        tests_path = resolve_checkout_project_dir(root) / "tests.toml"
        _tests_replacements(tests_path.read_text(encoding="utf-8"), tests_keys)


def _validate_retained_filesets(root: Path, plan: TargetRemovalPlan) -> None:
    for relative in sorted({item.core_path for item in plan.targets}):
        path = root / relative
        try:
            document = fusesoc_registry.read_core(path)
            targets = document.get("targets") or {}
            if not isinstance(targets, Mapping):
                raise TargetFinalizationError(
                    f"finalized .core {path} has no mapping-valued targets block"
                )
            for name, body in targets.items():
                if not isinstance(body, Mapping):
                    raise TargetFinalizationError(
                        f"finalized Target {name!r} in {path} is not a mapping"
                    )
                fusesoc_registry.target_fileset_definitions(document, body)
        except FuseSocError as exc:
            raise TargetFinalizationError(f"finalized .core {path} is invalid: {exc}") from exc


def _validate_finalized(root: Path, plan: TargetRemovalPlan) -> None:
    _validate_retained_filesets(root, plan)
    try:
        catalog = TargetCatalog.build(root)
    except FuseSocError as exc:
        raise TargetFinalizationError(str(exc)) from exc
    for removal in plan.targets:
        try:
            handle = catalog.select(removal.canonical)
        except UnknownTargetError:
            continue
        except FuseSocError as exc:
            raise TargetFinalizationError(str(exc)) from exc
        raise TargetFinalizationError(
            f"Target {removal.canonical!r} remains declared by {handle.core_file}"
        )
    try:
        tests_path = resolve_checkout_project_dir(root) / "tests.toml"
    except FileNotFoundError:
        return
    if not tests_path.is_file():
        return
    try:
        with tests_path.open("rb") as stream:
            raw = tomllib.load(stream)
        normalize_tests_toml(raw)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise TargetFinalizationError(f"finalized tests.toml is invalid: {exc}") from exc
    orphaned = sorted(
        key
        for key in raw
        if key != TEST_LISTS_TABLE
        and catalog.declaration_count(_bare_target(key), include_private=True) == 0
    )
    if orphaned:
        raise TargetFinalizationError(
            "finalized tests.toml has orphan Target section(s): " + ", ".join(orphaned)
        )


def apply_target_removals(project_root: Path | str, plan: TargetRemovalPlan) -> tuple[Path, ...]:
    """Apply a proven plan and return changed paths relative to the checkout."""
    root = Path(project_root).resolve()
    by_core: dict[str, set[str]] = defaultdict(set)
    filesets_by_core: dict[str, set[str]] = defaultdict(set)
    tests_keys: set[str] = set()
    for removal in plan.targets:
        by_core[removal.core_path].add(removal.name)
        if removal.tests_key:
            tests_keys.add(removal.tests_key)
    for removal in plan.filesets:
        filesets_by_core[removal.core_path].add(removal.name)
    changed: set[Path] = set()
    for relative, names in by_core.items():
        path = root / relative
        text = path.read_text(encoding="utf-8")
        replacements = _core_replacements(text, names, path)
        if filesets_by_core[relative]:
            replacements.extend(
                _core_replacements(text, filesets_by_core[relative], path, section="filesets")
            )
        path.write_text(
            _apply_replacements(text, replacements),
            encoding="utf-8",
        )
        changed.add(Path(relative))
    if tests_keys:
        tests_path = resolve_checkout_project_dir(root) / "tests.toml"
        text = tests_path.read_text(encoding="utf-8")
        tests_path.write_text(
            _apply_replacements(text, _tests_replacements(text, tests_keys)),
            encoding="utf-8",
        )
        changed.add(tests_path.relative_to(root))
    _validate_finalized(root, plan)
    return tuple(sorted(changed, key=lambda path: path.as_posix()))
