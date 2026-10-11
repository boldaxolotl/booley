#!/usr/bin/env python3
"""Run actual generated fanout workloads with durable native evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from booley.flows.synth.backends.openroad.timing import OpenRoadPdk, write_openroad_script
from booley.flows.synth.timing import StaTimingConfig

BENCHMARK_TIMEOUT_S = 120
BENCHMARK_TIMER = "/usr/bin/time"


def fixture(path: Path, count: int) -> tuple[Path, Path]:
    """Generate the deterministic physical reset-load chain."""
    netlist = path / "dut.v"
    lines = [
        "module dut(input clk, input reset_i, input data_i, output data_o);",
        "wire reset_n;",
        f"wire [{count}:0] q;",
        "assign q[0] = data_i;",
        f"assign data_o = q[{count}];",
        "INV_X1 driver (.A(reset_i), .ZN(reset_n));",
    ]
    lines.extend(
        f"DFFR_X1 load_{i} (.D(q[{i}]), .CK(clk), .RN(reset_n), .Q(q[{i + 1}]));"
        for i in range(count)
    )
    netlist.write_text("\n".join([*lines, "endmodule"]) + "\n", encoding="utf-8")
    sdc = path / "dut.sdc"
    sdc.write_text("create_clock -name clk -period 20 [get_ports clk]\n", encoding="utf-8")
    return netlist, sdc


def fixture_liberty(
    path: Path,
    pdk_root: Path,
    library_limit: float | None,
    library_pin_limit: float | None,
    library_pin_cell: str,
    fanout_load: float | None,
) -> Path:
    """Change only test library constraints and load units, never parse production authority."""
    liberty = pdk_root / "cell/lib/NangateOpenCellLibrary_typical_ccs.lib"
    if any(value is not None for value in (library_limit, library_pin_limit, fanout_load)):
        text = liberty.read_text(encoding="utf-8")
        if library_limit is not None:
            start = text.index("{") + 1
            text = text[:start] + f"\n default_max_fanout : {library_limit};\n" + text[start:]
        if library_pin_limit is not None:
            pin_name = "Z" if library_pin_cell.startswith("BUF_") else "ZN"
            start = text.index(f"pin ({pin_name})", text.index(f"cell ({library_pin_cell})"))
            start = text.index("{", start) + 1
            text = text[:start] + f"\n max_fanout : {library_pin_limit};\n" + text[start:]
        if fanout_load is not None:
            text = re.sub(r"(default_fanout_load\s*:)\s*[^;]+", rf"\g<1> {fanout_load}", text)
        liberty = path / "fanout.lib"
        liberty.write_text(text, encoding="utf-8")
    return liberty


def prepare_inputs(
    path: Path, count: int, sdc_rule: str, port_driver: bool, netlist_text: str | None
) -> tuple[Path, Path]:
    """Retain exact physical fixture inputs before native execution."""
    path.mkdir(parents=True, exist_ok=True)
    netlist, sdc = fixture(path, count)
    if netlist_text is not None:
        netlist.write_text(netlist_text, encoding="utf-8")
    elif port_driver:
        netlist.write_text(
            netlist.read_text().replace(
                "INV_X1 driver (.A(reset_i), .ZN(reset_n));", "assign reset_n=reset_i;"
            ),
            encoding="utf-8",
        )
    with sdc.open("a", encoding="utf-8") as stream:
        stream.write(sdc_rule)
    return netlist, sdc


def generate(
    path: Path,
    pdk_root: Path,
    count: int,
    *,
    setting: int | None,
    sdc_rule: str,
    library_limit: float | None,
    setup: bool,
    library_pin_limit: float | None = None,
    library_pin_cell: str = "INV_X1",
    fanout_load: float | None = None,
    port_driver: bool = False,
    netlist_text: str | None = None,
) -> Path:
    """Render the production generator, retaining authored inputs separately."""
    netlist, sdc = prepare_inputs(path, count, sdc_rule, port_driver, netlist_text)
    liberty = fixture_liberty(
        path, pdk_root, library_limit, library_pin_limit, library_pin_cell, fanout_load
    )
    reports = path / "reports/timing"
    reports.mkdir(parents=True, exist_ok=True)
    return write_openroad_script(
        "dut",
        liberty,
        netlist,
        (sdc,),
        OpenRoadPdk(
            *(
                pdk_root / "nangate45" / name
                for name in ("Nangate45_tech.lef", "Nangate45_stdcell.lef", "Nangate45.rc")
            )
        ),
        reports,
        path,
        StaTimingConfig(mode="physical", repair_timing=setup, max_fanout=setting),
        target="fanout_acceptance",
        source_sdc_paths=(sdc,),
        fanout_report_dir=path / "reports/fanout",
    )


def instrument(script: Path) -> Path:
    """Measure repair independently without changing any generated controls."""
    text = script.read_text(encoding="utf-8")
    text = "set _fanout_total_start [clock milliseconds]\n" + text
    text = text.replace(
        "\nrepair_design\n",
        "\nset _fanout_repair_start [clock milliseconds]\n"
        'repair_design\nputs "REPAIR_ELAPSED_MS: [expr {[clock milliseconds]-$_fanout_repair_start}]"\n',
    )
    text += '\nputs "POST_REPAIR_FANOUT_VIOLATIONS: [sta::max_fanout_violation_count]"\n'
    text += '\nputs "TOTAL_ELAPSED_MS: [expr {[clock milliseconds]-$_fanout_total_start}]"\n'
    result = script.with_name("measured.tcl")
    result.write_text(text, encoding="utf-8")
    return result


def native_run(script: Path, timeout: float, executable: str) -> dict:
    """Execute a required native arm and retain stdout even on failure."""
    started = time.monotonic()
    log = script.parent / "native.log"
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            [executable, "-exit", str(instrument(script))],
            cwd=script.parent,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    text = log.read_text(encoding="utf-8")
    if process.returncode:
        raise RuntimeError(f"native fanout arm failed ({process.returncode}): {log}")
    repair = re.findall(r"REPAIR_ELAPSED_MS: (\d+)", text)
    total = re.findall(r"TOTAL_ELAPSED_MS: (\d+)", text)
    if len(repair) != 1 or len(total) != 1:
        raise RuntimeError("native fanout arm missing measured stage evidence")
    return {
        "repair_s": int(repair[0]) / 1000,
        "full_s": int(total[0]) / 1000,
        "wall_s": time.monotonic() - started,
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "log": str(log),
        "engine": text.splitlines()[0],
    }


def run_baseline(path: Path, original: Path, executable: str) -> dict:
    """Retain original input bytes; relocate report outputs without altering controls."""
    path.mkdir(parents=True, exist_ok=True)
    reports = path / "reports"
    reports.mkdir()
    baseline = path / "baseline.tcl"
    original_text = original.read_text(encoding="utf-8")
    original_reports = str(original.parent / "reports")
    baseline.write_text(original_text.replace(original_reports, str(reports)), encoding="utf-8")
    result = native_run(baseline, 120, executable)
    result["original_script_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    result["output_sha256"] = hashlib.sha256((path / "openroad_dut.v").read_bytes()).hexdigest()
    result["output_path_relocation"] = {original_reports: str(reports)}
    text = (path / "native.log").read_text(encoding="utf-8")
    result["timing_markers"] = [line for line in text.splitlines() if line.startswith("STA_")]
    (path / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def run_case(
    path: Path,
    pdk: Path,
    count: int,
    *,
    setting: int | None = None,
    sdc_rule: str = "",
    library_limit: float | None = None,
    setup: bool = False,
    timeout: float = 120,
    executable: str = "openroad",
    library_pin_limit: float | None = None,
    library_pin_cell: str = "INV_X1",
    fanout_load: float | None = None,
    port_driver: bool = False,
    expected_limit: float | None = None,
) -> dict:
    """Check point source and real physical execution for one workload."""
    script = generate(
        path,
        pdk,
        count,
        setting=setting,
        sdc_rule=sdc_rule,
        library_limit=library_limit,
        setup=setup,
        library_pin_limit=library_pin_limit,
        library_pin_cell=library_pin_cell,
        fanout_load=fanout_load,
        port_driver=port_driver,
    )
    result = native_run(script, timeout, executable)
    expected = (
        "sdc"
        if sdc_rule
        else "library"
        if library_limit is not None or library_pin_limit is not None
        else "setting"
        if setting
        else "booley_default"
    )
    selected = "reset_i" if port_driver else "driver/ZN"
    if not sdc_rule:
        expected_limit = library_pin_limit if library_pin_limit is not None else library_limit
        if expected_limit is None:
            expected_limit = setting
    elif expected_limit is None:
        raise RuntimeError("authored physical fixture lacks its expected exact limit")
    return check_physical_arm(path, result, expected, selected, fanout_load, expected_limit)


def check_physical_arm(
    path: Path,
    result: dict,
    expected: str,
    selected: str,
    fanout_load: float | None,
    expected_limit: float | None,
) -> dict:
    """Require completed physical stages, exact provenance and actual fanout repair."""
    policy = json.loads((path / "reports/fanout/policy.json").read_text(encoding="utf-8"))
    text = (path / "native.log").read_text(encoding="utf-8")
    for stage in (
        "floorplan",
        "global_placement",
        "repair_design",
        "detailed_placement",
        "sta_report",
    ):
        if f"BOOLEY_STAGE: {stage}" not in text:
            raise RuntimeError(f"missing native physical stage {stage}")
    if "BOOLEY_INPUT_ERROR:" in text or "[WARNING " in text:
        raise RuntimeError("native policy arm emitted an input error or warning")
    if policy["worst_check"]["source"] != expected:
        raise RuntimeError(f"native source {policy} differs from {expected}")
    if policy["worst_check"]["max_fanout"] != expected_limit:
        raise RuntimeError(f"native exact limit {policy} differs from {expected_limit}")
    if expected != "booley_default" and policy["worst_check"]["pin"] != selected:
        raise RuntimeError("native fixture did not select constrained reset driver")
    remaining = re.findall(r"POST_REPAIR_FANOUT_VIOLATIONS: (\d+)", text)
    if len(remaining) != 1:
        raise RuntimeError("native arm missing post-repair fanout violation count")
    weighted = fanout_load is not None and fanout_load != 1
    if not weighted and remaining != ["0"]:
        raise RuntimeError("native ordinary-load arm retained fanout violations")
    netlist = (path / "openroad_dut.v").read_text(encoding="utf-8")
    buffers = len(re.findall(r"\bBUF_X\d+\s", netlist))
    if expected != "booley_default" and buffers == 0:
        raise RuntimeError("constrained actual load fixture inserted no structural buffers")
    result["inserted_buffer_count"] = buffers
    result["post_repair_fanout_violations"] = int(remaining[0])
    result["native_default_fanout_load"] = fanout_load if fanout_load is not None else 1
    if weighted:
        result["weighted_repair_scope"] = "observed native behavior; no all-net repair guarantee"
    result["policy"] = policy
    result["input_hashes"] = {
        name: hashlib.sha256((path / name).read_bytes()).hexdigest()
        for name in ("dut.v", "dut.sdc", "openroad_dut.v")
    }
    (path / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def diagnostic_case(
    path: Path,
    pdk: Path,
    *,
    sdc_rule: str = "",
    setting: int | None = None,
    library_limit: float | None = None,
    expected: str = "booley_default",
    expected_limit: float | None = None,
    rejected: bool = False,
    extra: str = "",
    timeout: float = 30,
    library_pin_limit: float | None = None,
    library_pin_cell: str = "INV_X1",
    fanout_load: float | None = None,
    netlist_text: str | None = None,
    boundary_only: bool = False,
    expected_pin: str | None = None,
) -> dict:
    """Run actual native provenance/guard code at its generated boundary."""
    script = generate(
        path,
        pdk,
        20,
        setting=setting,
        sdc_rule=sdc_rule,
        library_limit=library_limit,
        setup=False,
        library_pin_limit=library_pin_limit,
        library_pin_cell=library_pin_cell,
        fanout_load=fanout_load,
        netlist_text=netlist_text,
    )
    text = script.read_text(encoding="utf-8")
    if boundary_only:
        suffix = text.split("booley_fanout::checkpoint\n", 1)[1].split(
            'puts "BOOLEY_STAGE: repair_design"', 1
        )[0]
        text = (
            text.split('puts "BOOLEY_STAGE: floorplan"', 1)[0]
            + "booley_fanout::checkpoint\n"
            + suffix
        )
    text = text.replace("booley_fanout::checkpoint\n", extra + "\nbooley_fanout::checkpoint\n")
    text += '\nputs "DIAGNOSTIC_POST_FANOUT_VIOLATIONS: [sta::max_fanout_violation_count]"\nputs "DIAGNOSTIC_COMPLETE"\n'
    script.write_text(text, encoding="utf-8")
    result = diagnostic_run(script, timeout)
    return check_diagnostic(path, result, expected, expected_limit, rejected, expected_pin)


def diagnostic_run(script: Path, timeout: float) -> subprocess.CompletedProcess:
    """Keep all actual native diagnostic output, including deliberate failures."""
    log = script.parent / "native.log"
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            ["openroad", "-exit", str(script)],
            cwd=script.parent,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    return result


def check_refusal(path: Path, returncode: int, output: str) -> dict:
    """Assert unsupported native state never enters repair and complete evidence survives."""
    log = path / "native.log"
    if returncode == 0 or "unsupported exact fanout evidence" not in output:
        raise RuntimeError(f"native guard failed to reject {path.name}: {log}")
    if "BOOLEY_STAGE: repair_design" in output or "DIAGNOSTIC_COMPLETE" in output:
        raise RuntimeError("unsupported native state reached repair boundary")
    if path.name == "hidden_default_capacity61":
        private = (path / "reports/fanout/safety.rpt").read_text(encoding="utf-8")
        if (
            "CAPACITY_PROOF: 61 2" not in output
            or private.count("Pin ") != 20
            or private.count("(VIOLATED)") != 2
            or not private.endswith("BOOLEY_FANOUT_COMPLETE\n")
        ):
            raise RuntimeError("native complete61-pin/20-row/two-violation proof failed")
    return {"rejected_before_repair": True, "log": str(log)}


def check_diagnostic(
    path: Path,
    result: subprocess.CompletedProcess,
    expected: str,
    expected_limit: float | None,
    rejected: bool,
    expected_pin: str | None,
) -> dict:
    """Check actual native point, refusal boundary and required structure."""
    log = path / "native.log"
    output = log.read_text(encoding="utf-8")
    if rejected:
        return check_refusal(path, result.returncode, output)
    if result.returncode or "DIAGNOSTIC_COMPLETE" not in output:
        raise RuntimeError(f"native diagnostic failed {path.name}: {log}")
    policy = json.loads((path / "reports/fanout/policy.json").read_text(encoding="utf-8"))
    if expected == "no_applicable_driver":
        if policy["status"] != expected or policy["worst_check"] is not None:
            raise RuntimeError(f"unexpected native empty point: {policy}")
        return policy
    if expected_pin is not None and policy["worst_check"]["pin"] != expected_pin:
        raise RuntimeError(f"unexpected native selected pin: {policy}")
    if (
        policy["worst_check"]["source"] != expected
        or policy["worst_check"]["max_fanout"] != expected_limit
    ):
        raise RuntimeError(f"unexpected native diagnostic point: {policy}")
    if path.name.endswith("_exemption"):
        private = (path / "reports/fanout/safety.rpt").read_text(encoding="utf-8")
        exempt = "clk" if path.name == "clock_data_exemption" else "driver/ZN"
        if f"Pin {exempt}  " in private:
            raise RuntimeError("native fanout driver exemption was lost")
    if path.name in {"unused_scope_suppresses", "weighted_two"} and re.findall(
        r"DIAGNOSTIC_POST_FANOUT_VIOLATIONS: (\d+)", output
    ) != ["0"]:
        raise RuntimeError("native mixed/weighted arm retained fanout violations")
    if path.name == "huge_buffer_insertions":
        output_netlist = (path / "openroad_dut.v").read_text(encoding="utf-8")
        if len(re.findall(r"\bBUF_X\d+\s", output_netlist)) <= 1:
            raise RuntimeError("huge weighted buffer fixture did not insert repeaters")
    return policy


_DIAGNOSTIC_CASES = {
    "sdc100_5": {
        "sdc_rule": "set_max_fanout 100.5 [current_design]\n",
        "expected": "sdc",
        "expected_limit": 100.5,
    },
    "rounded_tie": {
        "sdc_rule": "set_max_fanout 26 [current_design]\n",
        "library_limit": 25.5,
        "expected": "library",
        "expected_limit": 25.5,
    },
    "exact_tie": {
        "sdc_rule": "set_max_fanout 25.5 [current_design]\n",
        "library_limit": 25.5,
        "expected": "sdc",
        "expected_limit": 25.5,
    },
    "nondyadic": {
        "sdc_rule": "set_max_fanout 20.1 [current_design]\n",
        "expected": "sdc",
        "expected_limit": 20.100000381469727,
    },
    "fractional_half": {
        "sdc_rule": "set_max_fanout 0.5 [current_design]\n",
        "expected": "sdc",
        "expected_limit": 0.5,
    },
    "float32": {
        "sdc_rule": "set_max_fanout 25.5000001 [current_design]\n",
        "expected": "sdc",
        "expected_limit": 25.5,
    },
    "setting_tie": {
        "setting": 50,
        "library_limit": 50,
        "expected": "setting",
        "expected_limit": 50,
    },
    "setting_float32": {
        "setting": 16777217,
        "expected": "setting",
        "expected_limit": 16777216.0,
    },
    "above_sentinel": {
        "setting": 10**21,
        "expected": "setting",
        "expected_limit": 1.0000000200408773e21,
    },
    "mode_noop": {"sdc_rule": "set_mode default\n"},
    "mode_transition": {"sdc_rule": "set_mode alpha\n", "rejected": True},
    "mode_caught": {"sdc_rule": "catch {set_mode alpha; error later}\n", "rejected": True},
    "mode_return": {"sdc_rule": "set_mode alpha; set_mode default\n", "rejected": True},
    "mode_alias": {
        "sdc_rule": "interp alias {} choose_mode {} sta::set_cmd_mode\nchoose_mode alpha\n",
        "rejected": True,
    },
    "clear_top": {
        "sdc_rule": "sta::clear_sta; create_clock -period 20 [get_ports clk]; set_max_fanout 20.1 [current_design]\n",
        "expected": "sdc",
        "expected_limit": 20.100000381469727,
    },
    "clear_scoped": {
        "sdc_rule": "sta::clear_sta; create_clock -period 20 [get_ports clk]; set_max_fanout 10.5 [sta::Instance_cell [get_cells driver]]\n",
        "expected": "sdc",
        "expected_limit": 10.5,
    },
    "clear_alias": {
        "sdc_rule": "interp alias {} reset_sta {} sta::clear_sta_corner_base\nreset_sta; create_clock -period 20 [get_ports clk]; set_max_fanout 10.5 [sta::Instance_cell [get_cells driver]]\n",
        "expected": "sdc",
        "expected_limit": 10.5,
    },
    "clear_nonok": {
        "sdc_rule": "catch {sta::clear_sta_corner_base invalid_argument}\n",
        "rejected": True,
    },
    "top_inf": {"sdc_rule": "set_max_fanout 1e30 [current_design]\n", "rejected": True},
    "non_sdc_network": {"sdc_rule": "sta::network_changed_non_sdc\n"},
}


def mixed_fixture(*, buffer_only: bool = False, inv_name: str = "a_inv", count: int = 20) -> str:
    """Retain distinct native identities and weighted sources in physical fixtures."""
    lines = [
        f"module dut(input clk, input reset_i, input data_i, output [{count - 1}:0] data_o);",
        "wire an,bn;",
    ]
    if not buffer_only:
        lines.append(f"INV_X1 {inv_name} (.A(reset_i), .ZN(an));")
    lines.append("BUF_X1 m_buf (.A(reset_i), .Z(bn));")
    for index in range(count):
        reset = "an" if index < 2 and not buffer_only else "bn"
        lines.append(
            f"DFFR_X1 load_{index} (.D(data_i), .CK(clk), .RN({reset}), .Q(data_o[{index}]));"
        )
    return "\n".join([*lines, "endmodule"]) + "\n"


_DIAGNOSTIC_CASES.update(
    {
        "authored_sentinel": {
            "sdc_rule": "set_max_fanout 1e20 [current_design]\n",
            "expected": "sdc",
            "expected_limit": 1.0000000200408773e20,
        },
        "scoped_sentinel": {
            "setting": 50,
            "sdc_rule": "set_max_fanout 1e20 [sta::Instance_cell [get_cells driver]]\n",
        },
        "library_sentinel": {"library_limit": 1e20},
        "scoped_inf_harmless": {
            "sdc_rule": "set_max_fanout 1e30 [sta::Instance_cell [get_cells driver]]\n"
        },
        "unused_scope_suppresses": {
            "setting": 50,
            "library_pin_limit": 25.5,
            "sdc_rule": "set_max_fanout 100 [sta::Instance_cell [get_cells load_0]]\n",
            "expected": "library",
            "expected_limit": 25.5,
            "expected_pin": "driver/ZN",
        },
        "weighted_two": {
            "fanout_load": 2,
            "sdc_rule": "set_max_fanout 50 [current_design]\n",
            "expected": "sdc",
            "expected_limit": 50,
            "expected_pin": "driver/ZN",
        },
        "native_nonok_fanout": {
            "sdc_rule": "catch {sta::set_cell_fanout_limit [current_design] max 20 extra}\n",
            "rejected": True,
        },
        "nondefault_mode": {"sdc_rule": "set_mode alpha\n", "rejected": True},
        "multimode": {"sdc_rule": "set_mode alpha; set_mode default\n", "rejected": True},
        "dynamic_alias_partial": {
            "setting": 50,
            "sdc_rule": "namespace eval fanout_input {namespace import ::sta::set_cell_fanout_limit; variable value 100.5}\ninterp alias {} set_native_fanout {} sta::set_cell_fanout_limit\ncatch {foreach object [list [sta::Instance_cell [sta::top_instance]]] {set_native_fanout $object max $::fanout_input::value}; error outer_failure}\n",
            "expected": "sdc",
            "expected_limit": 100.5,
        },
        "overwritten": {
            "sdc_rule": "foreach value {50 100.5} {set_max_fanout $value [current_design]}\n",
            "expected": "sdc",
            "expected_limit": 100.5,
        },
        "empty_objects": {
            "setting": 50,
            "sdc_rule": "set_max_fanout 100 {}\n",
            "expected": "setting",
            "expected_limit": 50,
        },
        "clear_caught_partial": {
            "setting": 50,
            "sdc_rule": "set_max_fanout 10 [current_design]\nset ::sta::corner_bundle_applied [dict create stale 1]\ncatch {sta::clear_sta; if {[dict size $::sta::corner_bundle_applied]} {error stale_corner}; create_clock -period 20 [get_ports clk]; set_max_fanout 100.5 [current_design]; error outer_failure}\n",
            "expected": "sdc",
            "expected_limit": 100.5,
        },
        "clear_import": {
            "sdc_rule": "namespace eval sta {namespace export clear_sta_corner_base}; namespace eval reset_input {namespace import ::sta::clear_sta_corner_base; proc run {} {uplevel 1 ::reset_input::clear_sta_corner_base}}\nreset_input::run; create_clock -period 20 [get_ports clk]; set_max_fanout 100.5 [current_design]\n",
            "expected": "sdc",
            "expected_limit": 100.5,
        },
        "generated_mutation": {
            "extra": "catch {set_max_fanout 50 [current_design]}",
            "rejected": True,
        },
        "lost_trace": {
            "extra": "trace remove execution sta::set_cell_fanout_limit leave [list booley_fanout::observe sta::set_cell_fanout_limit]",
            "rejected": True,
        },
        "missing_identity": {
            "extra": "rename booley_fanout::literal_pin booley_fanout::original_literal; proc booley_fanout::literal_pin {name} {original_literal absent_native_pin}",
            "rejected": True,
        },
        "getter_mismatch": {
            "extra": "rename sta::max_fanout_min_slack_limit original_native_limit; proc sta::max_fanout_min_slack_limit {} {return 25}",
            "rejected": True,
        },
        "getter_nonfinite": {
            "extra": "rename sta::max_fanout_min_slack_limit original_native_limit; proc sta::max_fanout_min_slack_limit {} {return 1e30}",
            "rejected": True,
        },
        "no_applicable": {
            "netlist_text": "module dut(input clk, input reset_i, input data_i, output data_o); assign data_o=1'b0; endmodule\n",
            "boundary_only": True,
            "sdc_rule": "set_disable_timing [get_ports {reset_i data_i}]\n",
            "expected": "no_applicable_driver",
        },
    }
)


for _name, _setting in (("hidden_default_unsafe", None), ("hidden_setting_unsafe", 10**21)):
    _DIAGNOSTIC_CASES[_name] = {
        "netlist_text": mixed_fixture(),
        "setting": _setting,
        "library_pin_cell": "BUF_X1",
        "library_pin_limit": 25.5,
        "fanout_load": 1e21,
        "sdc_rule": "set_disable_timing [get_ports {reset_i data_i}]; set_dont_touch [get_cells m_buf]\n",
        "rejected": True,
    }
_DIAGNOSTIC_CASES["huge_buffer_insertions"] = {
    "netlist_text": mixed_fixture(buffer_only=True, count=200),
    "extra": "unset_dont_touch [get_cells m_buf]",
    "library_pin_cell": "BUF_X1",
    "library_pin_limit": 25.5,
    "fanout_load": 1e21,
    "sdc_rule": "set_disable_timing [get_ports {reset_i data_i}]; set_dont_touch [get_cells m_buf]\n",
    "expected": "library",
    "expected_limit": 25.5,
    "expected_pin": "m_buf/Z",
}


_DIAGNOSTIC_CASES.update(
    {
        "driving_cell_then_sdc": {
            "netlist_text": mixed_fixture(buffer_only=True).replace(
                "BUF_X1 m_buf (.A(reset_i), .Z(bn));",
                "NAND2_X1 driver (.A1(reset_i), .A2(data_i), .ZN(bn));",
            ),
            "library_pin_limit": 25.5,
            "sdc_rule": "set_driving_cell -lib_cell INV_X1 -pin ZN [get_ports reset_i]; set_max_fanout 100 [get_ports reset_i]\n",
            "expected": "library",
            "expected_limit": 25.5,
            "expected_pin": "reset_i",
        },
        "sdc_then_driving_cell": {
            "netlist_text": mixed_fixture(buffer_only=True).replace(
                "BUF_X1 m_buf (.A(reset_i), .Z(bn));",
                "NAND2_X1 driver (.A1(reset_i), .A2(data_i), .ZN(bn));",
            ),
            "library_pin_limit": 25.5,
            "sdc_rule": "set_max_fanout 100 [get_ports reset_i]; set_driving_cell -lib_cell INV_X1 -pin ZN [get_ports reset_i]\n",
            "expected": "library",
            "expected_limit": 25.5,
            "expected_pin": "reset_i",
        },
        "multiple_scenes_one_mode": {
            "sdc_rule": "define_scene first -mode default -liberty NangateOpenCellLibrary; define_scene second -mode default -liberty NangateOpenCellLibrary\n"
        },
        "escaped_identity": {
            "library_pin_limit": 25.5,
            "netlist_text": mixed_fixture(buffer_only=True)
            .replace("BUF_X1 m_buf", "INV_X1 \\odd.driver[] ")
            .replace(".Z(bn)", ".ZN(bn)"),
            "sdc_rule": "set_disable_timing [get_ports {reset_i data_i}]\n",
            "expected": "library",
            "expected_limit": 25.5,
            "expected_pin": "odd.driver[]/ZN",
        },
    }
)


_DIAGNOSTIC_CASES.update(
    {
        "missing_native_leaf": {
            "extra": "rename sta::set_port_fanout_limit saved_native_port",
            "rejected": True,
        },
        "unexpected_native_type": {
            "extra": "rename sta::set_port_fanout_limit saved_native_port; proc sta::set_port_fanout_limit {args} {saved_native_port {*}$args}",
            "rejected": True,
        },
        "dynamic_sourced_file": {
            "setting": 50,
            "sdc_rule": "set f [file join [pwd] authored-source.sdc]; set stream [open $f w]; puts $stream {namespace eval authored_rule {variable limit 100.5; proc apply {} {set_max_fanout $::authored_rule::limit [current_design]}}; authored_rule::apply}; close $stream; source $f\n",
            "expected": "sdc",
            "expected_limit": 100.5,
        },
        "read_sdc_mode": {
            "sdc_rule": "set f [file join [pwd] alpha.sdc]; set stream [open $f w]; puts $stream {set_max_fanout 100 [current_design]}; close $stream; catch {read_sdc -mode alpha $f}\n",
            "rejected": True,
        },
    }
)


for _inv, _source, _pin, _limit in (
    ("a_inv", "sdc", "a_inv/ZN", 9.5),
    ("z_inv", "library", "m_buf/Z", 25.5),
):
    _DIAGNOSTIC_CASES[f"pinless_swap_{_inv}"] = {
        "netlist_text": mixed_fixture(inv_name=_inv),
        "library_pin_cell": "BUF_X1",
        "library_pin_limit": 25.5,
        "sdc_rule": "set_disable_timing [get_ports {reset_i data_i}]; set_dont_touch [get_cells m_buf]; set_max_fanout 9.5 [sta::Instance_cell [get_cells "
        + _inv
        + "]]\n",
        "extra": "unset_dont_touch [get_cells m_buf]",
        "expected": _source,
        "expected_pin": _pin,
        "expected_limit": _limit,
    }
for _name in ("library_sentinel", "authored_sentinel", "scoped_sentinel"):
    _DIAGNOSTIC_CASES[_name]["sdc_rule"] = (
        _DIAGNOSTIC_CASES[_name].get("sdc_rule", "")
        + "set_disable_timing [get_ports {reset_i data_i}]\n"
    )
    _DIAGNOSTIC_CASES[_name]["expected_pin"] = "driver/ZN"
_DIAGNOSTIC_CASES["authored_sentinel"]["library_limit"] = 1e20
_DIAGNOSTIC_CASES.update(
    {
        "integer_threshold_met": {
            "setting": 2147483648,
            "expected": "setting",
            "expected_limit": 2147483648,
        },
        "integer_below_threshold_violated": {
            "setting": 2147483520,
            "fanout_load": 2147483648,
            "expected": "setting",
            "expected_limit": 2147483520,
        },
        "integer_threshold_violated": {
            "setting": 2147483648,
            "fanout_load": 2147483648,
            "rejected": True,
        },
        "native_nonfinite": {
            "sdc_rule": "catch {sta::set_cell_fanout_limit [sta::Instance_cell [sta::top_instance]] max Inf}\n",
            "rejected": True,
        },
    }
)


_DIAGNOSTIC_CASES.update(
    {
        "constant_exemption": {
            "netlist_text": mixed_fixture(buffer_only=True).replace(
                "BUF_X1 m_buf (.A(reset_i), .Z(bn));", "INV_X1 driver (.A(1'b0), .ZN(bn));"
            )
        },
        "disabled_exemption": {"sdc_rule": "set_disable_timing [get_pins driver/ZN]\n"},
        "clock_data_exemption": {
            "netlist_text": mixed_fixture(buffer_only=True).replace(
                "BUF_X1 m_buf (.A(reset_i), .Z(bn));", "assign bn=clk;"
            )
        },
        "hidden_default_capacity61": {
            "netlist_text": mixed_fixture(count=9),
            "library_pin_cell": "BUF_X1",
            "library_pin_limit": 25.5,
            "fanout_load": 1e21,
            "sdc_rule": "set_disable_timing [get_ports {reset_i data_i}]; set_dont_touch [get_cells m_buf]\n",
            "rejected": True,
            "extra": 'report_check_types -max_fanout -verbose -max_count 1 > capacity-point.rpt\nputs "CAPACITY_PROOF: [booley_fanout::capacity] [sta::max_fanout_violation_count]"',
        },
    }
)

_DIAGNOSTIC_CASES["native_partial_alias"] = {
    "setting": 50,
    "sdc_rule": "interp alias {} authored_fanout {} sta::set_cell_fanout_limit [sta::Instance_cell [sta::top_instance]] max; catch {authored_fanout 100.5; error outer_failure}\n",
    "expected": "sdc",
    "expected_limit": 100.5,
}


_CACHE_CONTROLS = r"""
set compact [file join $::booley_fanout::report_dir cache-worst.rpt]
file mkdir $::booley_fanout::report_dir
report_check_types -max_fanout -verbose -max_count 1 > $compact
set first [booley_fanout::worst_row $compact]
if {![llength $first] || [sta::max_fanout_violation_count] != 0} {error "invalid cache control fixture"}
set first_text "Pin [lindex $first 0]  \n[join [lrange $first 1 4] \n]\n"
set body [join [lrange $first 1 4] \n]
set second_text "Pin cache_second  \n$body\n"
set third_text "Pin cache_third  \n$body\n"
set control [file join $::booley_fanout::report_dir cache-control.rpt]
proc cache_control {name text first capacity expect_error} {
  global control
  set stream [open $control w]
  puts -nonewline $stream $text
  close $stream
  set code [catch {booley_fanout::scan $control $capacity $first 1} message]
  if {($code != 0) != $expect_error} {error "cache control $name failed: $message"}
  puts "CACHE_CONTROL: $name passed"
}
set prefix "max fanout\n$first_text$second_text"
set end "BOOLEY_FANOUT_COMPLETE\n"
cache_control exact_hit "$prefix$third_text$end" $first 3 0
cache_control duplicate_hit "$prefix$second_text$end" $first 3 1
cache_control capacity_hit "$prefix$third_text$end" $first 2 1
cache_control malformed_status "$prefix[string map {MET INVALID} $third_text]$end" $first 3 1
cache_control changed_numeric "$prefix[string map {fanout {fanout broken}} $third_text]$end" $first 3 1
cache_control misplaced_header "${prefix}max fanout\n$third_text$end" $first 3 1
cache_control truncated_hit "$prefix$third_text" $first 3 1
cache_control truncated_row "${prefix}Pin cache_third  \nmax fanout 20\n$end" $first 3 1
cache_control trailing "$prefix$third_text${end}unparsed\n" $first 3 1
set sentinel 100000002004087734272
set unsafe "Pin cache_third  \nmax fanout $sentinel\nfanout $sentinel\n-----\nSlack 0 (MET)\n"
cache_control sentinel_bound "$prefix$unsafe$end" $first 3 1
set unsafe "Pin cache_third  \nmax fanout 2147483648\nfanout 2147483649\n-----\nSlack -1 (VIOLATED)\n"
cache_control unsafe_limit "$prefix$unsafe$end" $first 3 1
file delete $control $compact
"""


def diagnostics(path: Path, pdk: Path, *, shared: bool = False, budget: float = 30) -> dict:
    """Required fractional, reset, ties and persistent caught context guards."""
    cases = _DIAGNOSTIC_CASES
    if shared:
        cases = {name: cases[name] for name in ("sdc100_5", "mode_transition")}
    started = time.monotonic()
    results = {}
    for name, options in cases.items():
        remaining = budget - (time.monotonic() - started) if shared else 30
        if remaining <= 0:
            raise RuntimeError("shared small-arm fanout diagnostic budget exhausted")
        results[name] = diagnostic_case(path / name, pdk, timeout=remaining, **options)
    if shared:
        return results
    results["cache_controls"] = diagnostic_case(
        path / "cache_controls", pdk, extra=_CACHE_CONTROLS
    )
    output = (path / "cache_controls/native.log").read_text(encoding="utf-8")
    if output.count("CACHE_CONTROL:") != 11:
        raise RuntimeError("native parser cache controls incomplete")
    from booley.flows.synth.backends.openroad.fanout import _BLOCKED

    guard = r"""
set initial [sta::max_fanout_min_slack_limit]
set n 0
foreach leaf {@BLOCKED@} {
  incr n
  interp alias {} guarded_alias$n {} $leaf
  namespace eval [namespace qualifiers $leaf] [list namespace export [namespace tail $leaf]]
  namespace eval imported$n [list namespace import ::$leaf]
  foreach call [list $leaf guarded_alias$n imported${n}::[namespace tail $leaf]] {
    set ::booley_fanout::unsupported 0
    set code [catch {$call} message]
    if {$code != 1 || !$::booley_fanout::unsupported ||
        [sta::max_fanout_min_slack_limit] != $initial} {error "context ENTER guard failed $call"}
    puts "ATTEMPT_FLAG_PER_CALL $call $::booley_fanout::unsupported"
  }
}
puts "GUARDED_LEAVES $n CALLS [expr {$n*3}]"
"""
    results["context108"] = diagnostic_case(
        path / "context108", pdk, rejected=True, extra=guard.replace("@BLOCKED@", _BLOCKED)
    )
    output = (path / "context108/native.log").read_text(encoding="utf-8")
    if output.count("ATTEMPT_FLAG_PER_CALL") != 108:
        raise RuntimeError("native context closure did not exercise all108 routes")
    return results


def independent_fixture(path: Path, count: int) -> None:
    """Generate actual independent native drivers for cost measurement."""
    lines = [
        "module dut(input clk, input reset_i, input data_i, output data_o);",
        f"wire [{count - 1}:0] n;",
        f"wire [{count}:0] q;",
        "assign q[0]=data_i;",
        f"assign data_o=q[{count}];",
    ]
    for index in range(count):
        lines.extend(
            [
                f"INV_X1 driver_{index} (.A(reset_i), .ZN(n[{index}]));",
                f"DFFR_X1 load_{index} (.D(q[{index}]), .CK(clk), .RN(n[{index}]), .Q(q[{index + 1}]));",
            ]
        )
    (path / "dut.v").write_text("\n".join([*lines, "endmodule"]) + "\n", encoding="utf-8")


def benchmark_native(script: Path) -> int:
    """Bound the entire native process group and record this child's peak RSS."""
    rss = script.parent / "native-rss-kib.txt"
    with (script.parent / "native.log").open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            [BENCHMARK_TIMER, "-f", "%M", "-o", str(rss), "openroad", "-exit", str(script)],
            cwd=script.parent,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.communicate(timeout=BENCHMARK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    if process.returncode:
        raise RuntimeError(f"native diagnostic benchmark failed: {script.parent}")
    return int(rss.read_text(encoding="utf-8").strip())


def benchmark(path: Path, pdk: Path, count: int) -> dict:
    """Measure every native evidence pass on actual independent drivers."""
    script = generate(path, pdk, count, setting=None, sdc_rule="", library_limit=None, setup=False)
    independent_fixture(path, count)
    generated = script.read_text(encoding="utf-8")
    text = generated.split('puts "BOOLEY_STAGE: floorplan"', 1)[0]
    boundary_end = generated.split("booley_fanout::checkpoint\n", 1)[1].split(
        'puts "BOOLEY_STAGE: repair_design"', 1
    )[0]
    text += "\nset started [clock milliseconds]\nbooley_fanout::checkpoint\n" + boundary_end
    text += 'puts "DIAGNOSTIC_MS: [expr {[clock milliseconds]-$started}]"\n'
    script.write_text(text, encoding="utf-8")
    peak_rss = benchmark_native(script)
    output = (path / "native.log").read_text(encoding="utf-8")
    measured = re.findall(r"DIAGNOSTIC_MS: (\d+)", output)
    if len(measured) != 1:
        raise RuntimeError(f"native diagnostic benchmark failed: {path}")
    seconds = int(measured[0]) / 1000
    private_bytes = (path / "reports/fanout/safety.rpt").stat().st_size
    names = [f"driver_{index}/ZN" for index in range(count)]
    budget = max(64 * 1024 * 1024, 1024 * count + 4 * sum(len(name.encode()) for name in names))
    if private_bytes > budget:
        raise RuntimeError("private native fanout artifact exceeded driver/name budget")
    result = {
        "driver_count": count,
        "diagnostic_s": seconds,
        "private_bytes": private_bytes,
        "private_budget_bytes": budget,
        "peak_rss_kib": peak_rss,
        "public_bytes": sum(
            (path / "reports/fanout" / name).stat().st_size
            for name in ("policy.json", "worst.rpt")
        ),
    }
    (path / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if seconds > 10:
        raise RuntimeError(f"native diagnostic cost target failed: {result}")
    return result


def native_arguments() -> tuple[argparse.Namespace, str]:
    """Require the native binary and managed PDK; missing tools never skip."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--pdk-root", type=Path, default=Path("/opt/pdk"))
    parser.add_argument("--baseline-script", type=Path)
    parser.add_argument("--repair-timeout-s", type=float, default=15)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    executable = shutil.which("openroad")
    if executable is None:
        raise RuntimeError("required native OpenROAD executable unavailable")
    for name in (
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
        "nangate45/Nangate45_tech.lef",
        "nangate45/Nangate45_stdcell.lef",
        "nangate45/Nangate45.rc",
    ):
        if not (args.pdk_root / name).is_file():
            raise RuntimeError(f"required managed PDK input missing: {name}")
    return args, executable


def full_variants(path: Path, pdk: Path) -> dict:
    """Require full physical10k design/port/pin/load-unit variants."""
    results = {}
    for name, options in (
        (
            "design100",
            {
                "setting": 50,
                "sdc_rule": "set_max_fanout 100 [current_design]\n",
                "expected_limit": 100,
            },
        ),
        (
            "port100",
            {
                "setting": 50,
                "port_driver": True,
                "sdc_rule": "set_max_fanout 100 [get_ports reset_i]\n",
                "expected_limit": 100,
            },
        ),
        ("library_pin25", {"library_pin_limit": 25}),
        ("weighted_two50", {"setting": 50, "fanout_load": 2}),
    ):
        results[name] = run_case(path / name, pdk, 10000, **options)
    return results


def save_summary(path: Path, results: dict) -> None:
    """Persist complete acceptance results before announcing success."""
    (path / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("openroad.fanout: passed actual10k and setting/SDC/library native arms")


def main() -> None:
    """Run bounded shared CI or complete serial native acceptance."""
    args, executable = native_arguments()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "default": run_case(args.work_dir / "default", args.pdk_root, 10000, executable=executable)
    }
    if results["default"]["repair_s"] > args.repair_timeout_s:
        raise RuntimeError("actual10k repair exceeded the fixed15s acceptance bound")
    count = 10000 if args.standalone else 200
    for name, options in (
        ("setting50", {"setting": 50}),
        (
            "scoped100",
            {
                "setting": 50,
                "sdc_rule": "set_max_fanout 100 [sta::Instance_cell [get_cells driver]]\n",
                "expected_limit": 100,
            },
        ),
        ("library25", {"library_limit": 25}),
    ):
        results[name] = run_case(
            args.work_dir / name,
            args.pdk_root,
            count,
            timeout=120 if args.standalone else 30,
            executable=executable,
            **options,
        )
    diagnostic_budget = 30 - results["setting50"]["wall_s"]
    if args.standalone:
        results.update(full_variants(args.work_dir, args.pdk_root))
    results["diagnostics"] = diagnostics(
        args.work_dir / "diagnostics",
        args.pdk_root,
        shared=not args.standalone,
        budget=diagnostic_budget,
    )
    if args.standalone:
        results["benchmarks"] = {
            str(count): benchmark(args.work_dir / f"benchmark-{count}", args.pdk_root, count)
            for count in (10000, 100000)
        }
    if args.baseline_script:
        results["baseline"] = run_baseline(
            args.work_dir / "baseline", args.baseline_script, executable
        )
    save_summary(args.work_dir, results)


if __name__ == "__main__":
    main()
