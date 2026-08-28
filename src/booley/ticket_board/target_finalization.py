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

from .target_contract import (
    ContractTargetBinding,
    canonical_contract_bindings,
    criterion_targets,
)


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


@dataclass(frozen=True)
class TargetRemovalPlan:
    """A deterministic, seal-validated set of acceptance-time removals."""

    targets: tuple[PlannedTargetRemoval, ...]

    @property
    def canonical_targets(self) -> tuple[str, ...]:
        return tuple(item.canonical for item in self.targets)


def _bound_targets(bindings: Iterable[ContractTargetBinding]) -> set[str]:
    return {target for row in bindings for target in (row.baseline, row.candidate)}


def _tests_key(root: Path, ref: fusesoc_registry.TargetRef) -> str:
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
    canonical = f"{ref.vlnv}#{ref.name}"
    if canonical in raw:
        return canonical
    matching = [
        key
        for key in raw
        if key != TEST_LISTS_TABLE and _bare_target(key) == ref.name
    ]
    if not matching:
        return ""
    if len(matching) > 1:
        raise TargetFinalizationError(
            f"Target {canonical!r} matches multiple tests.toml sections: "
            + ", ".join(repr(key) for key in sorted(matching))
        )
    declarations = fusesoc_registry.target_declarations(root).get(ref.name, [])
    if matching[0] == ref.name and len(declarations) > 1:
        raise TargetFinalizationError(
            f"ambiguous bare tests.toml section [{ref.name}] is shared by "
            f"{len(declarations)} cores; use a VLNV-qualified table before sealing"
        )
    return matching[0]


def plan_target_removals(
    project_root: Path | str,
    selectors: Iterable[str],
    bindings: Iterable[ContractTargetBinding],
) -> TargetRemovalPlan:
    """Resolve selectors and prove every edit is criterion-bound and unambiguous."""
    root = Path(project_root).resolve()
    allowed = _bound_targets(bindings)
    removals: list[PlannedTargetRemoval] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            ref = fusesoc_registry.resolve_ref(root, selector)
        except fusesoc_registry.FuseSocError as exc:
            raise TargetFinalizationError(str(exc)) from exc
        canonical = f"{ref.vlnv}#{ref.name}"
        if canonical not in allowed:
            raise TargetFinalizationError(
                f"on_success.remove_targets target {canonical!r} is not bound by this "
                "Ticket's criteria"
            )
        if canonical in seen:
            raise TargetFinalizationError(
                f"on_success.remove_targets resolves {canonical!r} more than once"
            )
        seen.add(canonical)
        try:
            core_path = ref.core_file.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise TargetFinalizationError(
                f"Target {canonical!r} is declared outside the project checkout"
            ) from exc
        removals.append(
            PlannedTargetRemoval(canonical, ref.name, core_path, _tests_key(root, ref))
        )
    plan = TargetRemovalPlan(tuple(sorted(removals)))
    _validate_plan_spans(root, plan)
    return plan


def canonical_remove_targets(
    fields: Mapping[str, Any], project_root: Path | str
) -> tuple[str, ...]:
    """Return the full-VLNV removal identities declared by ticket fields."""
    on_success = fields.get("on_success")
    if not isinstance(on_success, Mapping):
        return ()
    selectors = on_success.get("remove_targets", [])
    if not isinstance(selectors, list) or not selectors:
        return ()
    bindings = canonical_contract_bindings(
        project_root, criterion_targets(fields.get("criteria"))
    )
    return plan_target_removals(project_root, selectors, bindings).canonical_targets


def validate_remove_targets_for_seal(
    fields: Mapping[str, Any], project_root: Path | str
) -> list[str]:
    """Return seal-time diagnostics for acceptance-time Target removal."""
    try:
        canonical_remove_targets(fields, project_root)
    except (TargetFinalizationError, fusesoc_registry.FuseSocError) as exc:
        return [str(exc)]
    return []


def _mapping_value(node: MappingNode, key: str) -> MappingNode | None:
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node if isinstance(value_node, MappingNode) else None
    return None


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline + 1


def _node_block_end(text: str, start: int, end: int) -> int:
    """Return the exclusive line boundary without consuming the next YAML key."""
    end_line = _line_start(text, end)
    return end_line if end_line > start else _line_end(text, end)


def _core_replacements(text: str, names: set[str], path: Path) -> list[tuple[int, int, str]]:
    try:
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise TargetFinalizationError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, MappingNode):
        raise TargetFinalizationError(f".core {path} is not a YAML mapping")
    targets = _mapping_value(document, "targets")
    if targets is None:
        raise TargetFinalizationError(f".core {path} has no mapping-valued targets block")
    entries = {
        key.value: (key, value)
        for key, value in targets.value
        if isinstance(key, ScalarNode)
    }
    missing = sorted(names - entries.keys())
    if missing:
        raise TargetFinalizationError(
            f".core {path} no longer declares Target(s): {', '.join(missing)}"
        )
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


_TOML_HEADER_RE = re.compile(r"^\s*\[(?!\[)(.+)\]\s*(?:#.*)?$")


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
                parsed = tomllib.loads(f"[{match.group(1)}]\n")
            except tomllib.TOMLDecodeError as exc:
                raise TargetFinalizationError(f"unsupported tests.toml table header: {exc}") from exc
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
    tests_keys: set[str] = set()
    for removal in plan.targets:
        by_core[removal.core_path].add(removal.name)
        if removal.tests_key:
            tests_keys.add(removal.tests_key)
    for relative, names in by_core.items():
        path = root / relative
        _core_replacements(path.read_text(encoding="utf-8"), names, path)
    if tests_keys:
        tests_path = resolve_checkout_project_dir(root) / "tests.toml"
        _tests_replacements(tests_path.read_text(encoding="utf-8"), tests_keys)


def _validate_finalized(root: Path, plan: TargetRemovalPlan) -> None:
    for removal in plan.targets:
        try:
            ref = fusesoc_registry.resolve_ref(root, removal.canonical)
        except fusesoc_registry.UnknownTargetError:
            continue
        except fusesoc_registry.FuseSocError as exc:
            raise TargetFinalizationError(str(exc)) from exc
        raise TargetFinalizationError(
            f"Target {removal.canonical!r} remains declared by {ref.core_file}"
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
    declarations = fusesoc_registry.target_declarations(root)
    orphaned = sorted(
        key
        for key in raw
        if key != TEST_LISTS_TABLE
        and _bare_target(key) not in declarations
    )
    if orphaned:
        raise TargetFinalizationError(
            "finalized tests.toml has orphan Target section(s): " + ", ".join(orphaned)
        )


def apply_target_removals(
    project_root: Path | str, plan: TargetRemovalPlan
) -> tuple[Path, ...]:
    """Apply a proven plan and return changed paths relative to the checkout."""
    root = Path(project_root).resolve()
    by_core: dict[str, set[str]] = defaultdict(set)
    tests_keys: set[str] = set()
    for removal in plan.targets:
        by_core[removal.core_path].add(removal.name)
        if removal.tests_key:
            tests_keys.add(removal.tests_key)
    changed: set[Path] = set()
    for relative, names in by_core.items():
        path = root / relative
        text = path.read_text(encoding="utf-8")
        path.write_text(
            _apply_replacements(text, _core_replacements(text, names, path)),
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
