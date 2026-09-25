#!/usr/bin/env python3
"""Record attributable phase timing for the RISC-V Sandbox Image CI lane."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.core.boundary import (
    BoundaryError,
    as_dict,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_str,
    require_str_value,
)


class TimingError(ValueError):
    """Raised when phase timing evidence is malformed."""


PhaseTopology = Literal["parallel", "nested", "post-group"]
PhaseOutcome = Literal["running", "success", "failure", "unavailable", "incomplete"]
MeasurementArm = Literal["automatic", "baseline", "warm", "cold"]
CacheState = Literal["not-requested", "hit", "miss"]


class PhaseRecord(TypedDict):
    """Stable timing evidence for one named CI phase."""

    name: str
    topology: PhaseTopology
    started_at: str
    completed_at: str | None
    elapsed_seconds: float | None
    outcome: PhaseOutcome
    exit_code: int | None
    reason: NotRequired[str]
    attribution: NotRequired[str]
    buildkit: NotRequired[dict[str, Any]]
    image_inspect: NotRequired[Any]
    parent_image_inspect: NotRequired[Any]


_EXPECTED_PHASES = {
    "agent_policy": "parallel",
    "cocotb": "parallel",
    "coverage_release": "parallel",
    "fifo": "parallel",
    "ibex_runtime": "post-group",
    "image_contract_size_resources": "nested",
    "isolation": "parallel",
    "native_fst": "parallel",
    "openroad": "parallel",
    "picorv32_runtime_demo": "nested",
    "riscv_candidate": "parallel",
    "riscv_tool_substrate_construction_export": "nested",
    "riscv_tool_substrate_transfer_load": "nested",
    "simulator": "parallel",
    "ticket_mode": "parallel",
    "verible": "parallel",
    "verilator": "parallel",
    "wheel_overlay_construction_export": "nested",
    "wheel_overlay_transfer_load": "nested",
}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TimingError(f"cannot read {path}: {error}") from error


def _phase(
    name: str,
    topology: PhaseTopology,
    started_at: str,
    completed_at: str | None,
    elapsed_seconds: float | None,
    outcome: PhaseOutcome,
    exit_code: int | None,
) -> PhaseRecord:
    return {
        "name": name,
        "topology": topology,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": elapsed_seconds,
        "outcome": outcome,
        "exit_code": exit_code,
    }


def start_record(path: Path, name: str, topology: PhaseTopology) -> None:
    """Persist a recoverable start marker before a shell-owned phase."""
    _write_json(
        path,
        {
            "schema_version": 1,
            "_started_epoch_ns": time.time_ns(),
            "phases": [_phase(name, topology, _utc_now(), None, None, "running", None)],
        },
    )


def _finish_phase(record: Any, exit_code: int) -> PhaseRecord:
    try:
        payload = require_dict(record, field="phase start record")
        phases = require_list(payload.get("phases"), field="phase start record.phases")
        started_ns = require_int(
            payload.get("_started_epoch_ns"), field="phase start record._started_epoch_ns"
        )
        if len(phases) != 1:
            raise BoundaryError("phase start record.phases must contain exactly one phase")
        phase = validate_phase(phases[0], allowed_outcomes={"running"})
    except BoundaryError as error:
        raise TimingError(str(error)) from error
    phase["completed_at"] = _utc_now()
    phase["elapsed_seconds"] = round((time.time_ns() - started_ns) / 1_000_000_000, 3)
    phase["outcome"] = "success" if exit_code == 0 else "failure"
    phase["exit_code"] = exit_code
    return phase


def finish_record(path: Path, exit_code: int) -> None:
    """Finish a shell-owned phase, retaining failures as evidence."""
    _write_json(
        path, {"schema_version": 1, "phases": [_finish_phase(_read_json(path), exit_code)]}
    )


# Dockerfile instruction vertices ("[3/7] RUN ...", "[stage-0 2/5] COPY ...").
# Named-context and internal vertices are excluded: BuildKit reports a
# docker-image:// context as cached on every build, which is not a tooling hit.
_BUILD_STEP = re.compile(r"^\[(?:[^\]\s]+ )?\d+/\d+\] ")


def _merge_vertices(vertices: dict[str, dict[str, Any]], line_payload: Any) -> None:
    payload = as_dict(line_payload) or {}
    for vertex in payload.get("vertexes") or []:
        vertex_dict = as_dict(vertex)
        if vertex_dict and isinstance(vertex_dict.get("digest"), str):
            vertices.setdefault(vertex_dict["digest"], {}).update(vertex_dict)


def _raw_events(path: Path) -> list[dict[str, Any]]:
    """Return one merged record per vertex from `docker buildx --progress rawjson`.

    Each output line is a SolveStatus snapshot (`{"vertexes": [...], ...}`);
    a vertex is repeated as its state advances, so later snapshots win.
    """
    vertices: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise TimingError(f"cannot read {path}: {error}") from error
    for line in lines:
        try:
            _merge_vertices(vertices, json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(vertices.values())


def _event_time(event: dict[str, Any], field: str) -> datetime | None:
    value = event.get(field)
    return _timestamp(value, f"buildkit.{field}") if value is not None else None


def _buildkit_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    steps = [event for event in events if _BUILD_STEP.match(str(event.get("name", "")))]
    cached_steps = [event for event in steps if event.get("cached") is True]
    imports = sorted(
        {
            str(event.get("name"))
            for event in events
            if "importing cache" in str(event.get("name", "")).lower()
        }
    )
    return {
        "vertex_count": len(events),
        "cached_vertex_count": sum(event.get("cached") is True for event in events),
        "build_step_count": len(steps),
        "cached_build_step_count": len(cached_steps),
        # A tooling hit means every Dockerfile step was reused, not just one.
        "cache_hit": bool(steps) and len(cached_steps) == len(steps),
        "imported_sources": imports,
    }


def _transfer_window(events: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    # "exporting to image" is the docker driver's in-daemon export: it commits
    # layers, writes, and names the image directly in the daemon store, so it
    # is the whole transfer/load cost. The other markers cover drivers that
    # stream a tarball into the daemon instead.
    markers = ("exporting to image", "sending tarball", "importing to docker", "loading layer")
    transfers = [
        event
        for event in events
        if any(marker in str(event.get("name", "")).lower() for marker in markers)
    ]
    starts = [value for event in transfers if (value := _event_time(event, "started"))]
    ends = [value for event in transfers if (value := _event_time(event, "completed"))]
    return (min(starts), max(ends)) if starts and ends else None


def _optional_json(path: Path) -> Any:
    return _read_json(path) if path.exists() else None


def _attach_build_evidence(
    phase: PhaseRecord,
    events: list[dict[str, Any]],
    metadata: Path,
    image_inspect: Path,
    parent_inspect: Path,
) -> None:
    buildkit = _buildkit_summary(events)
    buildkit.update(
        metadata=_optional_json(metadata),
        command_elapsed_seconds=phase["elapsed_seconds"],
        command_outcome=phase["outcome"],
        command_exit_code=phase["exit_code"],
    )
    phase["buildkit"] = buildkit
    phase["image_inspect"] = _optional_json(image_inspect)
    phase["parent_image_inspect"] = _optional_json(parent_inspect)


def _split_build_phases(
    construction: PhaseRecord,
    events: list[dict[str, Any]],
    exit_code: int,
) -> PhaseRecord:
    name = construction["name"]
    transfer = _transfer_window(events)
    if transfer is None:
        construction["attribution"] = "combined_construction_export_transfer_load"
        load = _phase(
            name.replace("_construction_export", "_transfer_load"),
            "nested",
            construction["started_at"],
            None,
            None,
            "unavailable",
            None,
        )
        load["reason"] = "BuildKit raw progress exposed no trustworthy daemon-import boundary"
        return load

    started, completed = transfer
    event_starts = [value for event in events if (value := _event_time(event, "started"))]
    build_started = min(
        event_starts, default=_timestamp(construction["started_at"], "phase.started_at")
    )
    construction["started_at"] = _format_timestamp(build_started)
    construction["completed_at"] = _format_timestamp(started)
    construction["elapsed_seconds"] = round((started - build_started).total_seconds(), 3)
    return _phase(
        name.replace("_construction_export", "_transfer_load"),
        "nested",
        _format_timestamp(started),
        _format_timestamp(completed),
        round((completed - started).total_seconds(), 3),
        "success" if exit_code == 0 else "failure",
        exit_code,
    )


def finish_build_record(
    path: Path,
    exit_code: int,
    progress: Path,
    metadata: Path,
    image_inspect: Path,
    parent_inspect: Path,
) -> None:
    """Finish a BuildKit phase and split transfer/load when directly observable."""
    construction = _finish_phase(_read_json(path), exit_code)
    events = _raw_events(progress)
    construction["name"] = f"{construction['name']}_construction_export"
    _attach_build_evidence(construction, events, metadata, image_inspect, parent_inspect)
    load = _split_build_phases(construction, events, exit_code)
    _write_json(path, {"schema_version": 1, "phases": [construction, load]})


def timed_run(path: Path, name: str, topology: PhaseTopology, command: list[str]) -> int:
    """Run one command and retain its success or failure timing."""
    start_record(path, name, topology)
    try:
        result = subprocess.run(command, check=False)
        exit_code = result.returncode
    except OSError as error:
        print(f"error: cannot run {command[0]}: {error}", file=sys.stderr)
        exit_code = 127
    finish_record(path, exit_code)
    return exit_code


def _timestamp(value: Any, field: str) -> datetime:
    try:
        text = require_str_value(value, field=field)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (BoundaryError, ValueError) as error:
        raise TimingError(f"{field} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise TimingError(f"{field} must include a timezone")
    return parsed


def _required_phase_fields(phase: Any) -> tuple[dict[str, Any], str, str, str, str]:
    try:
        payload = cast(dict[str, Any], require_dict(phase, field="phase"))
        return (
            payload,
            require_str(payload, "name"),
            require_str(payload, "topology"),
            require_str(payload, "started_at"),
            require_str(payload, "outcome"),
        )
    except BoundaryError as error:
        raise TimingError(str(error)) from error


def validate_phase(
    phase: Any,
    *,
    allowed_outcomes: set[str] | None = None,
) -> PhaseRecord:
    """Validate one stable phase record and return it with a precise type."""
    payload, name, topology, started_at, outcome = _required_phase_fields(phase)
    if topology not in {"parallel", "nested", "post-group"}:
        raise TimingError("phase.topology is invalid")
    started = _timestamp(started_at, "phase.started_at")
    completed = payload.get("completed_at")
    elapsed_raw = payload.get("elapsed_seconds")
    if completed is not None and _timestamp(completed, "phase.completed_at") < started:
        raise TimingError("phase completion cannot precede its start")
    try:
        elapsed = (
            require_finite_number(elapsed_raw, field="phase.elapsed_seconds")
            if elapsed_raw is not None
            else None
        )
        exit_code = (
            require_int(payload.get("exit_code"), field="phase.exit_code")
            if payload.get("exit_code") is not None
            else None
        )
    except BoundaryError as error:
        raise TimingError(str(error)) from error
    if elapsed is not None and elapsed < 0:
        raise TimingError("phase.elapsed_seconds cannot be negative")
    valid_outcomes = allowed_outcomes or {"success", "failure", "unavailable", "incomplete"}
    if outcome not in valid_outcomes:
        raise TimingError("phase.outcome is invalid")
    if outcome in {"success", "failure"} and (completed is None or elapsed is None):
        raise TimingError("completed phase must include completion and elapsed time")
    if outcome in {"unavailable", "incomplete"} and elapsed is not None:
        raise TimingError("unmeasured phase elapsed_seconds must be null")
    payload.update(
        name=name,
        topology=topology,
        started_at=started_at,
        elapsed_seconds=elapsed,
        outcome=outcome,
        exit_code=exit_code,
    )
    return cast(PhaseRecord, payload)


def _load_record(path: Path) -> list[PhaseRecord]:
    try:
        record = require_dict(_read_json(path), field=str(path))
        phases = require_list(record.get("phases"), field=f"{path}.phases")
    except BoundaryError as error:
        raise TimingError(str(error)) from error
    if not phases:
        raise TimingError(f"{path} contains no phases")
    if record.get("_started_epoch_ns") is not None:
        try:
            require_int(record["_started_epoch_ns"], field=f"{path}._started_epoch_ns")
        except BoundaryError as error:
            raise TimingError(str(error)) from error
        phase = validate_phase(phases[0], allowed_outcomes={"running"})
        phase.update(completed_at=None, elapsed_seconds=None, outcome="incomplete", exit_code=None)
        phases[0] = phase
        if phase["name"] in {"riscv_tool_substrate", "wheel_overlay"}:
            base_name = phase["name"]
            phase["name"] = f"{base_name}_construction_export"
            phases.append(
                _phase(
                    f"{base_name}_transfer_load",
                    "nested",
                    phase["started_at"],
                    None,
                    None,
                    "incomplete",
                    None,
                )
            )
    return [validate_phase(phase) for phase in phases]


def _add_missing_phases(phases: list[PhaseRecord], observed_at: str) -> None:
    present = {phase["name"] for phase in phases}
    for name, topology in _EXPECTED_PHASES.items():
        if name not in present:
            phases.append(_phase(name, topology, observed_at, None, None, "incomplete", None))


def _parallel_topology(phases: list[PhaseRecord]) -> dict[str, Any]:
    lanes = [phase for phase in phases if phase["topology"] == "parallel"]
    completed = [phase for phase in lanes if phase["completed_at"] is not None]
    ordered = sorted(completed, key=lambda phase: _timestamp(phase["completed_at"], "completed"))
    riscv = next((phase for phase in lanes if phase["name"] == "riscv_candidate"), None)
    other_completed = [
        phase
        for phase in lanes
        if phase["name"] != "riscv_candidate" and phase["completed_at"] is not None
    ]
    next_latest = max(
        other_completed,
        key=lambda phase: _timestamp(phase["completed_at"], "completed"),
        default=None,
    )
    headroom = None
    if riscv is not None and riscv["completed_at"] is not None and next_latest is not None:
        headroom = max(
            0.0,
            round(
                (
                    _timestamp(riscv["completed_at"], "riscv.completed_at")
                    - _timestamp(next_latest["completed_at"], "next_latest.completed_at")
                ).total_seconds(),
                3,
            ),
        )
    return {
        "lane_count": len(lanes),
        "completion_order": [phase["name"] for phase in ordered],
        "group_completed_at": ordered[-1]["completed_at"] if ordered else None,
        "next_latest_lane": next_latest["name"] if next_latest else None,
        "next_latest_lane_completed_at": next_latest["completed_at"] if next_latest else None,
        "riscv_headroom_seconds": headroom,
    }


def _tooling_cache_hit(phases: list[PhaseRecord]) -> bool:
    construction = next(
        (phase for phase in phases if phase["name"] == "riscv_tool_substrate_construction_export"),
        None,
    )
    if construction is None:
        return False
    buildkit = as_dict(construction.get("buildkit"), default={}) or {}
    return buildkit.get("cache_hit") is True


def summarize_records(
    records: list[Path],
    run_id: int,
    run_attempt: int,
    candidate_sha: str,
    observed_at: str,
    measurement_arm: MeasurementArm = "automatic",
    cache_state: CacheState = "not-requested",
) -> dict[str, Any]:
    """Validate and merge independent phase files into run-level evidence."""
    phases = [phase for path in records for phase in _load_record(path)]
    names = [phase["name"] for phase in phases]
    if len(names) != len(set(names)):
        raise TimingError("phase names must be unique")
    unexpected = sorted(set(names) - _EXPECTED_PHASES.keys())
    if unexpected:
        raise TimingError(f"unexpected phase names: {unexpected}")
    _add_missing_phases(phases, observed_at)
    phases.sort(key=lambda phase: (_timestamp(phase["started_at"], "started"), phase["name"]))
    parallel = _parallel_topology(phases)
    tooling_cache_hit = _tooling_cache_hit(phases)
    ibex = next((phase for phase in phases if phase["name"] == "ibex_runtime"), None)
    critical = None
    if parallel["group_completed_at"] and ibex and ibex["completed_at"]:
        critical = (
            _timestamp(ibex["completed_at"], "ibex.completed_at")
            - min(_timestamp(phase["started_at"], "started") for phase in phases)
        ).total_seconds()
    return {
        "schema_version": 1,
        "run": {
            "id": run_id,
            "attempt": run_attempt,
            "candidate_sha": candidate_sha,
            "measurement_arm": measurement_arm,
            "cache_state": cache_state,
            "tooling_cache_hit": tooling_cache_hit,
        },
        "observed_at": observed_at,
        "complete": all(phase["outcome"] == "success" for phase in phases),
        "representative": (
            measurement_arm != "warm" or (cache_state == "hit" and tooling_cache_hit)
        ),
        "phases": phases,
        "topology": {"parallel_group": parallel, "critical_path_elapsed_seconds": critical},
    }


def finalize(
    directory: Path,
    output: Path,
    run_id: int,
    attempt: int,
    sha: str,
    measurement_arm: MeasurementArm,
    cache_state: CacheState,
) -> None:
    """Merge every independent record while preserving partial evidence."""
    records = sorted((directory / "records").glob("*.json"))
    if not records:
        raise TimingError("no phase records were found")
    _write_json(
        output,
        summarize_records(records, run_id, attempt, sha, _utc_now(), measurement_arm, cache_state),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "finish", "finish-build", "timed-run"):
        child = subparsers.add_parser(command)
        child.add_argument("--record", type=Path, required=True)
        if command not in {"finish", "finish-build"}:
            child.add_argument("--name", required=True)
            child.add_argument(
                "--topology", choices=("parallel", "nested", "post-group"), required=True
            )
        else:
            child.add_argument("--exit-code", type=int, required=True)
        if command == "finish-build":
            child.add_argument("--progress", type=Path, required=True)
            child.add_argument("--metadata", type=Path, required=True)
            child.add_argument("--image-inspect", type=Path, required=True)
            child.add_argument("--parent-inspect", type=Path, required=True)
        if command == "timed-run":
            child.add_argument("argv", nargs=argparse.REMAINDER)
    child = subparsers.add_parser("finalize")
    child.add_argument("--directory", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)
    child.add_argument("--run-id", type=int, required=True)
    child.add_argument("--run-attempt", type=int, required=True)
    child.add_argument("--candidate-sha", required=True)
    child.add_argument(
        "--measurement-arm",
        choices=("automatic", "baseline", "warm", "cold"),
        default="automatic",
    )
    child.add_argument(
        "--cache-state", choices=("not-requested", "hit", "miss"), default="not-requested"
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "start":
            start_record(args.record, args.name, args.topology)
        elif args.command == "finish":
            finish_record(args.record, args.exit_code)
        elif args.command == "finish-build":
            finish_build_record(
                args.record,
                args.exit_code,
                args.progress,
                args.metadata,
                args.image_inspect,
                args.parent_inspect,
            )
        elif args.command == "timed-run":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not command:
                raise TimingError("timed-run requires a command after --")
            return timed_run(args.record, args.name, args.topology, command)
        else:
            finalize(
                args.directory,
                args.output,
                args.run_id,
                args.run_attempt,
                args.candidate_sha,
                args.measurement_arm,
                args.cache_state,
            )
    except TimingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
