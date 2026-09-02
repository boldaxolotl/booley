"""Focused regression tests for memory resource policy."""

import ast
from pathlib import Path

import pytest
from tests.architecture.production import assert_no_dependencies

from booley.audit import resource_policy

_ROOT = Path(__file__).resolve().parents[2]
_GIB = resource_policy.GIB_BYTES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6g", 6 * _GIB),
        ("512M", 512 * 1024**2),
        ("2048", 2048),
        ("lots", None),
    ],
)
def test_parse_memory_limit(text: str, expected: int | None) -> None:
    assert resource_policy.parse_memory_limit(text) == expected


def test_configured_sandbox_memory_accepts_current_and_legacy_shapes() -> None:
    assert resource_policy.configured_sandbox_memory({"sandbox": {"memory": " 8g "}}) == "8g"
    assert (
        resource_policy.configured_sandbox_memory({"sandbox": {"memory": {"default": "12g"}}})
        == "12g"
    )
    assert resource_policy.configured_sandbox_memory({"sandbox": "invalid"}) == ""


def test_cgroup_limit_handles_v2_and_v1_unlimited_values(tmp_path: Path, monkeypatch) -> None:
    invalid = tmp_path / "memory.max"
    invalid.write_text("invalid", encoding="utf-8")
    v1 = tmp_path / "memory.limit_in_bytes"
    v1.write_text(str(1 << 62), encoding="utf-8")
    monkeypatch.setattr(resource_policy, "CGROUP_MEMORY_LIMIT_PATHS", (invalid, v1))

    assert resource_policy.cgroup_memory_limit_bytes() is None

    invalid.write_text(str(9 * _GIB), encoding="utf-8")
    assert resource_policy.cgroup_memory_limit_bytes() == 9 * _GIB


def test_heavy_reservation_validates_configured_value() -> None:
    default = resource_policy.heavy_memory_reservation(None, None, ())
    invalid = resource_policy.heavy_memory_reservation("lots", None, ())
    zero = resource_policy.heavy_memory_reservation("0g", None, ())

    assert default == resource_policy.HeavyMemoryReservation(4 * _GIB, "4g default")
    assert invalid.bytes == 4 * _GIB
    assert "unparseable" in str(invalid.error)
    assert "greater than zero" in str(zero.error)


def test_calibrated_peak_adds_margin_and_rounds_up_to_gib() -> None:
    calibration = resource_policy.SynthesisMemoryCalibration(
        target="asic_full",
        peak_rss_bytes=int(15.8 * _GIB),
    )

    reservation = resource_policy.heavy_memory_reservation(
        "16g",
        calibration,
        ("asic_full",),
    )

    assert reservation.bytes == 19 * _GIB
    assert reservation.error is None
    assert reservation.evidence == "15.8g measured on asic_full + 15% margin"


def test_larger_configured_reservation_is_not_reduced_by_calibration() -> None:
    calibration = resource_policy.SynthesisMemoryCalibration("asic_full", 8 * _GIB)

    reservation = resource_policy.heavy_memory_reservation(
        "16g",
        calibration,
        ("asic_full",),
    )

    assert reservation.bytes == 16 * _GIB
    assert reservation.evidence == "configured 16g; calibrated peak 8g"


def test_calibration_must_belong_to_current_doctor_matrix() -> None:
    calibration = resource_policy.SynthesisMemoryCalibration("asic_small", 7 * _GIB)
    current = resource_policy.heavy_memory_reservation(
        None,
        calibration,
        ("asic_small", "asic_full"),
    )
    stale = resource_policy.heavy_memory_reservation(
        None,
        calibration,
        ("asic_full",),
    )

    assert current.error is None
    assert "unselected Target 'asic_small'" in str(stale.error)


def test_memory_requirement_exposes_typed_admission_arithmetic() -> None:
    requirement = resource_policy.memory_requirement(
        max_heavy=2,
        heavy_job_bytes=4 * _GIB,
        max_tickets=1,
        developer_bytes=_GIB,
    )

    assert requirement.required_bytes == 11 * _GIB


def test_doctor_does_not_reimplement_extracted_resource_mechanisms() -> None:
    doctor_path = _ROOT / "src" / "booley" / "harness" / "doctor.py"
    tree = ast.parse(doctor_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    extracted = {
        "_cgroup_memory_limit_bytes",
        "_configured_sandbox_memory",
        "_fmt_g",
        "_heavy_job_mem_bytes",
        "_parse_mem_limit",
    }
    assert not function_names & extracted


def test_resource_policy_does_not_depend_on_presentation_layers() -> None:
    module_path = _ROOT / "src" / "booley" / "audit" / "resource_policy.py"
    assert_no_dependencies(
        paths=(module_path,),
        target_prefixes=("booley.harness", "booley.mcp", "booley.specialists"),
    )
