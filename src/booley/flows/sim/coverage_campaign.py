"""Immutable, validated Coverage Campaign document model.

The codec is deliberately independent of filesystems, simulation orchestration,
policy evaluation, and Verilator's native coverage format.  Its input is an
already-read manifest mapping with its point rows and its output is one immutable
per-Target campaign.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, NewType, TypeAlias, cast

from booley.core.boundary import (
    BoundaryError,
    as_dict,
    as_str,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_sha256_digest,
    require_str,
    require_str_value,
)
from booley.flows.sim.coverage_json import (
    POINT_ID_JSON,
    FrozenJson,
    coverage_mapping_document,
    freeze_coverage_mapping,
)
from booley.flows.test_runs import build_test_run, declared_inputs_fingerprint
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_epoch

DurableTargetIdentity = NewType("DurableTargetIdentity", str)
CoverageCapabilityStatus: TypeAlias = Literal["reported", "absent", "unsupported"]
SimulationVerdict: TypeAlias = Literal["pass", "fail", "elab_error", "timeout", "inconclusive"]
CoverageRunCollectionState: TypeAlias = Literal["included", "collector_error"]

_SCHEMA = "booley.coverage-campaign/v1"
_SCORED_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})
_COLLECTION_STATUSES = frozenset({"complete", "collector_error", "incomplete", "incompatible"})
SOURCE_ROLLUP_METRICS = ("line", "branch", "expression", "toggle")
_METRIC_SEMANTICS = {
    "line": "One Verilator basic-block point; covered when its count is greater than zero.",
    "branch": "One branch outcome; each outcome is a separate point covered when count is greater than zero.",
    "expression": "One collector-reported expression outcome; each outcome is a separate point covered when count is greater than zero.",
    "toggle": "One direction for one signal bit; 0_to_1 and 1_to_0 are separate points.",
    "cover_property": "One user-authored cover-property point; covered when count is greater than zero.",
}
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
#: Fingerprint of a run that records no Test Variant: no declared arguments or
#: environment. It is what a run of a document older than storage v5 had.
NO_DECLARED_INPUTS_FINGERPRINT = declared_inputs_fingerprint((), {})
_TOP_LEVEL_FIELDS = (
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
    ("rollups", "array"),
    ("collection", "object"),
    ("findings", "array"),
    ("evaluation", "object"),
)


@dataclass(frozen=True)
class CoverageTarget:
    """Durable identity and display selector for the Campaign's one Target."""

    identity: str
    selector: str


@dataclass(frozen=True)
class CoverageCapability:
    """Collector support fact for one native coverage record class."""

    record_class: str
    status: CoverageCapabilityStatus
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
    """Independent simulation and collection truth for one Test Run.

    ``test`` is the bare Test; ``test_run`` is the resolved Test Run name
    (``test[+variant][@seed]``, ADR 0072) and ``fingerprint`` covers the Test
    Variant's declared arguments and environment. Both are always explicit.
    """

    id: str
    test: str
    simulation_verdict: SimulationVerdict
    collection: CoverageRunCollectionState
    raw_artifact: str | None
    attributes: Mapping[str, FrozenJson]
    test_run: str
    fingerprint: str
    #: The Test Variant and seed the Test Run name states; ``None`` when it
    #: has none (no Test Variants declared, an unseeded Target).
    variant: str | None
    seed: int | None


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


_VALIDATION_DISPLAY_FINDINGS = 3
_VALIDATION_DISPLAY_ITEM_CHARS = 256
_VALIDATION_MESSAGE_MAX_CHARS = 1024
_TRUNCATION_MARKER = "..."


def _escape_display_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _truncate_escaped_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    budget = limit - len(_TRUNCATION_MARKER)
    end = 0
    while end < len(value):
        token_length = 1
        if value[end] == "\\":
            token_length = 6 if value.startswith("\\u", end) else 2
        if end + token_length > budget:
            break
        end += token_length
    return value[:end] + _TRUNCATION_MARKER


def _format_validation_finding(finding: CoverageFinding) -> str:
    code = _escape_display_text(finding.code)
    pointer = _escape_display_text(finding.pointer) if finding.pointer else "<document-root>"
    message = _escape_display_text(finding.message)
    item = f"{code} at {pointer} — {message}"
    return _truncate_escaped_text(item, _VALIDATION_DISPLAY_ITEM_CHARS)


def _format_validation_error(findings: tuple[CoverageFinding, ...]) -> str:
    summary = f"coverage campaign is invalid ({len(findings)} findings)"
    displayed = findings[:_VALIDATION_DISPLAY_FINDINGS]
    if not displayed:
        return summary
    details = "; ".join(_format_validation_finding(finding) for finding in displayed)
    omitted = len(findings) - len(displayed)
    suffix = f"; +{omitted} more" if omitted else ""
    rendered = f"{summary}: {details}{suffix}"
    assert len(rendered) <= _VALIDATION_MESSAGE_MAX_CHARS
    return rendered


class CoverageCampaignValidationError(ValueError):
    """All stable findings produced while decoding one invalid Campaign."""

    def __init__(self, findings: tuple[CoverageFinding, ...]) -> None:
        self.findings = findings
        super().__init__(_format_validation_error(findings))


@dataclass(frozen=True)
class CoveragePointIdentity:
    """Lossless most-specific identity for one native measurement point."""

    metric: str
    location: Mapping[str, FrozenJson]
    hierarchy: str
    subject: Mapping[str, FrozenJson]
    collector: Mapping[str, FrozenJson]

    @property
    def source(self) -> str:
        """Return the validated source path named by this identity."""
        return str(self.location["source"])


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
class CoverageSourceRollup:
    """Deterministic summaries for one normalized source file."""

    source: str
    rollups: tuple[CoverageRollup, ...]


@dataclass(frozen=True)
class CoverageCampaign:
    """One indivisible normalized coverage record for one Target invocation."""

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


def _is_json_array(value: object) -> bool:
    try:
        require_list(value)
    except BoundaryError:
        return False
    return True


def _is_json_integer(value: object) -> bool:
    try:
        require_int(value)
    except BoundaryError:
        return False
    return True


def _is_json_number(value: object) -> bool:
    try:
        require_finite_number(value, field="value")
    except (BoundaryError, OverflowError):
        # A 401-digit integer is not a finite float: ``math.isfinite`` overflows.
        return False
    return True


def _matches_json_type(value: object, expected: str) -> bool:
    matches = {
        "object": as_dict(value) is not None,
        "array": _is_json_array(value),
        "string": as_str(value) is not None,
        "string_or_null": value is None or as_str(value) is not None,
        "boolean": isinstance(value, bool),
        "integer": _is_json_integer(value),
        "number": _is_json_number(value),
        "number_or_null": value is None or _is_json_number(value),
    }
    if expected not in matches:
        raise AssertionError(f"unknown JSON type {expected}")
    return matches[expected]


def _missing_field(pointer: str, key: str) -> CoverageFinding:
    """The finding for a required field a document does not record."""
    return _error("COV_FIELD_REQUIRED", pointer, f"Required field {key!r} is missing.")


def _check_field(
    document: Mapping[str, object],
    key: str,
    expected: str,
    parent: str,
    findings: list[CoverageFinding],
) -> object | None:
    pointer = f"{parent}/{_pointer_token(key)}"
    if key not in document:
        findings.append(_missing_field(pointer, key))
        return None
    value = document[key]
    if not _matches_json_type(value, expected):
        findings.append(
            _error("COV_FIELD_TYPE", pointer, f"Field {key!r} must be a JSON {expected}.")
        )
        return None
    return value


def _check_array_items(
    values: Sequence[object],
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
    started_at = _check_field(invocation, "started_at", "string", "/invocation", findings)
    if isinstance(started_at, str):
        try:
            parse_timestamp(started_at)
        except ValueError:
            findings.append(
                _error(
                    "COV_TIMESTAMP_INVALID",
                    "/invocation/started_at",
                    "Invocation timestamp must be valid ISO-8601/RFC-3339.",
                )
            )


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
        verdict = run.get("simulation_verdict")
        if isinstance(verdict, str) and verdict not in {
            "pass",
            "fail",
            "elab_error",
            "timeout",
            "inconclusive",
        }:
            findings.append(
                _error(
                    "COV_SIMULATION_VERDICT_INVALID",
                    f"{pointer}/simulation_verdict",
                    "Simulation verdict is not a supported normalized outcome.",
                )
            )
        collection = run.get("collection")
        if isinstance(collection, str) and collection not in {"included", "collector_error"}:
            findings.append(
                _error(
                    "COV_RUN_COLLECTION_STATE_INVALID",
                    f"{pointer}/collection",
                    "Run collection state must be included or collector_error.",
                )
            )
        if "raw_artifact" in run:
            _check_field(run, "raw_artifact", "string", pointer, findings)
        _validate_run_identity_shape(run, pointer, findings)


#: What every run records about its Test Run (storage v5).
RUN_IDENTITY_FIELDS = ("test_run", "variant", "seed", "fingerprint")


def _validate_run_identity_shape(
    run: Mapping[str, object], pointer: str, findings: list[CoverageFinding]
) -> None:
    """A run records its whole Test Run identity; ``variant`` and ``seed`` may be null."""
    for field in ("test_run", "fingerprint"):
        _check_field(run, field, "string", pointer, findings)
    for field in ("variant", "seed"):
        if field not in run:
            findings.append(_missing_field(f"{pointer}/{field}", field))


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


def _validate_point_shapes(points: Sequence[object], findings: list[CoverageFinding]) -> None:
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


def _validate_finding_shapes(records: list[object], findings: list[CoverageFinding]) -> None:
    valid = _check_array_items(records, "object", "/findings", findings)
    for index, record in enumerate(valid):
        assert isinstance(record, Mapping)
        _check_string_fields(
            record, ("severity", "code", "pointer", "message"), f"/findings/{index}", findings
        )


def _validate_diagnostic_shapes(
    records: list[object], pointer: str, findings: list[CoverageFinding]
) -> None:
    for index, record in enumerate(records):
        item_pointer = f"{pointer}/{index}"
        if isinstance(record, str):
            continue
        if not isinstance(record, Mapping):
            findings.append(
                _error(
                    "COV_FIELD_TYPE",
                    item_pointer,
                    "Diagnostic must be a code string or JSON object.",
                )
            )
            continue
        _check_string_fields(record, ("code", "pointer", "message"), item_pointer, findings)


def _validate_collection_shape(
    collection: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(collection, "status", "string", "/collection", findings)
    merge = _check_field(collection, "merge", "object", "/collection", findings)
    diagnostics = _check_field(collection, "diagnostics", "array", "/collection", findings)
    if isinstance(diagnostics, list):
        _validate_diagnostic_shapes(diagnostics, "/collection/diagnostics", findings)
    if isinstance(merge, Mapping):
        _check_field(merge, "status", "string", "/collection/merge", findings)
        if "artifact" in merge:
            _check_field(merge, "artifact", "string_or_null", "/collection/merge", findings)


def _validate_suite_shape(suite: Mapping[str, object], findings: list[CoverageFinding]) -> None:
    _check_field(suite, "status", "string", "/evaluation/suite", findings)
    if "required" not in suite:
        return
    required = _check_field(suite, "required", "array", "/evaluation/suite", findings)
    if isinstance(required, list):
        _check_array_items(required, "string", "/evaluation/suite/required", findings)


def _validate_metric_row_shapes(rows: list[object], findings: list[CoverageFinding]) -> None:
    """Each row is an object naming a string metric and verdict; a metric is named once."""
    seen: set[str] = set()
    _check_array_items(rows, "object", "/evaluation/metrics", findings)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        pointer = f"/evaluation/metrics/{index}"
        _check_string_fields(row, ("metric", "verdict"), pointer, findings)
        metric = row.get("metric")
        if not isinstance(metric, str):
            continue
        if metric in seen:
            findings.append(
                _error(
                    "COV_FIELD_TYPE",
                    f"{pointer}/metric",
                    f"Evaluation names metric {metric} more than once.",
                )
            )
        seen.add(metric)


def _validate_evaluation_shape(
    evaluation: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    _check_field(evaluation, "status", "string", "/evaluation", findings)
    fingerprint = _check_field(
        evaluation, "criterion_fingerprint", "string_or_null", "/evaluation", findings
    )
    if isinstance(fingerprint, str):
        _check_field(
            evaluation,
            "approved_waiver_set_digest",
            "string",
            "/evaluation",
            findings,
        )
    suite = _check_field(evaluation, "suite", "object", "/evaluation", findings)
    thresholds = _check_field(evaluation, "thresholds", "object", "/evaluation", findings)
    metrics = _check_field(evaluation, "metrics", "array", "/evaluation", findings)
    diagnostics = _check_field(evaluation, "diagnostics", "array", "/evaluation", findings)
    if isinstance(diagnostics, list):
        _validate_diagnostic_shapes(diagnostics, "/evaluation/diagnostics", findings)
    if isinstance(suite, Mapping):
        _validate_suite_shape(suite, findings)
    if isinstance(thresholds, Mapping):
        for metric, threshold in thresholds.items():
            pointer = f"/evaluation/thresholds/{_pointer_token(metric)}"
            if metric not in _SCORED_METRICS:
                findings.append(
                    _error(
                        "COV_FIELD_TYPE",
                        pointer,
                        "Coverage threshold must name a scored V1 metric.",
                    )
                )
            if not _matches_json_type(threshold, "number"):
                findings.append(
                    _error(
                        "COV_FIELD_TYPE",
                        pointer,
                        "Coverage threshold must be a finite JSON number.",
                    )
                )
    if isinstance(metrics, list):
        _validate_metric_row_shapes(metrics, findings)


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
    valid_top: Mapping[str, object], points: Sequence[object], findings: list[CoverageFinding]
) -> None:
    if isinstance(valid_top.get("tests"), Mapping):
        _validate_tests_shape(valid_top["tests"], findings)
    if isinstance(valid_top.get("artifacts"), list):
        _validate_artifact_shapes(valid_top["artifacts"], findings)
    if isinstance(valid_top.get("normalization"), Mapping):
        _validate_normalization_shape(valid_top["normalization"], findings)
    _validate_point_shapes(points, findings)


def _validate_outcome_shapes(
    valid_top: Mapping[str, object], findings: list[CoverageFinding]
) -> None:
    if isinstance(valid_top.get("findings"), list):
        _validate_finding_shapes(valid_top["findings"], findings)
    if isinstance(valid_top.get("collection"), Mapping):
        _validate_collection_shape(valid_top["collection"], findings)
    if isinstance(valid_top.get("evaluation"), Mapping):
        _validate_evaluation_shape(valid_top["evaluation"], findings)


def _structural_findings(
    document: Mapping[str, object], points: Sequence[object]
) -> tuple[CoverageFinding, ...]:
    findings: list[CoverageFinding] = []
    valid_top: dict[str, object] = {}
    for key, expected in _TOP_LEVEL_FIELDS:
        value = _check_field(document, key, expected, "", findings)
        if value is not None:
            valid_top[key] = value
    _validate_provenance_shapes(valid_top, findings)
    _validate_observation_shapes(valid_top, points, findings)
    _validate_outcome_shapes(valid_top, findings)
    return tuple(findings)


def encode_coverage_point_id(identity: object) -> str:
    """Return the ``cp1:`` Coverage Point id that encodes one canonical identity."""
    payload = base64.urlsafe_b64encode(POINT_ID_JSON.encode(identity)).decode("ascii").rstrip("=")
    return f"cp1:{payload}"


def _valid_point_position(position: object) -> bool:
    try:
        document = require_dict(position, field="position")
        line = require_int(document.get("line"), field="line")
        column = require_int(document.get("column"), field="column")
    except BoundaryError:
        return False
    return set(document) == {"line", "column"} and line > 0 and column > 0


def decode_coverage_point_id(point_id: object) -> Mapping[str, object] | None:
    """Return one complete canonical cp1 identity, or ``None`` when malformed."""
    text = as_str(point_id)
    if text is None or re.fullmatch(r"cp1:[A-Za-z0-9_-]+", text) is None:
        return None
    payload = text.removeprefix("cp1:")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        identity = require_dict(json.loads(raw), field="point identity")
        metric = require_str(identity, "metric")
        location = require_dict(identity.get("location"), field="location")
        hierarchy = require_str(identity, "hierarchy")
        subject = require_dict(identity.get("subject"), field="subject")
        collector = require_dict(identity.get("collector"), field="collector")
        source = require_str(location, "source")
        require_str(collector, "record_type")
        require_str(collector, "native_key")
    except (BoundaryError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    complete = (
        set(identity) == {"metric", "location", "hierarchy", "subject", "collector"}
        and set(location) == {"source", "start", "end"}
        and _valid_point_position(location.get("start"))
        and _valid_point_position(location.get("end"))
        and bool(metric and hierarchy and source and subject)
        and text == encode_coverage_point_id(identity)
    )
    return identity if complete else None


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
    findings.extend(_test_run_identity_findings(runs))
    run_ids = {str(run["id"]) for run in runs}
    return findings, run_ids


def _test_run_of(run: Mapping[str, object]) -> str:
    """The Test Run a run document records."""
    return str(run["test_run"])


def _test_run_identity_problem(run: Mapping[str, object]) -> str:
    """Why a run's recorded Test Run identity is invalid; empty when it is valid.

    The recorded name must be exactly the name of the recorded Test, Test
    Variant and seed, under the Test Run name rules; the fingerprint is a
    canonical SHA-256 digest.
    """
    name, test, variant, seed = run["test_run"], run["test"], run["variant"], run["seed"]
    try:
        require_sha256_digest(run["fingerprint"], field="fingerprint")
        recorded = build_test_run(
            require_str_value(test, field="test"),
            variant=None if variant is None else require_str_value(variant, field="variant"),
            seed=None if seed is None else require_int(seed, field="seed"),
        )
    except (BoundaryError, ValueError) as exc:
        return str(exc)
    if recorded.name != name:
        return (
            f"Test Run {name!r} is not the Test Run of Test {test!r}, Test Variant "
            f"{variant!r} and seed {seed!r}."
        )
    return ""


def _test_run_identity_findings(runs: list[object]) -> list[CoverageFinding]:
    """Refuse two runs of one Test Run, and a run whose recorded identity is invalid."""
    findings: list[CoverageFinding] = []
    seen: set[str] = set()
    for index, run in enumerate(runs):
        assert isinstance(run, Mapping)
        name = _test_run_of(run)
        pointer = f"/tests/runs/{index}/test_run"
        if name in seen:
            findings.append(
                _error("COV_TEST_RUN_DUPLICATE", pointer, f"Duplicate Test Run {name!r}.")
            )
        seen.add(name)
        problem = _test_run_identity_problem(run)
        if problem:
            findings.append(_error("COV_TEST_RUN_TEST_MISMATCH", pointer, problem))
    return findings


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
    if point["id"] != encode_coverage_point_id(point["identity"]):
        findings.append(
            _error(
                "COV_POINT_ID_MISMATCH",
                f"{pointer}/id",
                "Point id does not encode its full canonical identity.",
            )
        )
    if decode_coverage_point_id(point["id"]) is None:
        findings.append(
            _error(
                "COV_POINT_ID_INCOMPLETE",
                f"{pointer}/id",
                "Point id must encode one complete canonical Coverage Point identity.",
            )
        )
    encoded_identity = POINT_ID_JSON.encode(point["identity"])
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
    if metric not in _SCORED_METRICS and kind != "unscored":
        findings.append(
            _error(
                "COV_REPORTED_ONLY_POINT_SCORED",
                f"{pointer}/disposition",
                f"Metric {metric!r} must be retained as unscored in V1.",
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


def _validate_points(
    document: Mapping[str, object], points: Sequence[object], run_ids: set[str]
) -> list[CoverageFinding]:
    collector = document["collector"]
    source_closure = document["source_closure"]
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


def _validate_collection(document: Mapping[str, object]) -> list[CoverageFinding]:
    collection = document["collection"]
    assert isinstance(collection, Mapping)
    if collection["status"] in _COLLECTION_STATUSES:
        return []
    return [
        _error(
            "COV_COLLECTION_STATUS_INVALID",
            "/collection/status",
            "Collection status is not a supported Campaign state.",
        )
    ]


#: A stored pass/fail verdict the load cannot trust or re-score.
EVALUATION_MISMATCH = _error(
    "COV_EVALUATION_MISMATCH",
    "/evaluation",
    "Stored evaluation does not match the exact deterministic derivation.",
)


def _validate_evaluation(document: Mapping[str, object]) -> list[CoverageFinding]:
    """Check what the store's re-scoring of a stored pass/fail verdict relies on.

    The verdict itself is recomputed by the store from the points, so this only
    refuses a verdict that could not have been written: an unknown status, an
    incomplete collection, a suite that did not match, non-empty diagnostics, or a
    missing ``required`` list.
    """
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
    suite = evaluation["suite"]
    assert isinstance(suite, Mapping)
    if suite["status"] != "match" or evaluation["diagnostics"]:
        findings.append(EVALUATION_MISMATCH)
    elif "required" not in suite:
        findings.append(_missing_field("/evaluation/suite/required", "required"))
    return findings


def _sections_are_structurally_valid(
    structural_findings: tuple[CoverageFinding, ...], *pointers: str
) -> bool:
    return not any(
        finding.pointer == pointer or finding.pointer.startswith(f"{pointer}/")
        for finding in structural_findings
        for pointer in pointers
    )


def _identity_semantic_findings(
    document: Mapping[str, object],
    expected_target: DurableTargetIdentity,
    structural_findings: tuple[CoverageFinding, ...],
) -> list[CoverageFinding]:
    if not _sections_are_structurally_valid(structural_findings, "/target"):
        return []
    target = document["target"]
    assert isinstance(target, Mapping)
    if target["identity"] == expected_target:
        return []
    return [
        _error(
            "COV_TARGET_MISMATCH",
            "/target/identity",
            "Campaign belongs to a different Target.",
        )
    ]


def _observation_semantic_findings(
    document: Mapping[str, object],
    points: Sequence[object],
    structural_findings: tuple[CoverageFinding, ...],
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    tests_valid = _sections_are_structurally_valid(structural_findings, "/tests")
    run_ids: set[str] = set()
    if tests_valid:
        test_findings, run_ids = _validate_tests(document)
        findings.extend(test_findings)
    if _sections_are_structurally_valid(structural_findings, "/artifacts"):
        findings.extend(_validate_artifacts(document))
    if _sections_are_structurally_valid(structural_findings, "/collector"):
        findings.extend(_validate_capabilities(document))
    point_dependencies = ("/points", "/collector", "/source_closure")
    if tests_valid and _sections_are_structurally_valid(structural_findings, *point_dependencies):
        findings.extend(_validate_points(document, points, run_ids))
    return findings


def _outcome_semantic_findings(
    document: Mapping[str, object], structural_findings: tuple[CoverageFinding, ...]
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    if _sections_are_structurally_valid(structural_findings, "/collection"):
        findings.extend(_validate_collection(document))
    if _sections_are_structurally_valid(structural_findings, "/evaluation", "/collection"):
        findings.extend(_validate_evaluation(document))
    return findings


def _semantic_findings(
    document: Mapping[str, object],
    points: Sequence[object],
    expected_target: DurableTargetIdentity,
    structural_findings: tuple[CoverageFinding, ...],
) -> tuple[CoverageFinding, ...]:
    findings = _identity_semantic_findings(document, expected_target, structural_findings)
    findings.extend(_observation_semantic_findings(document, points, structural_findings))
    findings.extend(_outcome_semantic_findings(document, structural_findings))
    return tuple(findings)


def _decode_capability(document: Mapping[str, object]) -> CoverageCapability:
    attributes = {
        key: value for key, value in document.items() if key not in {"record_class", "status"}
    }
    return CoverageCapability(
        record_class=str(document["record_class"]),
        status=cast(CoverageCapabilityStatus, str(document["status"])),
        attributes=freeze_coverage_mapping(attributes),
    )


def _decode_run(document: Mapping[str, object]) -> CoverageRun:
    known = {
        "id",
        "test",
        "simulation_verdict",
        "collection",
        "raw_artifact",
        *RUN_IDENTITY_FIELDS,
    }
    return CoverageRun(
        id=str(document["id"]),
        test=str(document["test"]),
        simulation_verdict=cast(SimulationVerdict, str(document["simulation_verdict"])),
        collection=cast(CoverageRunCollectionState, str(document["collection"])),
        raw_artifact=(str(document["raw_artifact"]) if "raw_artifact" in document else None),
        attributes=freeze_coverage_mapping(
            {key: value for key, value in document.items() if key not in known}
        ),
        **_decode_run_identity(document),
    )


def _decode_run_identity(document: Mapping[str, object]) -> dict[str, object]:
    """A run's Test Run name, Test Variant, seed and fingerprint, exactly as recorded.

    The Campaign validator has already checked the recorded identity; nothing
    is coerced here.
    """
    variant, seed = document["variant"], document["seed"]
    return {
        "test_run": require_str_value(document["test_run"], field="test_run"),
        "variant": None if variant is None else require_str_value(variant, field="variant"),
        "seed": None if seed is None else require_int(seed, field="seed"),
        "fingerprint": require_sha256_digest(document["fingerprint"], field="fingerprint"),
    }


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
        attributes=freeze_coverage_mapping(
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
            location=freeze_coverage_mapping(identity["location"]),
            hierarchy=str(identity["hierarchy"]),
            subject=freeze_coverage_mapping(identity["subject"]),
            collector=freeze_coverage_mapping(identity["collector"]),
        ),
        hits_by_run=MappingProxyType(
            {str(run_id): int(hits[run_id]) for run_id in sorted(hits, key=str)}
        ),
        disposition=freeze_coverage_mapping(document["disposition"]),
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
        version=freeze_coverage_mapping(document["version"]),
        native_format=freeze_coverage_mapping(document["native_format"]),
        capabilities=tuple(_decode_capability(item) for item in capabilities),
    )


def _decode_invocation(document: Mapping[str, object]) -> Mapping[str, FrozenJson]:
    invocation = dict(document)
    started_at = str(invocation["started_at"])
    invocation["started_at"] = rfc3339_from_epoch(parse_timestamp(started_at).timestamp())
    return freeze_coverage_mapping(invocation)


def _decode_points(points: list[dict[str, object]]) -> tuple[CoveragePoint, ...]:
    """Decode the caller's row list, consuming it so each row is freed once frozen."""
    decoded = []
    while points:
        decoded.append(_decode_point(points.pop()))
    decoded.reverse()
    return tuple(decoded)


def _decode_valid_campaign(
    document: Mapping[str, object], points: list[dict[str, object]]
) -> CoverageCampaign:
    invocation = document["invocation"]
    target = document["target"]
    collector = document["collector"]
    tests = document["tests"]
    assert isinstance(invocation, Mapping)
    assert isinstance(target, Mapping)
    assert isinstance(collector, Mapping)
    assert isinstance(tests, Mapping)
    runs = tests["runs"]
    artifacts = document["artifacts"]
    findings = document["findings"]
    collection = document["collection"]
    assert isinstance(runs, list)
    assert isinstance(artifacts, list)
    assert isinstance(findings, list)
    assert isinstance(collection, Mapping)
    decoded_points = _decode_points(points)
    # Rollups exist only for complete collection; they are always derived from the
    # points and never decoded from the stored array.
    rollups = derive_coverage_rollups(decoded_points) if collection["status"] == "complete" else ()
    return CoverageCampaign(
        campaign_id=str(document["campaign_id"]),
        invocation=_decode_invocation(invocation),
        target=CoverageTarget(identity=str(target["identity"]), selector=str(target["selector"])),
        collector=_decode_collector(collector),
        build=freeze_coverage_mapping(document["build"]),
        coverage_window=freeze_coverage_mapping(document["coverage_window"]),
        fingerprints=freeze_coverage_mapping(document["fingerprints"]),
        source_closure=freeze_coverage_mapping(document["source_closure"]),
        declared_tests=tuple(str(item) for item in tests["declared"]),
        selected_tests=tuple(str(item) for item in tests["selected"]),
        runs=tuple(_decode_run(item) for item in runs),
        artifacts=tuple(_decode_artifact(item) for item in artifacts),
        normalization=freeze_coverage_mapping(document["normalization"]),
        points=decoded_points,
        rollups=rollups,
        collection=freeze_coverage_mapping(collection),
        findings=tuple(_decode_finding(item) for item in findings),
        evaluation=freeze_coverage_mapping(document["evaluation"]),
    )


def _decode_manifest(
    manifest: Mapping[str, object],
    points: list[dict[str, object]],
    expected_target: DurableTargetIdentity,
) -> CoverageCampaign:
    """Validate a manifest and its point rows once, then decode them to a Campaign.

    Consumes *points*. Stored rollups are ignored: the Campaign's are derived.
    """
    structural_findings = _structural_findings(manifest, points)
    semantic_findings = _semantic_findings(manifest, points, expected_target, structural_findings)
    if all_findings := structural_findings + semantic_findings:
        raise CoverageCampaignValidationError(all_findings)
    return _decode_valid_campaign(manifest, points)


def _encode_capability(capability: CoverageCapability) -> dict[str, object]:
    return {
        "record_class": capability.record_class,
        "status": capability.status,
        **coverage_mapping_document(capability.attributes),
    }


def _encode_run(run: CoverageRun) -> dict[str, object]:
    document: dict[str, object] = {
        "id": run.id,
        "test": run.test,
        "test_run": run.test_run,
        "variant": run.variant,
        "seed": run.seed,
        "fingerprint": run.fingerprint,
        "simulation_verdict": run.simulation_verdict,
        "collection": run.collection,
    }
    if run.raw_artifact is not None:
        document["raw_artifact"] = run.raw_artifact
    document.update(coverage_mapping_document(run.attributes))
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
    document.update(coverage_mapping_document(artifact.attributes))
    return document


def _encode_point(point: CoveragePoint) -> dict[str, object]:
    return {
        "id": point.id,
        "identity": {
            "metric": point.identity.metric,
            "location": coverage_mapping_document(point.identity.location),
            "hierarchy": point.identity.hierarchy,
            "subject": coverage_mapping_document(point.identity.subject),
            "collector": coverage_mapping_document(point.identity.collector),
        },
        "hits_by_run": dict(point.hits_by_run),
        "disposition": coverage_mapping_document(point.disposition),
    }


def encode_coverage_point(point: CoveragePoint) -> dict[str, object]:
    """Encode one normalized Coverage Point for durable point storage."""
    return _encode_point(point)


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
        "$schema": _SCHEMA,
        "campaign_id": campaign.campaign_id,
        "invocation": coverage_mapping_document(campaign.invocation),
        "target": {
            "identity": campaign.target.identity,
            "selector": campaign.target.selector,
        },
        "collector": {
            "kind": campaign.collector.kind,
            "version": coverage_mapping_document(campaign.collector.version),
            "native_format": coverage_mapping_document(campaign.collector.native_format),
            "capabilities": [
                _encode_capability(capability) for capability in campaign.collector.capabilities
            ],
        },
        "build": coverage_mapping_document(campaign.build),
        "coverage_window": coverage_mapping_document(campaign.coverage_window),
        "fingerprints": coverage_mapping_document(campaign.fingerprints),
        "source_closure": coverage_mapping_document(campaign.source_closure),
        "tests": {
            "declared": list(campaign.declared_tests),
            "selected": list(campaign.selected_tests),
            "runs": [_encode_run(run) for run in campaign.runs],
        },
        "artifacts": [_encode_artifact(artifact) for artifact in campaign.artifacts],
        "normalization": coverage_mapping_document(campaign.normalization),
        "points": [_encode_point(point) for point in campaign.points],
        "rollups": [_encode_rollup(rollup) for rollup in campaign.rollups],
        "collection": coverage_mapping_document(campaign.collection),
        "findings": [
            {
                "severity": finding.severity,
                "code": finding.code,
                "pointer": finding.pointer,
                "message": finding.message,
            }
            for finding in campaign.findings
        ],
        "evaluation": coverage_mapping_document(campaign.evaluation),
    }


def derive_coverage_rollups(points: Iterable[CoveragePoint]) -> tuple[CoverageRollup, ...]:
    """Derive canonical per-metric summaries: the one rollup function.

    One row per metric that has points, in canonical metric order.
    """
    by_metric: dict[str, list[CoveragePoint]] = {}
    for point in points:
        by_metric.setdefault(point.identity.metric, []).append(point)
    rollups: list[CoverageRollup] = []
    for metric, semantics in _METRIC_SEMANTICS.items():
        matching = by_metric.get(metric)
        if not matching:
            continue
        eligible = [point for point in matching if point.disposition.get("kind") == "eligible"]
        waived = [point for point in matching if point.disposition.get("kind") == "waived"]
        covered = [point for point in eligible if sum(point.hits_by_run.values()) > 0]
        percent = round(len(covered) * 100 / len(eligible), 2) if eligible else None
        rollups.append(
            CoverageRollup(
                metric, semantics, len(matching), len(eligible), len(covered), len(waived), percent
            )
        )
    return tuple(rollups)


def apply_point_dispositions(
    campaign: CoverageCampaign, dispositions: Mapping[str, Mapping[str, object]]
) -> CoverageCampaign:
    """Return *campaign* with the named points' dispositions and the rollups they imply.

    *dispositions* maps Coverage Point id to its new disposition; other points
    are unchanged. Rollups exist only for complete, compatible collection, so a
    Campaign with none keeps none and a Campaign with rollups gets exactly the
    derivation of its new points. Rows are always canonical: one per metric with
    points, with the canonical semantics text.
    """
    points = tuple(
        replace(point, disposition=freeze_coverage_mapping(dispositions[point.id]))
        if point.id in dispositions
        else point
        for point in campaign.points
    )
    rollups = derive_coverage_rollups(points) if campaign.rollups else ()
    return replace(campaign, points=points, rollups=rollups)


def coverage_metric_semantics(metric: str) -> str:
    """Return the canonical persisted description for one coverage metric."""
    return _METRIC_SEMANTICS[metric]


def derive_coverage_source_rollups(
    points: Iterable[CoveragePoint],
) -> tuple[CoverageSourceRollup, ...]:
    """Derive canonical source-file summaries without using hierarchy."""
    points_by_source: dict[str, list[CoveragePoint]] = {}
    for point in points:
        points_by_source.setdefault(point.identity.source, []).append(point)
    summaries: list[CoverageSourceRollup] = []
    for source in sorted(points_by_source):
        derived = {item.metric: item for item in derive_coverage_rollups(points_by_source[source])}
        rollups = tuple(
            derived.get(metric)
            or CoverageRollup(metric, _METRIC_SEMANTICS[metric], 0, 0, 0, 0, None)
            for metric in SOURCE_ROLLUP_METRICS
        )
        summaries.append(CoverageSourceRollup(source, rollups))
    return tuple(summaries)
