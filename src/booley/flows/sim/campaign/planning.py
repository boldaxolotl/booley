"""Pure planning helpers for durable Simulation Campaign workloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.flows.sim.runtime_inputs import preview_runtime_inputs
from booley.targets.domain import TargetInput

from .codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
    validate_relative_path,
)
from .model import SimulationCampaignManifest


@dataclass(frozen=True, slots=True)
class WorkloadMismatch:
    """One stable pointer/value difference found while validating resume."""

    pointer: str
    expected: object
    actual: object

    @property
    def message(self) -> str:
        return (
            f"{self.pointer}: manifest has {_display(self.expected)}, "
            f"current workload has {_display(self.actual)}"
        )


@dataclass(frozen=True, slots=True)
class WorkloadDiagnostic:
    """Presentation of an unchanged, complete workload comparison."""

    mismatches: tuple[WorkloadMismatch, ...]
    mismatch_summary: tuple[str, ...]
    derived_fingerprint_count: int

    @property
    def detail(self) -> dict[str, object]:
        return {
            "mismatches": [item.message for item in self.mismatches],
            "mismatch_summary": list(self.mismatch_summary),
            "derived_fingerprint_count": self.derived_fingerprint_count,
        }

    def report(self, *, verbose: bool = False) -> str:
        lines = ["campaign workload mismatch:", *self.mismatch_summary]
        if not self.mismatch_summary:
            lines.append("no root cause identified")
        lines.append(f"{self.derived_fingerprint_count} derived fingerprints differ")
        if verbose:
            lines.extend(item.message for item in self.mismatches)
        return "\n".join(lines)


class SimulationCampaignWorkloadMismatchError(SimulationCampaignIntegrityError):
    """Resume refusal retaining every original difference and its presentation."""

    def __init__(self, diagnostic: WorkloadDiagnostic) -> None:
        super().__init__(diagnostic.report())
        self.diagnostic = diagnostic


_EXPLAINED, _DERIVED = "explained", "derived"
_AMBIGUOUS_SOURCES = "source paths ambiguous: duplicate source path"


@dataclass(slots=True)
class _Projection:
    """Named causes in category order plus the regions whose raw leaves they cover."""

    documents: tuple[Mapping[str, Any], Mapping[str, Any]]
    regions: frozenset[tuple[str, ...]]
    roots: list[str]
    resolved: dict[tuple[str, ...], str]

    def explain(self, region: tuple[str, ...], lines: Sequence[str] = ()) -> None:
        self.resolved[region] = _EXPLAINED
        self.roots.extend(lines)


def project_workload_mismatches(
    expected: SimulationCampaignManifest,
    current: SimulationCampaignManifest,
    findings: tuple[WorkloadMismatch, ...] | None = None,
) -> WorkloadDiagnostic:
    """Summarize validated values without changing comparison or reading files."""
    findings = compare_manifests(expected, current) if findings is None else findings
    if not findings:
        return WorkloadDiagnostic(findings, (), 0)
    keys = [_region(item.pointer) for item in findings]
    regions = frozenset(key[:size] for key in keys if key for size in (1, len(key)))
    projection = _Projection((expected.document, current.document), regions, [], {})
    for project in _PROJECTORS:
        project(projection)
    resolved = projection.resolved
    kinds = [resolved.get(key, resolved.get(key[:1])) if key else None for key in keys]
    fallback = [
        f"workload changed: {item.message}"
        for item, kind in zip(findings, kinds, strict=True)
        if kind is None
    ]
    roots = (*dict.fromkeys(projection.roots), *fallback)
    return WorkloadDiagnostic(findings, roots, kinds.count(_DERIVED))


def _region(pointer: str) -> tuple[str, ...] | None:
    """Name the manifest region whose facts can account for one raw pointer."""
    region = None
    match pointer.split("/")[1:]:
        case ["workload", "source_recipe", ("sources" | "parameters") as name, *_]:
            region = (name,)
        case ["workload", "source_recipe", "defines", *_]:
            region = ("defines",)
        case ["workload", "runtime_inputs", position, "declaration_id"]:
            region = ("runtime_inputs", position)
        case ["workload", "runtime_inputs", *_]:
            region = ("runtime_inputs",)
        case ["workload", "build_recipe", "command_model_sha256", *_]:
            region = ("command_model",)
        case ["required_suite", *_]:
            region = ("required_suite",)
        case ["planning_disclosures", position, "generated_files", entry, "bytes" | "sha256"]:
            region = ("prepared", position, entry)
        case [("build_variants" | "work_items") as name, position, member, *_]:
            region = (name, position, member)
    return region


def _label(value: object) -> str:
    text = str(value)
    return _display(text) if any(not char.isprintable() for char in text) else text


def _diagnostic_value(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8").rstrip("\n")


def _diagnostic_display(text: str) -> str:
    if text == "<absent>":
        return text
    return json.dumps(json.loads(text), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _entry_map(entries: Sequence[Mapping[str, object]], key: str):
    names = [entry[key] for entry in entries]
    if len(set(names)) != len(names):
        return None
    return {entry[key]: entry for entry in entries}


def _source_maps(projection: _Projection):
    return tuple(
        _entry_map(document["workload"]["source_recipe"]["sources"], "path")
        for document in projection.documents
    )


def _named_changes(old, new, *, kind: str, ignored: frozenset[str] = frozenset()):
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            lines.append(f"{kind} added: {_label(name)}")
        elif name not in new:
            lines.append(f"{kind} removed: {_label(name)}")
        elif {key: value for key, value in old[name].items() if key not in ignored} != {
            key: value for key, value in new[name].items() if key not in ignored
        }:
            lines.append(f"{kind} changed: {_label(name)}")
    if not lines and tuple(old) != tuple(new):
        lines.append(f"{kind} order changed")
    return lines


def _project_sources(projection: _Projection) -> None:
    if ("sources",) not in projection.regions:
        return
    old_map, new_map = _source_maps(projection)
    if old_map is None or new_map is None:
        projection.roots.append(_AMBIGUOUS_SOURCES)
        return
    projection.explain(("sources",), _named_changes(old_map, new_map, kind="source"))


def _project_parameters(projection: _Projection) -> None:
    old_map, new_map = (
        _entry_map(document["workload"]["source_recipe"]["parameters"], "name")
        for document in projection.documents
    )
    lines = []
    for name in sorted(set(old_map) | set(new_map)):
        old_value = _diagnostic_value(old_map[name]["value"]) if name in old_map else "<absent>"
        new_value = _diagnostic_value(new_map[name]["value"]) if name in new_map else "<absent>"
        if old_value != new_value:
            old_display, new_display = (
                _diagnostic_display(old_value),
                _diagnostic_display(new_value),
            )
            lines.append(f"parameter {_label(name)}: {old_display} → {new_display}")
    if not lines and tuple(old_map) != tuple(new_map):
        lines.append("parameter order changed")
    projection.explain(("parameters",), lines)


def _project_defines(projection: _Projection) -> None:
    """Explain a legacy Campaign that recorded the unsupported `flow_options.defines`."""
    if ("defines",) not in projection.regions:
        return
    projection.explain(
        ("defines",),
        (
            "recorded flow_options.defines is no longer recorded (no Flow applies it); "
            "re-plan the Campaign",
        ),
    )


def _known_preparation_pair(old, new) -> bool:
    old_provenance, new_provenance = old["tool_provenance"], new["tool_provenance"]
    old_entries, new_entries = old["generated_files"], new["generated_files"]
    return (
        old["planner"] == new["planner"] == "fusesoc_setup"
        and old_provenance["kind"] == new_provenance["kind"] == "fusesoc"
        and old_provenance["contract_version"] == new_provenance["contract_version"]
        and old_provenance["contract_version"] in {"1", "2"}
        and old_provenance["version"] == new_provenance["version"]
        and _diagnostic_value(
            {key: value for key, value in old.items() if key != "generated_files"}
        )
        == _diagnostic_value(
            {key: value for key, value in new.items() if key != "generated_files"}
        )
        and _entry_map(old_entries, "path") is not None
        and _entry_map(new_entries, "path") is not None
        and [entry["path"] for entry in old_entries] == [entry["path"] for entry in new_entries]
    )


def _prepared_source_matches(entry, source) -> bool:
    return entry.get("source_path", entry["path"]) == source["path"] and all(
        entry[field] == source[field] for field in ("bytes", "sha256")
    )


def _replicates_source_change(before, after, sources) -> bool:
    path = before.get("source_path", before["path"])
    old_source, new_source = (
        None if entries is None else entries.get(path) for entries in sources
    )
    return (
        old_source is not None
        and new_source is not None
        and old_source != new_source
        and _prepared_source_matches(before, old_source)
        and _prepared_source_matches(after, new_source)
    )


def _prepared_changes(projection: _Projection):
    left, right = projection.documents
    for position, (old, new) in enumerate(
        zip(left["planning_disclosures"], right["planning_disclosures"], strict=False)
    ):
        if not _known_preparation_pair(old, new):
            continue
        for entry_position, (before, after) in enumerate(
            zip(old["generated_files"], new["generated_files"], strict=True)
        ):
            if before["kind"] != "generated_input" or after["kind"] != "generated_input":
                continue
            if {k: v for k, v in before.items() if k not in {"bytes", "sha256"}} != {
                k: v for k, v in after.items() if k not in {"bytes", "sha256"}
            }:
                continue
            region = ("prepared", str(position), str(entry_position))
            if region in projection.regions:
                yield region, position, before, after


def _project_prepared_sources(projection: _Projection) -> None:
    sources = _source_maps(projection)
    observations, paths = {}, {}
    for region, position, before, after in _prepared_changes(projection):
        if _replicates_source_change(before, after, sources):
            projection.explain(region)
            continue
        key = (before["path"], before["bytes"], before["sha256"], after["bytes"], after["sha256"])
        regions, positions = observations.setdefault(key, ([], set()))
        regions.append(region)
        positions.add(position)
        paths.setdefault(before["path"], set()).add(key)
    labels = {}
    for key, (regions, positions) in observations.items():
        line = f"prepared source changed: {_label(key[0])}"
        if len(paths[key[0]]) > 1:
            noun = "planning disclosure" if len(positions) == 1 else "planning disclosures"
            line += f" ({noun} {', '.join(str(position) for position in sorted(positions))})"
        labels.setdefault(line, []).append(regions)
    for line, candidates in labels.items():
        if len(candidates) == 1:
            projection.roots.append(line)
            projection.resolved.update(dict.fromkeys(candidates[0], _EXPLAINED))


def _project_runtime(projection: _Projection) -> None:
    if ("runtime_inputs",) not in projection.regions:
        return
    old, new = (document["workload"]["runtime_inputs"] for document in projection.documents)
    old_map, new_map = _entry_map(old, "destination"), _entry_map(new, "destination")
    lines = _named_changes(
        old_map, new_map, kind="runtime input", ignored=frozenset({"declaration_id"})
    )
    projection.explain(("runtime_inputs",), lines)
    for position, (before, after) in enumerate(zip(old, new, strict=False)):
        # An ID that changed at an unchanged destination only follows its
        # declaration, so it is derived, not a separate change.
        if before["destination"] == after["destination"]:
            projection.resolved[("runtime_inputs", str(position))] = _DERIVED


def _project_suite(projection: _Projection) -> None:
    if ("required_suite",) not in projection.regions:
        return
    old, new = (document["required_suite"] for document in projection.documents)
    old_names, new_names = old["names"], new["names"]
    added = [f"+{_label(name)}" for name in new_names if name not in old_names]
    removed = [f"-{_label(name)}" for name in old_names if name not in new_names]
    lines = []
    if added or removed:
        lines.append("suite changed: " + " ".join([*added, *removed]))
    elif old_names != new_names:
        lines.append("suite order changed")
    if any(
        old[key] != new[key]
        for key in ("source_path", "source_bytes", "source_sha256", "default_invocation")
    ):
        lines.append(
            f"suite changed: {_label(new['source_path'] or old['source_path'] or '<default invocation>')}"
        )
    projection.explain(("required_suite",), lines)


def _project_variants(projection: _Projection) -> None:
    left, right = projection.documents
    old_sources = left["workload"]["source_recipe"]["sources"]
    new_sources = right["workload"]["source_recipe"]["sources"]
    unique = all(entries is not None for entries in _source_maps(projection))
    for position, (old, new) in enumerate(
        zip(left["build_variants"], right["build_variants"], strict=False)
    ):
        prefix = ("build_variants", str(position))
        for member in ("build_variant_id", "recipe_sha256"):
            projection.resolved[(*prefix, member)] = _DERIVED
        if (*prefix, "source_closure") not in projection.regions:
            continue
        if (
            unique
            and old["source_closure"] == old_sources
            and new["source_closure"] == new_sources
        ):
            projection.explain((*prefix, "source_closure"))
        elif (
            _entry_map(old["source_closure"], "path") is None
            or _entry_map(new["source_closure"], "path") is None
        ):
            projection.roots.append(_AMBIGUOUS_SOURCES)


def _project_items(projection: _Projection) -> None:
    left, right = projection.documents
    old_variants = [entry["build_variant_id"] for entry in left["build_variants"]]
    new_variants = [entry["build_variant_id"] for entry in right["build_variants"]]
    for position, (old, new) in enumerate(
        zip(left["work_items"], right["work_items"], strict=False)
    ):
        prefix = ("work_items", str(position))
        for member in ("work_item_id", "fingerprint_sha256"):
            projection.resolved[(*prefix, member)] = _DERIVED
        if (
            old["build_variant_id"] in old_variants
            and new["build_variant_id"] in new_variants
            and old_variants.index(old["build_variant_id"])
            == new_variants.index(new["build_variant_id"])
        ):
            projection.resolved[(*prefix, "build_variant_id")] = _DERIVED
        for member in ("target", "revision"):
            old_value = left["target"] if member == "target" else left["target"]["revision"]
            new_value = right["target"] if member == "target" else right["target"]["revision"]
            if old[member] == old_value and new[member] == new_value:
                projection.explain((*prefix, member))


def _project_command_model(projection: _Projection) -> None:
    if ("command_model",) not in projection.regions:
        return
    left, right = projection.documents
    old, new = left["workload"], right["workload"]
    concrete = any(
        _diagnostic_value(old["source_recipe"][field])
        != _diagnostic_value(new["source_recipe"][field])
        for field in ("parameters", "defines")
    )
    concrete |= any(
        old["build_recipe"][field] != new["build_recipe"][field]
        for field in ("toplevel", "eda_tool")
    )
    concrete |= (
        old["trace"] != new["trace"]
        or left["target"]["vlnv"] != right["target"]["vlnv"]
        or left["target"]["name"] != right["target"]["name"]
    )
    if concrete:
        projection.resolved[("command_model",)] = _DERIVED
    else:
        projection.explain(("command_model",), ("build command model changed",))


_PROJECTORS = (
    _project_sources,
    _project_parameters,
    _project_defines,
    _project_runtime,
    _project_suite,
    _project_prepared_sources,
    _project_variants,
    _project_items,
    _project_command_model,
)


def canonical_sha256(value: object) -> str:
    """Hash one canonical JSON value without the document trailing newline."""
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()


def derive_runtime_input_declarations(
    inputs: Sequence[TargetInput],
) -> tuple[Mapping[str, str], ...]:
    """Derive ordered workload declarations from Target ``user`` copy inputs."""
    destinations = preview_runtime_inputs(inputs)
    declarations: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for destination in destinations:
        try:
            normalized = validate_relative_path(
                _relative_path(destination, field="runtime input destination")
            )
        except SimulationCampaignIntegrityError as exc:
            raise SimulationCampaignIntegrityError(
                f"Target input copyto destination {destination!r} must be a relative path "
                f"inside the run directory ({exc}); fix copyto in the Target's .core file"
            ) from exc
        if normalized in seen:
            raise SimulationCampaignIntegrityError(
                f"duplicate runtime input destination {normalized!r}"
            )
        seen.add(normalized)
        identity = {
            "source_artifact_path": normalized,
            "destination": normalized,
        }
        declarations.append({"declaration_id": canonical_sha256(identity), **identity})
    return tuple(declarations)


def finalize_manifest(document: Mapping[str, object]) -> SimulationCampaignManifest:
    """Compute every component fingerprint and return a strictly validated manifest."""
    mutable = _json_copy(document)
    required = {
        "$schema",
        "campaign_id",
        "created_at",
        "origin",
        "target",
        "workload",
        "required_suite",
        "build_variants",
        "planning_disclosures",
        "prerequisites",
        "work_items",
    }
    if set(mutable) != required:
        raise SimulationCampaignIntegrityError(
            f"manifest planning input must have exact fields {sorted(required)}"
        )
    mutable["fingerprints"] = _manifest_fingerprints(mutable)
    raw = canonical_json_bytes(mutable)
    return decode_simulation_campaign_manifest(raw)


def _manifest_fingerprints(mutable: Mapping[str, object]) -> dict[str, str]:
    workload = cast(Mapping[str, object], mutable["workload"])
    variants = cast(Sequence[Mapping[str, object]], mutable["build_variants"])
    return {
        "target_recipe_sha256": canonical_sha256(
            {
                "target": mutable["target"],
                "source_recipe": workload["source_recipe"],
                "build_recipe": workload["build_recipe"],
            }
        ),
        "source_closures_sha256": canonical_sha256(
            [
                {
                    "build_variant_id": variant["build_variant_id"],
                    "source_closure": variant["source_closure"],
                }
                for variant in variants
            ]
        ),
        "required_suite_sha256": canonical_sha256(mutable["required_suite"]),
        "planning_disclosures_sha256": canonical_sha256(mutable["planning_disclosures"]),
        "prerequisites_sha256": canonical_sha256(mutable["prerequisites"]),
        "work_items_sha256": canonical_sha256(mutable["work_items"]),
        "workload_sha256": canonical_sha256(
            {
                key: mutable[key]
                for key in (
                    "target",
                    "workload",
                    "required_suite",
                    "build_variants",
                    "planning_disclosures",
                    "prerequisites",
                    "work_items",
                )
            }
        ),
    }


def compare_manifests(
    expected: SimulationCampaignManifest,
    current: SimulationCampaignManifest,
) -> tuple[WorkloadMismatch, ...]:
    """Return all workload-identity mismatches in deterministic pointer order."""
    left = _identity_document(expected)
    right = _identity_document(current)
    findings: list[WorkloadMismatch] = []
    _compare_value(left, right, "", findings)
    return tuple(findings)


def verify_workload(
    expected: SimulationCampaignManifest,
    current: SimulationCampaignManifest,
) -> None:
    """Reject resume with one stable complete mismatch report."""
    findings = compare_manifests(expected, current)
    if findings:
        raise SimulationCampaignWorkloadMismatchError(
            project_workload_mismatches(expected, current, findings)
        )


def manifest_digest(manifest: SimulationCampaignManifest) -> str:
    """Return the canonical digest used by durable records."""
    raw = encode_simulation_campaign_manifest(manifest)
    return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()


def _identity_document(manifest: SimulationCampaignManifest) -> Mapping[str, object]:
    document = manifest.document
    return {
        key: document[key]
        for key in (
            "$schema",
            "target",
            "workload",
            "required_suite",
            "build_variants",
            "planning_disclosures",
            "prerequisites",
            "work_items",
        )
    }


def _compare_value(
    expected: object,
    actual: object,
    pointer: str,
    findings: list[WorkloadMismatch],
) -> None:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        for key in sorted(set(expected) | set(actual)):
            child = f"{pointer}/{_escape_pointer(str(key))}"
            if key not in expected:
                findings.append(WorkloadMismatch(child, "<absent>", actual[key]))
            elif key not in actual:
                findings.append(WorkloadMismatch(child, expected[key], "<absent>"))
            else:
                _compare_value(expected[key], actual[key], child, findings)
        return
    if isinstance(expected, tuple | list) and isinstance(actual, tuple | list):
        common = min(len(expected), len(actual))
        for index in range(common):
            _compare_value(expected[index], actual[index], f"{pointer}/{index}", findings)
        for index in range(common, max(len(expected), len(actual))):
            left = expected[index] if index < len(expected) else "<absent>"
            right = actual[index] if index < len(actual) else "<absent>"
            findings.append(WorkloadMismatch(f"{pointer}/{index}", left, right))
        return
    if expected != actual:
        findings.append(WorkloadMismatch(pointer or "/", expected, actual))


def _relative_path(value: str, *, field: str) -> str:
    path = Path(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SimulationCampaignIntegrityError(f"invalid {field} {value!r}")
    return path.as_posix()


def _json_copy(value: object) -> Any:
    return json.loads(canonical_json_bytes(value))


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _display(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "SimulationCampaignWorkloadMismatchError",
    "WorkloadDiagnostic",
    "WorkloadMismatch",
    "canonical_sha256",
    "compare_manifests",
    "derive_runtime_input_declarations",
    "finalize_manifest",
    "manifest_digest",
    "project_workload_mismatches",
    "verify_workload",
]
