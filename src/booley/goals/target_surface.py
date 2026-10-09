"""The ``target_surface`` digest Goal evidence is stamped with (ADR 0067 D6, B5).

ADR 0067 lets an agent edit Target definitions, so Goal evidence must go
stale when the bound Target's declaration changes, not only when its RTL or
testbench sources do. The digest covers, as SHA-256 over canonical bytes:

- every ``.core`` file in the bound Target's resolved dependency closure
  (:meth:`booley.targets.catalog.TargetCatalog.core_closure`, the resolver
  the Flows use), or every discovered ``.core`` when no Target is bound
  (review Goals);
- the Project's ``tests.toml`` as the checkout resolves it, when present;
- every program those cores reference that resolves inside the worktree:
  FuseSoC ``scripts`` commands (hooks), generator commands, and Target
  ``pre_run`` commands (:func:`booley.targets.declared_inputs.core_program_paths`);
- every non-HDL file the cores' filesets list that resolves inside the
  worktree (Tcl, constraints, Verilator control files, C/C++ models, ...),
  whatever its condition, because a file selected only under one flag is
  still part of the declaration. HDL sources are left to the ``rtl`` and
  ``tb`` source fingerprints.

Each file is digested by content; a referenced file that is absent is
digested as absent, so creating it changes the digest. The encoding is typed
JSON: one ``{kind, path, sha256}`` object per file, sorted, with no in-band
tags. The digest covers whole files, so any edit of a covered file (a
parameter, a define, a toplevel, a fileset, a comment) makes it change.

Not covered, by design or as a known limit: the contents of HDL files (source
fingerprints), files outside the worktree, files a generator instance's
parameters name (only its declaration is digested), headers reached only
through an ``include_path`` directory without being listed, files named
inside free-form tool options, and paths built from environment variables.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

from booley.core.boundary import as_dict
from booley.fusesoc.fusesoc_registry import core_relative_to_project, discover_cores, read_core
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.declared_inputs import core_program_paths
from booley.targets.domain import FuseSocError

TARGET_SURFACE_CATEGORY = "target_surface"
_DIGEST_FORMAT = "booley-goal-target-surface/1"
_HDL_FILE_TYPES = ("verilogSource", "systemVerilogSource", "vhdlSource")
_HDL_SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh", ".vhd", ".vhdl"})
# A CAPI2 conditional list item: ``flag ? (item)`` or ``!flag ? (item)``.
_CONDITIONAL = re.compile(r"\s*!?[^\s?()]+\s*\?\s*\((?P<item>.*)\)\s*")


class TargetSurfaceError(RuntimeError):
    """The bound Target's declaration cannot be resolved now."""


def target_surface_fingerprint(
    work_dir: Path,
    target: str | None,
    *,
    logical_roots: Mapping[Path, Path] | None = None,
    logical_tests: str | None = None,
) -> dict[str, Any]:
    """The ``target_surface`` fingerprint entry: ``{"digest", "files"}`` (module docstring).

    Raises :class:`TargetSurfaceError` when the Target or one of its cores
    cannot be resolved or read.
    """
    root = work_dir.resolve()
    try:
        cores = _cores(root, target)
        documents = {core: read_core(core) for core in cores}
    except FuseSocError as exc:
        raise TargetSurfaceError(f"Target {target!r} cannot be resolved: {exc}") from exc
    files: dict[tuple[str, str], Path] = {}
    for core in cores:
        files[("core", _label(core, root))] = core
    tests_toml = _tests_toml(root)
    if tests_toml is not None:
        files[("tests", _label(tests_toml, root))] = tests_toml
    for core, document in documents.items():
        for program in core_program_paths(document, core_file=core, project_root=root):
            files[("program", _label(program, root))] = program
        for listed in _auxiliary_fileset_files(document, core, root):
            files[("fileset", _label(listed, root))] = listed
    entries = [
        {
            "kind": kind,
            "path": logical_tests
            if kind == "tests" and logical_tests is not None
            else _logical_label(label, logical_roots),
            "sha256": _content_digest(path),
        }
        for (kind, label), path in sorted(files.items())
    ]
    payload = json.dumps(
        {"format": _DIGEST_FORMAT, "target": target, "files": entries},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {
        "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "files": [entry["path"] for entry in entries],
    }


def _cores(root: Path, target: str | None) -> list[Path]:
    if target is None:
        return [core.resolve() for core in discover_cores(root)]
    catalog = TargetCatalog.build(root)
    closure = catalog.core_closure((catalog.select(target),))
    if not closure:
        raise FuseSocError(f"Target {target!r} resolved without a core dependency closure")
    return sorted(core.resolve() for core in closure)


def _tests_toml(root: Path) -> Path | None:
    try:
        path = resolve_checkout_project_dir(root) / "tests.toml"
    except FileNotFoundError:
        return None
    return path.resolve() if path.is_file() else None


def _auxiliary_fileset_files(
    document: Mapping[str, Any], core: Path, root: Path
) -> Iterator[Path]:
    """Every non-HDL file a core's filesets list that resolves inside *root*."""
    filesets = as_dict(document.get("filesets")) or {}
    for raw_fileset in filesets.values():
        fileset = as_dict(raw_fileset)
        if fileset is None:
            continue
        default_type = fileset.get("file_type")
        raw_files = fileset.get("files")
        listed = cast("list[Any]", raw_files) if isinstance(raw_files, list) else []
        for item in listed:
            for text, file_type in _file_items(item, default_type):
                if _is_hdl(text, file_type):
                    continue
                try:
                    relative = core_relative_to_project(core, root, text)
                except ValueError as exc:
                    raise TargetSurfaceError(
                        f"Fileset path {text!r} in {core} cannot be rebased: {exc}"
                    ) from exc
                path = (root / relative).resolve()
                if path.is_relative_to(root) and not path.is_dir():
                    yield path


def _file_items(item: object, default_type: object) -> Iterator[tuple[str, object]]:
    """``(path text, file_type)`` for one fileset ``files`` entry."""
    if isinstance(item, str):
        match = _CONDITIONAL.fullmatch(item)
        yield (match["item"].strip() if match else item), default_type
    elif isinstance(item, Mapping):
        for raw_path, raw_attributes in cast("Mapping[object, object]", item).items():
            attributes = as_dict(raw_attributes) or {}
            for text, _ in _file_items(raw_path, default_type):
                yield text, attributes.get("file_type", default_type)


def _is_hdl(text: str, file_type: object) -> bool:
    if isinstance(file_type, str) and file_type:
        return file_type.startswith(_HDL_FILE_TYPES)
    return PurePosixPath(text).suffix.casefold() in _HDL_SUFFIXES


def _label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _content_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TargetSurfaceError(f"cannot read Target declaration file {path}: {exc}") from exc


def _logical_label(label: str, roots: Mapping[Path, Path] | None) -> str:
    path = Path(label)
    if path.is_absolute():
        for physical, logical in (roots or {}).items():
            if path.is_relative_to(physical):
                return (logical / path.relative_to(physical)).as_posix()
    return label
