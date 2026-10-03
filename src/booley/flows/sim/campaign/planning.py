"""Pure planning helpers for durable Simulation Campaign workloads."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
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


class _DiagnosticProjection:
    def __init__(self, findings: tuple[WorkloadMismatch, ...]) -> None:
        self.findings = findings
        self.roots: list[str] = []
        self.explained: set[int] = set()
        self.derived: set[int] = set()
        self.fallback: set[int] = set()

    def indices(self, prefix: str) -> set[int]:
        return {
            index
            for index, item in enumerate(self.findings)
            if item.pointer == prefix or item.pointer.startswith(prefix + "/")
        }

    def explain(self, indices: set[int], lines: Sequence[str]) -> None:
        self.explained.update(indices)
        self.roots.extend(line for line in lines if line not in self.roots)

    def finish(self) -> WorkloadDiagnostic:
        for index, item in enumerate(self.findings):
            if index not in self.explained | self.derived:
                self.fallback.add(index)
                self.roots.append(f"workload changed: {item.message}")
        assert not (
            self.explained & self.derived
            | self.explained & self.fallback
            | self.derived & self.fallback
        )
        assert self.explained | self.derived | self.fallback == set(range(len(self.findings)))
        return WorkloadDiagnostic(self.findings, tuple(self.roots), len(self.derived))


def project_workload_mismatches(
    expected: SimulationCampaignManifest,
    current: SimulationCampaignManifest,
    findings: tuple[WorkloadMismatch, ...] | None = None,
) -> WorkloadDiagnostic:
    """Summarize validated values without changing comparison or reading files."""
    projection = _DiagnosticProjection(
        compare_manifests(expected, current) if findings is None else findings
    )
    if not projection.findings:
        return projection.finish()
    left, right = expected.document, current.document
    old_workload, new_workload = left["workload"], right["workload"]
    old_recipe, new_recipe = old_workload["source_recipe"], new_workload["source_recipe"]
    _project_sources(projection, old_recipe["sources"], new_recipe["sources"])
    _project_parameters(projection, old_recipe["parameters"], new_recipe["parameters"])
    _project_runtime(projection, old_workload["runtime_inputs"], new_workload["runtime_inputs"])
    _project_suite(projection, left["required_suite"], right["required_suite"])
    _project_prepared_sources(projection, left, right)
    _project_variants(projection, left, right)
    _project_items(projection, left, right)
    _project_command_model(projection, left, right)
    return projection.finish()


def _label(value: object) -> str:
    text = str(value)
    return _display(text) if any(not char.isprintable() for char in text) else text


def _diagnostic_value(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8").rstrip("\n")


def _entry_map(entries: Sequence[Mapping[str, object]], key: str):
    names = [entry[key] for entry in entries]
    if len(set(names)) != len(names):
        return None
    return {entry[key]: entry for entry in entries}


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


def _project_sources(projection, old, new) -> None:
    prefix = "/workload/source_recipe/sources"
    indices = projection.indices(prefix)
    if not indices:
        return
    old_map, new_map = _entry_map(old, "path"), _entry_map(new, "path")
    if old_map is None or new_map is None:
        projection.roots.append("source paths ambiguous: duplicate source path")
        return
    projection.explain(indices, _named_changes(old_map, new_map, kind="source"))


def _project_parameters(projection, old, new) -> None:
    indices = projection.indices("/workload/source_recipe/parameters")
    old_map, new_map = _entry_map(old, "name"), _entry_map(new, "name")
    lines = []
    for name in sorted(set(old_map) | set(new_map)):
        old_value = _diagnostic_value(old_map[name]["value"]) if name in old_map else "<absent>"
        new_value = _diagnostic_value(new_map[name]["value"]) if name in new_map else "<absent>"
        if name not in old_map or name not in new_map or old_value != new_value:
            lines.append(f"parameter {_label(name)}: {old_value} → {new_value}")
    if not lines and tuple(old_map) != tuple(new_map):
        lines.append("parameter order changed")
    projection.explain(indices, lines)


def _known_preparation_pair(old, new) -> bool:
    old_provenance, new_provenance = old["tool_provenance"], new["tool_provenance"]
    old_entries, new_entries = old["generated_files"], new["generated_files"]
    return (
        old["planner"] == new["planner"] == "fusesoc_setup"
        and old_provenance["kind"] == new_provenance["kind"] == "fusesoc"
        and old_provenance["contract_version"] == new_provenance["contract_version"] == "1"
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
    return all(entry[field] == source[field] for field in ("path", "bytes", "sha256"))


def _prepared_existing_cause(projection, before, after, sources) -> bool:
    old_sources, new_sources = sources
    path = before["path"]
    return (
        old_sources is not None
        and new_sources is not None
        and path in old_sources
        and path in new_sources
        and f"source changed: {_label(path)}" in projection.roots
        and _prepared_source_matches(before, old_sources[path])
        and _prepared_source_matches(after, new_sources[path])
    )


def _prepared_leaf_indices(projection, disclosure_position, entry_position) -> set[int]:
    prefix = f"/planning_disclosures/{disclosure_position}/generated_files/{entry_position}"
    pointers = {f"{prefix}/bytes", f"{prefix}/sha256"}
    return {
        index for index, finding in enumerate(projection.findings) if finding.pointer in pointers
    }


def _collect_prepared_entries(projection, position, old, new, sources, groups) -> None:
    for entry_position, (before, after) in enumerate(zip(old, new, strict=True)):
        if before["kind"] != "generated_input" or after["kind"] != "generated_input":
            continue
        if {key: value for key, value in before.items() if key not in {"bytes", "sha256"}} != {
            key: value for key, value in after.items() if key not in {"bytes", "sha256"}
        }:
            continue
        indices = _prepared_leaf_indices(projection, position, entry_position)
        if not indices:
            continue
        if _prepared_existing_cause(projection, before, after, sources):
            projection.explain(indices, ())
            continue
        key = (before["path"], before["bytes"], before["sha256"], after["bytes"], after["sha256"])
        grouped_indices, positions = groups.setdefault(key, (set(), set()))
        grouped_indices.update(indices)
        positions.add(position)


def _render_prepared_groups(projection, groups) -> None:
    path_counts = Counter(key[0] for key in groups)
    rendered = {}
    for key, (_indices, positions) in groups.items():
        line = f"prepared source changed: {_label(key[0])}"
        if path_counts[key[0]] > 1:
            noun = "planning disclosure" if len(positions) == 1 else "planning disclosures"
            line += f" ({noun} {', '.join(str(position) for position in sorted(positions))})"
        rendered[key] = line
    line_counts = Counter(rendered.values())
    for key, line in rendered.items():
        if line_counts[line] == 1 and line not in projection.roots:
            projection.explain(groups[key][0], (line,))


def _project_prepared_sources(projection, left, right) -> None:
    sources = (
        _entry_map(left["workload"]["source_recipe"]["sources"], "path"),
        _entry_map(right["workload"]["source_recipe"]["sources"], "path"),
    )
    groups = {}
    for position, (old, new) in enumerate(
        zip(left["planning_disclosures"], right["planning_disclosures"], strict=False)
    ):
        if _known_preparation_pair(old, new):
            _collect_prepared_entries(
                projection,
                position,
                old["generated_files"],
                new["generated_files"],
                sources,
                groups,
            )
    _render_prepared_groups(projection, groups)


def _project_runtime(projection, old, new) -> None:
    prefix = "/workload/runtime_inputs"
    indices = projection.indices(prefix)
    if not indices:
        return
    old_map, new_map = _entry_map(old, "destination"), _entry_map(new, "destination")
    lines = _named_changes(
        old_map, new_map, kind="runtime input", ignored=frozenset({"declaration_id"})
    )
    if not lines:
        return
    for index in indices:
        finding = projection.findings[index]
        if finding.pointer.endswith("/declaration_id"):
            position = int(finding.pointer.split("/")[-2])
            if (
                position < len(old)
                and position < len(new)
                and old[position]["destination"] == new[position]["destination"]
            ):
                destination = old[position]["destination"]
                old_declaration = {
                    key: value
                    for key, value in old_map[destination].items()
                    if key != "declaration_id"
                }
                new_declaration = {
                    key: value
                    for key, value in new_map[destination].items()
                    if key != "declaration_id"
                }
                if old_declaration != new_declaration:
                    projection.derived.add(index)
    projection.explain(indices - projection.derived, lines)


def _project_suite(projection, old, new) -> None:
    indices = projection.indices("/required_suite")
    if not indices:
        return
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
    projection.explain(indices, lines)


def _project_variants(projection, left, right) -> None:
    old_sources = left["workload"]["source_recipe"]["sources"]
    new_sources = right["workload"]["source_recipe"]["sources"]
    for position, (old, new) in enumerate(
        zip(left["build_variants"], right["build_variants"], strict=False)
    ):
        prefix = f"/build_variants/{position}"
        for field in ("build_variant_id", "recipe_sha256"):
            projection.derived.update(projection.indices(f"{prefix}/{field}"))
        indices = projection.indices(f"{prefix}/source_closure")
        if not indices:
            continue
        if (
            old["source_closure"] == old_sources
            and new["source_closure"] == new_sources
            and _entry_map(old_sources, "path") is not None
            and _entry_map(new_sources, "path") is not None
        ):
            projection.explain(indices, ())
        elif (
            _entry_map(old["source_closure"], "path") is None
            or _entry_map(new["source_closure"], "path") is None
        ) and "source paths ambiguous: duplicate source path" not in projection.roots:
            projection.roots.append("source paths ambiguous: duplicate source path")


def _project_items(projection, left, right) -> None:
    old_variants = [entry["build_variant_id"] for entry in left["build_variants"]]
    new_variants = [entry["build_variant_id"] for entry in right["build_variants"]]
    for position, (old, new) in enumerate(
        zip(left["work_items"], right["work_items"], strict=False)
    ):
        prefix = f"/work_items/{position}"
        for field in ("work_item_id", "fingerprint_sha256"):
            projection.derived.update(projection.indices(f"{prefix}/{field}"))
        if (
            old["build_variant_id"] in old_variants
            and new["build_variant_id"] in new_variants
            and old_variants.index(old["build_variant_id"])
            == new_variants.index(new["build_variant_id"])
        ):
            projection.derived.update(projection.indices(f"{prefix}/build_variant_id"))
        for field in ("target", "revision"):
            old_value = left["target"] if field == "target" else left["target"]["revision"]
            new_value = right["target"] if field == "target" else right["target"]["revision"]
            if old[field] == old_value and new[field] == new_value:
                projection.explain(projection.indices(f"{prefix}/{field}"), ())


def _project_command_model(projection, left, right) -> None:
    indices = projection.indices("/workload/build_recipe/command_model_sha256")
    if not indices:
        return
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
        projection.derived.update(indices)
    else:
        projection.explain(indices, ("build command model changed",))


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
        normalized = _relative_path(destination, field="runtime input destination")
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
