"""Verilator-native coverage collection behind one deep collector interface."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    FrozenJson,
    SimulationVerdict,
)
from .execution.freshness import (
    ArtifactStamp,
    ArtifactValidationError,
    snapshot_artifact,
    validate_fresh_artifact,
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
    tag="v5.052",
    commit="ea338be98e1e838d3518809ce8899f85a009963c",
)

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
    infrastructure_error: bool = False


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
    unrecognized_records: tuple[Mapping[str, FrozenJson], ...] = ()
    infrastructure_error: bool = False


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


@dataclass(frozen=True)
class _RunContext:
    run_id: str
    index: int
    test: SelectedCoverageTest
    raw_path: Path
    raw_before: ArtifactStamp | None
    hook_path: Path | None
    hook_before: ArtifactStamp | None


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
            raise _NativeRecordError("malformed or duplicate native coverage attribute")
        attributes[key] = value
    return MappingProxyType(dict(sorted(attributes.items())))


def _validate_record_attributes(attributes: Mapping[str, str]) -> None:
    for name in ("l", "n"):
        value = attributes.get(name)
        if value is not None and not value.isdigit():
            raise _NativeRecordError(
                f"native coverage attribute {name!r} must be a nonnegative integer"
            )


def _parse_native(path: Path) -> tuple[_NativeRecord, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _NativeRecordError("native coverage database is not readable UTF-8") from exc
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
        attributes = _parse_attributes(identity)
        _validate_record_attributes(attributes)
        records.append(
            _NativeRecord(
                identity=identity,
                attributes=attributes,
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
    context = _prepare_run(request, index, selected)
    result = execution.run(_run_request(request, context))
    failure = _read_raw_artifact(request, context, result.verdict)
    if isinstance(failure, _CollectedRun):
        return failure
    raw_artifact, records = failure
    return _finish_run(request, context, result.verdict, raw_artifact, records)


def _prepare_run(
    request: CoverageCollectionRequest,
    index: int,
    selected: SelectedCoverageTest,
) -> _RunContext:
    run_id = f"run:{index:03d}:{_path_component(selected.name)}"
    raw_path = (
        request.artifact_root
        / "native"
        / "raw"
        / f"{index:03d}-{_path_component(selected.name)}.dat"
    )
    hook_path = (
        request.artifact_root / "hooks" / f"{index:03d}-{_path_component(selected.name)}.json"
        if not request.reset_included or request.target.harness == "custom_main"
        else None
    )
    return _RunContext(
        run_id,
        index,
        selected,
        raw_path,
        snapshot_artifact(raw_path),
        hook_path,
        snapshot_artifact(hook_path) if hook_path is not None else None,
    )


def _run_request(request: CoverageCollectionRequest, context: _RunContext) -> SimulationRunRequest:
    custom_main = request.target.harness == "custom_main"
    suffix = () if custom_main else (f"+verilator+coverage+file+{context.raw_path}",)
    environment = {"BOOLEY_COVERAGE_FILE": str(context.raw_path)} if custom_main else {}
    environment["BOOLEY_COVERAGE_RUN_ID"] = context.run_id
    if context.hook_path is not None:
        environment["BOOLEY_COVERAGE_HOOK_EVIDENCE"] = str(context.hook_path)
    return SimulationRunRequest(
        request.target,
        context.test,
        context.run_id,
        context.raw_path,
        context.hook_path,
        request.trace,
        suffix,
        MappingProxyType(environment),
    )


def _read_raw_artifact(
    request: CoverageCollectionRequest,
    context: _RunContext,
    verdict: SimulationVerdict,
) -> tuple[CoverageArtifact, tuple[_NativeRecord, ...]] | _CollectedRun:
    try:
        validate_fresh_artifact(
            context.raw_path,
            roots=(request.artifact_root,),
            before=context.raw_before,
        )
    except ArtifactValidationError:
        missing = not context.raw_path.is_file()
        return _run_failure(
            request,
            context,
            verdict,
            code="COV_RAW_FILE_MISSING" if missing else "COV_RAW_FILE_STALE",
            message=_raw_freshness_message(context.test.name, missing),
            state=None if missing else "stale",
        )
    try:
        records = _parse_native(context.raw_path)
    except _NativeFormatError:
        return _run_failure(
            request,
            context,
            verdict,
            code="COV_NATIVE_FORMAT_INCOMPATIBLE",
            message=f"Test {context.test.name!r} produced an incompatible native database.",
            state="incompatible",
        )
    except _NativeRecordError:
        return _run_failure(
            request,
            context,
            verdict,
            code="COV_RAW_NOT_QUERYABLE",
            message=f"Test {context.test.name!r} produced an unqueryable native database.",
            state="unqueryable",
        )
    artifact = _artifact(
        context.raw_path,
        request.artifact_root,
        artifact_id=f"artifact:raw:{context.index:03d}",
        kind="raw_native",
        run_id=context.run_id,
    )
    return artifact, records


def _raw_freshness_message(test: str, missing: bool) -> str:
    if missing:
        return f"Test {test!r} produced no native coverage database."
    return f"Test {test!r} did not freshly write its native database."


def _finish_run(
    request: CoverageCollectionRequest,
    context: _RunContext,
    verdict: SimulationVerdict,
    raw_artifact: CoverageArtifact,
    records: tuple[_NativeRecord, ...],
) -> _CollectedRun:
    hook_artifact = None
    if context.hook_path is not None:
        try:
            hook_artifact = _validate_hook_evidence(
                context.hook_path,
                request.artifact_root,
                run_id=context.run_id,
                index=context.index,
                before=context.hook_before,
                require_start=not request.reset_included,
                require_write=request.target.harness == "custom_main",
            )
        except _HookEvidenceError as exc:
            return _run_failure(
                request,
                context,
                verdict,
                code=exc.code,
                message=str(exc),
                artifact=raw_artifact,
                records=records,
                pointer="hook_evidence",
            )
    return _CollectedRun(
        run=CoverageRun(
            id=context.run_id,
            test=context.test.name,
            simulation_verdict=verdict,
            collection="included",
            raw_artifact=raw_artifact.id,
            attributes=MappingProxyType({}),
        ),
        artifact=raw_artifact,
        records=records,
        hook_artifact=hook_artifact,
    )


def _run_failure(
    request: CoverageCollectionRequest,
    context: _RunContext,
    verdict: SimulationVerdict,
    *,
    code: str,
    message: str,
    state: str | None = None,
    artifact: CoverageArtifact | None = None,
    records: tuple[_NativeRecord, ...] = (),
    pointer: str = "raw_artifact",
) -> _CollectedRun:
    artifact_id = f"artifact:raw:{context.index:03d}" if state else None
    if state is not None:
        artifact = _artifact(
            context.raw_path,
            request.artifact_root,
            artifact_id=artifact_id or "",
            kind="raw_native",
            run_id=context.run_id,
            state=state,
        )
    run = CoverageRun(
        context.run_id,
        context.test.name,
        verdict,
        "collector_error",
        artifact.id if artifact is not None else None,
        MappingProxyType({}),
    )
    finding = CoverageFinding("error", code, f"/tests/runs/{context.index - 1}/{pointer}", message)
    return _CollectedRun(run, artifact, records, (finding,))


def _validate_hook_evidence(
    path: Path,
    root: Path,
    *,
    run_id: str,
    index: int,
    before: ArtifactStamp | None,
    require_start: bool,
    require_write: bool,
) -> CoverageArtifact:
    try:
        validate_fresh_artifact(path, roots=(root,), before=before)
    except ArtifactValidationError as exc:
        raise _HookEvidenceError(
            "COV_WINDOW_HOOK_MISSING",
            "Coverage start hook produced no fresh evidence.",
        ) from exc
    document = _read_hook_document(path, run_id)
    _validate_hook_events(document["events"], require_start, require_write)
    return _artifact(
        path,
        root,
        artifact_id=f"artifact:hook:{index:03d}",
        kind="coverage_hook_evidence",
        run_id=run_id,
    )


def _read_hook_document(path: Path, run_id: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise _HookEvidenceError(
            "COV_WINDOW_HOOK_INVALID",
            "Coverage hook evidence was not valid JSON.",
        ) from exc
    if not isinstance(document, dict) or document.get("$schema") != "booley.coverage-hook/v1":
        raise _HookEvidenceError(
            "COV_WINDOW_HOOK_INVALID",
            "Coverage hook evidence has an invalid schema.",
        )
    events = document.get("events")
    if document.get("run_id") != run_id or not isinstance(events, list):
        raise _HookEvidenceError(
            "COV_WINDOW_HOOK_INVALID",
            "Coverage hook evidence does not identify its run.",
        )
    return document


def _validate_hook_events(events: object, require_start: bool, require_write: bool) -> None:
    assert isinstance(events, list)
    starts = _hook_events(events, "start")
    writes = _hook_events(events, "write")
    start_sequence = _require_hook(starts, "WINDOW") if require_start else None
    write_sequence = _require_hook(writes, "WRITE") if require_write else None
    if (
        start_sequence is not None
        and write_sequence is not None
        and start_sequence >= write_sequence
    ):
        raise _HookEvidenceError(
            "COV_CUSTOM_MAIN_HOOK_OUT_OF_ORDER",
            "Custom-main start_hook must run before write_hook.",
        )


def _hook_events(events: list[object], name: str) -> list[dict[str, object]]:
    return [event for event in events if isinstance(event, dict) and event.get("hook") == name]


def _require_hook(events: list[dict[str, object]], code_stem: str) -> int:
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
    sequence = events[0].get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise _HookEvidenceError(
            "COV_WINDOW_HOOK_INVALID",
            "Coverage hook evidence has an invalid event sequence.",
        )
    return sequence


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
    location: dict[str, FrozenJson] = {
        "source": source.path,
        "start": {"line": line, "column": column},
        "end": {"line": line, "column": column},
    }
    subject = dict(_subject(metric, record.attributes))
    collector = {"record_type": record_type, "native_key": record.identity}
    identity_document: dict[str, object] = {
        "metric": metric,
        "location": location,
        "hierarchy": record.attributes.get("h", ""),
        "subject": subject,
        "collector": collector,
    }
    return CoveragePoint(
        id=_point_id(identity_document),
        identity=CoveragePointIdentity(
            metric=metric,
            location=MappingProxyType(location),
            hierarchy=str(identity_document["hierarchy"]),
            subject=MappingProxyType(subject),
            collector=MappingProxyType(collector),
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
    points = []
    for identity, record in records.items():
        if record.attributes.get("t") not in _RECORD_METRICS:
            continue
        native_path = record.attributes.get("f", "")
        source = _match_source(sources, native_path)
        points.append(_point_for_record(record, source, incidence.get(identity, {})))
    return tuple(sorted(points, key=lambda point: (point.identity.metric, point.id)))


def _match_source(sources: Mapping[str, CoverageSource], native_path: str) -> CoverageSource:
    """Match Verilator's staged/absolute filename to one resolved Target input."""
    if native_path in sources:
        return sources[native_path]
    normalized = native_path.replace("\\", "/")
    matches = []
    for declared, source in sources.items():
        suffix = declared.replace("\\", "/").lstrip("/")
        if normalized.endswith(f"/{suffix}"):
            matches.append(source)
    if len(matches) == 1:
        return matches[0]
    return CoverageSource(native_path=native_path, path=native_path, kind="foreign")


def _capabilities_and_findings(
    collected: tuple[_CollectedRun, ...],
) -> tuple[tuple[CoverageCapability, ...], tuple[CoverageFinding, ...]]:
    record_types = {
        record.attributes.get("t", "") for item in collected for record in item.records
    }
    capabilities: list[CoverageCapability] = []
    findings: list[CoverageFinding] = []
    for record_type in sorted(set(_RECORD_METRICS) | record_types):
        record_class = _RECORD_METRICS.get(record_type, f"native:{record_type}")
        capabilities.append(
            CoverageCapability(
                record_class=record_class,
                status="reported" if record_type in record_types else "absent",
                attributes=MappingProxyType(
                    {
                        "native_record_type": record_type,
                        "collection": "supported"
                        if record_type in _RECORD_METRICS
                        else "unsupported",
                        "scoring": "scored_v1" if record_class in _SCORED_METRICS else "unscored",
                    }
                ),
            )
        )
    return tuple(capabilities), tuple(findings)


def _merge(
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
    collected: tuple[_CollectedRun, ...],
) -> tuple[CoverageArtifact, tuple[_NativeRecord, ...]]:
    merged_path = request.artifact_root / "native" / "merged" / "coverage.dat"
    before = snapshot_artifact(merged_path)
    raw_paths = tuple(
        request.artifact_root / item.artifact.path
        for item in collected
        if item.artifact is not None
    )
    command = SimulationCommandRequest(
        argv=(
            "verilator_coverage",
            "--write",
            str(merged_path),
            *(str(path) for path in raw_paths),
        ),
        cwd=request.artifact_root,
        output_path=merged_path,
    )
    result = execution.command(command)
    if result.returncode != 0:
        raise _MergeError("COV_NATIVE_MERGE_FAILED", "Verilator native merge failed.")
    _validate_merged_freshness(request, merged_path, before)
    try:
        records = _parse_native(merged_path)
    except (_NativeFormatError, _NativeRecordError):
        raise _MergeError(
            "COV_NATIVE_MERGE_NOT_QUERYABLE",
            "Verilator native merge output was not queryable.",
            artifact=_merged_artifact(request, merged_path, state="unqueryable"),
        ) from None
    return _merged_artifact(request, merged_path), records


def _validate_merged_freshness(
    request: CoverageCollectionRequest,
    path: Path,
    before: ArtifactStamp | None,
) -> None:
    try:
        validate_fresh_artifact(path, roots=(request.artifact_root,), before=before)
    except ArtifactValidationError:
        if not path.is_file():
            raise _MergeError(
                "COV_NATIVE_MERGE_MISSING",
                "Verilator native merge produced no output database.",
            ) from None
        raise _MergeError(
            "COV_NATIVE_MERGE_STALE",
            "Verilator native merge output was not freshly written.",
            artifact=_merged_artifact(request, path, state="stale"),
        ) from None


def _merged_artifact(
    request: CoverageCollectionRequest,
    path: Path,
    *,
    state: str = "fresh_queryable",
) -> CoverageArtifact:
    return _artifact(
        path,
        request.artifact_root,
        artifact_id="artifact:merged",
        kind="merged_native",
        run_id=None,
        state=state,
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


def coverage_request_finding(request: CoverageCollectionRequest) -> CoverageFinding | None:
    """Validate Target-owned hook declarations without executing or allocating paths."""
    target = request.target
    if target.harness != "custom_main":
        if target.custom_main_hooks:
            return CoverageFinding(
                "error",
                "COV_CUSTOM_MAIN_HOOK_FORBIDDEN",
                "/target/custom_main_hooks",
                "custom_main_hooks is allowed only for a custom-main Target.",
            )
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


def _unknown_records(collected: tuple[_CollectedRun, ...]) -> tuple[Mapping[str, FrozenJson], ...]:
    return tuple(
        MappingProxyType(
            {
                "raw_artifact": item.run.raw_artifact,
                "record_ordinal": ordinal,
                "native_type": record.attributes.get("t", ""),
                "state": "unknown_retained",
                "native_key": record.identity,
                "hits": record.hits,
            }
        )
        for item in collected
        for ordinal, record in enumerate(item.records, start=1)
        if record.attributes.get("t", "") not in _RECORD_METRICS
    )


def _unknown_findings(collected: tuple[_CollectedRun, ...]) -> tuple[CoverageFinding, ...]:
    return tuple(
        CoverageFinding(
            "warning",
            "COV_NATIVE_RECORD_UNKNOWN",
            f"/normalization/unrecognized_records/{index}",
            "Unknown native record retained losslessly without V1 scoring.",
        )
        for index, _record in enumerate(_unknown_records(collected))
    )


def _result(
    request: CoverageCollectionRequest,
    build: CoverageBuildEvidence,
    collected: tuple[_CollectedRun, ...],
    *,
    status: CoverageCollectionStatus,
    capabilities: tuple[CoverageCapability, ...] = (),
    findings: tuple[CoverageFinding, ...] = (),
    merge: NativeMergeEvidence,
    compatibility: Literal["compatible", "incompatible", "unknown"],
    extra_artifacts: tuple[CoverageArtifact, ...] = (),
) -> CoverageCollectionResult:
    return CoverageCollectionResult(
        status=status,
        build=build,
        runs=tuple(item.run for item in collected),
        artifacts=(*_result_artifacts(collected), *extra_artifacts),
        points=() if compatibility == "incompatible" else _normalize_points(request, collected),
        capabilities=capabilities,
        findings=(*findings, *_unknown_findings(collected)),
        merge=merge,
        native_format=NativeFormatEvidence("verilator-coverage", compatibility),
        coverage_window=_window_evidence(request, collected),
        collector=PINNED_VERILATOR,
        unrecognized_records=_unknown_records(collected),
    )


def _build_collection(
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
) -> tuple[CoverageBuildEvidence, CoverageCollectionResult | None]:
    variant = SimulationBuildVariant(trace=request.trace, coverage=True)
    build = CoverageBuildEvidence(variant, VERILATOR_COVERAGE_INSTRUMENTATION)
    request_finding = coverage_request_finding(request)
    if request_finding is not None:
        return build, _preflight_error_result(request, build, request_finding)
    try:
        build_result = execution.build(
            SimulationBuildRequest(request.target, variant, VERILATOR_COVERAGE_INSTRUMENTATION)
        )
    except OSError as exc:
        return build, _infrastructure_failure(request, build, (), str(exc))
    if build_result.infrastructure_error:
        return build, _infrastructure_failure(request, build, (), build_result.output)
    if not build_result.success:
        return build, _build_error_result(request, build, build_result.output)
    if build_result.collector != PINNED_VERILATOR:
        finding = CoverageFinding(
            "error",
            "COV_VERILATOR_IDENTITY_MISMATCH",
            "/collector/version",
            "Coverage collection requires the exact pinned stable Verilator identity.",
        )
        return build, _infrastructure_failure(
            request, build, (), finding.message, code=finding.code
        )
    return build, None


def _merge_collection(
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
    build: CoverageBuildEvidence,
    collected: tuple[_CollectedRun, ...],
) -> CoverageCollectionResult:
    capabilities, normalization_findings = _capabilities_and_findings(collected)
    try:
        merged_artifact, merged_records = _merge(request, execution, collected)
    except _MergeError as exc:
        finding = CoverageFinding("error", exc.code, "/collection/merge", str(exc))
        extra = (exc.artifact,) if exc.artifact is not None else ()
        return _result(
            request,
            build,
            collected,
            status="collector_error",
            capabilities=capabilities,
            findings=(*normalization_findings, finding),
            merge=NativeMergeEvidence("failed"),
            compatibility="compatible",
            extra_artifacts=extra,
        )
    if not _merge_is_equivalent(collected, merged_records):
        return _merge_mismatch_result(
            request, build, collected, capabilities, normalization_findings, merged_artifact
        )
    return _result(
        request,
        build,
        collected,
        status="complete",
        capabilities=capabilities,
        findings=normalization_findings,
        merge=NativeMergeEvidence("equivalent", merged_artifact.id),
        compatibility="compatible",
        extra_artifacts=(merged_artifact,),
    )


def _merge_is_equivalent(
    collected: tuple[_CollectedRun, ...], merged: tuple[_NativeRecord, ...]
) -> bool:
    raw = tuple(record for item in collected for record in item.records)
    return _record_totals(raw) == _record_totals(merged)


def _merge_mismatch_result(
    request: CoverageCollectionRequest,
    build: CoverageBuildEvidence,
    collected: tuple[_CollectedRun, ...],
    capabilities: tuple[CoverageCapability, ...],
    findings: tuple[CoverageFinding, ...],
    artifact: CoverageArtifact,
) -> CoverageCollectionResult:
    mismatch = CoverageFinding(
        "error",
        "COV_NATIVE_MERGE_MISMATCH",
        "/collection/merge",
        "Native merge disagrees with independently normalized per-run counts.",
    )
    return _result(
        request,
        build,
        collected,
        status="collector_error",
        capabilities=capabilities,
        findings=(*findings, mismatch),
        merge=NativeMergeEvidence("mismatch", artifact.id),
        compatibility="compatible",
        extra_artifacts=(artifact,),
    )


def collect(
    request: CoverageCollectionRequest,
    execution: SimulationExecutionPort,
) -> CoverageCollectionResult:
    """Collect and normalize one Verilator-native database per selected test."""
    build, failure = _build_collection(request, execution)
    if failure is not None:
        return failure
    completed: list[_CollectedRun] = []
    try:
        for index, selected in enumerate(request.selected_tests, start=1):
            completed.append(_collect_one_run(request, execution, index, selected))
        return _finish_collection(request, execution, build, tuple(completed))
    except OSError as exc:
        return _infrastructure_failure(request, build, tuple(completed), str(exc))


def _finish_collection(request, execution, build, collected) -> CoverageCollectionResult:
    findings = tuple(finding for item in collected for finding in item.findings)
    if findings:
        capabilities, capability_findings = _capabilities_and_findings(collected)
        compatibility = (
            "incompatible"
            if any(item.code == "COV_NATIVE_FORMAT_INCOMPATIBLE" for item in findings)
            else "unknown"
        )
        return _result(
            request,
            build,
            collected,
            status="collector_error",
            findings=(*findings, *capability_findings),
            capabilities=capabilities,
            merge=NativeMergeEvidence("not_run"),
            compatibility=compatibility,
        )
    return _merge_collection(request, execution, build, collected)


def _infrastructure_failure(
    request, build, completed, message, *, code="COV_INFRASTRUCTURE_ERROR"
) -> CoverageCollectionResult:
    collected = list(completed)
    for index, selected in enumerate(request.selected_tests[len(completed) :], len(completed) + 1):
        collected.append(
            _CollectedRun(
                CoverageRun(
                    id=f"run:{index:03d}:{_path_component(selected.name)}",
                    test=selected.name,
                    simulation_verdict="inconclusive",
                    collection="collector_error",
                    raw_artifact=None,
                    attributes=MappingProxyType({"execution": "not_completed"}),
                ),
                None,
                (),
            )
        )
    observations = tuple(collected)
    capabilities, capability_findings = _capabilities_and_findings(observations)
    findings = tuple(f for item in observations for f in item.findings)
    finding = CoverageFinding(
        "error",
        code,
        "/collection",
        message,
    )
    result = _result(
        request,
        build,
        observations,
        status="collector_error",
        capabilities=capabilities,
        findings=(*findings, *capability_findings, finding),
        merge=NativeMergeEvidence("not_run"),
        compatibility="incompatible"
        if any(f.code == "COV_NATIVE_FORMAT_INCOMPATIBLE" for f in findings)
        else "unknown",
    )
    return replace(result, infrastructure_error=True)
