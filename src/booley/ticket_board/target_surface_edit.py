"""Byte-preserving insertion of approved Target definitions into ``.core`` files."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode

from .persistence import atomic_replace_bytes


class TargetSurfaceEditError(ValueError):
    """A Target definition cannot be composed without broad YAML rewriting."""


_NEW_CORE_KEYS = frozenset({"CAPI=2", "name", "targets"})


def _mapping_value(node: MappingNode, key: str) -> tuple[ScalarNode, MappingNode] | None:
    for key_node, value_node in node.value:
        if (
            isinstance(key_node, ScalarNode)
            and key_node.value == key
            and isinstance(value_node, MappingNode)
        ):
            return key_node, value_node
    return None


def _document(text: str, path: Path) -> tuple[MappingNode, ScalarNode, MappingNode]:
    try:
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise TargetSurfaceEditError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, MappingNode):
        raise TargetSurfaceEditError(f".core {path} is not a YAML mapping")
    targets = _mapping_value(document, "targets")
    if targets is None:
        raise TargetSurfaceEditError(f".core {path} has no mapping-valued targets block")
    return document, *targets


def _entries(node: MappingNode) -> dict[str, tuple[ScalarNode, object]]:
    return {key.value: (key, value) for key, value in node.value if isinstance(key, ScalarNode)}


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline + 1


def _block_end(text: str, start: int, end: int) -> int:
    end_line = _line_start(text, end)
    return end_line if end_line > start else _line_end(text, end)


def _entry_span(text: str, entry: tuple[ScalarNode, object]) -> tuple[int, int]:
    key, value = entry
    start = _line_start(text, key.start_mark.index)
    return start, _block_end(text, start, value.end_mark.index)


def _reindent(block: str, source_column: int, destination_column: int) -> str:
    prefix = " " * source_column
    replacement = " " * destination_column
    lines = block.splitlines(keepends=True)
    if any(line.strip() and not line.startswith(prefix) for line in lines):
        raise TargetSurfaceEditError("Target definition has inconsistent YAML indentation")
    return "".join(replacement + line[len(prefix) :] if line.strip() else line for line in lines)


def _target_body(text: str, path: Path, target: str) -> object:
    try:
        value = yaml.safe_load(text)
        return value["targets"][target]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise TargetSurfaceEditError(
            f".core {path} does not contain a readable Target {target!r}"
        ) from exc


def validate_new_core_surface(text: str, path: Path) -> None:
    """Require a new ``.core`` file to contain only identity plus Targets."""
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TargetSurfaceEditError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TargetSurfaceEditError(f".core {path} is not a mapping")
    extra = sorted(set(value) - _NEW_CORE_KEYS)
    if extra:
        raise TargetSurfaceEditError(
            f"new .core {path} contains content outside Target definitions: " + ", ".join(extra)
        )


def toml_table_block(content: str, key: str) -> str:
    """Return one complete top-level TOML table without consuming its successor."""
    headers = [
        (match, _toml_header_path(match.group(1)))
        for match in re.finditer(r"(?m)^[ \t]*(\[\[?[^\]\r\n]+\]\]?)[ \t]*(?:#.*)?$", content)
    ]
    start = next((match.start() for match, path in headers if path == (key,)), None)
    if start is None:
        raise TargetSurfaceEditError(f"tests.toml table {key!r} disappeared")
    blocks = []
    for index, (header, path) in enumerate(headers):
        if header.start() < start or not path or path[0] != key:
            continue
        end = headers[index + 1][0].start() if index + 1 < len(headers) else len(content)
        blocks.append(content[header.start() : end].strip())
    return "\n\n".join(blocks) + "\n"


def toml_table_spans(content: str, keys: Iterable[str]) -> tuple[tuple[int, int], ...]:
    """Locate complete top-level tables, including an adjacent source prefix."""
    selected = set(keys)
    headers = [
        (match, _toml_header_path(match.group(1)))
        for match in re.finditer(r"(?m)^[ \t]*(\[\[?[^\]\r\n]+\]\]?)[ \t]*(?:#.*)?$", content)
    ]
    selected_paths = {(key,) for key in selected}
    spans: list[tuple[int, int]] = []
    for index, (header, path) in enumerate(headers):
        if path not in selected_paths:
            continue
        end = next(
            (
                candidate.start()
                for candidate, candidate_path in headers[index + 1 :]
                if not candidate_path or candidate_path[0] != path[0]
            ),
            len(content),
        )
        spans.append((_adjacent_prefix_start(content, header.start()), end))
    return tuple(spans)


def _adjacent_prefix_start(content: str, start: int) -> int:
    cursor = start
    while cursor:
        previous_start = content.rfind("\n", 0, max(0, cursor - 1)) + 1
        line = content[previous_start:cursor]
        if line.strip() and not line.lstrip().startswith("#"):
            break
        cursor = previous_start
    return cursor


def only_authorized_insertions(
    baseline: str, current: str, spans: Iterable[tuple[int, int]]
) -> bool:
    """Return whether current differs only by insertions inside approved spans."""
    allowed = tuple(spans)
    opcodes = SequenceMatcher(None, baseline, current, autojunk=False).get_opcodes()
    for operation, _old_start, _old_end, new_start, new_end in opcodes:
        if operation == "equal":
            continue
        if operation != "insert":
            return False
        if not any(start <= new_start and new_end <= end for start, end in allowed):
            return False
    return True


def _toml_header_path(header: str) -> tuple[str, ...]:
    try:
        parsed = tomllib.loads(header + "\n")
    except tomllib.TOMLDecodeError as exc:
        raise TargetSurfaceEditError(f"cannot parse tests.toml header {header!r}") from exc
    path = []
    current: object = parsed
    while isinstance(current, Mapping) and current:
        if len(current) != 1:
            raise TargetSurfaceEditError(f"tests.toml header {header!r} is ambiguous")
        item, current = next(iter(current.items()))
        path.append(str(item))
    return tuple(path)


def _write_text(path: Path, content: str) -> None:
    atomic_replace_bytes(path, content.encode(), mode=0o644)


def _single_target_source(text: str, path: Path, target: str) -> tuple[str, int, object]:
    _document_node, _targets_key, targets = _document(text, path)
    if targets.flow_style:
        raise TargetSurfaceEditError(
            f".core {path} uses an inline targets mapping that cannot be edited narrowly"
        )
    entries = _entries(targets)
    if target not in entries:
        raise TargetSurfaceEditError(f".core {path} does not declare Target {target!r}")
    start, end = _entry_span(text, entries[target])
    return text[start:end], entries[target][0].start_mark.column, _target_body(text, path, target)


def _without_sibling_targets(text: str, path: Path, keep: str) -> str:
    _document_node, _targets_key, targets = _document(text, path)
    entries = _entries(targets)
    result = text
    spans = [_entry_span(text, entries[name]) for name in set(entries) - {keep}]
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + result[end:]
    return result


def target_definition_spans(
    text: str, path: Path, targets: Iterable[str]
) -> tuple[tuple[int, int], ...]:
    """Locate named Target definition sources in one core file."""
    _document_node, _targets_key, target_mapping = _document(text, path)
    entries = _entries(target_mapping)
    selected = set(targets)
    missing = sorted(selected - set(entries))
    if missing:
        raise TargetSurfaceEditError(
            f".core {path} does not declare Target(s): {', '.join(missing)}"
        )
    return tuple(
        (_adjacent_prefix_start(text, start), end)
        for start, end in (_entry_span(text, entries[name]) for name in sorted(selected))
    )


def merge_target_definition(source: Path, destination: Path, target: str) -> bool:
    """Insert one Target while retaining every unrelated destination byte."""
    source_text = source.read_text(encoding="utf-8")
    block, source_column, source_body = _single_target_source(source_text, source, target)
    if not destination.is_file():
        validate_new_core_surface(source_text, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_text(destination, _without_sibling_targets(source_text, source, target))
        return True

    destination_text = destination.read_text(encoding="utf-8")
    _document_node, targets_key, targets = _document(destination_text, destination)
    entries = _entries(targets)
    if entries and targets.flow_style:
        raise TargetSurfaceEditError(
            f".core {destination} uses an inline targets mapping that cannot be edited narrowly"
        )
    if target in entries:
        if _target_body(destination_text, destination, target) != source_body:
            raise TargetSurfaceEditError(
                f"destination Target {target!r} differs from the approved definition"
            )
        return False

    if entries:
        last = max(entries.values(), key=lambda entry: entry[1].end_mark.index)
        _start, insertion = _entry_span(destination_text, last)
        destination_column = last[0].start_mark.column
        rendered = _reindent(block, source_column, destination_column)
        if insertion and not destination_text[:insertion].endswith("\n"):
            rendered = "\n" + rendered
        updated = destination_text[:insertion] + rendered + destination_text[insertion:]
    else:
        destination_column = targets_key.start_mark.column + 2
        rendered = _reindent(block, source_column, destination_column)
        start = targets.start_mark.index
        end = targets.end_mark.index
        updated = destination_text[:start] + "\n" + rendered + destination_text[end:]
    _write_text(destination, updated)
    return True
