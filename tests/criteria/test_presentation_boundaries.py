"""Criterion presentation tolerates malformed observations."""

import pytest

from booley.criteria.presentation import _format_criterion_presentation, _format_metric


@pytest.mark.parametrize(
    ("key", "entry", "expected"),
    [
        ("coverage_sim", {"detail": None, "params": None}, ""),
        ("coverage_sim", {"detail": {"status": None}}, ""),
        (
            "coverage_sim",
            {"detail": {"status": "pass"}},
            "pass",
        ),
        ("fpga_impl_ok", {"detail": {"lut_count": "bad", "wns_ns": []}}, ""),
        (
            "fpga_impl_ok",
            {"detail": {"lut_count": "1200", "ff_count": 12, "wns_ns": "-0.25"}},
            "1.2k LUTs | 12 FFs | WNS -0.25ns",
        ),
        ("synthesis_ok", {"detail": {"cells": object(), "per_clock": []}}, ""),
        ("mutation_score", {"detail": {"detected": "bad", "total_valid": 0}}, ""),
        ("lint_clean", {"detail": "not-a-table"}, ""),
        ("review_correctness", None, ""),
    ],
)
def test_malformed_metric_detail_degrades_without_raising(
    key: str,
    entry: dict | None,
    expected: str,
) -> None:
    """CRT-04/05: external metric payloads cannot crash header rendering."""
    assert _format_metric(key, entry) == expected


@pytest.mark.parametrize(
    ("key", "entry", "label", "detail"),
    [
        (
            "lint_clean_lint_core",
            {"mandatory": True, "detail": {"warnings": 0}},
            "Lint · lint_core",
            "required no unwaived findings · observed clean",
        ),
        (
            "elab_pass_sim_core",
            {"mandatory": True},
            "Elaboration · sim_core",
            "required every selected design elaborates",
        ),
        (
            "fpga_impl_ok_fpga_core",
            {
                "mandatory": True,
                "params": {"lut_count_max": 4_000, "clk_i.fmax_mhz_min": 250},
                "detail": {
                    "checks": [
                        {"param": "lut_count_max", "value": 3_800, "pass": True},
                        {
                            "param": "clk_i.fmax_mhz_min",
                            "value": 275,
                            "pass": True,
                        },
                    ]
                },
            },
            "FPGA implementation · fpga_core",
            (
                "required LUTs ≤ 4,000 · clk_i Fmax ≥ 250 MHz · "
                "observed LUTs 3,800 · clk_i Fmax 275 MHz"
            ),
        ),
        (
            "synthesis_ok_synth_core",
            {
                "mandatory": True,
                "params": {"area_um2_max": 1_000, "wire_count_max": 10_000},
                "detail": {
                    "checks": [
                        {"param": "area_um2_max", "value": 950, "pass": True},
                        {"param": "wire_count_max", "value": 9_500, "pass": True},
                    ]
                },
            },
            "ASIC synthesis · synth_core",
            ("required area ≤ 1,000 μm² · wires ≤ 10,000 · observed area 950 μm² · wires 9,500"),
        ),
        (
            "mutation_score_sim_core",
            {"mandatory": False, "params": {"min_detected": 8, "total": 10}},
            "Mutation testing · sim_core",
            "goal ≥ 8/10 mutations detected",
        ),
        (
            "coverage_sim_core",
            {
                "mandatory": True,
                "params": {"metrics": {"line": {"min_pct": 90}}},
                "detail": {
                    "status": "pass",
                    "metrics": [{"metric": "line", "actual_percent": 92.5}],
                },
            },
            "Coverage · sim_core",
            "required line ≥ 90% · observed line 92.5%",
        ),
        (
            "coverage_encoded",
            {
                "mandatory": True,
                "params": {
                    "target": "vendor:library:core:1#sim_generated",
                    "metrics": {"line": {"min_pct": 90}},
                },
                "detail": {
                    "evaluation": {
                        "metrics": [
                            {
                                "metric": "line",
                                "covered_points": 3,
                                "eligible_points": 3,
                                "actual_percent": 100,
                            }
                        ]
                    }
                },
            },
            "Coverage · sim_generated",
            (
                "target vendor:library:core:1#sim_generated · required line ≥ 90% "
                "· observed line 3/3 (100%)"
            ),
        ),
        (
            "coverage_zero",
            {
                "mandatory": True,
                "params": {
                    "target": "sim_empty",
                    "metrics": {"line": {"min_pct": 90}},
                },
                "detail": {
                    "evaluation": {
                        "metrics": [
                            {
                                "metric": "line",
                                "covered_points": 0,
                                "eligible_points": 0,
                                "actual_percent": None,
                            }
                        ]
                    }
                },
            },
            "Coverage · sim_empty",
            "required line ≥ 90% · observed line 0/0 (not scorable)",
        ),
        (
            "review_rtl_bugs_done",
            {"mandatory": True},
            "RTL bugs review",
            "required review completed",
        ),
        (
            "sim_pass_tb_uart.sv_sim_uart_test_transmit",
            {
                "mandatory": True,
                "params": {
                    "target": "sim_uart",
                    "tb_path": "tb/tb_uart.sv",
                    "test_selector": "test_transmit",
                    "from_state": "fail",
                },
            },
            "Simulation · sim_uart",
            "tb tb_uart.sv · test test_transmit · required fail → pass transition",
        ),
        (
            "project_specific_gate",
            {"mandatory": True},
            "project_specific_gate",
            "",
        ),
    ],
)
def test_builtin_criterion_presentations_are_concise_and_informative(
    key: str,
    entry: dict,
    label: str,
    detail: str,
) -> None:
    presentation = _format_criterion_presentation(key, entry)
    assert presentation.label == label
    assert presentation.detail == detail
