"""Tool-native synthesis warning and structural-condition parsing."""

from __future__ import annotations

import pytest

from booley.flows.synth.warnings import parse_synth_diagnostics
from tests.flows.synth.captured_yosys import diagnostic_sources

_CLEAN_CHECK = "Found and reported 0 problems.\n"


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

    diagnostics = parse_synth_diagnostics(
        {"yosys": yosys, "premap_check": _CLEAN_CHECK, "final_check": final_check}
    )

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
            "premap_check": _CLEAN_CHECK,
            "final_check": _CLEAN_CHECK,
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
            "premap_check": _CLEAN_CHECK,
            "final_check": _CLEAN_CHECK,
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
    assert "completed post-optimisation check reports no combinational loops" in (
        representative.rationale or ""
    )


def test_final_check_without_completion_marker_is_incomplete() -> None:
    diagnostics = parse_synth_diagnostics(
        {
            "premap_check": _CLEAN_CHECK,
            "final_check": "Warning: found logic loop in module top:\n    wire \\feedback\n",
        }
    )

    assert diagnostics.structural.complete is False
    assert diagnostics.structural.comb_loops == 0
    assert diagnostics.warnings.total_warnings == 1


def test_informational_warning_summary_is_not_a_warning_record() -> None:
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": "Warnings: 7 unique messages, 9 total\n",
            "premap_check": _CLEAN_CHECK,
            "final_check": _CLEAN_CHECK,
        }
    )

    assert diagnostics.warnings.total_warnings == 0


@pytest.mark.parametrize(
    ("premap", "final"),
    [
        (_CLEAN_CHECK, None),
        (_CLEAN_CHECK, ""),
        (_CLEAN_CHECK, "Checking module top...\n"),
        (None, _CLEAN_CHECK),
        ("", _CLEAN_CHECK),
        ("Checking module top...\n", _CLEAN_CHECK),
        (None, None),
    ],
)
def test_earlier_loop_without_both_completed_checks_stays_structural(premap, final):
    sources = {"yosys": "Warning: found logic loop in module top:\n    wire \\feedback\n"}
    if premap is not None:
        sources["premap_check"] = premap
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
            "premap_check": loop * count + f"Found and reported {count} problems.\n",
            "final_check": _CLEAN_CHECK,
        }
    )
    assert diagnostics.structural.comb_loops == count
    # The pre-mapping check is a count source only: it is not in the inventory.
    assert diagnostics.warnings.total_warnings == 20
    assert diagnostics.warnings.advisory_warnings == (20 if count == 0 else 0)


def test_earlier_driver_clean_final_remains_inventory_only():
    diagnostics = parse_synth_diagnostics(
        {
            "yosys": "Warning: multiple conflicting drivers for top.sig:\n",
            "premap_check": _CLEAN_CHECK,
            "final_check": _CLEAN_CHECK,
        }
    )
    assert diagnostics.structural.multi_driven == 0
    assert diagnostics.warnings.structural_warnings == 1


def test_real_yosys_v070_check_output_is_counted() -> None:
    # Captured from Yosys 0.70+ (28ba3cb92) through Booley's generated script:
    # one combinational loop and one multi-driven net in the design.
    sources = diagnostic_sources("check_yosys_0_70.txt")
    yosys, premap_check, final_check = (
        sources["yosys"],
        sources["premap_check"],
        sources["final_check"],
    )

    diagnostics = parse_synth_diagnostics(sources)

    # The pre-mapping check sees the loop through generic cells; the final check
    # runs after Liberty mapping, where cells are opaque, so it sees only the
    # multi-driven net.
    assert diagnostics.structural.complete is True
    assert diagnostics.structural.comb_loops == 1
    assert diagnostics.structural.multi_driven == 1
    # Booley's own pre-mapping check is not an inventory source: Yosys's
    # ``check`` passes inside ``synth`` already report the same warnings.
    assert diagnostics.warnings.by_category == {"combinational_loop": 2, "multi_driver": 3}
    assert diagnostics.warnings.by_disposition == {"structural": 5}
    assert "deprecated" not in (yosys + premap_check + final_check).lower()
    # ``tee -q`` keeps both Booley-owned checks out of ``yosys.log``.
    assert "Checking module chk" in premap_check
    assert "Checking module chk" in final_check
    assert yosys.count("Found and reported") == 2


@pytest.mark.parametrize(
    ("fixture", "comb_loops", "disposition"),
    [
        ("comb_loop_yosys_0_70.txt", 1, "structural"),
        ("ff_feedback_yosys_0_70.txt", 0, None),
        ("transient_loop_yosys_0_70.txt", 0, "advisory"),
    ],
    ids=["comb_loop", "ff_feedback", "transient_loop"],
)
def test_real_yosys_v070_loop_cases_are_counted(fixture, comb_loops, disposition) -> None:
    # Captured from Yosys 0.70+ (28ba3cb92) and the pinned Nangate45 Liberty
    # through Booley's generated script. The transient case is the #1417
    # pattern: a loop ``synth``'s first check reports and its optimisation
    # removes. Only the slang frontend produces it; the sv2v path does not.
    diagnostics = parse_synth_diagnostics(diagnostic_sources(fixture))

    assert diagnostics.structural.complete is True
    assert diagnostics.structural.comb_loops == comb_loops
    assert diagnostics.structural.multi_driven == 0
    loops = [
        group
        for group in diagnostics.warnings.representatives
        if group.category == "combinational_loop"
    ]
    assert {group.disposition for group in loops} == ({disposition} if disposition else set())
    assert diagnostics.warnings.structural_warnings == (
        diagnostics.warnings.total_warnings if disposition == "structural" else 0
    )


def test_loop_in_final_check_alone_still_counts() -> None:
    # The larger of the two counts wins, so a loop the final check ever reports
    # still fails the run even when the pre-mapping check is clean.
    loop = "Warning: found logic loop in module top:\n    wire \\feedback\n"
    diagnostics = parse_synth_diagnostics(
        {
            "premap_check": _CLEAN_CHECK,
            "final_check": loop + "Found and reported 1 problems.\n",
        }
    )

    assert diagnostics.structural.complete is True
    assert diagnostics.structural.comb_loops == 1
