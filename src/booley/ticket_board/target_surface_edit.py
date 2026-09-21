"""Byte-preserving insertion of approved Target definitions into ``.core`` files."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from booley.fusesoc import fusesoc_registry
from booley.targets.domain import FuseSocError

from .persistence import atomic_replace_bytes


class TargetSurfaceEditError(ValueError):
    """A Target definition cannot be composed without broad YAML rewriting."""


_NEW_CORE_KEYS = frozenset({"CAPI=2", "name", "filesets", "parameters", "targets"})
_TOML_HEADER_RE = re.compile(r"(?m)^[ \t]*(\[\[?[^\]\r\n]+\]\]?)[ \t]*(?:#.*)?\r?$")


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


def _entries(node: MappingNode) -> dict[str, tuple[ScalarNode, Node]]:
    return {key.value: (key, value) for key, value in node.value if isinstance(key, ScalarNode)}


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline + 1


def _block_scalar_content_end(text: str, node: ScalarNode) -> int:
    cursor = node.end_mark.index
    while cursor > node.start_mark.index:
        line_start = _line_start(text, cursor - 1)
        line_end = _line_end(text, line_start)
        if text[line_start:line_end].strip():
            return (
                line_end - 1 if line_end > line_start and text[line_end - 1] == "\n" else line_end
            )
        cursor = line_start
    return node.start_mark.index


def _node_content_end(text: str, node: Node) -> int:
    if isinstance(node, ScalarNode) and node.style in {"|", ">"}:
        return _block_scalar_content_end(text, node)

    last_child: Node | None = None
    if isinstance(node, MappingNode) and node.value:
        last_child = node.value[-1][1]
    elif isinstance(node, SequenceNode) and node.value:
        last_child = node.value[-1]
    if last_child is not None:
        return _node_content_end(text, last_child)
    return node.end_mark.index


def _block_end(text: str, node: Node) -> int:
    content_end = _node_content_end(text, node)
    return _line_end(text, content_end)


def _entry_span(text: str, entry: tuple[ScalarNode, Node]) -> tuple[int, int]:
    key, value = entry
    start = _line_start(text, key.start_mark.index)
    return start, _block_end(text, value)


def _reindent(block: str, source_column: int, destination_column: int) -> str:
    prefix = " " * source_column
    replacement = " " * destination_column
    lines = block.splitlines(keepends=True)
    if any(line.strip() and not line.startswith(prefix) for line in lines):
        raise TargetSurfaceEditError("Target definition has inconsistent YAML indentation")
    return "".join(replacement + line[len(prefix) :] if line.strip() else line for line in lines)


def _mapping_body(text: str, path: Path, section: str, name: str) -> object:
    try:
        value = yaml.safe_load(text)
        return value[section][name]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise TargetSurfaceEditError(
            f".core {path} does not contain a readable {section}.{name} definition"
        ) from exc


def validate_new_core_surface(text: str, path: Path) -> None:
    """Require a new ``.core`` file to contain only identity and planned inputs."""
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
        (match, _toml_header_path(match.group(1))) for match in _TOML_HEADER_RE.finditer(content)
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
        (match, _toml_header_path(match.group(1))) for match in _TOML_HEADER_RE.finditer(content)
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
    without_authorized = current
    for start, end in sorted(allowed, reverse=True):
        without_authorized = without_authorized[:start] + without_authorized[end:]
    if without_authorized == baseline:
        return True
    opcodes = SequenceMatcher(None, baseline, current, autojunk=False).get_opcodes()
    for operation, _old_start, _old_end, new_start, new_end in opcodes:
        if operation == "equal":
            continue
        if operation != "insert":
            return False
        inside = any(start <= new_start and new_end <= end for start, end in allowed)
        prefixed = any(
            new_start < start <= new_end <= end and not current[new_start:start].strip()
            for start, end in allowed
        )
        if not inside and not prefixed:
            return False
    return True


def _mapping_addition_replacements(
    baseline: str,
    current: str,
    path: Path,
    section: str,
    names: Iterable[str],
) -> list[tuple[int, int, str]]:
    selected = set(names)
    if not selected:
        return []
    baseline_document, _baseline_key, _baseline_targets = _document(baseline, path)
    current_document, _current_key, _current_targets = _document(current, path)
    current_section = _mapping_value(current_document, section)
    if current_section is None:
        raise TargetSurfaceEditError(f".core {path} has no mapping-valued {section} block")
    current_key, current_mapping = current_section
    current_entries = _entries(current_mapping)
    missing = sorted(selected - set(current_entries))
    if missing:
        raise TargetSurfaceEditError(
            f".core {path} does not declare {section} entries: {', '.join(missing)}"
        )
    baseline_section = _mapping_value(baseline_document, section)
    current_span = _entry_span(current, (current_key, current_mapping))
    if baseline_section is None:
        return [(*current_span, "")]
    baseline_key, baseline_mapping = baseline_section
    baseline_entries = _entries(baseline_mapping)
    if selected & set(baseline_entries):
        raise TargetSurfaceEditError(f".core {path} did not add unique {section} entries")
    if not baseline_entries:
        baseline_start, baseline_end = _entry_span(baseline, (baseline_key, baseline_mapping))
        return [(*current_span, baseline[baseline_start:baseline_end])]
    if current_mapping.flow_style:
        raise TargetSurfaceEditError(
            f".core {path} uses an inline {section} mapping that cannot be edited narrowly"
        )
    return [
        (_adjacent_prefix_start(current, start), end, "")
        for start, end in (_entry_span(current, current_entries[name]) for name in selected)
    ]


def only_authorized_core_additions(
    baseline: str,
    current: str,
    path: Path,
    additions: Mapping[str, Iterable[str]],
) -> bool:
    """Return whether a core differs only by named mapping-entry additions."""
    replacements = [
        replacement
        for section, names in additions.items()
        for replacement in _mapping_addition_replacements(baseline, current, path, section, names)
    ]
    without_authorized = current
    for start, end, replacement in sorted(replacements, reverse=True):
        without_authorized = without_authorized[:start] + replacement + without_authorized[end:]
    return without_authorized == baseline


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


def _single_mapping_source(
    text: str, path: Path, section: str, name: str
) -> tuple[str, int, object]:
    document, _targets_key, _targets = _document(text, path)
    mapping_value = _mapping_value(document, section)
    if mapping_value is None:
        raise TargetSurfaceEditError(f".core {path} has no mapping-valued {section} block")
    _section_key, mapping = mapping_value
    if mapping.flow_style:
        raise TargetSurfaceEditError(
            f".core {path} uses an inline {section} mapping that cannot be edited narrowly"
        )
    entries = _entries(mapping)
    if name not in entries:
        raise TargetSurfaceEditError(f".core {path} does not declare {section}.{name}")
    start, end = _entry_span(text, entries[name])
    return (
        text[start:end],
        entries[name][0].start_mark.column,
        _mapping_body(text, path, section, name),
    )


def _without_mapping_siblings(text: str, path: Path, section: str, keep: set[str]) -> str:
    document, _targets_key, _targets = _document(text, path)
    mapping_value = _mapping_value(document, section)
    if mapping_value is None:
        return text
    _section_key, mapping = mapping_value
    entries = _entries(mapping)
    result = text
    removed = set(entries) - keep
    spans = [_entry_span(text, entries[name]) for name in removed]
    if entries and removed == set(entries):
        start = mapping.start_mark.index
        end = mapping.end_mark.index
        replacement = "{}" if mapping.flow_style else "{}\n"
        return text[:start] + replacement + text[end:]
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + result[end:]
    return result


def _without_sibling_target_surface(
    text: str,
    path: Path,
    target: str,
    filesets: set[str],
    parameters: set[str],
) -> str:
    result = _without_mapping_siblings(text, path, "targets", {target})
    result = _without_mapping_siblings(result, path, "filesets", filesets)
    return _without_mapping_siblings(result, path, "parameters", parameters)


def _defined_target_filesets(text: str, path: Path, target_body: object) -> tuple[str, ...]:
    if not isinstance(target_body, Mapping):
        raise TargetSurfaceEditError(f".core {path} Target definition is not a mapping")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TargetSurfaceEditError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise TargetSurfaceEditError(f".core {path} is not a YAML mapping")
    try:
        return tuple(fusesoc_registry.target_fileset_definitions(document, target_body))
    except FuseSocError as exc:
        raise TargetSurfaceEditError(f".core {path}: {exc}") from exc


def _defined_target_parameters(text: str, path: Path, target_body: object) -> tuple[str, ...]:
    if not isinstance(target_body, Mapping):
        raise TargetSurfaceEditError(f".core {path} Target definition is not a mapping")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TargetSurfaceEditError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise TargetSurfaceEditError(f".core {path} is not a YAML mapping")
    try:
        return tuple(fusesoc_registry.target_parameter_definitions(document, target_body))
    except FuseSocError as exc:
        raise TargetSurfaceEditError(f".core {path}: {exc}") from exc


def _insert_absent_mapping(
    destination_text: str,
    destination_path: Path,
    section: str,
    block: str,
    source_column: int,
) -> tuple[str, bool]:
    _document_node, targets_key, _target_mapping = _document(destination_text, destination_path)
    insertion = _line_start(destination_text, targets_key.start_mark.index)
    rendered = _reindent(block, source_column, targets_key.start_mark.column + 2)
    prefix = " " * targets_key.start_mark.column + f"{section}:\n"
    return destination_text[:insertion] + prefix + rendered + destination_text[insertion:], True


def _insert_mapping_definition(
    source_text: str,
    source_path: Path,
    destination_text: str,
    destination_path: Path,
    section: str,
    name: str,
) -> tuple[str, bool]:
    block, source_column, source_body = _single_mapping_source(
        source_text, source_path, section, name
    )
    document, _targets_key, _targets = _document(destination_text, destination_path)
    mapping_value = _mapping_value(document, section)
    if mapping_value is None:
        if section not in {"filesets", "parameters"}:
            raise TargetSurfaceEditError(
                f".core {destination_path} has no mapping-valued {section} block"
            )
        return _insert_absent_mapping(
            destination_text, destination_path, section, block, source_column
        )
    section_key, mapping = mapping_value
    entries = _entries(mapping)
    if entries and mapping.flow_style:
        raise TargetSurfaceEditError(
            f".core {destination_path} uses an inline {section} mapping that cannot be edited narrowly"
        )
    if name in entries:
        if _mapping_body(destination_text, destination_path, section, name) != source_body:
            raise TargetSurfaceEditError(
                f"destination {section}.{name} differs from the approved definition"
            )
        return destination_text, False
    if entries:
        last = max(entries.values(), key=lambda entry: entry[1].end_mark.index)
        _start, insertion = _entry_span(destination_text, last)
        destination_column = last[0].start_mark.column
        rendered = _reindent(block, source_column, destination_column)
        return destination_text[:insertion] + rendered + destination_text[insertion:], True
    destination_column = section_key.start_mark.column + 2
    rendered = _reindent(block, source_column, destination_column)
    start = mapping.start_mark.index
    end = mapping.end_mark.index
    return destination_text[:start] + "\n" + rendered + destination_text[end:], True


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


def fileset_definition_spans(
    text: str, path: Path, filesets: Iterable[str]
) -> tuple[tuple[int, int], ...]:
    """Locate named fileset definition sources in one core file."""
    document, _targets_key, _targets = _document(text, path)
    fileset_mapping = _mapping_value(document, "filesets")
    selected = set(filesets)
    if fileset_mapping is None:
        if selected:
            raise TargetSurfaceEditError(f".core {path} has no mapping-valued filesets block")
        return ()
    _filesets_key, mapping = fileset_mapping
    entries = _entries(mapping)
    missing = sorted(selected - set(entries))
    if missing:
        raise TargetSurfaceEditError(
            f".core {path} does not declare fileset(s): {', '.join(missing)}"
        )
    return tuple(
        (_adjacent_prefix_start(text, start), end)
        for start, end in (_entry_span(text, entries[name]) for name in sorted(selected))
    )


def merge_target_definition(source: Path, destination: Path, target: str) -> bool:
    """Insert one Target and its referenced local inputs, retaining unrelated bytes."""
    source_text = source.read_text(encoding="utf-8")
    _block, _column, source_body = _single_mapping_source(source_text, source, "targets", target)
    selected_filesets = _defined_target_filesets(source_text, source, source_body)
    selected_parameters = _defined_target_parameters(source_text, source, source_body)
    if not destination.is_file():
        validate_new_core_surface(source_text, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_text(
            destination,
            _without_sibling_target_surface(
                source_text,
                source,
                target,
                set(selected_filesets),
                set(selected_parameters),
            ),
        )
        return True

    destination_text = destination.read_text(encoding="utf-8")
    changed = False
    for fileset in selected_filesets:
        destination_text, inserted = _insert_mapping_definition(
            source_text,
            source,
            destination_text,
            destination,
            "filesets",
            fileset,
        )
        changed = changed or inserted
    for parameter in selected_parameters:
        destination_text, inserted = _insert_mapping_definition(
            source_text,
            source,
            destination_text,
            destination,
            "parameters",
            parameter,
        )
        changed = changed or inserted
    destination_text, inserted = _insert_mapping_definition(
        source_text, source, destination_text, destination, "targets", target
    )
    changed = changed or inserted
    if changed:
        _write_text(destination, destination_text)
    return changed
