#!/usr/bin/env python3
"""Record attributable phase timing for the RISC-V Sandbox Image CI lane."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TimingError(ValueError):
    """Raised when phase timing evidence is malformed."""


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
    topology: str,
    started_at: str,
    completed_at: str | None,
    elapsed_seconds: float | None,
    outcome: str,
    exit_code: int | None,
) -> dict[str, Any]:
    return {
        "name": name,
        "topology": topology,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": elapsed_seconds,
        "outcome": outcome,
        "exit_code": exit_code,
    }


def start_record(path: Path, name: str, topology: str) -> None:
    """Persist a recoverable start marker before a shell-owned phase."""
    _write_json(
        path,
        {
            "schema_version": 1,
            "_started_epoch_ns": time.time_ns(),
            "phases": [_phase(name, topology, _utc_now(), None, None, "running", None)],
        },
    )


def _finish_phase(record: dict[str, Any], exit_code: int) -> dict[str, Any]:
    phases = record.get("phases")
    started_ns = record.get("_started_epoch_ns")
    if not isinstance(phases, list) or len(phases) != 1 or not isinstance(started_ns, int):
        raise TimingError("phase start record is malformed")
    phase = phases[0]
    if not isinstance(phase, dict) or phase.get("outcome") != "running":
        raise TimingError("phase start record is not running")
    phase["completed_at"] = _utc_now()
    phase["elapsed_seconds"] = round((time.time_ns() - started_ns) / 1_000_000_000, 3)
    phase["outcome"] = "success" if exit_code == 0 else "failure"
    phase["exit_code"] = exit_code
    return {"schema_version": 1, "phases": [phase]}


def finish_record(path: Path, exit_code: int) -> None:
    """Finish a shell-owned phase, retaining failures as evidence."""
    _write_json(path, _finish_phase(_read_json(path), exit_code))


def _raw_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise TimingError(f"cannot read {path}: {error}") from error
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_time(event: dict[str, Any], field: str) -> datetime | None:
    value = event.get(field)
    return _timestamp(value, f"buildkit.{field}") if value is not None else None


def _buildkit_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    vertices = {event.get("id"): event for event in events if isinstance(event.get("id"), str)}
    cached = [event for event in vertices.values() if event.get("cached") is True]
    imports = sorted(
        {
            str(event.get("name"))
            for event in vertices.values()
            if "importing cache" in str(event.get("name", "")).lower()
        }
    )
    return {
        "vertex_count": len(vertices),
        "cached_vertex_count": len(cached),
        "cache_hit": bool(cached),
        "imported_sources": imports,
    }


def _transfer_window(events: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    markers = ("sending tarball", "importing to docker", "loading layer")
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


def finish_build_record(
    path: Path,
    exit_code: int,
    progress: Path,
    metadata: Path,
    image_inspect: Path,
    parent_inspect: Path,
) -> None:
    """Finish a BuildKit phase and split transfer/load when directly observable."""
    finished = _finish_phase(_read_json(path), exit_code)["phases"][0]
    events = _raw_events(progress)
    name = str(finished["name"])
    buildkit = _buildkit_summary(events)
    buildkit["metadata"] = _optional_json(metadata)
    buildkit["command_elapsed_seconds"] = finished["elapsed_seconds"]
    buildkit["command_outcome"] = finished["outcome"]
    buildkit["command_exit_code"] = finished["exit_code"]
    finished["name"] = f"{name}_construction_export"
    finished["buildkit"] = buildkit
    finished["image_inspect"] = _optional_json(image_inspect)
    finished["parent_image_inspect"] = _optional_json(parent_inspect)
    transfer = _transfer_window(events)
    if transfer is None:
        finished.update(
            completed_at=None,
            elapsed_seconds=None,
            outcome="unavailable",
            exit_code=None,
        )
        finished["reason"] = "BuildKit raw progress exposed no trustworthy daemon-import boundary"
        load = _phase(
            f"{name}_transfer_load",
            "nested",
            finished["started_at"],
            None,
            None,
            "unavailable",
            None,
        )
        load["reason"] = "BuildKit raw progress exposed no trustworthy daemon-import boundary"
    else:
        started, completed = transfer
        event_starts = [value for event in events if (value := _event_time(event, "started"))]
        build_started = min(
            event_starts, default=_timestamp(finished["started_at"], "phase.started_at")
        )
        finished["started_at"] = (
            build_started.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        finished["completed_at"] = (
            started.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        finished["elapsed_seconds"] = round((started - build_started).total_seconds(), 3)
        load = _phase(
            f"{name}_transfer_load",
            "nested",
            started.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            completed.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            round((completed - started).total_seconds(), 3),
            "success" if exit_code == 0 else "failure",
            exit_code,
        )
    _write_json(path, {"schema_version": 1, "phases": [finished, load]})


def timed_run(path: Path, name: str, topology: str, command: list[str]) -> int:
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
    if not isinstance(value, str):
        raise TimingError(f"{field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TimingError(f"{field} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise TimingError(f"{field} must include a timezone")
    return parsed


def validate_phase(phase: Any) -> dict[str, Any]:
    """Validate one stable phase record and return it with a precise type."""
    if not isinstance(phase, dict):
        raise TimingError("phase must be an object")
    for field in ("name", "topology", "started_at", "outcome"):
        if not isinstance(phase.get(field), str) or not phase[field]:
            raise TimingError(f"phase.{field} must be a non-empty string")
    if phase["topology"] not in {"parallel", "nested", "post-group"}:
        raise TimingError("phase.topology is invalid")
    started = _timestamp(phase["started_at"], "phase.started_at")
    completed = phase.get("completed_at")
    elapsed = phase.get("elapsed_seconds")
    if completed is not None and _timestamp(completed, "phase.completed_at") < started:
        raise TimingError("phase completion cannot precede its start")
    if elapsed is not None and (isinstance(elapsed, bool) or not isinstance(elapsed, int | float)):
        raise TimingError("phase.elapsed_seconds must be numeric or null")
    if isinstance(elapsed, int | float) and elapsed < 0:
        raise TimingError("phase.elapsed_seconds cannot be negative")
    if phase["outcome"] not in {"success", "failure", "unavailable", "incomplete"}:
        raise TimingError("phase.outcome is invalid")
    if phase["outcome"] in {"success", "failure"} and (completed is None or elapsed is None):
        raise TimingError("completed phase must include completion and elapsed time")
    if phase["outcome"] in {"unavailable", "incomplete"} and elapsed is not None:
        raise TimingError("unmeasured phase elapsed_seconds must be null")
    return phase


def _load_record(path: Path) -> list[dict[str, Any]]:
    record = _read_json(path)
    if not isinstance(record, dict) or not isinstance(record.get("phases"), list):
        raise TimingError(f"{path} does not contain a phases list")
    phases = record["phases"]
    if not phases:
        raise TimingError(f"{path} contains no phases")
    if record.get("_started_epoch_ns") is not None:
        phase = phases[0]
        if isinstance(phase, dict):
            phase.update(
                completed_at=None, elapsed_seconds=None, outcome="incomplete", exit_code=None
            )
            if phase.get("name") in {"riscv_tool_substrate", "wheel_overlay"}:
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


def _add_missing_phases(phases: list[dict[str, Any]], observed_at: str) -> None:
    present = {phase["name"] for phase in phases}
    for name, topology in _EXPECTED_PHASES.items():
        if name not in present:
            phases.append(_phase(name, topology, observed_at, None, None, "incomplete", None))


def _parallel_topology(phases: list[dict[str, Any]]) -> dict[str, Any]:
    lanes = [phase for phase in phases if phase["topology"] == "parallel"]
    completed = [phase for phase in lanes if phase["completed_at"] is not None]
    ordered = sorted(completed, key=lambda phase: _timestamp(phase["completed_at"], "completed"))
    riscv = next((phase for phase in lanes if phase["name"] == "riscv_candidate"), None)
    others = [phase for phase in lanes if phase["name"] != "riscv_candidate"]
    longest_other = max(
        (phase["elapsed_seconds"] for phase in others if phase["elapsed_seconds"] is not None),
        default=None,
    )
    headroom = None
    if riscv is not None and riscv["elapsed_seconds"] is not None and longest_other is not None:
        headroom = max(0.0, round(riscv["elapsed_seconds"] - longest_other, 3))
    return {
        "lane_count": len(lanes),
        "completion_order": [phase["name"] for phase in ordered],
        "group_completed_at": ordered[-1]["completed_at"] if ordered else None,
        "next_longest_lane_seconds": longest_other,
        "riscv_headroom_seconds": headroom,
    }


def summarize_records(
    records: list[Path],
    run_id: int,
    run_attempt: int,
    candidate_sha: str,
    observed_at: str,
    measurement_arm: str = "automatic",
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
        },
        "observed_at": observed_at,
        "complete": all(phase["outcome"] in {"success", "unavailable"} for phase in phases),
        "phases": phases,
        "topology": {"parallel_group": parallel, "critical_path_elapsed_seconds": critical},
    }


def finalize(
    directory: Path, output: Path, run_id: int, attempt: int, sha: str, measurement_arm: str
) -> None:
    """Merge every independent record while preserving partial evidence."""
    records = sorted((directory / "records").glob("*.json"))
    if not records:
        raise TimingError("no phase records were found")
    _write_json(
        output,
        summarize_records(records, run_id, attempt, sha, _utc_now(), measurement_arm),
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
        "--measurement-arm", choices=("automatic", "warm", "cold"), default="automatic"
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
            )
    except TimingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
