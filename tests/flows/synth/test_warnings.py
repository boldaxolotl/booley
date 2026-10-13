"""Tool-native synthesis warning and structural-condition parsing."""

from __future__ import annotations

import pytest

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


def test_huge_warning_message_is_bounded_and_traceable() -> None:
    instance_list = " ".join(f"u{i}" for i in range(500_000))
    warning = f"[WARNING STA-0349] missing clocks on {instance_list}\n"

    summary = parse_synth_diagnostics({"openroad": warning}).warnings.to_detail()

    representative = summary["representatives"][0]
    assert len(representative["message"].encode("utf-8")) <= 2 * 1024
    assert representative["truncated"] is True
    assert representative["original_bytes"] > 2 * 1024
    assert len(representative["message_sha256"]) == 64
    assert representative["code"] == "STA-0349"


def test_huge_warning_groups_use_full_message_digest() -> None:
    prefix = "[WARNING STA-0349] " + "x" * 4_000
    warnings = f"{prefix} first\n{prefix} second\n"

    summary = parse_synth_diagnostics({"openroad": warnings}).warnings.to_detail()

    representatives = summary["representatives"]
    assert summary["unique_warnings"] == 2
    assert representatives[0]["message"] == representatives[1]["message"]
    assert representatives[0]["message_sha256"] != representatives[1]["message_sha256"]


def test_missing_final_check_is_incomplete_not_clean() -> None:
    diagnostics = parse_synth_diagnostics({"yosys": "Warnings: 0 unique messages.\n"})

    assert diagnostics.structural.complete is False
    assert diagnostics.structural.comb_loops == 0
    assert diagnostics.structural.multi_driven == 0
    assert diagnostics.warnings.total_warnings == 0


def test_earlier_loop_is_advisory_when_final_check_is_clean() -> None:
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": "Warning: found logic loop in module top:\n    cell $add$rtl.sv:7$1\n    wire \\feedback\n",
            "final_check": "Found and reported 0 problems.\n",
        }
    )

    assert diagnostics.structural.complete is True
    assert diagnostics.structural.comb_loops == 0
    assert diagnostics.structural.multi_driven == 0
    assert diagnostics.warnings.by_category == {"combinational_loop": 1}
    assert diagnostics.warnings.advisory_warnings == 1
    representative = diagnostics.warnings.representatives[0]
    assert "$add$rtl.sv:7$1" in representative.message
    assert "\\feedback" in representative.message
    assert (
        "completed final netlist check reports no combinational loops" in representative.rationale
    )


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


@pytest.mark.parametrize("final", [None, "", "Checking module top...\n"])
def test_earlier_loop_without_completed_final_stays_structural(final):
    sources = {"yosys": "Warning: found logic loop in module top:\n    wire \\feedback\n"}
    if final is not None:
        sources["final_check"] = final
    diagnostics = parse_synth_diagnostics(sources)
    assert not diagnostics.structural.complete
    assert diagnostics.warnings.structural_warnings == 1
    assert diagnostics.warnings.representatives[0].rationale is None


@pytest.mark.parametrize("count", [0, 2])
def test_earlier_repeated_loops_do_not_change_exact_final_count(count):
    loop = "Warning: found logic loop in module top:\n    wire \\feedback\n"
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": loop * 20,
            "final_check": loop * count + f"Found and reported {count} problems.\n",
        }
    )
    assert diagnostics.structural.comb_loops == count
    assert diagnostics.warnings.total_warnings == 20 + count
    assert diagnostics.warnings.advisory_warnings == (20 if count == 0 else 0)


def test_earlier_driver_clean_final_remains_inventory_only():
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": "Warning: multiple conflicting drivers for top.sig:\n",
            "final_check": "Found and reported 0 problems.\n",
        }
    )
    assert diagnostics.structural.multi_driven == 0
    assert diagnostics.warnings.structural_warnings == 1
