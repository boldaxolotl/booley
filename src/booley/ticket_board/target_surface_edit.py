"""Byte-preserving insertion of approved Target definitions into ``.core`` files."""

from __future__ import annotations

from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode


class TargetSurfaceEditError(ValueError):
    """A Target definition cannot be composed without broad YAML rewriting."""


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


def merge_target_definition(source: Path, destination: Path, target: str) -> bool:
    """Insert one Target while retaining every unrelated destination byte."""
    source_text = source.read_text(encoding="utf-8")
    block, source_column, source_body = _single_target_source(source_text, source, target)
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _without_sibling_targets(source_text, source, target), encoding="utf-8"
        )
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
    destination.write_text(updated, encoding="utf-8")
    return True
