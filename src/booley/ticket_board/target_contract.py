"""Immutable FuseSoC Target contracts for Ticket Mode.

The module is deliberately below the harness and Flows.  It owns the persisted
schema, normalized Target/control-plane surface, criterion-to-Target bindings,
and pure/runtime verification used at every enforcement boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    as_str,
    is_str_list,
    require_dict,
    require_list,
    require_str,
)
from booley.dev_support.thresholds import has_relative_threshold
from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.declared_inputs import referenced_program_paths
from booley.targets.target import flow_can_drive, inspect_target, select_target

WORKSPACE_SCHEMA_VERSION = 3
SCHEMA_VERSION = 4
SUPPORTED_SCHEMA_VERSIONS = frozenset({WORKSPACE_SCHEMA_VERSION, SCHEMA_VERSION})
CONTRACT_BLOCK_REASON = "target-contract-change-required"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FLOW_BY_CRITERION = {
    "sim_pass": "sim",
    "cycle_count": "sim",
    "lint_clean": "lint",
    "synthesis_ok": "synth",
    "fpga_impl_ok": "fpga",
    "mutation_score": "sim",
    "coverage": "sim",
}


class TargetContractError(ValueError):
    """A Target contract is malformed or does not match its repository."""


@dataclass(frozen=True, order=True)
class ContractParticipant:
    """One repository whose sealed history participates in Ticket acceptance."""

    role: str
    sealed_sha: str
    ticket_ref: str
    destination_ref: str
    destination_sha: str

    def as_dict(self) -> dict[str, str]:
        """Return the stable frontmatter representation."""
        return {
            "role": self.role,
            "sealed_sha": self.sealed_sha,
            "ticket_ref": self.ticket_ref,
            "destination_ref": self.destination_ref,
            "destination_sha": self.destination_sha,
        }


@dataclass(frozen=True)
class TargetContract:
    """Identity of a sealed Target surface and its directed criterion bindings."""

    outer_sha: str
    project_sha: str
    surface_digest: str
    targets: tuple[str, ...]
    removal_targets: tuple[str, ...] = ()
    bindings: tuple[ContractTargetBinding, ...] = ()
    participants: tuple[ContractParticipant, ...] = ()
    surface_entries: tuple[ContractSurfaceEntry, ...] = ()
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema not in SUPPORTED_SCHEMA_VERSIONS:
            raise TargetContractError(
                "target_contract.schema must be one of "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}, got {self.schema!r}"
            )
        _validate_participants(self.participants, self.outer_sha, self.project_sha)
        _validate_contract_removal_targets(self.removal_targets, self.bindings)

    @classmethod
    def from_mapping(cls, value: Any) -> TargetContract:
        """Validate external frontmatter and return a typed contract."""
        try:
            value = require_dict(value, field="target_contract")
        except BoundaryError as exc:
            raise TargetContractError(str(exc)) from exc
        schema = value.get("schema")
        if schema not in SUPPORTED_SCHEMA_VERSIONS:
            raise TargetContractError(
                "target_contract.schema must be one of "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}, got {schema!r}"
            )
        outer_sha = _required_string(value, "outer_sha")
        project_sha = _optional_string(value, "project_sha")
        digest = _required_string(value, "surface_digest").lower()
        targets = _string_tuple(value.get("targets"), "targets")
        removal_targets = _string_tuple(value.get("removal_targets", []), "removal_targets")
        bindings = _binding_tuple(value.get("bindings"), schema=schema)
        participants = _participant_tuple(value.get("participants"))
        entries = _surface_entry_tuple(value.get("surface_entries"))
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
        if tuple(sorted(set(bindings))) != bindings:
            raise TargetContractError("target_contract.bindings must be sorted and unique")
        return cls(
            outer_sha=outer_sha.lower(),
            project_sha=project_sha.lower(),
            surface_digest=digest,
            targets=targets,
            removal_targets=removal_targets,
            bindings=bindings,
            participants=participants,
            surface_entries=entries,
            schema=schema,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the frontmatter representation."""
        result = {
            "schema": self.schema,
            "outer_sha": self.outer_sha,
            "project_sha": self.project_sha,
            "surface_digest": self.surface_digest,
            "targets": list(self.targets),
        }
        if self.schema >= SCHEMA_VERSION or self.removal_targets:
            result["removal_targets"] = list(self.removal_targets)
        result["bindings"] = [binding.as_dict(schema=self.schema) for binding in self.bindings]
        result["participants"] = [participant.as_dict() for participant in self.participants]
        result["surface_entries"] = [entry.as_dict() for entry in self.surface_entries]
        return result


@dataclass(frozen=True, order=True)
class ContractTargetBinding:
    """Canonical directed Target identities and their callable selectors."""

    flow: str
    criterion: str
    baseline: str
    candidate: str
    baseline_selector: str = ""
    candidate_selector: str = ""

    def as_dict(self, *, schema: int = SCHEMA_VERSION) -> dict[str, str]:
        result = {
            "flow": self.flow,
            "criterion": self.criterion,
            "baseline": self.baseline,
            "candidate": self.candidate,
        }
        if schema >= SCHEMA_VERSION:
            result["baseline_selector"] = self.baseline_selector
            result["candidate_selector"] = self.candidate_selector
        return result


def _validate_contract_removal_targets(
    removal_targets: tuple[str, ...], bindings: tuple[ContractTargetBinding, ...]
) -> None:
    if tuple(sorted(set(removal_targets))) != removal_targets:
        raise TargetContractError("target_contract.removal_targets must be sorted and unique")
    bound_targets = {
        target for binding in bindings for target in (binding.baseline, binding.candidate)
    }
    if not set(removal_targets) <= bound_targets:
        raise TargetContractError(
            "target_contract.removal_targets must contain only criterion-bound Targets"
        )


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
    baseline_target: str | None = None

    @property
    def baseline(self) -> str:
        """Baseline Target, defaulting to the candidate for equal-Target criteria."""
        return self.baseline_target or self.target

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


def _binding_tuple(value: Any, *, schema: int) -> tuple[ContractTargetBinding, ...]:
    try:
        raw_bindings = require_list(value, field="target_contract.bindings")
    except BoundaryError as exc:
        raise TargetContractError("target_contract.bindings must be a list[dict]") from exc
    bindings: list[ContractTargetBinding] = []
    for index, raw in enumerate(raw_bindings):
        field = f"target_contract.bindings[{index}]"
        try:
            mapping = require_dict(raw, field=field)
        except BoundaryError as exc:
            raise TargetContractError(str(exc)) from exc
        keys = {"flow", "criterion", "baseline", "candidate"}
        if schema >= SCHEMA_VERSION:
            keys.update({"baseline_selector", "candidate_selector"})
        if set(mapping) != keys:
            raise TargetContractError(
                f"target_contract.bindings[{index}] must contain exactly "
                + ", ".join(sorted(keys))
            )
        fields: dict[str, str] = {}
        for key in sorted(keys):
            try:
                item = require_str(mapping, key).strip()
            except BoundaryError as exc:
                raise TargetContractError(
                    f"target_contract.bindings[{index}].{key} must be a non-empty string"
                ) from exc
            if not item:
                raise TargetContractError(
                    f"target_contract.bindings[{index}].{key} must be a non-empty string"
                )
            fields[key] = item
        bindings.append(ContractTargetBinding(**fields))
    return tuple(bindings)


def _participant_tuple(value: Any) -> tuple[ContractParticipant, ...]:
    try:
        raw_rows = require_list(value, field="target_contract.participants")
    except BoundaryError as exc:
        raise TargetContractError("target_contract.participants must be a list[dict]") from exc
    participants: list[ContractParticipant] = []
    for index, raw in enumerate(raw_rows):
        field = f"target_contract.participants[{index}]"
        try:
            item = require_dict(raw, field=field)
            role = require_str(item, "role").strip()
            sealed_sha = require_str(item, "sealed_sha").strip().lower()
            ticket_ref = require_str(item, "ticket_ref").strip()
            destination_ref = require_str(item, "destination_ref").strip()
            destination_sha = require_str(item, "destination_sha").strip().lower()
        except BoundaryError as exc:
            raise TargetContractError(f"{field} is malformed: {exc}") from exc
        if role not in {"outer", "project"}:
            raise TargetContractError(f"{field}.role must be 'outer' or 'project'")
        if not _COMMIT_RE.fullmatch(sealed_sha) or not _COMMIT_RE.fullmatch(destination_sha):
            raise TargetContractError(f"{field} commit identities must be full Git SHAs")
        if not ticket_ref.startswith("refs/heads/") or not destination_ref.startswith(
            "refs/heads/"
        ):
            raise TargetContractError(f"{field} refs must be full refs/heads names")
        participants.append(
            ContractParticipant(role, sealed_sha, ticket_ref, destination_ref, destination_sha)
        )
    result = tuple(participants)
    if tuple(sorted(set(result))) != result:
        raise TargetContractError("target_contract.participants must be sorted and unique")
    return result


def _surface_entry_tuple(value: Any) -> tuple[ContractSurfaceEntry, ...]:
    try:
        raw_rows = require_list(value, field="target_contract.surface_entries")
    except BoundaryError as exc:
        raise TargetContractError("target_contract.surface_entries must be a list[dict]") from exc
    entries: list[ContractSurfaceEntry] = []
    for index, raw in enumerate(raw_rows):
        field = f"target_contract.surface_entries[{index}]"
        try:
            item = require_dict(raw, field=field)
            path = require_str(item, "path").strip()
            kind = require_str(item, "kind").strip()
            sha256 = require_str(item, "sha256").strip().lower()
        except BoundaryError as exc:
            raise TargetContractError(f"{field} is malformed: {exc}") from exc
        if not path or not kind or not _DIGEST_RE.fullmatch(sha256):
            raise TargetContractError(f"{field} has an invalid path, kind, or digest")
        entries.append(ContractSurfaceEntry(path, kind, sha256))
    result = tuple(entries)
    if tuple(sorted(set(result), key=lambda row: (row.path, row.kind, row.sha256))) != result:
        raise TargetContractError("target_contract.surface_entries must be sorted and unique")
    return result


def _validate_participants(
    participants: tuple[ContractParticipant, ...], outer_sha: str, project_sha: str
) -> None:
    by_role = {participant.role: participant for participant in participants}
    if len(by_role) != len(participants):
        raise TargetContractError("target_contract.participants may contain each role once")
    outer = by_role.get("outer")
    if outer is None:
        raise TargetContractError("target_contract.participants requires an outer participant")
    if outer.sealed_sha != outer_sha:
        raise TargetContractError("outer participant sealed_sha must equal outer_sha")
    project = by_role.get("project")
    if bool(project) != bool(project_sha):
        raise TargetContractError("project participant presence must match project_sha")
    if project is not None and project.sealed_sha != project_sha:
        raise TargetContractError("project participant sealed_sha must equal project_sha")


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
    if kind == "constraint":
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return ContractSurfaceEntry(_identity(root, path), kind, _sha256(payload))


def _project_control_files(root: Path) -> Iterator[tuple[Path, str, bytes | None]]:
    try:
        project_dir = resolve_checkout_project_dir(root)
    except FileNotFoundError:
        return
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
    paths.update(
        referenced_program_paths(
            imperative,
            search_roots=(core_file.parent,),
            project_root=root,
        )
    )
    return paths


def _config_auxiliary_paths(root: Path, config_path: Path) -> set[Path]:
    """Find executable hooks referenced by Target-selection configuration."""
    return set(
        referenced_program_paths(
            _target_config(config_path),
            search_roots=(root, config_path.parent),
            project_root=root,
        )
    )


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


def _inspection_tokens(root: Path, targets: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(sorted(set(targets)))
    if selected:
        return selected
    identities = {
        f"{ref.vlnv}#{ref.name}"
        for refs in fusesoc_registry.target_declarations(root).values()
        for ref in refs
        if not ref.doctor_selftest
    }
    return tuple(sorted(identities))


def _semantic_surface(project_root: Path | str, targets: Iterable[str]) -> dict[str, Any]:
    """Project selected FuseSoC semantics without source existence or spelling."""
    root = Path(project_root).resolve()
    projected: list[dict[str, Any]] = []
    auxiliary: set[Path] = set()
    for token in _inspection_tokens(root, targets):
        inspection = inspect_target(root, token)
        inputs = [
            {
                "path": item.path,
                "core": item.core,
                "file_type": item.file_type,
                "tags": list(item.tags),
                "is_include": item.is_include,
                "attributes": dict(item.attributes),
            }
            for item in inspection.inputs
        ]
        projected.append(
            {
                "identity": inspection.handle.identity,
                "selector": inspection.handle.selector,
                "toplevel": inspection.toplevel,
                "flow": inspection.flow,
                "eda_tool": inspection.eda_tool,
                "flow_options": dict(inspection.flow_options),
                "parameters": dict(inspection.parameters),
                "inputs": inputs,
            }
        )
        for item in inspection.inputs:
            path = root / item.path
            if path.suffix.casefold() in {".sdc", ".xdc"} and path.is_file():
                auxiliary.add(path)
        auxiliary.update(
            referenced_program_paths(
                {
                    "flow_options": inspection.flow_options,
                    "parameters": inspection.parameters,
                },
                search_roots=(inspection.handle.core_file.parent,),
                project_root=root,
            )
        )

    controls: dict[tuple[str, str], ContractSurfaceEntry] = {}
    for path, kind, data in _project_control_files(root):
        row = _entry(root, path, kind, data)
        controls[(row.path, row.kind)] = row
        if kind == "target-selection":
            auxiliary.update(_config_auxiliary_paths(root, path))
    for path in sorted(auxiliary):
        kind = "constraint" if path.suffix.casefold() in {".sdc", ".xdc"} else "hook"
        row = _entry(root, path, kind)
        controls[(row.path, row.kind)] = row
    return {
        "targets": sorted(projected, key=lambda item: item["identity"]),
        "controls": [controls[key].as_dict() for key in sorted(controls)],
    }


def surface_digest(
    project_root: Path | str,
    *,
    schema: int = SCHEMA_VERSION,
    targets: Iterable[str] = (),
) -> str:
    """Hash the versioned Target/control-plane projection."""
    if schema == SCHEMA_VERSION:
        return _sha256(
            _canonical_bytes(
                {"schema": schema, "surface": _semantic_surface(project_root, targets)}
            )
        )
    if schema != WORKSPACE_SCHEMA_VERSION:
        raise TargetContractError(f"unsupported Target contract schema {schema!r}")
    manifest = [row.as_dict() for row in surface_entries(project_root)]
    return _sha256(_canonical_bytes({"schema": schema, "files": manifest}))


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
    return has_relative_threshold(dict(value))


def _targets_from_value(key: str, value: Any) -> list[tuple[str, str, bool]]:
    if isinstance(value, Mapping):
        from booley.dev_support.criteria import parse_target_pair

        targets = value.get("targets")
        if isinstance(targets, list):
            pairs: list[tuple[str, str, bool]] = []
            for index, target in enumerate(targets):
                try:
                    pair = parse_target_pair(target, field=f"{key}.targets[{index}]")
                except ValueError:
                    continue
                pairs.append((pair.candidate, pair.baseline, _relative_params(value)))
            return pairs
        return []
    if not isinstance(value, list):
        return []
    return _targets_from_list(key, value)


def _targets_from_list(key: str, value: list[Any]) -> list[tuple[str, str, bool]]:
    from booley.dev_support.criteria import parse_sim_criterion

    targets: list[tuple[str, str, bool]] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("target"), str):
            targets.append((item["target"], item["target"], _relative_params(item)))
        elif isinstance(item, str) and "->" in item:
            try:
                target = parse_sim_criterion(item).target
                targets.append((target, target, False))
            except ValueError:
                continue
        elif isinstance(item, str) and "@" not in item:
            targets.append((item, item, False))
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
            for target, baseline, relative in _targets_from_value(str(key), value):
                bindings.append(
                    CriterionTarget(
                        section_name,
                        str(key),
                        target,
                        flow,
                        relative,
                        baseline if baseline != target else None,
                    )
                )
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


def _missing_target_sources(
    root: Path,
    target: str,
    *,
    schema: int = SCHEMA_VERSION,
) -> list[str]:
    if schema == WORKSPACE_SCHEMA_VERSION:
        sources = fusesoc_registry.target_source_files(
            root,
            target,
            include_dependencies=True,
            include_headers=True,
        )
        selected = (*sources.rtl_source_files, *sources.tb_files)
    else:
        selected = tuple(item.path for item in inspect_target(root, target).inputs)
    missing: list[str] = []
    for path in selected:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            missing.append(path)
    return sorted(set(missing))


def _validation_schema(fields: Mapping[str, Any]) -> int:
    contract = fields.get("target_contract")
    if isinstance(contract, Mapping) and contract.get("schema") == WORKSPACE_SCHEMA_VERSION:
        return WORKSPACE_SCHEMA_VERSION
    return SCHEMA_VERSION


def validate_criterion_targets(fields: Mapping[str, Any], project_root: Path | str) -> list[str]:
    """Validate every mandatory/optional criterion Target without running tools."""
    root = Path(project_root)
    schema = _validation_schema(fields)
    errors: list[str] = []
    for binding in criterion_targets(fields.get("criteria")):
        errors.extend(_validate_binding(binding, fields, root, schema=schema))
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
    schema = _validation_schema(fields)
    errors = validate_criterion_targets(fields, root)
    if errors:
        return errors
    bindings = criterion_targets(fields.get("criteria"))
    required = _required_targets(root, bindings, schema=schema)
    errors.extend(_validate_required_targets(root, Path(build_root), required))
    for binding in bindings:
        if binding.baseline != binding.target and not _missing_target_sources(
            root, binding.target, schema=schema
        ):
            errors.extend(_validate_comparison_basis(binding, root, Path(build_root)))
    errors.extend(
        _validate_changed_targets(
            fields,
            root,
            Path(build_root),
            changed_targets,
            seen=set(required),
            schema=schema,
        )
    )
    return errors


def _required_targets(
    root: Path,
    bindings: Iterable[CriterionTarget],
    *,
    schema: int = SCHEMA_VERSION,
) -> dict[str, tuple[CriterionTarget, bool]]:
    # A Target used as a baseline anywhere always receives the stronger
    # executable-at-base requirement, even if another pair also uses it as a
    # candidate whose [new] sources could otherwise defer resolution.
    required: dict[str, tuple[CriterionTarget, bool]] = {}
    for binding in bindings:
        candidate_missing = bool(_missing_target_sources(root, binding.target, schema=schema))
        prior = required.get(binding.target)
        required[binding.target] = (
            binding,
            (prior[1] if prior else False) or not candidate_missing,
        )
        if binding.relative or binding.baseline != binding.target:
            required[binding.baseline] = (binding, True)
    return required


def _validate_required_targets(
    root: Path,
    build_root: Path,
    required: Mapping[str, tuple[CriterionTarget, bool]],
) -> list[str]:
    errors: list[str] = []
    for target, (binding, must_resolve) in required.items():
        if not must_resolve:
            continue
        target_build = Path(build_root) / _safe_target_dir(target)
        errors.extend(_dry_resolve_binding(binding, root, target_build, target=target))
    return errors


def _validate_changed_targets(
    fields: Mapping[str, Any],
    root: Path,
    build_root: Path,
    changed_targets: Iterable[str],
    *,
    seen: set[str],
    schema: int = SCHEMA_VERSION,
) -> list[str]:
    errors: list[str] = []
    for target in changed_targets:
        if target in seen:
            continue
        seen.add(target)
        missing = _missing_target_sources(root, target, schema=schema)
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
        target_build = build_root / _safe_target_dir(target)
        errors.extend(_dry_resolve_binding(binding, root, target_build))
    return errors


def _safe_target_dir(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", target).strip("_") or "target"


def _validate_comparison_basis(
    binding: CriterionTarget,
    root: Path,
    build_root: Path,
) -> list[str]:
    """Fail sealing when two resolvable Targets change measurement methodology."""
    try:
        snapshots = _comparison_snapshots(binding, root, build_root)
    except (fusesoc_registry.FuseSocError, BoundaryError, OSError) as exc:
        return [f"{binding.label}: cannot compare Target measurement basis: {exc}"]
    if snapshots is None:
        return []
    from booley.flows.recipe_evidence import implementation_comparison_basis, recipe_changes

    baseline_snapshot, candidate_snapshot = snapshots
    changes = recipe_changes(
        implementation_comparison_basis(baseline_snapshot),
        implementation_comparison_basis(candidate_snapshot),
    )
    if not changes:
        return []
    paths = ", ".join(str(change.get("path")) for change in changes[:5])
    return [
        f"{binding.label}: baseline Target {binding.baseline!r} and candidate Target "
        f"{binding.target!r} use incompatible measurement bases ({paths})"
    ]


def _comparison_snapshots(
    binding: CriterionTarget, root: Path, build_root: Path
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    baseline = fusesoc_registry.resolve_target(
        binding.baseline,
        project_root=root,
        build_root=build_root / f"basis-baseline-{_safe_target_dir(binding.baseline)}",
    )
    candidate = fusesoc_registry.resolve_target(
        binding.target,
        project_root=root,
        build_root=build_root / f"basis-candidate-{_safe_target_dir(binding.target)}",
    )
    if binding.flow == "synth":
        from booley.flows.synth.recipe import default_recipe_args, synthesis_recipe_snapshot

        args = default_recipe_args()
        return (
            synthesis_recipe_snapshot(baseline, args, target=binding.baseline),
            synthesis_recipe_snapshot(candidate, args, target=binding.target),
        )
    if binding.flow == "fpga":
        from booley.flows.fpga.recipe import fpga_recipe_snapshot

        return (
            fpga_recipe_snapshot(baseline, target=binding.baseline),
            fpga_recipe_snapshot(candidate, target=binding.target),
        )
    return None


def _dry_resolve_binding(
    binding: CriterionTarget,
    root: Path,
    build_root: Path,
    *,
    target: str | None = None,
) -> list[str]:
    selected = target or binding.target
    try:
        resolved = fusesoc_registry.resolve_target(
            selected,
            project_root=root,
            build_root=build_root,
        )
    except (fusesoc_registry.FuseSocError, OSError) as exc:
        return [f"{binding.label}: target {selected!r} dry-run failed: {exc}"]
    if not resolved.toplevel:
        return [f"{binding.label}: target {selected!r} resolves without a toplevel"]
    return []


def _validate_binding(
    binding: CriterionTarget,
    fields: Mapping[str, Any],
    root: Path,
    *,
    schema: int = SCHEMA_VERSION,
) -> list[str]:
    errors: list[str] = []
    for role, target in (("candidate", binding.target), ("baseline", binding.baseline)):
        if role == "baseline" and target == binding.target:
            continue
        try:
            ref = select_target(root, target)
        except fusesoc_registry.FuseSocError as exc:
            errors.append(f"{binding.label}: {role} target {target!r}: {exc}")
            continue
        if not flow_can_drive(binding.flow, ref):
            errors.append(
                f"{binding.label}: {role} target {target!r} cannot satisfy {binding.key} "
                f"with Flow {binding.flow} (flow={ref.flow!r}, EDA tool={ref.eda_tool!r})"
            )
            continue
        missing = _missing_target_sources(root, target, schema=schema)
        if not missing:
            continue
        if role == "baseline" or (binding.relative and binding.baseline == binding.target):
            errors.append(
                f"{binding.label}: relative-QoR baseline target {target!r} has missing "
                f"source(s): {', '.join(missing)}"
            )
            continue
        undeclared = [
            path for path in missing if not _new_scope_matches(fields.get("scope"), path)
        ]
        if undeclared:
            errors.append(
                f"{binding.label}: {role} target {target!r} has missing source(s) not "
                f"declared Scope [new]: {', '.join(undeclared)}"
            )
    return errors


def validate_contract_fields(  # noqa: PLR0911 - ordered version and identity gates
    fields: Mapping[str, Any],
    project_root: Path | str | None = None,
) -> list[str]:
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
    on_success = fields.get("on_success")
    declared_removals = (
        on_success.get("remove_targets", []) if isinstance(on_success, Mapping) else []
    )
    if is_str_list(declared_removals) and tuple(declared_removals) != contract.removal_targets:
        return ["on_success.remove_targets changed after Target Contract sealing"]
    declared = set(contract.targets)
    criterion_bindings = criterion_targets(fields.get("criteria"))
    referenced = {
        target for binding in criterion_bindings for target in (binding.target, binding.baseline)
    }
    missing = sorted(referenced - declared)
    if missing:
        return [f"target_contract.targets omits criterion Target(s): {', '.join(missing)}"]
    if project_root is not None:
        try:
            expected = canonical_contract_bindings(
                project_root, criterion_bindings, schema=contract.schema
            )
        except fusesoc_registry.FuseSocError as exc:
            return [f"target_contract.bindings cannot be resolved: {exc}"]
        if expected != contract.bindings:
            return ["target_contract.bindings do not match the ticket's criterion Target pairs"]
    return []


def validate_materialized_contract(
    fields: Mapping[str, Any], project_root: Path | str
) -> list[str]:
    """Validate a sealed contract in the checkout that execution will use."""
    raw = fields.get("target_contract")
    if raw is None:
        return []
    try:
        contract = TargetContract.from_mapping(raw)
        verify_surface(contract, project_root)
    except (TargetContractError, fusesoc_registry.FuseSocError, OSError) as exc:
        return [str(exc)]
    errors = validate_contract_fields(fields, project_root)
    if errors:
        return errors
    try:
        return validate_criterion_targets(fields, project_root)
    except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
        return [str(exc)]


def canonical_contract_bindings(
    project_root: Path | str,
    bindings: Iterable[CriterionTarget],
    *,
    schema: int = SCHEMA_VERSION,
) -> tuple[ContractTargetBinding, ...]:
    """Resolve bindings to durable identities and current callable selectors."""
    root = Path(project_root)
    rows: set[ContractTargetBinding] = set()
    for binding in bindings:
        baseline = select_target(root, binding.baseline)
        candidate = select_target(root, binding.target)
        rows.add(
            ContractTargetBinding(
                flow=binding.flow,
                criterion=binding.key,
                baseline=baseline.identity,
                candidate=candidate.identity,
                baseline_selector=(baseline.selector if schema >= SCHEMA_VERSION else ""),
                candidate_selector=(candidate.selector if schema >= SCHEMA_VERSION else ""),
            )
        )
    return tuple(sorted(rows))


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
    actual = surface_digest(project_root, schema=contract.schema, targets=contract.targets)
    if actual != contract.surface_digest:
        raise TargetContractError(
            f"{CONTRACT_BLOCK_REASON}: Target surface digest is {actual}, "
            f"expected {contract.surface_digest}"
        )


def load_ticket_contract(ticket_path: Path | str) -> TargetContract | None:
    """Read a supported contract from a ticket file, or None when unsealed."""
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
    removal_targets: Iterable[str] = (),
    bindings: Iterable[CriterionTarget] = (),
    participants: Iterable[ContractParticipant],
) -> TargetContract:
    """Build a sealed contract from already committed repository state."""
    sealed_targets = tuple(sorted(set(targets)))
    return TargetContract(
        outer_sha=outer_sha.lower(),
        project_sha=project_sha.lower(),
        surface_digest=surface_digest(project_root, targets=sealed_targets),
        targets=sealed_targets,
        removal_targets=tuple(sorted(set(removal_targets))),
        bindings=canonical_contract_bindings(project_root, bindings),
        participants=tuple(sorted(set(participants))),
        surface_entries=surface_entries(project_root),
        schema=SCHEMA_VERSION,
    )
