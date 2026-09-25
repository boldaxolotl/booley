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
    timed_run,
    validate_phase,
)


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _rawjson(path: Path, *vertices: dict[str, object]) -> Path:
    """Write BuildKit `--progress rawjson` snapshots: one `{"vertexes": [...]}` per line.

    Each vertex is emitted twice, first as a bare start and then complete, the
    way BuildKit repeats a vertex as its state advances.
    """
    lines = []
    for vertex in vertices:
        started = {key: vertex[key] for key in ("digest", "name", "started") if key in vertex}
        lines.append(json.dumps({"vertexes": [started]}))
        lines.append(json.dumps({"vertexes": [vertex], "statuses": [], "logs": []}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _vertex(digest: str, name: str, started: str, completed: str, **extra: object) -> dict:
    return {"digest": digest, "name": name, "started": started, "completed": completed, **extra}


_CONTEXT = _vertex(
    "sha256:context",
    "[context booley-standard-substrate] booley-standard-substrate:ci",
    "2026-09-24T12:00:00.000000001Z",
    "2026-09-24T12:00:00.000000002Z",
    cached=True,
)


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


def test_timed_run_preserves_wrapped_command_failure(tmp_path: Path) -> None:
    record = tmp_path / "wrapped.json"

    exit_code = timed_run(
        record,
        "openroad",
        "parallel",
        [sys.executable, "-c", "raise SystemExit(7)"],
    )

    phase = json.loads(record.read_text(encoding="utf-8"))["phases"][0]
    assert exit_code == 7
    assert phase["outcome"] == "failure"
    assert phase["exit_code"] == 7


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
        "cache_state": "not-requested",
        "tooling_cache_hit": False,
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
    _rawjson(
        progress,
        _vertex(
            "sha256:compile",
            "[stage-0 1/1] RUN build",
            "2026-09-24T12:00:00Z",
            "2026-09-24T12:00:04Z",
            cached=True,
        ),
        _vertex(
            "sha256:load", "importing to docker", "2026-09-24T12:00:04Z", "2026-09-24T12:00:06Z"
        ),
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
    _rawjson(
        progress,
        _vertex("sha256:run", "[1/1] RUN make", "2026-09-24T12:00:00Z", "2026-09-24T12:00:02Z"),
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
    assert construction["outcome"] == "success"
    assert construction["elapsed_seconds"] >= 0
    assert construction["attribution"] == "combined_construction_export_transfer_load"
    assert construction["buildkit"]["command_outcome"] == "success"
    assert construction["buildkit"]["command_elapsed_seconds"] >= 0
    assert load["outcome"] == "unavailable"
    assert load["elapsed_seconds"] is None
    assert "no trustworthy" in load["reason"]


def test_unavailable_transfer_prevents_complete_sample(tmp_path: Path) -> None:
    record = tmp_path / "records" / "build.json"
    start_record(record, "riscv_tool_substrate", "nested")
    progress = _rawjson(
        tmp_path / "progress.jsonl",
        _vertex("sha256:run", "[1/1] RUN make", "2026-09-24T12:00:00Z", "2026-09-24T12:00:02Z"),
    )
    finish_build_record(
        record,
        0,
        progress,
        tmp_path / "metadata.json",
        tmp_path / "image.json",
        tmp_path / "parent.json",
    )

    summary = summarize_records([record], 1, 1, "sha", "2026-09-24T12:00:03Z")

    assert summary["complete"] is False


def test_parallel_headroom_uses_completion_times_not_durations(tmp_path: Path) -> None:
    records = tmp_path / "records"
    for name in ("riscv_candidate", "openroad", "verilator"):
        _completed_record(records / f"{name}.json", name, "parallel")
    payloads = []
    observations = (
        ("riscv_candidate", "2026-09-24T12:00:00Z", "2026-09-24T12:00:12Z", 12.0),
        ("openroad", "2026-09-24T12:00:04Z", "2026-09-24T12:00:13Z", 9.0),
        ("verilator", "2026-09-24T12:00:01Z", "2026-09-24T12:00:08Z", 7.0),
    )
    for name, started, completed, elapsed in observations:
        path = records / f"{name}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["phases"][0]["started_at"] = started
        payload["phases"][0]["completed_at"] = completed
        payload["phases"][0]["elapsed_seconds"] = elapsed
        _write(path, payload)
        payloads.append(path)

    summary = summarize_records(payloads, 1, 1, "sha", "2026-09-24T12:00:00Z")

    topology = summary["topology"]["parallel_group"]
    assert topology["next_latest_lane"] == "openroad"
    assert topology["next_latest_lane_completed_at"] == "2026-09-24T12:00:13Z"
    assert topology["riscv_headroom_seconds"] == 0


def test_warm_cache_miss_is_not_a_representative_sample(tmp_path: Path) -> None:
    record = tmp_path / "records" / "build.json"
    progress = tmp_path / "progress.jsonl"
    start_record(record, "riscv_tool_substrate", "nested")
    _rawjson(
        progress,
        _CONTEXT,
        _vertex(
            "sha256:compile",
            "[1/1] RUN build",
            "2026-09-24T12:00:00Z",
            "2026-09-24T12:00:01Z",
            cached=True,
        ),
        _vertex(
            "sha256:load", "exporting to image", "2026-09-24T12:00:01Z", "2026-09-24T12:00:02Z"
        ),
    )
    finish_build_record(
        record,
        0,
        progress,
        tmp_path / "metadata.json",
        tmp_path / "image.json",
        tmp_path / "parent.json",
    )

    miss = summarize_records([record], 1, 1, "sha", "2026-09-24T12:00:00Z", "warm", "miss")
    hit = summarize_records([record], 2, 1, "sha", "2026-09-24T12:00:00Z", "warm", "hit")

    assert miss["representative"] is False
    assert hit["representative"] is True
    assert hit["run"]["tooling_cache_hit"] is True


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


def test_start_record_rejects_boolean_epoch(tmp_path: Path) -> None:
    record = tmp_path / "bad.json"
    _write(
        record,
        {
            "_started_epoch_ns": True,
            "phases": [
                {
                    "name": "riscv_candidate",
                    "topology": "parallel",
                    "started_at": "2026-09-24T12:00:00Z",
                    "completed_at": None,
                    "elapsed_seconds": None,
                    "outcome": "running",
                    "exit_code": None,
                }
            ],
        },
    )

    with pytest.raises(TimingError, match="must be an integer"):
        finish_record(record, 0)


def test_docker_driver_export_is_the_transfer_load_boundary(tmp_path: Path) -> None:
    """The default docker driver writes straight into the daemon store.

    Its only load step is the "exporting to image" vertex, so that vertex must
    split construction from transfer/load instead of leaving load unavailable.
    """
    record = tmp_path / "build.json"
    start_record(record, "riscv_tool_substrate", "nested")
    progress = _rawjson(
        tmp_path / "progress.jsonl",
        _CONTEXT,
        _vertex(
            "sha256:spike",
            "[4/7] RUN build spike",
            "2026-09-24T12:00:00.123456789Z",
            "2026-09-24T12:08:00.123456789Z",
        ),
        _vertex(
            "sha256:export",
            "exporting to image",
            "2026-09-24T12:08:00.2Z",
            "2026-09-24T12:08:07.2Z",
        ),
    )

    finish_build_record(
        record, 0, progress, tmp_path / "m.json", tmp_path / "i.json", tmp_path / "p.json"
    )

    construction, load = json.loads(record.read_text(encoding="utf-8"))["phases"]
    assert "attribution" not in construction
    assert construction["elapsed_seconds"] == 480.2  # from the earliest vertex (context)
    assert load["name"] == "riscv_tool_substrate_transfer_load"
    assert load["outcome"] == "success"
    assert load["elapsed_seconds"] == 7
    summary = summarize_records([record], 1, 1, "sha", "2026-09-24T12:09:00Z")
    measured = {phase["name"]: phase["outcome"] for phase in summary["phases"]}
    assert measured["riscv_tool_substrate_transfer_load"] == "success"


@pytest.mark.parametrize(
    ("cached_steps", "expected_hit"),
    [((), False), (("sha256:gcc",), False), (("sha256:gcc", "sha256:spike"), True)],
)
def test_tooling_cache_hit_requires_every_build_step_cached(
    tmp_path: Path, cached_steps: tuple[str, ...], expected_hit: bool
) -> None:
    """The named parent context is always reported cached and must not count."""
    record = tmp_path / "build.json"
    start_record(record, "riscv_tool_substrate", "nested")
    steps = [
        _vertex(
            digest,
            f"[{index}/2] RUN {digest}",
            "2026-09-24T12:00:00Z",
            "2026-09-24T12:00:01Z",
            **({"cached": True} if digest in cached_steps else {}),
        )
        for index, digest in enumerate(("sha256:gcc", "sha256:spike"), start=1)
    ]
    progress = _rawjson(tmp_path / "progress.jsonl", _CONTEXT, *steps)

    finish_build_record(
        record, 0, progress, tmp_path / "m.json", tmp_path / "i.json", tmp_path / "p.json"
    )

    buildkit = json.loads(record.read_text(encoding="utf-8"))["phases"][0]["buildkit"]
    assert buildkit["build_step_count"] == 2
    assert buildkit["cached_build_step_count"] == len(cached_steps)
    assert buildkit["cache_hit"] is expected_hit


def test_baseline_arm_is_a_representative_sample(tmp_path: Path) -> None:
    record = tmp_path / "records" / "lane.json"
    _completed_record(record, "riscv_candidate", "parallel")

    summary = summarize_records([record], 1, 1, "sha", "2026-09-24T12:00:00Z", "baseline")

    assert summary["run"]["measurement_arm"] == "baseline"
    assert summary["representative"] is True
