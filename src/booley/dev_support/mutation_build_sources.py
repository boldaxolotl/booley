"""Prove FuseSoC export identity and compiler routing before private mutation."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from fusesoc.vlnv import Vlnv

from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan
from booley.fusesoc import fusesoc_registry


def validate_mutation_closure(core_files: frozenset[Path]) -> None:
    """Reject imperative source consumers before FuseSoC setup can run them."""
    for core_file in core_files:
        data = fusesoc_registry.read_core(core_file)
        targets = data.get("targets", {})
        imperative = any(
            data.get(key) for key in ("generators", "generate", "scripts", "provider", "hooks")
        )
        if (
            imperative
            or not isinstance(targets, dict)
            or any(
                not isinstance(target, dict) or target.get("generate") or target.get("hooks")
                for target in targets.values()
            )
        ):
            raise MutationVariantError(
                f"unsupported generated/imperative mutation core {core_file}; "
                "use a declarative Target with private compiler source exports"
            )


def _private_path(root: Path, name: str) -> Path:
    path = root / name
    if Path(name).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise MutationVariantError(f"compiler source escapes private build: {name}")
    return path


def _declarations(data: dict[str, Any]):
    for fileset in data.get("filesets", {}).values():
        for entry in fileset.get("files", []):
            if isinstance(entry, str):
                yield entry, {}
            elif isinstance(entry, dict):
                yield from entry.items()


def _source_aliases(
    edam: dict[str, Any], root: Path, project: Path, source: Path
) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for vlnv, metadata in edam.get("cores", {}).items():
        core_file = (root / metadata["core_file"]).resolve()
        data = fusesoc_registry.read_core(core_file)
        if str(Vlnv(str(data.get("name")))) != str(Vlnv(vlnv)):
            raise MutationVariantError(f"ambiguous core provenance: {vlnv}")
        files_root = fusesoc_registry.core_files_root(core_file, project)
        export_root = Path("src") / Vlnv(vlnv).sanitized_name
        for declared, attributes in _declarations(data):
            if (files_root / declared).resolve() != source:
                continue
            exported = export_root / declared
            copyto = attributes.get("copyto")
            compiler_name = str(exported)
            if copyto:
                compiler_name = (
                    str(Path(copyto) / Path(declared).name) if (root / copyto).is_dir() else copyto
                )
            aliases.append((vlnv, compiler_name))
    if not aliases:
        raise MutationVariantError(f"no declared core provenance for mutation source {source}")
    return aliases


def _response_tokens(contents: str, *, windows: bool) -> list[str]:
    """Preserve native Windows separators while honoring response-file quotes."""
    lexer = shlex.shlex(contents, posix=True)
    lexer.whitespace_split = True
    if windows:
        lexer.escape = ""
    return list(lexer)


def _compiler_inputs(root: Path) -> tuple[set[Path], set[Path]]:
    """Read Edalize response files, rejecting compiler search outside the build."""
    sources: set[Path] = set()
    includes: set[Path] = set()
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    target = re.search(r"^TARGET\s*:?=\s*(\S+)\s*$", makefile, re.MULTILINE)
    recipes = [
        recipe
        for recipe in [*root.glob("*.scr"), *root.glob("*.vc")]
        if recipe.name in makefile
        or (target and recipe.name == target.group(1) + ".scr" and "-c $(TARGET).scr" in makefile)
    ]
    if not recipes:
        raise MutationVariantError(
            "cannot prove compiler routing: no Edalize .scr/.vc response file"
        )
    for recipe in recipes:
        for token in _response_tokens(recipe.read_text(encoding="utf-8"), windows=os.name == "nt"):
            if token.startswith("+incdir+"):
                includes.add(_private_path(root, token[len("+incdir+") :]).resolve())
            elif token.startswith("-I") and len(token) > 2:
                includes.add(_private_path(root, token[2:]).resolve())
            elif not token.startswith(("-", "+")) and (root / token).is_file():
                sources.add(_private_path(root, token).resolve())
    return sources, includes


def _validate_include_lookup(
    root: Path, path: Path, entry: dict[str, Any], sources: set[Path], includes: set[Path]
) -> None:
    directory = _private_path(
        root, entry.get("include_path") or str(path.relative_to(root).parent)
    ).resolve()
    if directory not in includes or path.resolve().parent != directory:
        raise MutationVariantError(
            f"unproven private include lookup for {path.name}; check include_path/copyto"
        )
    # Without parsing HDL include expressions, accept only an unambiguous
    # basename across every compiler search location, including source-local
    # and working-directory lookup. Search precedence cannot change identity.
    search_dirs = includes | {source.parent for source in sources} | {root.resolve()}
    candidates = {
        (search / path.name).resolve() for search in search_dirs if (search / path.name).exists()
    }
    if candidates != {path.resolve()}:
        raise MutationVariantError(f"ambiguous private include lookup for {path.name}")


def materialize_build_mutation(
    variants: MutationVariantPlan,
    index: int,
    work_dir: Path,
    resolved: fusesoc_registry.ResolvedTarget,
) -> None:
    """Mutate every proven compiler alias, or reject the variant before make."""
    mutation = next(m for m in variants.mutations if m.index == index)
    root = Path(resolved.build_root)
    edam = yaml.safe_load(Path(resolved.edam_path).read_text(encoding="utf-8"))
    source = (work_dir / mutation.file).resolve()
    aliases = _source_aliases(edam, root, work_dir, source)
    compiler_sources, include_dirs = _compiler_inputs(root)
    destinations: list[Path] = []
    for vlnv, name in aliases:
        entries = [
            entry
            for entry in edam["files"]
            if entry.get("core") == vlnv and Path(entry.get("name", "")) == Path(name)
        ]
        if not entries:
            continue  # A declaration in an unselected fileset is not consumed.
        owners = {
            entry.get("core")
            for entry in edam["files"]
            if Path(entry.get("name", "")) == Path(name)
        }
        if owners != {vlnv}:
            raise MutationVariantError(f"ambiguous compiler alias provenance: {name}")
        path = _private_path(root, name)
        for entry in entries:
            if entry.get("is_include_file"):
                _validate_include_lookup(root, path, entry, compiler_sources, include_dirs)
            elif path.resolve() not in compiler_sources:
                raise MutationVariantError(
                    f"compiler does not consume proven mutation source {name}"
                )
        destinations.append(path)
    variants.materialize(index, destinations, root)
