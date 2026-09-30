"""Capture declaration evidence while the producing build generation is leased."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml

from booley.core.boundary import require_dict, require_str
from booley.fusesoc.fusesoc_registry import ResolvedFile
from booley.targets.domain import TargetInspection

from .build import PreparedSimulationBuild
from .coverage_provenance import content_digest, coverage_digest
from .verilator_declarations import (
    BUILTIN_LOCATIONS,
    MAX_EVIDENCE_BYTES,
    DeclarationDiagnostic,
    DeclarationInventory,
    DeclarationSource,
    declaration_source_aliases,
    decode_declarations,
    possible_logical_locations,
)


def _mapped_source(
    item: ResolvedFile,
    prepared: PreparedSimulationBuild,
    inspection: TargetInspection,
    cores: Mapping,
):
    root = inspection.handle.project_root.resolve()
    physical = item.absolute(prepared.build_root)
    authored = physical
    parts = Path(item.name).parts
    core = require_dict(cores.get(item.core, {}), field="EDAM core")
    if len(parts) >= 3 and parts[0] == "src" and core.get("core_file"):
        core_file = (prepared.build_root / require_str(core, "core_file")).resolve()
        authored = core_file.parent.joinpath(*parts[2:]).resolve()
    if not authored.is_relative_to(root) or not (
        physical.is_relative_to(root) or physical.is_relative_to(prepared.work_root.resolve())
    ):
        raise ValueError(f"Unmapped HDL input: {item.name}")
    path = authored.relative_to(root).as_posix()
    declared = [s for s in inspection.inputs if s.path == path]
    if len(declared) != 1:
        raise ValueError(f"Ambiguous HDL input: {item.name}")
    original, staged = authored.read_bytes(), physical.read_bytes()
    if content_digest(original) != content_digest(staged):
        from .build_session import SimulationBuildSlotError

        raise SimulationBuildSlotError(f"Staged coverage source changed: {path}")
    source = declared[0]
    aliases = tuple(
        sorted(
            alias
            for alias in {path, authored.as_posix(), item.name, physical.as_posix()}
            if ".." not in Path(alias).parts
        )
    )
    mapped = DeclarationSource(
        path,
        aliases,
        content_digest(staged),
        item.file_type,
        "tb" in source.tags,
        source.is_include,
    )
    diagnostic = (
        DeclarationDiagnostic("logical_locations", f"Possible logical source mapping in {path}")
        if possible_logical_locations(staged)
        else None
    )
    return mapped, diagnostic


def _source_mapping(prepared: PreparedSimulationBuild, inspection: TargetInspection):
    edam = require_dict(yaml.safe_load(prepared.resolved.edam_path.read_text()), field="EDAM")
    cores = require_dict(edam.get("cores", {}), field="EDAM cores")
    sources, diagnostics, seen = [], [], set()
    for item in prepared.resolved.files:
        if item.file_type not in {"verilogSource", "systemVerilogSource"}:
            continue
        try:
            source, diagnostic = _mapped_source(item, prepared, inspection, cores)
            if source.path in seen:
                raise ValueError(f"Repeated HDL input: {source.path}")
            sources.append(source)
            seen.add(source.path)
            if diagnostic:
                diagnostics.append(diagnostic)
        except (OSError, ValueError) as exc:
            diagnostics.append(DeclarationDiagnostic("unmapped_input", str(exc)))
    return tuple(sources), tuple(diagnostics)


def _dump_pair(root: Path) -> tuple[Path, Path]:
    # Edalize may place --Mdir output underneath the resolved output directory.
    trees = []
    for index, path in enumerate(root.rglob("*")):
        if index >= 100_000:
            raise ValueError("declaration output search exceeds the file limit")
        if path.name.endswith("_cells.tree.json"):
            trees.append(path)
    if len(trees) != 1:
        raise ValueError("expected exactly one producing cells tree")
    tree = trees[0]
    # Numeric stage prefixes vary; the compiler prefix is the part before it.
    match = re.fullmatch(r"(.+)_[0-9]+_cells\.tree\.json", tree.name)
    if match is None:
        raise ValueError("unsupported cells dump filename")
    metadata = tree.with_name(match[1] + ".tree.meta.json")
    if not metadata.is_file() or not tree.resolve().is_relative_to(root.resolve()):
        raise ValueError("missing or unsafe cells metadata")
    if not metadata.resolve().is_relative_to(root.resolve()):
        raise ValueError("metadata escapes the producing build")
    return tree, metadata


def _unenumerated_inputs(
    inventory: DeclarationInventory,
    prepared: PreparedSimulationBuild,
    build_inputs: dict[str, str],
) -> tuple[DeclarationDiagnostic, ...]:
    if not inventory.raw_evidence:
        return ()
    files = json.loads(inventory.raw_evidence[1][1]).get("files", {})
    diagnostics = []
    aliases = declaration_source_aliases(inventory.sources)
    for file in files.values():
        name = file["filename"]
        if name in BUILTIN_LOCATIONS or aliases.get(name.replace("\\", "/")):
            continue
        physical = (prepared.build_root / name).resolve()
        if physical.is_relative_to(prepared.work_root.resolve()) and physical.suffix in {
            ".vc",
            ".f",
        }:
            relative = physical.relative_to(prepared.work_root).as_posix()
            if relative in build_inputs and physical.is_file():
                data = physical.read_bytes()
                if content_digest(data).removeprefix("sha256:") != build_inputs[
                    relative
                ].removeprefix("sha256:"):
                    from .build_session import SimulationBuildSlotError

                    raise SimulationBuildSlotError(
                        "Compiler response input changed during discovery"
                    )
                if not possible_logical_locations(data):
                    continue
        diagnostics.append(
            DeclarationDiagnostic("unenumerated_input", f"Unenumerated compiler source: {name}")
        )
    return tuple(diagnostics)


def capture_build_declarations(
    prepared: PreparedSimulationBuild,
    inspection: TargetInspection,
    *,
    compiler: tuple[str, str],
    build_inputs: dict[str, str],
) -> DeclarationInventory:
    """Reduce one authenticated producing generation to immutable declaration facts."""
    identity = coverage_digest(
        {"inputs": build_inputs, "compiler": compiler, "generation": prepared.work_root.name}
    )
    inventory = DeclarationInventory(compiler, identity)
    try:
        sources, diagnostics = _source_mapping(prepared, inspection)
        options = json.dumps(dict(prepared.resolved.flow_options), default=str).encode()
        if possible_logical_locations(options):
            diagnostics += (
                DeclarationDiagnostic(
                    "logical_options", "Possible logical source mapping in compiler options"
                ),
            )
        inventory = replace(inventory, sources=sources, diagnostics=diagnostics)
        tree, metadata = _dump_pair(prepared.build_root)
        if metadata.stat().st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("declaration metadata exceeds the byte limit")
        inventory = decode_declarations(
            tree,
            metadata,
            compiler=compiler,
            sources=sources,
            build_identity=identity,
            diagnostics=diagnostics,
        )
        if inventory.status == "complete":
            inventory = replace(
                inventory, diagnostics=_unenumerated_inputs(inventory, prepared, build_inputs)
            )
        return inventory
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return replace(
            inventory,
            diagnostics=(
                *inventory.diagnostics,
                DeclarationDiagnostic("capture_unavailable", str(exc)),
            ),
        )
