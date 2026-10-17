from pathlib import Path

import yaml
from fusesoc.vlnv import Vlnv

from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan
from booley.fusesoc import fusesoc_registry
from booley.fusesoc.fusesoc_registry import ResolvedTarget


def validate_mutation_closure(core_files: frozenset[Path]) -> None:
    """Reject imperative source consumers before setup."""
    for core_file in core_files:
        data = fusesoc_registry.read_core(core_file)
        targets = data.get("targets", {})
        if (
            any(
                data.get(key) for key in ("generators", "generate", "scripts", "provider", "hooks")
            )
            or not isinstance(targets, dict)
            or any(
                not isinstance(t, dict) or t.get("generate") or t.get("hooks")
                for t in targets.values()
            )
        ):
            raise MutationVariantError(f"unsupported generated/imperative core: {core_file}")


def _copies(edam, root, project):
    copies = {}
    for core, metadata in edam["cores"].items():
        core_file = (root / metadata["core_file"]).resolve()
        files_root = fusesoc_registry.core_files_root(core_file, project)
        for fileset in fusesoc_registry.read_core(core_file).get("filesets", {}).values():
            for entry in fileset.get("files", []):
                for name, attrs in entry.items() if isinstance(entry, dict) else [(entry, {})]:
                    exported = Path("src") / Vlnv(core).sanitized_name / name
                    destination = Path(attrs.get("copyto", exported))
                    if (root / destination).is_dir():
                        destination /= Path(name).name
                    original = (files_root / name).resolve()
                    copies.setdefault(destination, set()).add((core, original, exported))
    return copies


def materialize_build_mutation(
    variants: MutationVariantPlan, index: int, work_dir: Path, resolved: ResolvedTarget
) -> None:
    """Collect and validate every selected copy before any write."""
    root = Path(resolved.build_root)
    edam = yaml.safe_load(Path(resolved.edam_path).read_text(encoding="utf-8"))
    mutation = next(m for m in variants.mutations if m.index == index)
    source = (work_dir / mutation.file).resolve()
    copies = _copies(edam, root, work_dir)
    destinations = []
    for entry in edam["files"]:
        name = Path(entry["name"])
        candidates = copies.get(name, set())
        if not entry.get("file_type", "").startswith(("verilogSource", "systemVerilogSource")):
            continue
        if not any(c == entry.get("core") and s == source for c, s, _ in candidates):
            continue
        if len({original for core, original, exported in candidates}) != 1:
            raise MutationVariantError(f"ambiguous private source mapping: {name}")
        for core, _original, exported in candidates:
            if name.is_absolute() or exported.is_absolute():
                raise MutationVariantError(f"absolute mutation export: {name}")
            if core == entry.get("core"):
                destinations.extend((root / name, root / exported))
    variants.materialize(index, destinations, root)
