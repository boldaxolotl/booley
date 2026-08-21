"""Immutable FuseSoC Target contracts for Ticket Mode.

The module is deliberately below the harness and Flows.  It owns the persisted
schema, normalized Target/control-plane surface, criterion-to-Target bindings,
and pure/runtime verification used at every enforcement boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    as_str,
    is_str_list,
    require_dict,
    require_str,
)
from booley.fusesoc import fusesoc_registry
from booley.targets.target_surface import flow_can_drive

SCHEMA_VERSION = 1
CONTRACT_BLOCK_REASON = "target-contract-change-required"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROGRAM_SUFFIXES = frozenset({".py", ".sh", ".tcl", ".pl", ".rb"})
_PROGRAM_BASENAMES = frozenset({"makefile", "gnumakefile"})
_FLOW_BY_CRITERION = {
    "sim_pass": "sim",
    "lint_clean": "lint",
    "synthesis_ok": "synth",
    "fpga_impl_ok": "fpga",
    "mutation_score": "sim",
    "coverage_toggle": "sim",
    "coverage_fsm": "sim",
    "coverage_value": "sim",
    "coverage_branch": "sim",
    "coverage_expression": "sim",
    "coverage_mean": "sim",
}


class TargetContractError(ValueError):
    """A Target contract is malformed or does not match its repository."""


@dataclass(frozen=True)
class TargetContract:
    """Schema-1 identity of a sealed Target execution surface."""

    outer_sha: str
    project_sha: str
    surface_digest: str
    targets: tuple[str, ...]
    schema: int = SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: Any) -> TargetContract:
        """Validate external frontmatter and return a typed contract."""
        try:
            value = require_dict(value, field="target_contract")
        except BoundaryError as exc:
            raise TargetContractError(str(exc)) from exc
        schema = value.get("schema")
        if schema != SCHEMA_VERSION:
            raise TargetContractError(
                f"target_contract.schema must be {SCHEMA_VERSION}, got {schema!r}"
            )
        outer_sha = _required_string(value, "outer_sha")
        project_sha = _optional_string(value, "project_sha")
        digest = _required_string(value, "surface_digest").lower()
        targets = _string_tuple(value.get("targets"), "targets")
        if not _COMMIT_RE.fullmatch(outer_sha.lower()):
            raise TargetContractError("target_contract.outer_sha must be a full Git commit SHA")
        if project_sha and not _COMMIT_RE.fullmatch(project_sha.lower()):
            raise TargetContractError("target_contract.project_sha must be a full Git commit SHA")
        if not _DIGEST_RE.fullmatch(digest):
            raise TargetContractError(
                "target_contract.surface_digest must be a SHA-256 hex digest"
            )
        if tuple(sorted(set(targets))) != targets:
            raise TargetContractError("target_contract.targets must be sorted and unique")
        return cls(outer_sha.lower(), project_sha.lower(), digest, targets, schema)

    def as_dict(self) -> dict[str, Any]:
        """Return the frontmatter representation."""
        return {
            "schema": self.schema,
            "outer_sha": self.outer_sha,
            "project_sha": self.project_sha,
            "surface_digest": self.surface_digest,
            "targets": list(self.targets),
        }


@dataclass(frozen=True)
class ContractSurfaceEntry:
    """One normalized file or configuration projection in the surface."""

    path: str
    kind: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        """Return a stable JSON-ready row."""
        return {"path": self.path, "kind": self.kind, "sha256": self.sha256}


@dataclass(frozen=True)
class CriterionTarget:
    """One ticket criterion bound to one FuseSoC Target and owning Flow."""

    section: str
    key: str
    target: str
    flow: str
    relative: bool

    @property
    def label(self) -> str:
        """Frontmatter-style location used in diagnostics."""
        return f"criteria.{self.section}.{self.key}"


def _required_string(value: Mapping[str, Any], key: str) -> str:
    try:
        raw = require_str(value, key).strip()
    except BoundaryError as exc:
        raise TargetContractError(f"target_contract.{key} must be a non-empty string") from exc
    if not raw:
        raise TargetContractError(f"target_contract.{key} must be a non-empty string")
    return raw


def _optional_string(value: Mapping[str, Any], key: str) -> str:
    raw = as_str(value.get(key, ""))
    if raw is None:
        raise TargetContractError(f"target_contract.{key} must be a string")
    return raw.strip()


def _string_tuple(value: Any, key: str) -> tuple[str, ...]:
    if not is_str_list(value):
        raise TargetContractError(f"target_contract.{key} must be a list[str]")
    normalized = tuple(item.strip() for item in value)
    if any(not item for item in normalized):
        raise TargetContractError(f"target_contract.{key} cannot contain empty names")
    return normalized


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _identity(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _entry(root: Path, path: Path, kind: str, data: bytes | None = None) -> ContractSurfaceEntry:
    payload = path.read_bytes() if data is None else data
    return ContractSurfaceEntry(_identity(root, path), kind, _sha256(payload))


def _project_control_files(root: Path) -> Iterator[tuple[Path, str, bytes | None]]:
    project_dir = root / ".booley_project"
    tests_path = project_dir / "tests.toml"
    if tests_path.is_file():
        yield tests_path, "tests", _canonical_toml(tests_path)
    config_path = project_dir / "booley.toml"
    if config_path.is_file():
        projected = _target_config(config_path)
        yield config_path, "target-selection", _canonical_bytes(projected)


def _canonical_toml(path: Path) -> bytes:
    with path.open("rb") as stream:
        return _canonical_bytes(tomllib.load(stream))


def _target_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    return {key: data[key] for key in ("flows", "targets", "fusesoc") if key in data}


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _program_tokens(value: Any) -> Iterator[str]:
    for raw in _walk_strings(value):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        for token in tokens:
            candidate = token.strip("'\";,()")
            path = PurePosixPath(candidate)
            if (
                path.suffix.casefold() in _PROGRAM_SUFFIXES
                or path.name.casefold() in _PROGRAM_BASENAMES
                or "/" in candidate
                or "\\" in candidate
            ):
                yield candidate


def _core_referenced_files(root: Path, core_file: Path, doc: Mapping[str, Any]) -> Iterator[Path]:
    filesets = doc.get("filesets")
    if not isinstance(filesets, Mapping):
        return
    for fileset in filesets.values():
        if not isinstance(fileset, Mapping):
            continue
        files = fileset.get("files")
        if not isinstance(files, list):
            continue
        for entry in files:
            if isinstance(entry, str):
                raw = entry
            elif isinstance(entry, Mapping):
                raw = next(iter(entry), "")
            else:
                continue
            if not raw:
                continue
            rel = fusesoc_registry.core_relative_to_project(core_file, root, str(raw))
            yield root / rel


def _core_auxiliary_paths(root: Path, core_file: Path, doc: Mapping[str, Any]) -> set[Path]:
    paths: set[Path] = set()
    for candidate in _core_referenced_files(root, core_file, doc):
        if candidate.suffix.casefold() in {".sdc", ".xdc"} and candidate.is_file():
            paths.add(candidate)
    imperative = {
        key: doc[key] for key in ("generators", "generate", "scripts", "targets") if key in doc
    }
    for token in _program_tokens(imperative):
        candidate = (core_file.parent / token).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            paths.add(candidate)
    return paths


def _config_auxiliary_paths(root: Path, config_path: Path) -> set[Path]:
    """Find executable hooks referenced by Target-selection configuration."""
    paths: set[Path] = set()
    for token in _program_tokens(_target_config(config_path)):
        candidates = ((root / token).resolve(), (config_path.parent / token).resolve())
        for candidate in candidates:
            if candidate.is_relative_to(root) and candidate.is_file():
                paths.add(candidate)
                break
    return paths


def surface_entries(project_root: Path | str) -> tuple[ContractSurfaceEntry, ...]:
    """Return the normalized, path-identifying Target/control-plane manifest."""
    root = Path(project_root).resolve()
    rows: list[ContractSurfaceEntry] = []
    auxiliary: set[Path] = set()
    for core_file in fusesoc_registry.discover_cores(root):
        doc = fusesoc_registry.read_core(core_file)
        rows.append(_entry(root, core_file, "core", _canonical_bytes(doc)))
        auxiliary.update(_core_auxiliary_paths(root, core_file, doc))
    for path, kind, data in _project_control_files(root):
        rows.append(_entry(root, path, kind, data))
        if kind == "target-selection":
            auxiliary.update(_config_auxiliary_paths(root, path))
    for path in sorted(auxiliary):
        kind = "constraint" if path.suffix.casefold() in {".sdc", ".xdc"} else "hook"
        rows.append(_entry(root, path, kind))
    return tuple(sorted(rows, key=lambda row: (row.path, row.kind)))


def surface_digest(project_root: Path | str) -> str:
    """Hash the normalized Target/control-plane manifest."""
    manifest = [row.as_dict() for row in surface_entries(project_root)]
    return _sha256(_canonical_bytes({"schema": SCHEMA_VERSION, "files": manifest}))


def contract_control_paths(project_root: Path | str) -> tuple[str, ...]:
    """Return concrete surface paths for commit and pre-commit enforcement."""
    return tuple(row.path for row in surface_entries(project_root))


def _criterion_flow(key: str) -> str | None:
    for prefix in sorted(_FLOW_BY_CRITERION, key=len, reverse=True):
        if key == prefix or key.startswith(prefix + "_"):
            return _FLOW_BY_CRITERION[prefix]
    return None


def _relative_params(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(
        isinstance(key, str) and key.endswith(("_increase_at_most", "_reduce_at_least"))
        for key in value
        if not str(key).startswith("_")
    )


def _targets_from_value(key: str, value: Any) -> list[tuple[str, bool]]:
    if isinstance(value, Mapping):
        targets = value.get("targets")
        if isinstance(targets, list):
            return [
                (target, _relative_params(value)) for target in targets if isinstance(target, str)
            ]
        return []
    if not isinstance(value, list):
        return []
    return _targets_from_list(key, value)


def _targets_from_list(key: str, value: list[Any]) -> list[tuple[str, bool]]:
    from booley.dev_support.criteria import parse_sim_criterion

    targets: list[tuple[str, bool]] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("target"), str):
            targets.append((item["target"], _relative_params(item)))
        elif isinstance(item, str) and "->" in item:
            try:
                targets.append((parse_sim_criterion(item).target, False))
            except ValueError:
                continue
        elif isinstance(item, str) and "@" not in item:
            targets.append((item, False))
    return targets


def criterion_targets(criteria: Any) -> tuple[CriterionTarget, ...]:
    """Extract Target bindings from mandatory and optional ticket criteria."""
    if not isinstance(criteria, Mapping):
        return ()
    bindings: list[CriterionTarget] = []
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if not isinstance(section, Mapping):
            continue
        for key, value in section.items():
            flow = _criterion_flow(str(key))
            if flow is None:
                continue
            for target, relative in _targets_from_value(str(key), value):
                bindings.append(CriterionTarget(section_name, str(key), target, flow, relative))
    return tuple(bindings)


def _new_scope_matches(scope: Any, path: str) -> bool:
    import fnmatch

    if not isinstance(scope, list):
        return False
    normalized = path.replace("\\", "/").removeprefix("./")
    for raw in scope:
        if not isinstance(raw, str) or not raw.endswith(" [new]"):
            continue
        entry = raw.removesuffix(" [new]").replace("\\", "/").removeprefix("./")
        if normalized == entry or normalized.startswith(entry.rstrip("/") + "/"):
            return True
        if any(char in entry for char in "*?[") and fnmatch.fnmatchcase(normalized, entry):
            return True
    return False


def _missing_target_sources(root: Path, target: str) -> list[str]:
    sources = fusesoc_registry.target_source_files(
        root, target, include_dependencies=True, include_headers=True
    )
    missing: list[str] = []
    for name in (*sources.rtl_source_files, *sources.tb_files):
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            missing.append(name)
    return sorted(set(missing))


def validate_criterion_targets(fields: Mapping[str, Any], project_root: Path | str) -> list[str]:
    """Validate every mandatory/optional criterion Target without running tools."""
    root = Path(project_root)
    errors: list[str] = []
    for binding in criterion_targets(fields.get("criteria")):
        errors.extend(_validate_binding(binding, fields, root))
    return errors


def validate_targets_for_seal(
    fields: Mapping[str, Any],
    project_root: Path | str,
    build_root: Path | str,
    *,
    changed_targets: Iterable[str] = (),
) -> list[str]:
    """Validate bindings and dry-resolve criterion and changed Targets."""
    root = Path(project_root)
    errors = validate_criterion_targets(fields, root)
    if errors:
        return errors
    seen: set[str] = set()
    for binding in criterion_targets(fields.get("criteria")):
        if binding.target in seen or _missing_target_sources(root, binding.target):
            continue
        seen.add(binding.target)
        target_build = Path(build_root) / _safe_target_dir(binding.target)
        errors.extend(_dry_resolve_binding(binding, root, target_build))
    for target in changed_targets:
        if target in seen:
            continue
        seen.add(target)
        missing = _missing_target_sources(root, target)
        undeclared = [
            path for path in missing if not _new_scope_matches(fields.get("scope"), path)
        ]
        if undeclared:
            errors.append(
                f"changed Target {target!r} has missing source(s) not declared Scope [new]: "
                + ", ".join(undeclared)
            )
            continue
        if missing:
            continue
        binding = CriterionTarget("contract", "changed_target", target, "", False)
        target_build = Path(build_root) / _safe_target_dir(target)
        errors.extend(_dry_resolve_binding(binding, root, target_build))
    return errors


def _safe_target_dir(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", target).strip("_") or "target"


def _dry_resolve_binding(binding: CriterionTarget, root: Path, build_root: Path) -> list[str]:
    try:
        resolved = fusesoc_registry.resolve_target(
            binding.target,
            project_root=root,
            build_root=build_root,
        )
    except (fusesoc_registry.FuseSocError, OSError) as exc:
        return [f"{binding.label}: target {binding.target!r} dry-run failed: {exc}"]
    if not resolved.toplevel:
        return [f"{binding.label}: target {binding.target!r} resolves without a toplevel"]
    return []


def _validate_binding(
    binding: CriterionTarget, fields: Mapping[str, Any], root: Path
) -> list[str]:
    try:
        ref = fusesoc_registry.resolve_ref(root, binding.target)
    except fusesoc_registry.FuseSocError as exc:
        return [f"{binding.label}: target {binding.target!r}: {exc}"]
    if not flow_can_drive(binding.flow, ref):
        return [
            f"{binding.label}: target {binding.target!r} cannot satisfy {binding.key} "
            f"with Flow {binding.flow} (flow={ref.flow!r}, EDA tool={ref.eda_tool!r})"
        ]
    missing = _missing_target_sources(root, binding.target)
    if not missing:
        return []
    if binding.relative:
        return [
            f"{binding.label}: relative-QoR target {binding.target!r} has missing baseline "
            f"source(s): {', '.join(missing)}"
        ]
    undeclared = [path for path in missing if not _new_scope_matches(fields.get("scope"), path)]
    if not undeclared:
        return []
    return [
        f"{binding.label}: target {binding.target!r} has missing source(s) not declared "
        f"Scope [new]: {', '.join(undeclared)}"
    ]


def validate_contract_fields(fields: Mapping[str, Any]) -> list[str]:
    """Validate a present contract and its compatibility `base_sha`."""
    raw = fields.get("target_contract")
    if raw is None:
        return []
    try:
        contract = TargetContract.from_mapping(raw)
    except TargetContractError as exc:
        return [str(exc)]
    if fields.get("base_sha") != contract.outer_sha:
        return ["base_sha must equal target_contract.outer_sha"]
    declared = set(contract.targets)
    referenced = {binding.target for binding in criterion_targets(fields.get("criteria"))}
    missing = sorted(referenced - declared)
    if missing:
        return [f"target_contract.targets omits criterion Target(s): {', '.join(missing)}"]
    return []


def resolve_commit(repository: Path | str, sha: str) -> str:
    """Resolve and require an exact full commit SHA in *repository*."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    resolved = result.stdout.strip().lower()
    if result.returncode != 0 or resolved != sha.lower():
        detail = (result.stderr or result.stdout).strip()
        raise TargetContractError(f"commit {sha!r} does not resolve exactly: {detail}")
    return resolved


def verify_surface(contract: TargetContract, project_root: Path | str) -> None:
    """Raise when the checkout's current contract surface differs from sealed data."""
    actual = surface_digest(project_root)
    if actual != contract.surface_digest:
        raise TargetContractError(
            f"{CONTRACT_BLOCK_REASON}: Target surface digest is {actual}, "
            f"expected {contract.surface_digest}"
        )


def load_ticket_contract(ticket_path: Path | str) -> TargetContract | None:
    """Read a contract from a ticket file; return None for explicit legacy mode."""
    from .frontmatter import parse_frontmatter

    path = Path(ticket_path)
    fields, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    raw = fields.get("target_contract")
    return TargetContract.from_mapping(raw) if raw is not None else None


def build_contract(
    project_root: Path | str,
    *,
    outer_sha: str,
    project_sha: str = "",
    targets: Iterable[str] = (),
) -> TargetContract:
    """Build a sealed contract from already committed repository state."""
    return TargetContract(
        outer_sha=outer_sha.lower(),
        project_sha=project_sha.lower(),
        surface_digest=surface_digest(project_root),
        targets=tuple(sorted(set(targets))),
    )
