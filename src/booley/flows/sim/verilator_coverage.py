"""Verilator-native coverage collection behind one deep collector interface.

This module is preserved as blocked Phase 3 work.  It must remain unintegrated
until Booley pins the first stable Verilator release that both contains upstream
fix ``a6f4dd031f50387ae0169490c6d8843b91dd1c07`` and supports the required
``--coverage-per-instance`` option.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from .coverage_campaign import (
    CoverageArtifact,
    CoverageCapability,
    CoverageFinding,
    CoveragePoint,
    CoveragePointIdentity,
    CoverageRun,
    SimulationVerdict,
)

CoverageHarness = Literal["generated_main", "custom_main", "cocotb", "hdl_testbench"]
CoverageSourceKind = Literal["rtl", "testbench", "generated", "foreign"]
CoverageCollectionStatus = Literal["complete", "collector_error"]

VERILATOR_COVERAGE_INSTRUMENTATION = (
    "--coverage-line",
    "--coverage-toggle",
    "--coverage-expr",
    "--coverage-user",
    "--coverage-per-instance",
)


@dataclass(frozen=True)
class VerilatorCollectorIdentity:
    """Exact stable Verilator tag and full upstream commit identity."""

    tag: str
    commit: str


PINNED_VERILATOR = VerilatorCollectorIdentity(
    tag="v5.046",
    commit="24b2ac24c721fdad89bba75a492e02c6aa63f32e",
)
# This records Booley's current image identity for the blocked smoke test.  It
# is not a viable Phase 3 pin: v5.046 rejects --coverage-per-instance.

_NATIVE_HEADER = "# SystemC::Coverage-3"
_NATIVE_RECORD_RE = re.compile(r"^C '(?P<identity>.*)' (?P<hits>[0-9]+)$")
_RECORD_METRICS = {
    "line": "line",
    "branch": "branch",
    "expr": "expression",
    "toggle": "toggle",
    "user": "cover_property",
    "fsm": "fsm",
    "covergroup": "covergroup",
}
_SCORED_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})
_FRESHNESS_CLOCK_TOLERANCE_NS = 1_000_000_000


class _NativeRecordError(ValueError):
    """A compatible native file contains an unqueryable record."""


class _NativeFormatError(ValueError):
    """A native file uses a header outside the pinned compatibility contract."""


class _HookEvidenceError(ValueError):
    """A Coverage Window hook contract failed for one simulator process."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _MergeError(RuntimeError):
    """The native merge could not establish equivalent complete evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        artifact: CoverageArtifact | None = None,
    ) -> None:
        self.code = code
        self.artifact = artifact
        super().__init__(message)


@dataclass(frozen=True)
class CoverageSource:
    """One explicit native-to-Campaign source mapping from the resolved Target."""

    native_path: str
    path: str
    kind: CoverageSourceKind


@dataclass(frozen=True)
class CoverageTarget:
    """Resolved Target facts required by native collection."""

    identity: str
    selector: str
    toplevel: str
    harness: CoverageHarness
    sources: tuple[CoverageSource, ...]
    custom_main_hooks: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectedCoverageTest:
    """One registered test selected for an independent simulator process."""

    name: str


@dataclass(frozen=True)
class CoverageCollectionRequest:
    """All resolved, policy-free input for one Target collection."""

    target: CoverageTarget
    selected_tests: tuple[SelectedCoverageTest, ...]
    artifact_root: Path
    trace: bool = False
    reset_included: bool = True


@dataclass(frozen=True)
class SimulationBuildVariant:
    """Typed simulator build identity across trace and coverage dimensions."""

    trace: bool
    coverage: bool

    @property
    def name(self) -> str:
        """Return the stable directory suffix for this build variant."""
        if self.trace and self.coverage:
            return "trace-coverage"
        if self.trace:
            return "trace"
        if self.coverage:
            return "coverage"
        return ""


@dataclass(frozen=True)
class SimulationBuildRequest:
    """Collector-selected build envelope supplied to Simulation execution."""

    target: CoverageTarget
    variant: SimulationBuildVariant
    instrumentation: tuple[str, ...]


@dataclass(frozen=True)
class SimulationBuildResult:
    """Mechanical build outcome returned by the execution port."""

    success: bool
    output: str = ""
    collector: VerilatorCollectorIdentity | None = None


@dataclass(frozen=True)
class SimulationRunRequest:
    """One collector-owned process envelope and unique native destination."""

    target: CoverageTarget
    test: SelectedCoverageTest
    run_id: str
    raw_path: Path
    hook_evidence_path: Path | None
    trace: bool
    argv_suffix: tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class SimulationRunResult:
    """Independent Simulation truth returned by the execution port."""

    verdict: SimulationVerdict
    output: str = ""


@dataclass(frozen=True)
class SimulationCommandRequest:
    """One collector-selected native utility command."""

    argv: tuple[str, ...]
    cwd: Path
    output_path: Path


@dataclass(frozen=True)
class SimulationCommandResult:
    """Captured result of one native utility command."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class SimulationExecutionPort(Protocol):
    """Substitutable Simulation build/run/command execution seam."""

    def build(self, request: SimulationBuildRequest) -> SimulationBuildResult: ...

    def run(self, request: SimulationRunRequest) -> SimulationRunResult: ...

    def command(self, request: SimulationCommandRequest) -> SimulationCommandResult: ...


@dataclass(frozen=True)
class CoverageBuildEvidence:
    """Exact collector-selected build dimensions and instrumentation."""

    variant: SimulationBuildVariant
    instrumentation: tuple[str, ...]


@dataclass(frozen=True)
class NativeMergeEvidence:
    """Independent comparison between native merge and per-run normalization."""

    status: Literal["equivalent", "not_run", "failed", "mismatch"]
    artifact: str | None = None


@dataclass(frozen=True)
class NativeFormatEvidence:
    """Observed compatibility with the pinned Verilator native format."""

    name: str
    compatibility: Literal["compatible", "incompatible", "unknown"]


@dataclass(frozen=True)
class CoverageWindowEvidence:
    """Coverage Window mode and verified per-process hook evidence."""

    mode: Literal["whole_run", "post_reset"]
    hook_artifacts: tuple[str, ...]


@dataclass(frozen=True)
class CoverageCollectionResult:
    """Immutable policy-free evidence returned by the native collector."""

    status: CoverageCollectionStatus
    build: CoverageBuildEvidence
    runs: tuple[CoverageRun, ...]
    artifacts: tuple[CoverageArtifact, ...]
    points: tuple[CoveragePoint, ...]
    capabilities: tuple[CoverageCapability, ...]
    findings: tuple[CoverageFinding, ...]
    merge: NativeMergeEvidence
    native_format: NativeFormatEvidence
    coverage_window: CoverageWindowEvidence
    collector: VerilatorCollectorIdentity


@dataclass(frozen=True)
class _NativeRecord:
    identity: str
    attributes: Mapping[str, str]
    hits: int


@dataclass(frozen=True)
class _CollectedRun:
    run: CoverageRun
    artifact: CoverageArtifact | None
    records: tuple[_NativeRecord, ...]
    findings: tuple[CoverageFinding, ...] = ()
    hook_artifact: CoverageArtifact | None = None


def _path_component(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return value or "test"


def _parse_attributes(identity: str) -> Mapping[str, str]:
    attributes: dict[str, str] = {}
    for field in identity.split("\x01"):
        if not field:
            continue
        key, separator, value = field.partition("\x02")
        if not separator or not key or key in attributes:
            raise ValueError("malformed or duplicate native coverage attribute")
        attributes[key] = value
    return MappingProxyType(dict(sorted(attributes.items())))


def _parse_native(path: Path) -> tuple[_NativeRecord, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != _NATIVE_HEADER:
        raise _NativeFormatError("incompatible Verilator native coverage header")
    records: list[_NativeRecord] = []
    for line in lines[1:]:
        if not line:
            continue
        match = _NATIVE_RECORD_RE.fullmatch(line)
        if match is None:
            raise _NativeRecordError("malformed Verilator native coverage record")
        identity = match["identity"]
        records.append(
            _NativeRecord(
                identity=identity,
                attributes=_parse_attributes(identity),
                hits=int(match["hits"]),
            )
        )
    return tuple(records)


def _artifact(
    path: Path,
    root: Path,
    *,
    artifact_id: str,
    kind: str,
    run_id: str | None,
    state: str = "fresh_queryable",
) -> CoverageArtifact:
    content = path.read_bytes()
    return CoverageArtifact(
        id=artifact_id,
        kind=kind,
        path=path.relative_to(root).as_posix(),
        sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
        bytes=len(content),
        state=state,
        owner_run=run_id,
        attributes=MappingProxyType({}),
    )


def _collect_one_run(
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
    index: int,
    selected: SelectedCoverageTest,
) -> _CollectedRun:
    run_id = f"run:{index:03d}:{_path_component(selected.name)}"
    raw_path = request.artifact_root / "native" / "raw" / f"{index:03d}-{_path_component(selected.name)}.dat"
    hook_path = (
        request.artifact_root / "hooks" / f"{index:03d}-{_path_component(selected.name)}.json"
        if not request.reset_included or request.target.harness == "custom_main"
        else None
    )
    dispatched_ns = time.time_ns()
    custom_main = request.target.harness == "custom_main"
    argv_suffix = () if custom_main else (f"+verilator+coverage+file+{raw_path}",)
    environment = {"BOOLEY_COVERAGE_FILE": str(raw_path)} if custom_main else {}
    if hook_path is not None:
        environment["BOOLEY_COVERAGE_HOOK_EVIDENCE"] = str(hook_path)
    run_result = execution.run(
        SimulationRunRequest(
            target=request.target,
            test=selected,
            run_id=run_id,
            raw_path=raw_path,
            hook_evidence_path=hook_path,
            trace=request.trace,
            argv_suffix=argv_suffix,
            environment=MappingProxyType(environment),
        )
    )
    if not raw_path.is_file():
        finding = CoverageFinding(
            severity="error",
            code="COV_RAW_FILE_MISSING",
            pointer=f"/tests/runs/{index - 1}/raw_artifact",
            message=f"Test {selected.name!r} produced no native coverage database.",
        )
        return _CollectedRun(
            run=CoverageRun(
                id=run_id,
                test=selected.name,
                simulation_verdict=run_result.verdict,
                collection="collector_error",
                raw_artifact=None,
                attributes=MappingProxyType({}),
            ),
            artifact=None,
            records=(),
            findings=(finding,),
        )
    artifact_id = f"artifact:raw:{index:03d}"
    if raw_path.stat().st_mtime_ns < dispatched_ns - _FRESHNESS_CLOCK_TOLERANCE_NS:
        finding = CoverageFinding(
            severity="error",
            code="COV_RAW_FILE_STALE",
            pointer=f"/tests/runs/{index - 1}/raw_artifact",
            message=f"Test {selected.name!r} did not freshly write its native database.",
        )
        return _CollectedRun(
            run=CoverageRun(
                id=run_id,
                test=selected.name,
                simulation_verdict=run_result.verdict,
                collection="collector_error",
                raw_artifact=artifact_id,
                attributes=MappingProxyType({}),
            ),
            artifact=_artifact(
                raw_path,
                request.artifact_root,
                artifact_id=artifact_id,
                kind="raw_native",
                run_id=run_id,
                state="stale",
            ),
            records=(),
            findings=(finding,),
        )
    try:
        records = _parse_native(raw_path)
    except _NativeFormatError:
        finding = CoverageFinding(
            severity="error",
            code="COV_NATIVE_FORMAT_INCOMPATIBLE",
            pointer=f"/tests/runs/{index - 1}/raw_artifact",
            message=f"Test {selected.name!r} produced an incompatible native database.",
        )
        return _CollectedRun(
            run=CoverageRun(
                id=run_id,
                test=selected.name,
                simulation_verdict=run_result.verdict,
                collection="collector_error",
                raw_artifact=artifact_id,
                attributes=MappingProxyType({}),
            ),
            artifact=_artifact(
                raw_path,
                request.artifact_root,
                artifact_id=artifact_id,
                kind="raw_native",
                run_id=run_id,
                state="incompatible",
            ),
            records=(),
            findings=(finding,),
        )
    except _NativeRecordError:
        finding = CoverageFinding(
            severity="error",
            code="COV_RAW_NOT_QUERYABLE",
            pointer=f"/tests/runs/{index - 1}/raw_artifact",
            message=f"Test {selected.name!r} produced an unqueryable native database.",
        )
        return _CollectedRun(
            run=CoverageRun(
                id=run_id,
                test=selected.name,
                simulation_verdict=run_result.verdict,
                collection="collector_error",
                raw_artifact=artifact_id,
                attributes=MappingProxyType({}),
            ),
            artifact=_artifact(
                raw_path,
                request.artifact_root,
                artifact_id=artifact_id,
                kind="raw_native",
                run_id=run_id,
                state="unqueryable",
            ),
            records=(),
            findings=(finding,),
        )
    raw_artifact = _artifact(
        raw_path,
        request.artifact_root,
        artifact_id=artifact_id,
        kind="raw_native",
        run_id=run_id,
    )
    hook_artifact = None
    if hook_path is not None:
        try:
            hook_artifact = _validate_hook_evidence(
                hook_path,
                request.artifact_root,
                run_id=run_id,
                index=index,
                dispatched_ns=dispatched_ns,
                require_start=not request.reset_included,
                require_write=request.target.harness == "custom_main",
            )
        except _HookEvidenceError as exc:
            finding = CoverageFinding(
                severity="error",
                code=exc.code,
                pointer=f"/tests/runs/{index - 1}/hook_evidence",
                message=str(exc),
            )
            return _CollectedRun(
                run=CoverageRun(
                    id=run_id,
                    test=selected.name,
                    simulation_verdict=run_result.verdict,
                    collection="collector_error",
                    raw_artifact=artifact_id,
                    attributes=MappingProxyType({}),
                ),
                artifact=raw_artifact,
                records=records,
                findings=(finding,),
            )
    return _CollectedRun(
        run=CoverageRun(
            id=run_id,
            test=selected.name,
            simulation_verdict=run_result.verdict,
            collection="included",
            raw_artifact=artifact_id,
            attributes=MappingProxyType({}),
        ),
        artifact=raw_artifact,
        records=records,
        hook_artifact=hook_artifact,
    )


def _validate_hook_evidence(
    path: Path,
    root: Path,
    *,
    run_id: str,
    index: int,
    dispatched_ns: int,
    require_start: bool,
    require_write: bool,
) -> CoverageArtifact:
    if (
        not path.is_file()
        or path.stat().st_mtime_ns < dispatched_ns - _FRESHNESS_CLOCK_TOLERANCE_NS
    ):
        raise _HookEvidenceError(
            "COV_WINDOW_HOOK_MISSING",
            "Coverage start hook produced no fresh evidence.",
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("$schema") != "booley.coverage-hook/v1":
        raise ValueError("coverage hook evidence has an invalid schema")
    events = document.get("events")
    if document.get("run_id") != run_id or not isinstance(events, list):
        raise ValueError("coverage hook evidence does not identify its run")
    starts = _hook_events(events, "start")
    writes = _hook_events(events, "write")
    if require_start:
        _require_hook(starts, "WINDOW")
    if require_write:
        _require_hook(writes, "WRITE")
    if require_start and require_write and starts[0]["sequence"] >= writes[0]["sequence"]:
        raise _HookEvidenceError(
            "COV_CUSTOM_MAIN_HOOK_OUT_OF_ORDER",
            "Custom-main start_hook must run before write_hook.",
        )
    return _artifact(
        path,
        root,
        artifact_id=f"artifact:hook:{index:03d}",
        kind="coverage_hook_evidence",
        run_id=run_id,
    )


def _hook_events(events: list[object], name: str) -> list[dict[str, object]]:
    return [event for event in events if isinstance(event, dict) and event.get("hook") == name]


def _require_hook(events: list[dict[str, object]], code_stem: str) -> None:
    if len(events) > 1:
        raise _HookEvidenceError(
            f"COV_{code_stem}_HOOK_DUPLICATE",
            f"Coverage {code_stem.lower()} hook ran more than once.",
        )
    if not events:
        raise _HookEvidenceError(
            f"COV_{code_stem}_HOOK_MISSING",
            f"Coverage {code_stem.lower()} hook did not run.",
        )
    if events[0].get("success") is not True:
        raise _HookEvidenceError(
            f"COV_{code_stem}_HOOK_FAILED",
            f"Coverage {code_stem.lower()} hook did not complete successfully.",
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _point_id(identity: Mapping[str, object]) -> str:
    payload = base64.urlsafe_b64encode(_canonical_json(identity)).decode().rstrip("=")
    return f"cp1:{payload}"


def _subject(metric: str, attributes: Mapping[str, str]) -> Mapping[str, str]:
    labels = {
        "line": "basic_block",
        "branch": "outcome",
        "expression": "outcome",
        "toggle": "signal_bit_direction",
        "cover_property": "cover_property",
        "fsm": "native_fsm",
        "covergroup": "native_covergroup",
    }
    return MappingProxyType({labels[metric]: attributes.get("o", "")})


def _point_for_record(
    record: _NativeRecord,
    source: CoverageSource,
    hits_by_run: Mapping[str, int],
) -> CoveragePoint:
    record_type = record.attributes.get("t", "")
    metric = _RECORD_METRICS[record_type]
    line = int(record.attributes.get("l", "0"))
    column = int(record.attributes.get("n", "0"))
    identity_document: dict[str, object] = {
        "metric": metric,
        "location": {
            "source": source.path,
            "start": {"line": line, "column": column},
            "end": {"line": line, "column": column},
        },
        "hierarchy": record.attributes.get("h", ""),
        "subject": dict(_subject(metric, record.attributes)),
        "collector": {"record_type": record_type, "native_key": record.identity},
    }
    return CoveragePoint(
        id=_point_id(identity_document),
        identity=CoveragePointIdentity(
            metric=metric,
            location=MappingProxyType(identity_document["location"]),
            hierarchy=str(identity_document["hierarchy"]),
            subject=MappingProxyType(identity_document["subject"]),
            collector=MappingProxyType(identity_document["collector"]),
        ),
        hits_by_run=MappingProxyType(dict(sorted(hits_by_run.items()))),
        disposition=MappingProxyType(
            {
                "kind": (
                    "eligible"
                    if source.kind == "rtl" and metric in _SCORED_METRICS
                    else "unscored"
                )
            }
        ),
    )


def _normalize_points(
    request: CoverageCollectionRequest, collected: tuple[_CollectedRun, ...]
) -> tuple[CoveragePoint, ...]:
    sources = {source.native_path: source for source in request.target.sources}
    incidence: dict[str, dict[str, int]] = {}
    records: dict[str, _NativeRecord] = {}
    for item in collected:
        for record in item.records:
            records[record.identity] = record
            if record.hits > 0:
                incidence.setdefault(record.identity, {})[item.run.id] = record.hits
    points = [
        _point_for_record(record, sources[record.attributes["f"]], incidence.get(identity, {}))
        for identity, record in records.items()
        if record.attributes.get("t") in _RECORD_METRICS
    ]
    return tuple(sorted(points, key=lambda point: (point.identity.metric, point.id)))


def _capabilities_and_findings(
    collected: tuple[_CollectedRun, ...],
) -> tuple[tuple[CoverageCapability, ...], tuple[CoverageFinding, ...]]:
    record_types = {
        record.attributes.get("t", "") for item in collected for record in item.records
    }
    capabilities: list[CoverageCapability] = []
    findings: list[CoverageFinding] = []
    for record_type in sorted(record_types):
        record_class = _RECORD_METRICS.get(record_type, record_type)
        capabilities.append(
            CoverageCapability(
                record_class=record_class,
                status="reported",
                attributes=MappingProxyType({"native_record_type": record_type}),
            )
        )
        if record_type not in _RECORD_METRICS:
            findings.append(
                CoverageFinding(
                    severity="warning",
                    code="COV_NATIVE_RECORD_UNKNOWN",
                    pointer="/normalization/unrecognized_records",
                    message=f"Native record class {record_type!r} was retained but not normalized.",
                )
            )
    return tuple(capabilities), tuple(findings)


def _merge(
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
    collected: tuple[_CollectedRun, ...],
) -> tuple[CoverageArtifact, tuple[_NativeRecord, ...]]:
    merged_path = request.artifact_root / "native" / "merged" / "coverage.dat"
    raw_paths = tuple(
        request.artifact_root / item.artifact.path
        for item in collected
        if item.artifact is not None
    )
    command = SimulationCommandRequest(
        argv=("verilator_coverage", "--write", str(merged_path), *(str(path) for path in raw_paths)),
        cwd=request.artifact_root,
        output_path=merged_path,
    )
    dispatched_ns = time.time_ns()
    result = execution.command(command)
    if result.returncode != 0:
        raise _MergeError("COV_NATIVE_MERGE_FAILED", "Verilator native merge failed.")
    if not merged_path.is_file():
        raise _MergeError(
            "COV_NATIVE_MERGE_MISSING",
            "Verilator native merge produced no output database.",
        )
    if merged_path.stat().st_mtime_ns < dispatched_ns - _FRESHNESS_CLOCK_TOLERANCE_NS:
        artifact = _artifact(
            merged_path,
            request.artifact_root,
            artifact_id="artifact:merged",
            kind="merged_native",
            run_id=None,
            state="stale",
        )
        raise _MergeError(
            "COV_NATIVE_MERGE_STALE",
            "Verilator native merge output was not freshly written.",
            artifact=artifact,
        )
    try:
        records = _parse_native(merged_path)
    except (_NativeFormatError, _NativeRecordError, UnicodeError):
        artifact = _artifact(
            merged_path,
            request.artifact_root,
            artifact_id="artifact:merged",
            kind="merged_native",
            run_id=None,
            state="unqueryable",
        )
        raise _MergeError(
            "COV_NATIVE_MERGE_NOT_QUERYABLE",
            "Verilator native merge output was not queryable.",
            artifact=artifact,
        ) from None
    return (
        _artifact(
            merged_path,
            request.artifact_root,
            artifact_id="artifact:merged",
            kind="merged_native",
            run_id=None,
        ),
        records,
    )


def _record_totals(records: tuple[_NativeRecord, ...]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for record in records:
        totals[record.identity] = totals.get(record.identity, 0) + record.hits
    return totals


def _result_artifacts(collected: tuple[_CollectedRun, ...]) -> tuple[CoverageArtifact, ...]:
    artifacts: list[CoverageArtifact] = []
    for item in collected:
        if item.artifact is not None:
            artifacts.append(item.artifact)
        if item.hook_artifact is not None:
            artifacts.append(item.hook_artifact)
    return tuple(artifacts)


def _window_evidence(
    request: CoverageCollectionRequest, collected: tuple[_CollectedRun, ...]
) -> CoverageWindowEvidence:
    return CoverageWindowEvidence(
        mode="whole_run" if request.reset_included else "post_reset",
        hook_artifacts=tuple(
            item.hook_artifact.id for item in collected if item.hook_artifact is not None
        ),
    )


def _request_finding(request: CoverageCollectionRequest) -> CoverageFinding | None:
    target = request.target
    if target.harness != "custom_main":
        return None
    hooks = target.custom_main_hooks
    if len(set(hooks)) != len(hooks):
        code = "COV_CUSTOM_MAIN_HOOK_DUPLICATE"
        message = "Custom-main hook declarations must not contain duplicates."
    elif set(hooks) - {"start_hook", "write_hook"}:
        code = "COV_CUSTOM_MAIN_HOOK_UNKNOWN"
        message = "Custom-main hook declarations contain an unknown hook."
    elif "write_hook" not in hooks:
        code = "COV_CUSTOM_MAIN_WRITE_HOOK_REQUIRED"
        message = "A custom main must declare write_hook."
    elif not request.reset_included and "start_hook" not in hooks:
        code = "COV_CUSTOM_MAIN_START_HOOK_REQUIRED"
        message = "A post-reset custom main must declare start_hook."
    elif request.reset_included and "start_hook" in hooks:
        code = "COV_CUSTOM_MAIN_START_HOOK_UNNECESSARY"
        message = "start_hook is invalid when reset activity is included."
    else:
        return None
    return CoverageFinding("error", code, "/target/custom_main_hooks", message)


def _preflight_error_result(
    request: CoverageCollectionRequest,
    build: CoverageBuildEvidence,
    finding: CoverageFinding,
) -> CoverageCollectionResult:
    return CoverageCollectionResult(
        status="collector_error",
        build=build,
        runs=(),
        artifacts=(),
        points=(),
        capabilities=(),
        findings=(finding,),
        merge=NativeMergeEvidence("not_run"),
        native_format=NativeFormatEvidence("verilator-coverage", "unknown"),
        coverage_window=_window_evidence(request, ()),
        collector=PINNED_VERILATOR,
    )


def _build_error_result(
    request: CoverageCollectionRequest,
    build: CoverageBuildEvidence,
    output: str,
) -> CoverageCollectionResult:
    runs = tuple(
        CoverageRun(
            id=f"run:{index:03d}:{_path_component(selected.name)}",
            test=selected.name,
            simulation_verdict="elab_error",
            collection="collector_error",
            raw_artifact=None,
            attributes=MappingProxyType({}),
        )
        for index, selected in enumerate(request.selected_tests, start=1)
    )
    finding = CoverageFinding(
        "error",
        "COV_COVERAGE_BUILD_FAILED",
        "/build",
        "Verilator rejected or could not build the coverage-instrumented model. "
        + output.strip()[-1000:],
    )
    return CoverageCollectionResult(
        status="collector_error",
        build=build,
        runs=runs,
        artifacts=(),
        points=(),
        capabilities=(),
        findings=(finding,),
        merge=NativeMergeEvidence("not_run"),
        native_format=NativeFormatEvidence("verilator-coverage", "unknown"),
        coverage_window=_window_evidence(request, ()),
        collector=PINNED_VERILATOR,
    )


def collect(  # noqa: PLR0911 - each return preserves a distinct evidence gate
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
) -> CoverageCollectionResult:
    """Collect and normalize one Verilator-native database per selected test."""
    variant = SimulationBuildVariant(trace=request.trace, coverage=True)
    build = CoverageBuildEvidence(variant, VERILATOR_COVERAGE_INSTRUMENTATION)
    request_finding = _request_finding(request)
    if request_finding is not None:
        return _preflight_error_result(request, build, request_finding)
    build_result = execution.build(
        SimulationBuildRequest(request.target, variant, VERILATOR_COVERAGE_INSTRUMENTATION)
    )
    if not build_result.success:
        return _build_error_result(request, build, build_result.output)
    if build_result.collector != PINNED_VERILATOR:
        finding = CoverageFinding(
            "error",
            "COV_VERILATOR_IDENTITY_MISMATCH",
            "/collector/version",
            "Coverage collection requires the exact pinned stable Verilator identity.",
        )
        return _preflight_error_result(request, build, finding)
    collected = tuple(
        _collect_one_run(request, execution, index, selected)
        for index, selected in enumerate(request.selected_tests, start=1)
    )
    findings = tuple(finding for item in collected for finding in item.findings)
    if findings:
        return CoverageCollectionResult(
            status="collector_error",
            build=build,
            runs=tuple(item.run for item in collected),
            artifacts=_result_artifacts(collected),
            points=_normalize_points(request, collected),
            capabilities=(),
            findings=findings,
            merge=NativeMergeEvidence("not_run"),
            native_format=NativeFormatEvidence(
                "verilator-coverage",
                "incompatible"
                if any(finding.code == "COV_NATIVE_FORMAT_INCOMPATIBLE" for finding in findings)
                else "unknown",
            ),
            coverage_window=_window_evidence(request, collected),
            collector=PINNED_VERILATOR,
        )
    points = _normalize_points(request, collected)
    capabilities, normalization_findings = _capabilities_and_findings(collected)
    try:
        merged_artifact, merged_records = _merge(request, execution, collected)
    except _MergeError as exc:
        finding = CoverageFinding(
            "error",
            exc.code,
            "/collection/merge",
            str(exc),
        )
        artifacts = _result_artifacts(collected)
        if exc.artifact is not None:
            artifacts = (*artifacts, exc.artifact)
        return CoverageCollectionResult(
            status="collector_error",
            build=build,
            runs=tuple(item.run for item in collected),
            artifacts=artifacts,
            points=points,
            capabilities=capabilities,
            findings=(*normalization_findings, finding),
            merge=NativeMergeEvidence("failed"),
            native_format=NativeFormatEvidence("verilator-coverage", "compatible"),
            coverage_window=_window_evidence(request, collected),
            collector=PINNED_VERILATOR,
        )
    raw_totals = _record_totals(tuple(record for item in collected for record in item.records))
    if raw_totals != _record_totals(merged_records):
        finding = CoverageFinding(
            "error",
            "COV_NATIVE_MERGE_MISMATCH",
            "/collection/merge",
            "Native merge disagrees with independently normalized per-run counts.",
        )
        return CoverageCollectionResult(
            status="collector_error",
            build=build,
            runs=tuple(item.run for item in collected),
            artifacts=(*_result_artifacts(collected), merged_artifact),
            points=points,
            capabilities=capabilities,
            findings=(*normalization_findings, finding),
            merge=NativeMergeEvidence("mismatch", merged_artifact.id),
            native_format=NativeFormatEvidence("verilator-coverage", "compatible"),
            coverage_window=_window_evidence(request, collected),
            collector=PINNED_VERILATOR,
        )
    return CoverageCollectionResult(
        status="complete",
        build=build,
        runs=tuple(item.run for item in collected),
        artifacts=(*_result_artifacts(collected), merged_artifact),
        points=points,
        capabilities=capabilities,
        findings=normalization_findings,
        merge=NativeMergeEvidence("equivalent", merged_artifact.id),
        native_format=NativeFormatEvidence("verilator-coverage", "compatible"),
        coverage_window=_window_evidence(request, collected),
        collector=PINNED_VERILATOR,
    )
