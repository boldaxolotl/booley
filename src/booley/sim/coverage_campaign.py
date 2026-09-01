"""Immutable, validated Coverage Campaign document model.

The codec is deliberately independent of filesystems, simulation orchestration,
policy evaluation, and Verilator's native coverage format.  Its input is an
already-read JSON value and its output is one immutable per-Target campaign.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import NewType, TypeAlias

DurableTargetIdentity = NewType("DurableTargetIdentity", str)

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

_SCHEMA = "booley.coverage-campaign/v1"
_SCORED_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})
_METRIC_SEMANTICS = {
    "line": "One Verilator basic-block point; covered when its count is greater than zero.",
    "branch": "One branch outcome; each outcome is a separate point covered when count is greater than zero.",
    "expression": "One collector-reported expression outcome; each outcome is a separate point covered when count is greater than zero.",
    "toggle": "One direction for one signal bit; 0_to_1 and 1_to_0 are separate points.",
    "cover_property": "One user-authored cover-property point; covered when count is greater than zero.",
}
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_TOP_LEVEL_FIELDS = (
    ("$schema", "string"),
    ("campaign_id", "string"),
    ("invocation", "object"),
    ("target", "object"),
    ("collector", "object"),
    ("build", "object"),
    ("coverage_window", "object"),
    ("fingerprints", "object"),
    ("source_closure", "object"),
    ("tests", "object"),
    ("artifacts", "array"),
    ("normalization", "object"),
    ("points", "array"),
    ("rollups", "array"),
    ("collection", "object"),
    ("findings", "array"),
    ("evaluation", "object"),
)


def _freeze(value: object) -> FrozenJson:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(value[key]) for key in sorted(value, key=str)})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"coverage campaign contains non-JSON value {type(value).__name__}")


def _freeze_mapping(value: object) -> Mapping[str, FrozenJson]:
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("coverage campaign object must be a JSON object")
    return frozen


def _thaw(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class CoverageTarget:
    """Durable identity and display selector for the Campaign's one Target."""

    identity: str
    selector: str


@dataclass(frozen=True)
class CoverageCapability:
    """Collector support fact for one native coverage record class."""

    record_class: str
    status: str
    attributes: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CoverageCollector:
    """Exact collector identity, native format, and reported capabilities."""

    kind: str
    version: Mapping[str, FrozenJson]
    native_format: Mapping[str, FrozenJson]
    capabilities: tuple[CoverageCapability, ...]


@dataclass(frozen=True)
class CoverageRun:
    """Independent simulation and collection truth for one selected test."""

    id: str
    test: str
    simulation_verdict: str
    collection: str
    raw_artifact: str | None
    attributes: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CoverageArtifact:
    """One report-root-relative native or hook artifact reference."""

    id: str
    kind: str
    path: str
    sha256: str
    bytes: int
    state: str
    owner_run: str | None
    attributes: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CoverageFinding:
    """Stable structured validation or collection finding."""

    severity: str
    code: str
    pointer: str
    message: str


class CoverageCampaignValidationError(ValueError):
    """All stable findings produced while decoding one invalid Campaign."""

    def __init__(self, findings: tuple[CoverageFinding, ...]) -> None:
        self.findings = findings
        super().__init__(f"coverage campaign is invalid ({len(findings)} findings)")


@dataclass(frozen=True)
class CoveragePointIdentity:
    """Lossless most-specific identity for one native measurement point."""

    metric: str
    location: Mapping[str, FrozenJson]
    hierarchy: str
    subject: Mapping[str, FrozenJson]
    collector: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CoveragePoint:
    """One normalized observation with sparse positive per-run incidence."""

    id: str
    identity: CoveragePointIdentity
    hits_by_run: Mapping[str, int]
    disposition: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CoverageRollup:
    """Stored deterministic summary recomputed from Coverage Points."""

    metric: str
    semantics: str
    total_points: int
    eligible_points: int
    covered_points: int
    waived_points: int
    percent: float | None


@dataclass(frozen=True)
class CoverageCampaign:
    """One indivisible normalized coverage record for one Target invocation."""

    schema: str
    campaign_id: str
    invocation: Mapping[str, FrozenJson]
    target: CoverageTarget
    collector: CoverageCollector
    build: Mapping[str, FrozenJson]
    coverage_window: Mapping[str, FrozenJson]
    fingerprints: Mapping[str, FrozenJson]
    source_closure: Mapping[str, FrozenJson]
    declared_tests: tuple[str, ...]
    selected_tests: tuple[str, ...]
    runs: tuple[CoverageRun, ...]
    artifacts: tuple[CoverageArtifact, ...]
    normalization: Mapping[str, FrozenJson]
    points: tuple[CoveragePoint, ...]
    rollups: tuple[CoverageRollup, ...]
    collection: Mapping[str, FrozenJson]
    findings: tuple[CoverageFinding, ...]
    evaluation: Mapping[str, FrozenJson]


def _error(code: str, pointer: str, message: str) -> CoverageFinding:
    return CoverageFinding("error", code, pointer, message)


def _matches_json_type(value: object, expected: str) -> bool:
    matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number_or_null": value is None
        or (isinstance(value, int | float) and not isinstance(value, bool)),
        "string_or_null": value is None or isinstance(value, str),
        "boolean": isinstance(value, bool),
    }
    if expected not in matches:
        raise AssertionError(f"unknown JSON type {expected}")
    return matches[expected]


def _check_field(
    document: Mapping[str, object],
    key: str,
    expected: str,
    parent: str,
    findings: list[CoverageFinding],
) -> object | None:
    pointer = f"{parent}/{_pointer_token(key)}"
    if key not in document:
        findings.append(
            _error("COV_FIELD_REQUIRED", pointer, f"Required field {key!r} is missing.")
        )
        return None
    value = document[key]
    if not _matches_json_type(value, expected):
        findings.append(
            _error("COV_FIELD_TYPE", pointer, f"Field {key!r} must be a JSON {expected}.")
        )
        return None
    return value


def _check_array_items(
    values: list[object],
    expected: str,
    pointer: str,
    findings: list[CoverageFinding],
) -> list[object]:
    valid: list[object] = []
    for index, value in enumerate(values):
        if _matches_json_type(value, expected):
            valid.append(value)
        else:
            findings.append(
                _error(
                    "COV_FIELD_TYPE",
                    f"{pointer}/{index}",
                    f"Array item must be a JSON {expected}.",
                )
            )
    return valid


def _check_string_fields(
    document: Mapping[str, object],
    keys: tuple[str, ...],
    pointer: str,
    findings: list[CoverageFinding],
) -> None:
    for key in keys:
        _check_field(document, key, "string", pointer, findings)


def _validate_target_shape(target: Mapping[str, object], findings: list[CoverageFinding]) -> None:
    _check_string_fields(target, ("identity", "selector"), "/target", findings)


def _validate_invocation_shape(
    invocation: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(invocation, "id", "integer", "/invocation", findings)
    _check_field(invocation, "started_at", "string", "/invocation", findings)


def _validate_capability_shapes(
    capabilities: list[object], findings: list[CoverageFinding]
) -> None:
    valid = _check_array_items(capabilities, "object", "/collector/capabilities", findings)
    for index, capability in enumerate(valid):
        assert isinstance(capability, Mapping)
        _check_string_fields(
            capability,
            ("record_class", "status"),
            f"/collector/capabilities/{index}",
            findings,
        )


def _validate_collector_shape(
    collector: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(collector, "kind", "string", "/collector", findings)
    version = _check_field(collector, "version", "object", "/collector", findings)
    if isinstance(version, Mapping):
        _check_string_fields(version, ("tag", "commit"), "/collector/version", findings)
    native_format = _check_field(collector, "native_format", "object", "/collector", findings)
    if isinstance(native_format, Mapping):
        _check_string_fields(
            native_format,
            ("name", "compatibility"),
            "/collector/native_format",
            findings,
        )
    capabilities = _check_field(collector, "capabilities", "array", "/collector", findings)
    if isinstance(capabilities, list):
        _validate_capability_shapes(capabilities, findings)


def _validate_window_shape(window: Mapping[str, object], findings: list[CoverageFinding]) -> None:
    _check_field(window, "mode", "string", "/coverage_window", findings)
    hooks = _check_field(window, "hook_evidence_artifacts", "array", "/coverage_window", findings)
    if isinstance(hooks, list):
        _check_array_items(hooks, "string", "/coverage_window/hook_evidence_artifacts", findings)


def _validate_build_shape(build: Mapping[str, object], findings: list[CoverageFinding]) -> None:
    _check_field(build, "recipe_fingerprint", "string", "/build", findings)
    instrumentation = _check_field(build, "instrumentation", "array", "/build", findings)
    _check_field(build, "trace", "boolean", "/build", findings)
    _check_field(build, "coverage", "boolean", "/build", findings)
    if isinstance(instrumentation, list):
        _check_array_items(instrumentation, "string", "/build/instrumentation", findings)


def _validate_fingerprints_shape(
    fingerprints: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_string_fields(
        fingerprints,
        (
            "target_definition",
            "rtl_sources",
            "testbench_sources",
            "test_declarations",
            "instrumented_build",
        ),
        "/fingerprints",
        findings,
    )


def _validate_source_closure_shape(
    closure: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    for category in ("rtl", "testbench"):
        sources = _check_field(closure, category, "array", "/source_closure", findings)
        if not isinstance(sources, list):
            continue
        valid = _check_array_items(sources, "object", f"/source_closure/{category}", findings)
        for index, source in enumerate(valid):
            assert isinstance(source, Mapping)
            _check_string_fields(
                source,
                ("path", "sha256"),
                f"/source_closure/{category}/{index}",
                findings,
            )


def _validate_run_shapes(runs: list[object], findings: list[CoverageFinding]) -> None:
    valid = _check_array_items(runs, "object", "/tests/runs", findings)
    for index, run in enumerate(valid):
        assert isinstance(run, Mapping)
        pointer = f"/tests/runs/{index}"
        _check_string_fields(
            run, ("id", "test", "simulation_verdict", "collection"), pointer, findings
        )
        if "raw_artifact" in run:
            _check_field(run, "raw_artifact", "string", pointer, findings)


def _validate_tests_shape(tests: Mapping[str, object], findings: list[CoverageFinding]) -> None:
    declared = _check_field(tests, "declared", "array", "/tests", findings)
    selected = _check_field(tests, "selected", "array", "/tests", findings)
    runs = _check_field(tests, "runs", "array", "/tests", findings)
    if isinstance(declared, list):
        _check_array_items(declared, "string", "/tests/declared", findings)
    if isinstance(selected, list):
        _check_array_items(selected, "string", "/tests/selected", findings)
    if isinstance(runs, list):
        _validate_run_shapes(runs, findings)


def _validate_artifact_shapes(artifacts: list[object], findings: list[CoverageFinding]) -> None:
    valid = _check_array_items(artifacts, "object", "/artifacts", findings)
    for index, artifact in enumerate(valid):
        assert isinstance(artifact, Mapping)
        pointer = f"/artifacts/{index}"
        _check_string_fields(
            artifact, ("id", "kind", "path", "sha256", "state"), pointer, findings
        )
        _check_field(artifact, "bytes", "integer", pointer, findings)
        if "owner_run" in artifact:
            _check_field(artifact, "owner_run", "string", pointer, findings)


def _validate_unknown_record_shapes(
    records: list[object], findings: list[CoverageFinding]
) -> None:
    valid = _check_array_items(records, "object", "/normalization/unrecognized_records", findings)
    for index, record in enumerate(valid):
        assert isinstance(record, Mapping)
        pointer = f"/normalization/unrecognized_records/{index}"
        _check_string_fields(record, ("raw_artifact", "native_type", "state"), pointer, findings)
        _check_field(record, "record_ordinal", "integer", pointer, findings)


def _validate_normalization_shape(
    normalization: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(normalization, "status", "string", "/normalization", findings)
    records = _check_field(
        normalization, "unrecognized_records", "array", "/normalization", findings
    )
    if isinstance(records, list):
        _validate_unknown_record_shapes(records, findings)


def _validate_position_shape(
    position: object, pointer: str, findings: list[CoverageFinding]
) -> None:
    if not isinstance(position, Mapping):
        return
    _check_field(position, "line", "integer", pointer, findings)
    _check_field(position, "column", "integer", pointer, findings)


def _validate_identity_shape(
    identity: Mapping[str, object], pointer: str, findings: list[CoverageFinding]
) -> None:
    _check_field(identity, "metric", "string", pointer, findings)
    location = _check_field(identity, "location", "object", pointer, findings)
    _check_field(identity, "hierarchy", "string", pointer, findings)
    _check_field(identity, "subject", "object", pointer, findings)
    _check_field(identity, "collector", "object", pointer, findings)
    if isinstance(location, Mapping):
        _check_field(location, "source", "string", f"{pointer}/location", findings)
        start = _check_field(location, "start", "object", f"{pointer}/location", findings)
        end = _check_field(location, "end", "object", f"{pointer}/location", findings)
        _validate_position_shape(start, f"{pointer}/location/start", findings)
        _validate_position_shape(end, f"{pointer}/location/end", findings)
    collector = identity.get("collector")
    if isinstance(collector, Mapping):
        _check_string_fields(
            collector, ("record_type", "native_key"), f"{pointer}/collector", findings
        )


def _validate_point_shapes(points: list[object], findings: list[CoverageFinding]) -> None:
    valid = _check_array_items(points, "object", "/points", findings)
    for index, point in enumerate(valid):
        assert isinstance(point, Mapping)
        pointer = f"/points/{index}"
        _check_field(point, "id", "string", pointer, findings)
        identity = _check_field(point, "identity", "object", pointer, findings)
        hits = _check_field(point, "hits_by_run", "object", pointer, findings)
        disposition = _check_field(point, "disposition", "object", pointer, findings)
        if isinstance(identity, Mapping):
            _validate_identity_shape(identity, f"{pointer}/identity", findings)
        if isinstance(hits, Mapping):
            for run_id, count in hits.items():
                hit_pointer = f"{pointer}/hits_by_run/{_pointer_token(run_id)}"
                if not isinstance(run_id, str) or not _matches_json_type(count, "integer"):
                    findings.append(
                        _error(
                            "COV_FIELD_TYPE",
                            hit_pointer,
                            "Sparse incidence keys must be strings and values must be integers.",
                        )
                    )
        if isinstance(disposition, Mapping):
            _check_field(disposition, "kind", "string", f"{pointer}/disposition", findings)


def _validate_rollup_shapes(rollups: list[object], findings: list[CoverageFinding]) -> None:
    valid = _check_array_items(rollups, "object", "/rollups", findings)
    for index, rollup in enumerate(valid):
        assert isinstance(rollup, Mapping)
        pointer = f"/rollups/{index}"
        _check_string_fields(rollup, ("metric", "semantics"), pointer, findings)
        for key in ("total_points", "eligible_points", "covered_points", "waived_points"):
            _check_field(rollup, key, "integer", pointer, findings)
        _check_field(rollup, "percent", "number_or_null", pointer, findings)


def _validate_finding_shapes(records: list[object], findings: list[CoverageFinding]) -> None:
    valid = _check_array_items(records, "object", "/findings", findings)
    for index, record in enumerate(valid):
        assert isinstance(record, Mapping)
        _check_string_fields(
            record, ("severity", "code", "pointer", "message"), f"/findings/{index}", findings
        )


def _validate_collection_shape(
    collection: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(collection, "status", "string", "/collection", findings)
    merge = _check_field(collection, "merge", "object", "/collection", findings)
    _check_field(collection, "diagnostics", "array", "/collection", findings)
    if isinstance(merge, Mapping):
        _check_field(merge, "status", "string", "/collection/merge", findings)
        if "artifact" in merge:
            _check_field(merge, "artifact", "string_or_null", "/collection/merge", findings)


def _validate_evaluation_shape(
    evaluation: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(evaluation, "status", "string", "/evaluation", findings)
    _check_field(evaluation, "criterion_fingerprint", "string_or_null", "/evaluation", findings)
    _check_field(evaluation, "suite", "object", "/evaluation", findings)
    _check_field(evaluation, "thresholds", "object", "/evaluation", findings)
    _check_field(evaluation, "metrics", "array", "/evaluation", findings)
    _check_field(evaluation, "diagnostics", "array", "/evaluation", findings)


def _validate_provenance_shapes(
    valid_top: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    if isinstance(valid_top.get("invocation"), Mapping):
        _validate_invocation_shape(valid_top["invocation"], findings)
    if isinstance(valid_top.get("target"), Mapping):
        _validate_target_shape(valid_top["target"], findings)
    if isinstance(valid_top.get("collector"), Mapping):
        _validate_collector_shape(valid_top["collector"], findings)
    if isinstance(valid_top.get("build"), Mapping):
        _validate_build_shape(valid_top["build"], findings)
    if isinstance(valid_top.get("coverage_window"), Mapping):
        _validate_window_shape(valid_top["coverage_window"], findings)
    if isinstance(valid_top.get("fingerprints"), Mapping):
        _validate_fingerprints_shape(valid_top["fingerprints"], findings)
    if isinstance(valid_top.get("source_closure"), Mapping):
        _validate_source_closure_shape(valid_top["source_closure"], findings)


def _validate_observation_shapes(
    valid_top: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    if isinstance(valid_top.get("tests"), Mapping):
        _validate_tests_shape(valid_top["tests"], findings)
    if isinstance(valid_top.get("artifacts"), list):
        _validate_artifact_shapes(valid_top["artifacts"], findings)
    if isinstance(valid_top.get("normalization"), Mapping):
        _validate_normalization_shape(valid_top["normalization"], findings)
    if isinstance(valid_top.get("points"), list):
        _validate_point_shapes(valid_top["points"], findings)
    if isinstance(valid_top.get("rollups"), list):
        _validate_rollup_shapes(valid_top["rollups"], findings)


def _validate_outcome_shapes(
    valid_top: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    if isinstance(valid_top.get("findings"), list):
        _validate_finding_shapes(valid_top["findings"], findings)
    if isinstance(valid_top.get("collection"), Mapping):
        _validate_collection_shape(valid_top["collection"], findings)
    if isinstance(valid_top.get("evaluation"), Mapping):
        _validate_evaluation_shape(valid_top["evaluation"], findings)


def _structural_findings(document: Mapping[str, object]) -> tuple[CoverageFinding, ...]:
    findings: list[CoverageFinding] = []
    valid_top: dict[str, object] = {}
    for key, expected in _TOP_LEVEL_FIELDS:
        value = _check_field(document, key, expected, "", findings)
        if value is not None:
            valid_top[key] = value
    _validate_provenance_shapes(valid_top, findings)
    _validate_observation_shapes(valid_top, findings)
    _validate_outcome_shapes(valid_top, findings)
    return tuple(findings)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _point_id(identity: object) -> str:
    payload = base64.urlsafe_b64encode(_canonical_json(identity)).decode("ascii").rstrip("=")
    return f"cp1:{payload}"


def _pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _artifact_path_is_unsafe(path: object) -> bool:
    text = str(path).replace("\\", "/")
    return (
        text.startswith("/")
        or _WINDOWS_DRIVE_RE.match(text) is not None
        or ".." in text.split("/")
    )


def _duplicate_findings(values: list[object], code: str, pointer: str) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    seen: set[object] = set()
    for index, value in enumerate(values):
        if value in seen:
            findings.append(_error(code, f"{pointer}/{index}", f"Duplicate value {value!r}."))
        seen.add(value)
    return findings


def _duplicate_key_findings(
    records: list[object], key: str, code: str, pointer: str
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    seen: set[object] = set()
    for index, record in enumerate(records):
        value = record[key]
        if value in seen:
            findings.append(
                _error(code, f"{pointer}/{index}/{key}", f"Duplicate {key} {value!r}.")
            )
        seen.add(value)
    return findings


def _validate_tests(document: Mapping[str, object]) -> tuple[list[CoverageFinding], set[str]]:
    tests = document["tests"]
    assert isinstance(tests, Mapping)
    declared = tests["declared"]
    selected = tests["selected"]
    runs = tests["runs"]
    assert isinstance(declared, list)
    assert isinstance(selected, list)
    assert isinstance(runs, list)
    findings = _duplicate_findings(declared, "COV_DECLARED_TEST_DUPLICATE", "/tests/declared")
    findings.extend(
        _duplicate_findings(selected, "COV_SELECTED_TEST_DUPLICATE", "/tests/selected")
    )
    findings.extend(_duplicate_key_findings(runs, "id", "COV_RUN_ID_DUPLICATE", "/tests/runs"))
    declared_set = {str(test) for test in declared}
    selected_set = {str(test) for test in selected}
    for index, test in enumerate(selected):
        if str(test) not in declared_set:
            findings.append(
                _error(
                    "COV_SELECTED_TEST_UNDECLARED",
                    f"/tests/selected/{index}",
                    f"Selected test {test!r} is not declared by the Target.",
                )
            )
        matching_runs = [run for run in runs if str(run["test"]) == str(test)]
        if len(matching_runs) != 1:
            findings.append(
                _error(
                    "COV_SELECTED_TEST_RUN_CARDINALITY",
                    f"/tests/selected/{index}",
                    f"Selected test {test!r} must have exactly one simulator run.",
                )
            )
    for index, run in enumerate(runs):
        if str(run["test"]) not in selected_set:
            findings.append(
                _error(
                    "COV_RUN_FOR_UNSELECTED_TEST",
                    f"/tests/runs/{index}",
                    f"Run {run['id']!r} belongs to an unselected test.",
                )
            )
    run_ids = {str(run["id"]) for run in runs}
    return findings, run_ids


def _validate_artifacts(document: Mapping[str, object]) -> list[CoverageFinding]:
    artifacts = document["artifacts"]
    assert isinstance(artifacts, list)
    findings = _duplicate_key_findings(artifacts, "id", "COV_ARTIFACT_ID_DUPLICATE", "/artifacts")
    findings.extend(
        _error(
            "COV_ARTIFACT_PATH_UNSAFE",
            f"/artifacts/{index}/path",
            "Artifact path must be report-root-relative and cannot escape the report.",
        )
        for index, artifact in enumerate(artifacts)
        if _artifact_path_is_unsafe(artifact["path"])
    )
    return findings


def _validate_point_identity(
    point: Mapping[str, object],
    pointer: str,
    seen_identities: set[bytes],
    reported_capabilities: set[str],
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    if point["id"] != _point_id(point["identity"]):
        findings.append(
            _error(
                "COV_POINT_ID_MISMATCH",
                f"{pointer}/id",
                "Point id does not encode its full canonical identity.",
            )
        )
    encoded_identity = _canonical_json(point["identity"])
    if encoded_identity in seen_identities:
        findings.append(
            _error(
                "COV_POINT_ID_DUPLICATE",
                f"{pointer}/id",
                "The same most-specific Coverage Point appears more than once.",
            )
        )
    seen_identities.add(encoded_identity)
    metric = str(point["identity"]["metric"])
    if metric not in reported_capabilities:
        findings.append(
            _error(
                "COV_POINT_CAPABILITY_MISSING",
                f"{pointer}/identity/metric",
                "Normalized point has no reported collector capability fact.",
            )
        )
    return findings


def _validate_point_disposition(
    point: Mapping[str, object], pointer: str, rtl_sources: set[str]
) -> list[CoverageFinding]:
    identity = point["identity"]
    disposition = point["disposition"]
    assert isinstance(identity, Mapping)
    assert isinstance(disposition, Mapping)
    metric = str(identity["metric"])
    kind = disposition["kind"]
    findings: list[CoverageFinding] = []
    if kind not in {"eligible", "waived", "unscored"}:
        findings.append(
            _error(
                "COV_POINT_DISPOSITION_INVALID",
                f"{pointer}/disposition/kind",
                "Point disposition must be eligible, waived, or unscored.",
            )
        )
    if kind == "eligible" and metric not in _SCORED_METRICS:
        findings.append(
            _error(
                "COV_UNSCORABLE_POINT_ELIGIBLE",
                f"{pointer}/disposition",
                f"Metric {metric!r} is retained but not scored in V1.",
            )
        )
    waiver_fields = {"waiver_id", "waiver_file", "waiver_fingerprint"}
    if kind == "waived" and not waiver_fields.issubset(disposition):
        findings.append(
            _error(
                "COV_WAIVER_REFERENCE_INCOMPLETE",
                f"{pointer}/disposition",
                "Waived point must identify its approval and exact waiver file.",
            )
        )
    source = str(identity["location"]["source"])
    if kind in {"eligible", "waived"} and source not in rtl_sources:
        findings.append(
            _error(
                "COV_SCORED_POINT_OUTSIDE_RTL",
                f"{pointer}/disposition",
                "Only points in the resolved RTL source closure may be scored or waived.",
            )
        )
    return findings


def _validate_point_incidence(
    point: Mapping[str, object], pointer: str, run_ids: set[str]
) -> list[CoverageFinding]:
    hits = point["hits_by_run"]
    assert isinstance(hits, Mapping)
    findings: list[CoverageFinding] = []
    for run_id, count in hits.items():
        hit_pointer = f"{pointer}/hits_by_run/{_pointer_token(run_id)}"
        if str(run_id) not in run_ids:
            findings.append(
                _error(
                    "COV_POINT_RUN_UNKNOWN",
                    hit_pointer,
                    "Point incidence references an unknown simulator run.",
                )
            )
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            findings.append(
                _error(
                    "COV_POINT_HIT_NONPOSITIVE",
                    hit_pointer,
                    "Sparse incidence records only positive integer hit counts.",
                )
            )
    return findings


def _validate_points(document: Mapping[str, object], run_ids: set[str]) -> list[CoverageFinding]:
    points = document["points"]
    collector = document["collector"]
    source_closure = document["source_closure"]
    assert isinstance(points, list)
    assert isinstance(collector, Mapping)
    assert isinstance(source_closure, Mapping)
    reported = {
        str(item["record_class"])
        for item in collector["capabilities"]
        if item["status"] == "reported"
    }
    rtl_sources = {str(source["path"]) for source in source_closure["rtl"]}
    findings: list[CoverageFinding] = []
    seen: set[bytes] = set()
    for index, point in enumerate(points):
        pointer = f"/points/{index}"
        findings.extend(_validate_point_identity(point, pointer, seen, reported))
        findings.extend(_validate_point_disposition(point, pointer, rtl_sources))
        findings.extend(_validate_point_incidence(point, pointer, run_ids))
    return findings


def _validate_capabilities(document: Mapping[str, object]) -> list[CoverageFinding]:
    collector = document["collector"]
    assert isinstance(collector, Mapping)
    capabilities = collector["capabilities"]
    findings = _duplicate_key_findings(
        capabilities,
        "record_class",
        "COV_CAPABILITY_DUPLICATE",
        "/collector/capabilities",
    )
    for index, capability in enumerate(capabilities):
        if capability["status"] not in {"reported", "absent", "unsupported"}:
            findings.append(
                _error(
                    "COV_CAPABILITY_STATUS_INVALID",
                    f"/collector/capabilities/{index}/status",
                    "Capability status must be reported, absent, or unsupported.",
                )
            )
    return findings


def _validate_included_raw_artifacts(
    runs: list[object], by_id: Mapping[str, Mapping[str, object]]
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    for index, run in enumerate(runs):
        assert isinstance(run, Mapping)
        if run["collection"] != "included":
            continue
        raw = by_id.get(str(run.get("raw_artifact", "")))
        if (
            raw is None
            or raw["kind"] != "raw_native"
            or raw.get("owner_run") != run["id"]
            or raw["state"] != "fresh_queryable"
        ):
            findings.append(
                _error(
                    "COV_INCLUDED_RUN_RAW_INVALID",
                    f"/tests/runs/{index}/raw_artifact",
                    "Included run must own one fresh, queryable raw native artifact.",
                )
            )
    return findings


def _validate_complete_collection(
    runs: list[object],
    collection: Mapping[str, object],
    normalization: Mapping[str, object],
    by_id: Mapping[str, Mapping[str, object]],
) -> list[CoverageFinding]:
    if collection["status"] != "complete":
        return []
    findings: list[CoverageFinding] = []
    if any(run["collection"] != "included" for run in runs):
        findings.append(
            _error(
                "COV_COMPLETE_EXCLUDES_RUN",
                "/collection/status",
                "Complete collection must include every selected simulator run.",
            )
        )
    if normalization["status"] not in {"complete", "complete_with_unknown_records"}:
        findings.append(
            _error(
                "COV_COMPLETE_NORMALIZATION_INVALID",
                "/normalization/status",
                "Complete collection requires successful lossless normalization.",
            )
        )
    merge = collection["merge"]
    if merge["status"] != "equivalent":
        findings.append(
            _error(
                "COV_COMPLETE_MERGE_UNVERIFIED",
                "/collection/merge/status",
                "Complete collection requires independently verified merge equivalence.",
            )
        )
    merged = by_id.get(str(merge.get("artifact", "")))
    if merged is None or merged["kind"] != "merged_native" or merged["state"] != "fresh_queryable":
        findings.append(
            _error(
                "COV_COMPLETE_MERGED_ARTIFACT_INVALID",
                "/collection/merge/artifact",
                "Complete collection requires one fresh, queryable merged artifact.",
            )
        )
    return findings


def _validate_collection(document: Mapping[str, object]) -> list[CoverageFinding]:
    tests = document["tests"]
    artifacts = document["artifacts"]
    collection = document["collection"]
    normalization = document["normalization"]
    assert isinstance(tests, Mapping)
    assert isinstance(artifacts, list)
    assert isinstance(collection, Mapping)
    assert isinstance(normalization, Mapping)
    runs = tests["runs"]
    assert isinstance(runs, list)
    by_id = {str(artifact["id"]): artifact for artifact in artifacts}
    findings = _validate_included_raw_artifacts(runs, by_id)
    findings.extend(_validate_complete_collection(runs, collection, normalization, by_id))
    return findings


def _expected_evaluation_metrics(document: Mapping[str, object]) -> list[dict[str, object]] | None:
    evaluation = document["evaluation"]
    assert isinstance(evaluation, Mapping)
    thresholds = evaluation["thresholds"]
    assert isinstance(thresholds, Mapping)
    by_metric = {str(rollup["metric"]): rollup for rollup in document["rollups"]}
    metrics: list[dict[str, object]] = []
    for metric, threshold in thresholds.items():
        rollup = by_metric.get(str(metric))
        if (
            rollup is None
            or isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not math.isfinite(threshold)
            or rollup["eligible_points"] == 0
        ):
            return None
        actual = Fraction(rollup["covered_points"] * 100, rollup["eligible_points"])
        verdict = "pass" if actual >= Fraction(str(threshold)) else "fail"
        metrics.append(
            {
                "metric": metric,
                "actual_percent": rollup["percent"],
                "minimum_percent": threshold,
                "verdict": verdict,
            }
        )
    return metrics


def _validate_evaluation(document: Mapping[str, object]) -> list[CoverageFinding]:
    evaluation = document["evaluation"]
    collection = document["collection"]
    assert isinstance(evaluation, Mapping)
    assert isinstance(collection, Mapping)
    status = evaluation["status"]
    if status not in {"pass", "fail", "blocked", "not_requested"}:
        return [
            _error(
                "COV_EVALUATION_STATUS_INVALID",
                "/evaluation/status",
                "Evaluation status is not a V1 verdict.",
            )
        ]
    if status not in {"pass", "fail"}:
        return []
    findings: list[CoverageFinding] = []
    if collection["status"] != "complete":
        findings.append(
            _error(
                "COV_INCOMPLETE_EVALUATED",
                "/evaluation/status",
                "Pass or fail requires complete compatible collection evidence.",
            )
        )
    expected_metrics = _expected_evaluation_metrics(document)
    expected_status = (
        "pass"
        if expected_metrics is not None
        and expected_metrics
        and all(metric["verdict"] == "pass" for metric in expected_metrics)
        else "fail"
    )
    if evaluation["metrics"] != expected_metrics or status != expected_status:
        findings.append(
            _error(
                "COV_EVALUATION_MISMATCH",
                "/evaluation",
                "Stored evaluation does not match the exact deterministic derivation.",
            )
        )
    return findings


def _calculate_rollups(document: Mapping[str, object]) -> list[dict[str, object]]:
    points = document["points"]
    assert isinstance(points, list)
    rollups: list[dict[str, object]] = []
    for metric, semantics in _METRIC_SEMANTICS.items():
        matching = [point for point in points if point["identity"]["metric"] == metric]
        if not matching:
            continue
        eligible = [point for point in matching if point["disposition"]["kind"] == "eligible"]
        waived = [point for point in matching if point["disposition"]["kind"] == "waived"]
        covered = [point for point in eligible if sum(point["hits_by_run"].values()) > 0]
        percentage = round(len(covered) * 100 / len(eligible), 2) if eligible else None
        rollups.append(
            {
                "metric": metric,
                "semantics": semantics,
                "total_points": len(matching),
                "eligible_points": len(eligible),
                "covered_points": len(covered),
                "waived_points": len(waived),
                "percent": percentage,
            }
        )
    return rollups


def _validate_native_format(document: Mapping[str, object]) -> list[CoverageFinding]:
    collector = document["collector"]
    normalization = document["normalization"]
    assert isinstance(collector, Mapping)
    assert isinstance(normalization, Mapping)
    native_format = collector["native_format"]
    assert isinstance(native_format, Mapping)
    findings: list[CoverageFinding] = []
    capabilities = {
        (str(item["record_class"]), str(item["status"])) for item in collector["capabilities"]
    }
    for index, record in enumerate(normalization["unrecognized_records"]):
        required_capability = (f"native:{record['native_type']}", "reported")
        if required_capability not in capabilities:
            findings.append(
                _error(
                    "COV_UNKNOWN_RECORD_CAPABILITY_MISSING",
                    f"/normalization/unrecognized_records/{index}/native_type",
                    "A retained unknown record requires a reported capability fact.",
                )
            )
    if native_format["compatibility"] == "incompatible":
        if normalization["status"] != "incompatible":
            findings.append(
                _error(
                    "COV_INCOMPATIBLE_FORMAT_NORMALIZED",
                    "/normalization/status",
                    "An incompatible native format cannot claim successful normalization.",
                )
            )
        if document["points"]:
            findings.append(
                _error(
                    "COV_INCOMPATIBLE_FORMAT_POINTS_PRESENT",
                    "/points",
                    "An incompatible native format cannot expose normalized points.",
                )
            )
    return findings


def _semantic_findings(
    document: Mapping[str, object], expected_target: DurableTargetIdentity
) -> tuple[CoverageFinding, ...]:
    findings: list[CoverageFinding] = []
    if document["$schema"] != _SCHEMA:
        findings.append(
            _error(
                "COV_SCHEMA_VERSION_UNSUPPORTED",
                "/$schema",
                f"Expected {_SCHEMA!r}.",
            )
        )
    target = document["target"]
    assert isinstance(target, Mapping)
    if target["identity"] != expected_target:
        findings.append(
            _error(
                "COV_TARGET_MISMATCH",
                "/target/identity",
                "Campaign belongs to a different Target.",
            )
        )
    test_findings, run_ids = _validate_tests(document)
    findings.extend(test_findings)
    findings.extend(_validate_artifacts(document))
    findings.extend(_validate_capabilities(document))
    findings.extend(_validate_points(document, run_ids))
    findings.extend(_validate_native_format(document))
    if document["rollups"] != _calculate_rollups(document):
        findings.append(
            _error(
                "COV_ROLLUP_MISMATCH",
                "/rollups",
                "Rollups do not match the deterministic derivation from points.",
            )
        )
    findings.extend(_validate_collection(document))
    findings.extend(_validate_evaluation(document))
    return tuple(findings)


def _decode_capability(document: Mapping[str, object]) -> CoverageCapability:
    attributes = {
        key: value for key, value in document.items() if key not in {"record_class", "status"}
    }
    return CoverageCapability(
        record_class=str(document["record_class"]),
        status=str(document["status"]),
        attributes=_freeze_mapping(attributes),
    )


def _decode_run(document: Mapping[str, object]) -> CoverageRun:
    known = {"id", "test", "simulation_verdict", "collection", "raw_artifact"}
    return CoverageRun(
        id=str(document["id"]),
        test=str(document["test"]),
        simulation_verdict=str(document["simulation_verdict"]),
        collection=str(document["collection"]),
        raw_artifact=(str(document["raw_artifact"]) if "raw_artifact" in document else None),
        attributes=_freeze_mapping(
            {key: value for key, value in document.items() if key not in known}
        ),
    )


def _decode_artifact(document: Mapping[str, object]) -> CoverageArtifact:
    known = {"id", "kind", "path", "sha256", "bytes", "state", "owner_run"}
    return CoverageArtifact(
        id=str(document["id"]),
        kind=str(document["kind"]),
        path=str(document["path"]),
        sha256=str(document["sha256"]),
        bytes=int(document["bytes"]),
        state=str(document["state"]),
        owner_run=str(document["owner_run"]) if "owner_run" in document else None,
        attributes=_freeze_mapping(
            {key: value for key, value in document.items() if key not in known}
        ),
    )


def _decode_point(document: Mapping[str, object]) -> CoveragePoint:
    identity = document["identity"]
    assert isinstance(identity, Mapping)
    hits = document["hits_by_run"]
    assert isinstance(hits, Mapping)
    return CoveragePoint(
        id=str(document["id"]),
        identity=CoveragePointIdentity(
            metric=str(identity["metric"]),
            location=_freeze_mapping(identity["location"]),
            hierarchy=str(identity["hierarchy"]),
            subject=_freeze_mapping(identity["subject"]),
            collector=_freeze_mapping(identity["collector"]),
        ),
        hits_by_run=MappingProxyType(
            {str(run_id): int(hits[run_id]) for run_id in sorted(hits, key=str)}
        ),
        disposition=_freeze_mapping(document["disposition"]),
    )


def _decode_rollup(document: Mapping[str, object]) -> CoverageRollup:
    percent = document["percent"]
    return CoverageRollup(
        metric=str(document["metric"]),
        semantics=str(document["semantics"]),
        total_points=int(document["total_points"]),
        eligible_points=int(document["eligible_points"]),
        covered_points=int(document["covered_points"]),
        waived_points=int(document["waived_points"]),
        percent=float(percent) if percent is not None else None,
    )


def _decode_finding(document: Mapping[str, object]) -> CoverageFinding:
    return CoverageFinding(
        severity=str(document["severity"]),
        code=str(document["code"]),
        pointer=str(document["pointer"]),
        message=str(document["message"]),
    )


def _decode_collector(document: Mapping[str, object]) -> CoverageCollector:
    capabilities = document["capabilities"]
    assert isinstance(capabilities, list)
    return CoverageCollector(
        kind=str(document["kind"]),
        version=_freeze_mapping(document["version"]),
        native_format=_freeze_mapping(document["native_format"]),
        capabilities=tuple(_decode_capability(item) for item in capabilities),
    )


def _decode_valid_campaign(document: Mapping[str, object]) -> CoverageCampaign:
    target = document["target"]
    collector = document["collector"]
    tests = document["tests"]
    assert isinstance(target, Mapping)
    assert isinstance(collector, Mapping)
    assert isinstance(tests, Mapping)
    runs = tests["runs"]
    artifacts = document["artifacts"]
    points = document["points"]
    rollups = document["rollups"]
    findings = document["findings"]
    assert isinstance(runs, list)
    assert isinstance(artifacts, list)
    assert isinstance(points, list)
    assert isinstance(rollups, list)
    assert isinstance(findings, list)
    return CoverageCampaign(
        schema=str(document["$schema"]),
        campaign_id=str(document["campaign_id"]),
        invocation=_freeze_mapping(document["invocation"]),
        target=CoverageTarget(identity=str(target["identity"]), selector=str(target["selector"])),
        collector=_decode_collector(collector),
        build=_freeze_mapping(document["build"]),
        coverage_window=_freeze_mapping(document["coverage_window"]),
        fingerprints=_freeze_mapping(document["fingerprints"]),
        source_closure=_freeze_mapping(document["source_closure"]),
        declared_tests=tuple(str(item) for item in tests["declared"]),
        selected_tests=tuple(str(item) for item in tests["selected"]),
        runs=tuple(_decode_run(item) for item in runs),
        artifacts=tuple(_decode_artifact(item) for item in artifacts),
        normalization=_freeze_mapping(document["normalization"]),
        points=tuple(_decode_point(item) for item in points),
        rollups=tuple(_decode_rollup(item) for item in rollups),
        collection=_freeze_mapping(document["collection"]),
        findings=tuple(_decode_finding(item) for item in findings),
        evaluation=_freeze_mapping(document["evaluation"]),
    )


def decode_coverage_campaign(
    document: object,
    expected_target: DurableTargetIdentity,
) -> CoverageCampaign:
    """Decode one already-read V1 document into an immutable Campaign."""
    if not isinstance(document, Mapping):
        raise CoverageCampaignValidationError(
            (
                _error(
                    "COV_DOCUMENT_TYPE",
                    "",
                    "Coverage Campaign document must be a JSON object.",
                ),
            )
        )
    structural_findings = _structural_findings(document)
    if structural_findings:
        raise CoverageCampaignValidationError(structural_findings)
    semantic_findings = _semantic_findings(document, expected_target)
    if semantic_findings:
        raise CoverageCampaignValidationError(semantic_findings)
    return _decode_valid_campaign(document)


def _encode_capability(capability: CoverageCapability) -> dict[str, object]:
    return {
        "record_class": capability.record_class,
        "status": capability.status,
        **_thaw(capability.attributes),
    }


def _encode_run(run: CoverageRun) -> dict[str, object]:
    document: dict[str, object] = {
        "id": run.id,
        "test": run.test,
        "simulation_verdict": run.simulation_verdict,
        "collection": run.collection,
    }
    if run.raw_artifact is not None:
        document["raw_artifact"] = run.raw_artifact
    document.update(_thaw(run.attributes))
    return document


def _encode_artifact(artifact: CoverageArtifact) -> dict[str, object]:
    document: dict[str, object] = {
        "id": artifact.id,
        "kind": artifact.kind,
    }
    if artifact.owner_run is not None:
        document["owner_run"] = artifact.owner_run
    document.update(
        {
            "path": artifact.path,
            "sha256": artifact.sha256,
            "bytes": artifact.bytes,
            "state": artifact.state,
        }
    )
    document.update(_thaw(artifact.attributes))
    return document


def _encode_point(point: CoveragePoint) -> dict[str, object]:
    return {
        "id": point.id,
        "identity": {
            "metric": point.identity.metric,
            "location": _thaw(point.identity.location),
            "hierarchy": point.identity.hierarchy,
            "subject": _thaw(point.identity.subject),
            "collector": _thaw(point.identity.collector),
        },
        "hits_by_run": dict(point.hits_by_run),
        "disposition": _thaw(point.disposition),
    }


def _encode_rollup(rollup: CoverageRollup) -> dict[str, object]:
    return {
        "metric": rollup.metric,
        "semantics": rollup.semantics,
        "total_points": rollup.total_points,
        "eligible_points": rollup.eligible_points,
        "covered_points": rollup.covered_points,
        "waived_points": rollup.waived_points,
        "percent": rollup.percent,
    }


def encode_coverage_campaign(campaign: CoverageCampaign) -> dict[str, object]:
    """Encode *campaign* as a fresh canonical V1 JSON object."""
    return {
        "$schema": campaign.schema,
        "campaign_id": campaign.campaign_id,
        "invocation": _thaw(campaign.invocation),
        "target": {
            "identity": campaign.target.identity,
            "selector": campaign.target.selector,
        },
        "collector": {
            "kind": campaign.collector.kind,
            "version": _thaw(campaign.collector.version),
            "native_format": _thaw(campaign.collector.native_format),
            "capabilities": [
                _encode_capability(capability) for capability in campaign.collector.capabilities
            ],
        },
        "build": _thaw(campaign.build),
        "coverage_window": _thaw(campaign.coverage_window),
        "fingerprints": _thaw(campaign.fingerprints),
        "source_closure": _thaw(campaign.source_closure),
        "tests": {
            "declared": list(campaign.declared_tests),
            "selected": list(campaign.selected_tests),
            "runs": [_encode_run(run) for run in campaign.runs],
        },
        "artifacts": [_encode_artifact(artifact) for artifact in campaign.artifacts],
        "normalization": _thaw(campaign.normalization),
        "points": [_encode_point(point) for point in campaign.points],
        "rollups": [_encode_rollup(rollup) for rollup in campaign.rollups],
        "collection": _thaw(campaign.collection),
        "findings": [
            {
                "severity": finding.severity,
                "code": finding.code,
                "pointer": finding.pointer,
                "message": finding.message,
            }
            for finding in campaign.findings
        ],
        "evaluation": _thaw(campaign.evaluation),
    }
