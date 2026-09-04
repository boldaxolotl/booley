"""Tool-native synthesis warning and structural-condition parsing."""

from __future__ import annotations

from booley.flows.synth.warnings import parse_synth_diagnostics


def test_real_yosys_v068_structural_warnings_are_counted() -> None:
    yosys = (
        "Warning: multiple conflicting drivers for top.sig[0]:\n"
        "    port Q[0] of cell $procdff$1 ($dff)\n"
    ) * 24 + (
        "Warning: found logic loop in module top:\n"
        "    cell $logic_and$1 ($and)\n"
        "    wire \\feedback\n"
    ) * 16
    final_check = (
        (
            "Warning: multiple conflicting drivers for top.sig[0]:\n"
            "    port Q[0] of cell $procdff$1 ($dff)\n"
        )
        * 24
        + (
            "Warning: found logic loop in module top:\n"
            "    cell $logic_and$1 ($and)\n"
            "    wire \\feedback\n"
        )
        * 16
        + "Found and reported 40 problems.\n"
    )

    diagnostics = parse_synth_diagnostics({"yosys": yosys, "final_check": final_check})

    assert diagnostics.structural.complete is True
    assert diagnostics.structural.comb_loops == 16
    assert diagnostics.structural.multi_driven == 24
    assert diagnostics.warnings.total_warnings == 80
    assert diagnostics.warnings.unique_warnings == 2
    assert diagnostics.warnings.by_tool == {"yosys": 80}
    assert diagnostics.warnings.by_category == {
        "combinational_loop": 32,
        "multi_driver": 48,
    }
    assert diagnostics.warnings.by_disposition == {"structural": 80}


def test_warning_groups_keep_counts_and_representative_diagnostics() -> None:
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": "ABC: Warning: The network has multiple outputs.\n",
            "openroad": (
                "[WARNING STA-0441] set_input_delay relative to a clock defined "
                "on the same port/pin not allowed.\n"
                "[WARNING STA-0503] find_timing_paths -group_count is deprecated. "
                "Use -group_path_count instead.\n"
                "[WARNING STA-0503] find_timing_paths -group_count is deprecated. "
                "Use -group_path_count instead.\n"
            ),
            "final_check": "Found and reported 0 problems.\n",
        }
    )

    summary = diagnostics.warnings.to_detail()
    assert summary["total_warnings"] == 4
    assert summary["unique_warnings"] == 3
    assert summary["by_tool"] == {"abc": 1, "openroad": 3}
    assert summary["by_category"] == {
        "constraint": 1,
        "deprecation": 2,
        "other": 1,
    }
    assert summary["by_disposition"] == {"advisory": 2, "benign": 2}
    representatives = summary["representatives"]
    assert len(representatives) == 3
    deprecation = next(item for item in representatives if item["code"] == "STA-0503")
    assert deprecation["count"] == 2
    assert deprecation["disposition"] == "benign"
    assert "Booley-generated deprecated query" in deprecation["rationale"]


def test_missing_final_check_is_incomplete_not_clean() -> None:
    diagnostics = parse_synth_diagnostics({"yosys": "Warnings: 0 unique messages.\n"})

    assert diagnostics.structural.complete is False
    assert diagnostics.structural.comb_loops == 0
    assert diagnostics.structural.multi_driven == 0
    assert diagnostics.warnings.total_warnings == 0


def test_final_check_without_completion_marker_is_incomplete() -> None:
    diagnostics = parse_synth_diagnostics(
        {"final_check": ("Warning: found logic loop in module top:\n    wire \\feedback\n")}
    )

    assert diagnostics.structural.complete is False
    assert diagnostics.structural.comb_loops == 0
    assert diagnostics.warnings.total_warnings == 1


def test_informational_warning_summary_is_not_a_warning_record() -> None:
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": "Warnings: 7 unique messages, 9 total\n",
            "final_check": "Found and reported 0 problems.\n",
        }
    )

    assert diagnostics.warnings.total_warnings == 0
