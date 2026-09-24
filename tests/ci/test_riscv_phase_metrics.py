"""Contracts for attributable RISC-V CI phase timing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from riscv_phase_metrics import (
    TimingError,
    finish_build_record,
    finish_record,
    start_record,
    summarize_records,
    validate_phase,
)


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _completed_record(path: Path, name: str, topology: str) -> None:
    start_record(path, name, topology)
    finish_record(path, 0)


def test_records_failed_phase_instead_of_losing_partial_evidence(tmp_path: Path) -> None:
    record = tmp_path / "failed.json"
    start_record(record, "picorv32_runtime_demo", "nested")

    finish_record(record, 9)

    phase = json.loads(record.read_text(encoding="utf-8"))["phases"][0]
    assert phase["outcome"] == "failure"
    assert phase["exit_code"] == 9
    assert phase["elapsed_seconds"] >= 0
    assert phase["completed_at"].endswith("Z")


def test_finalizer_preserves_interrupted_phase(tmp_path: Path) -> None:
    records = tmp_path / "records"
    start_record(records / "interrupted.json", "riscv_candidate", "parallel")

    summary = summarize_records(
        [records / "interrupted.json"], 42, 3, "abc123", "2026-09-24T12:00:00Z"
    )

    assert summary["complete"] is False
    assert summary["run"] == {
        "id": 42,
        "attempt": 3,
        "candidate_sha": "abc123",
        "measurement_arm": "automatic",
    }
    assert summary["phases"][0]["outcome"] == "incomplete"


def test_finalizer_expands_interrupted_build_into_partial_phases(tmp_path: Path) -> None:
    record = tmp_path / "records" / "build.json"
    start_record(record, "riscv_tool_substrate", "nested")

    summary = summarize_records([record], 42, 1, "sha", "2026-09-24T12:00:00Z")

    phases = {phase["name"]: phase for phase in summary["phases"]}
    assert phases["riscv_tool_substrate_construction_export"]["outcome"] == "incomplete"
    assert phases["riscv_tool_substrate_transfer_load"]["outcome"] == "incomplete"


def test_buildkit_import_boundary_and_cache_metadata_are_attributed(tmp_path: Path) -> None:
    record = tmp_path / "build.json"
    progress = tmp_path / "progress.jsonl"
    metadata = tmp_path / "metadata.json"
    image = tmp_path / "image.json"
    parent = tmp_path / "parent.json"
    start_record(record, "wheel_overlay", "nested")
    progress.write_text(
        "\n".join(
            json.dumps(value)
            for value in (
                {
                    "id": "compile",
                    "name": "RUN build",
                    "started": "2026-09-24T12:00:00Z",
                    "completed": "2026-09-24T12:00:04Z",
                    "cached": True,
                },
                {
                    "id": "load",
                    "name": "importing to docker",
                    "started": "2026-09-24T12:00:04Z",
                    "completed": "2026-09-24T12:00:06Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _write(metadata, {"containerimage.digest": "sha256:candidate"})
    _write(image, [{"Id": "sha256:candidate"}])
    _write(parent, [{"Id": "sha256:parent"}])

    finish_build_record(record, 0, progress, metadata, image, parent)

    phases = json.loads(record.read_text(encoding="utf-8"))["phases"]
    assert [phase["name"] for phase in phases] == [
        "wheel_overlay_construction_export",
        "wheel_overlay_transfer_load",
    ]
    assert phases[0]["buildkit"]["cache_hit"] is True
    assert phases[0]["buildkit"]["metadata"]["containerimage.digest"] == "sha256:candidate"
    assert phases[0]["image_inspect"][0]["Id"] == "sha256:candidate"
    assert phases[0]["parent_image_inspect"][0]["Id"] == "sha256:parent"
    assert phases[1]["elapsed_seconds"] == 2


def test_buildkit_missing_import_boundary_is_explicitly_unavailable(tmp_path: Path) -> None:
    record = tmp_path / "build.json"
    progress = tmp_path / "progress.jsonl"
    start_record(record, "riscv_tool_substrate", "nested")
    progress.write_text(
        '{"id":"export","name":"exporting layers","started":"2026-09-24T12:00:00Z",'
        '"completed":"2026-09-24T12:00:02Z"}\n',
        encoding="utf-8",
    )

    finish_build_record(
        record,
        0,
        progress,
        tmp_path / "missing-metadata.json",
        tmp_path / "missing-image.json",
        tmp_path / "missing-parent.json",
    )

    construction, load = json.loads(record.read_text(encoding="utf-8"))["phases"]
    assert construction["outcome"] == "unavailable"
    assert construction["elapsed_seconds"] is None
    assert construction["buildkit"]["command_outcome"] == "success"
    assert construction["buildkit"]["command_elapsed_seconds"] >= 0
    assert load["outcome"] == "unavailable"
    assert load["elapsed_seconds"] is None
    assert "no trustworthy" in load["reason"]


def test_parallel_headroom_uses_next_longest_lane(tmp_path: Path) -> None:
    records = tmp_path / "records"
    for name in ("riscv_candidate", "openroad", "verilator"):
        _completed_record(records / f"{name}.json", name, "parallel")
    payloads = []
    for name, elapsed in (("riscv_candidate", 12.0), ("openroad", 7.0), ("verilator", 9.0)):
        path = records / f"{name}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["phases"][0]["elapsed_seconds"] = elapsed
        _write(path, payload)
        payloads.append(path)

    summary = summarize_records(payloads, 1, 1, "sha", "2026-09-24T12:00:00Z")

    topology = summary["topology"]["parallel_group"]
    assert topology["next_longest_lane_seconds"] == 9
    assert topology["riscv_headroom_seconds"] == 3


def test_schema_rejects_negative_elapsed_time() -> None:
    with pytest.raises(TimingError, match="cannot be negative"):
        validate_phase(
            {
                "name": "bad",
                "topology": "parallel",
                "started_at": "2026-09-24T12:00:00Z",
                "completed_at": "2026-09-24T12:00:01Z",
                "elapsed_seconds": -1,
                "outcome": "success",
                "exit_code": 0,
            }
        )
