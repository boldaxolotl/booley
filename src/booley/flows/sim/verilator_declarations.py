"""Decode the pinned producing Verilator build's private early declaration dump.

Locations are logical compiler locations. Without a physical source map, possible
`line directives (including token construction) make the entire inventory incomplete.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from booley.core.boundary import require_dict, require_list, require_str
from booley.runtime.regular_file import open_regular_nofollow

from .coverage_provenance import content_digest
from .verilator_identity import PINNED_VERILATOR

DECLARATION_CONTRACT = "booley.verilator-declarations/v1"
DECLARATION_OPTIONS = ("--dumpi-tree-json", "1", "--dumpi-V3Global", "9")
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_DECLARATIONS = 100_000
MAX_JSON_DEPTH = 128
BUILTIN_LOCATIONS = frozenset(
    {"<built-in>", "<command-line>", "<verilated_std>", "<verilated_std_waiver>"}
)
_HDL = frozenset({"verilogSource", "systemVerilogSource"})
_LOCATION = re.compile(r"^([^,]+),([0-9]+):([0-9]+),[0-9]+:[0-9]+$")


@dataclass(frozen=True)
class DeclarationSource:
    path: str
    aliases: tuple[str, ...]
    sha256: str
    file_type: str
    testbench: bool
    include: bool

    @property
    def eligible(self) -> bool:
        return self.file_type in _HDL and not self.testbench and not self.include


@dataclass(frozen=True, order=True)
class Declaration:
    kind: str
    name: str
    source: str
    compiler_location: str
    line: int
    column: int


@dataclass(frozen=True)
class DeclarationDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class DeclarationInventory:
    compiler: tuple[str, str]
    build_identity: str
    sources: tuple[DeclarationSource, ...] = ()
    declarations: tuple[Declaration, ...] = ()
    diagnostics: tuple[DeclarationDiagnostic, ...] = ()
    raw_evidence: tuple[tuple[str, bytes], ...] = ()

    @property
    def status(self) -> str:
        return "incomplete" if self.diagnostics else "complete"

    def document(self) -> dict[str, object]:
        return {
            "$schema": DECLARATION_CONTRACT,
            "status": self.status,
            "compiler": {"tag": self.compiler[0], "commit": self.compiler[1]},
            "build_identity": self.build_identity,
            "sources": [
                {
                    "path": s.path,
                    "aliases": list(s.aliases),
                    "sha256": s.sha256,
                    "file_type": s.file_type,
                    "testbench": s.testbench,
                    "include": s.include,
                }
                for s in self.sources
            ],
            "declarations": [
                {
                    "kind": d.kind,
                    "name": d.name,
                    "source": d.source,
                    "compiler_location": d.compiler_location,
                    "line": d.line,
                    "column": d.column,
                }
                for d in self.declarations
            ],
            "diagnostics": [{"code": d.code, "message": d.message} for d in self.diagnostics],
            "raw_evidence": [
                {"path": "build-evidence/" + name, "sha256": content_digest(data)}
                for name, data in self.raw_evidence
            ],
        }


def declaration_source_aliases(
    sources: tuple[DeclarationSource, ...],
) -> Mapping[str, DeclarationSource | None]:
    """Build one immutable alias index, retaining collisions as unresolved."""
    aliases = {}
    for source in sources:
        for alias in source.aliases:
            if alias in aliases and aliases[alias] != source:
                aliases[alias] = None
            else:
                aliases[alias] = source
    return MappingProxyType(aliases)


def resolve_declaration_source(
    sources: tuple[DeclarationSource, ...], location: str
) -> DeclarationSource | None:
    """Resolve only established aliases, never suffixes or filename adjacency."""
    return declaration_source_aliases(sources).get(location.replace("\\", "/"))


def _read_evidence_bytes(path: Path) -> bytes:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("unsafe declaration evidence")
    with os.fdopen(open_regular_nofollow(path), "rb") as stream:
        data = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError("declaration evidence exceeds the byte limit")
    return data


def possible_logical_locations(data: bytes) -> bool:
    """A conservative safety trigger, not RTL interpretation or attribution.

    The pinned lexer accepts literal `line; macros can construct it by joining
    tokens or continued macro text. Join continued lines before checking for
    directives, and reject continuations that construct the identifier ``line``.
    Ordinary multiline macros remain usable. Token joins and directive-like
    text in comments/strings or disabled branches remain conservative triggers.
    """
    continuation = rb"\\\r?\n"
    joined = re.sub(continuation, b"", data)
    if re.search(rb"`\s*line\b|``", joined):
        return True
    continued_identifiers = re.finditer(
        rb"[a-zA-Z_$][a-zA-Z0-9_$]*(?:\\\r?\n[a-zA-Z0-9_$]+)+", data
    )
    return any(re.sub(continuation, b"", match[0]) == b"line" for match in continued_identifiers)


def _bounded_json(data: bytes) -> dict:
    if len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError("declaration evidence exceeds the byte limit")
    # Bound nesting before json.loads, without walking arbitrary AST children.
    depth, quoted, escaped = 0, False, False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("declaration evidence exceeds the nesting limit")
        elif byte in (93, 125):
            depth -= 1
    return require_dict(json.loads(data), field="declaration evidence")


def _decode_records(tree: dict, metadata: dict, sources: tuple[DeclarationSource, ...]):
    if tree.get("type") != "NETLIST":
        raise ValueError("unsupported early-tree root")
    files = require_dict(metadata.get("files"), field="files")
    for file in files.values():
        item = require_dict(file, field="compiler file")
        require_str(item, "filename")
        require_str(item, "realpath")
    modules = require_list(tree.get("modulesp"), field="modulesp")
    if len(modules) > MAX_DECLARATIONS or len(files) > MAX_DECLARATIONS:
        raise ValueError("declaration evidence exceeds the record limit")
    declarations = []
    aliases = declaration_source_aliases(sources)
    for value in modules:
        record = require_dict(value, field="module")
        kind = require_str(record, "type")
        if kind not in {"MODULE", "IFACE", "PACKAGE"}:
            raise ValueError("unsupported declaration kind")
        name, location = require_str(record, "name"), require_str(record, "loc")
        match = _LOCATION.fullmatch(location)
        if match is None:
            raise ValueError("malformed declaration location")
        file = require_dict(files.get(match[1]), field="declaration file")
        filename = require_str(file, "filename")
        if filename in BUILTIN_LOCATIONS or (kind == "PACKAGE" and name == "$unit"):
            continue
        if int(match[2]) < 1 or int(match[3]) < 1:
            raise ValueError("invalid declaration location")
        source = aliases.get(filename.replace("\\", "/"))
        if source is None:
            raise ValueError(f"unresolved or ambiguous declaration source: {filename}")
        declarations.append(
            Declaration(kind, name, source.path, filename, int(match[2]), int(match[3]))
        )
    return tuple(sorted(set(declarations)))


def decode_declarations(
    tree_path: Path,
    metadata_path: Path,
    *,
    compiler: tuple[str, str],
    sources: tuple[DeclarationSource, ...],
    build_identity: str,
    diagnostics: tuple[DeclarationDiagnostic, ...] = (),
) -> DeclarationInventory:
    """Return immutable complete or explicitly incomplete producing-build evidence."""
    inventory = DeclarationInventory(compiler, build_identity, sources, diagnostics=diagnostics)
    try:
        if compiler != (PINNED_VERILATOR.tag, PINNED_VERILATOR.commit):
            raise ValueError("unsupported producing compiler identity")
        raw = []
        for name, path in (("cells.tree.json", tree_path), ("tree.meta.json", metadata_path)):
            raw.append((name, _read_evidence_bytes(path)))
            inventory = replace(inventory, raw_evidence=tuple(raw))
        declarations = _decode_records(_bounded_json(raw[0][1]), _bounded_json(raw[1][1]), sources)
        return replace(inventory, declarations=declarations)
    except (OSError, ValueError, RecursionError) as exc:
        diagnostic = DeclarationDiagnostic("evidence_unavailable", str(exc))
        return replace(inventory, diagnostics=(*diagnostics, diagnostic))
