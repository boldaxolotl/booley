"""Bounded real-Flow cache measurement with a separately supplied public PicoRV32.

Fetch picorv32.v at ef203c2b0a3fb793280f5114941416c425c5b461 from
https://github.com/YosysHQ/picorv32 (ISC license). No network is used here.
Run within an issued Sandbox; keep each repetition's Project/cache isolated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import yaml

from booley.flows.base import SubprocessResult
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.flows.sim.verilator_coverage_execution import collect_target_coverage
from booley.targets.catalog import TargetCatalog


def write_design(
    root: Path,
    source: Path,
    revision: int,
    harness: str,
    *,
    max_size: str = "5G",
    disabled: bool = False,
    debug: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    data = root / ".booley_project"
    data.mkdir(exist_ok=True)
    (data / "booley.toml").write_text(
        '[project]\nname="public-cache-acceptance"\n[flows.sim]\ntrace_files=["trace.vcd"]\n'
    )
    with (data / "booley.toml").open("a") as stream:
        stream.write(
            f'\n[flows.sim.compiler_cache]\nmax_size="{max_size}"\nenabled={str(not disabled).lower()}\n'
        )
    original = source.read_text()
    needle = "alu_out = reg_op1 ^ reg_op2;"
    assert original.count(needle) == 1
    rtl = original.replace(needle, f"alu_out = reg_op1 ^ reg_op2 ^ 32'd{revision};")
    (root / "picorv32.v").write_text(rtl)
    (root / "top.sv").write_text(_testbench())
    (root / "test_cpu.py").write_text("""import os
import cocotb
from cocotb.triggers import Timer
@cocotb.test()
async def check(dut):
    await Timer(2000, unit="ns")
    assert int(dut.observed.value) == (5 ^ int(os.environ["EXPECTED_REVISION"]))
""")
    core = _core(harness)
    if debug:
        core["targets"]["sim"]["flow_options"]["make_options"].append("CXXFLAGS=-g")
    (root / "cpu.core").write_text("CAPI=2:\n" + yaml.safe_dump(core))
    (data / "tests.toml").write_text(f'''[sim]
tests=["check"]
[sim.env]
EXPECTED_REVISION="{revision}"
''')


def _testbench() -> str:
    return """module top;
reg clk=0; always #5 clk=~clk;
initial begin $dumpfile("trace.vcd"); $dumpvars(0, top); end
reg resetn=0; initial begin repeat(5) @(negedge clk); resetn=1; end
wire valid, instr, trap; wire [31:0] addr, wdata; wire [3:0] wstrb;
reg [31:0] rdata; reg [31:0] observed=32'hffffffff;
picorv32 #(.PROGADDR_RESET(0), .ENABLE_IRQ(0)) cpu(
.clk(clk), .resetn(resetn), .trap(trap), .mem_valid(valid), .mem_instr(instr),
.mem_ready(valid), .mem_addr(addr), .mem_wdata(wdata), .mem_wstrb(wstrb), .mem_rdata(rdata));
always_comb case(addr)
0: rdata=32'h00600093; // addi x1, x0, 6
4: rdata=32'h00300113; // addi x2, x0, 3
8: rdata=32'h0020c1b3; // xor x3, x1, x2
12: rdata=32'h10302023; // sw x3, 256(x0)
default: rdata=32'h00100073; // ebreak
endcase
always @(posedge clk) if(valid && addr==256 && wstrb==15) begin
observed <= wdata;
$display("VALUE=%0d", wdata);
end
`ifndef COCOTB_SIM
integer revision;
initial begin
if (!$value$plusargs("REVISION=%d", revision)) revision=0;
#2000;
if(observed != (5 ^ revision)) $fatal(1, "incorrect CPU result");
$display("[SIM_RESULT] PASSED"); $finish;
end
`endif
endmodule
"""


def _core(harness: str) -> dict:
    options = {
        "tool": "verilator",
        "make_options": ["-j2"],
        "verilator_options": ["--timing", "-Wno-fatal", "--output-split", "2000"],
    }
    tb_files = [{"top.sv": {"file_type": "systemVerilogSource"}}]
    if harness == "cocotb":
        options["cocotb_module"] = "test_cpu"
        options["verilator_options"] += ["-DCOCOTB_SIM"]
        options["timescale"] = "1ns/1ps"
        tb_files.append({"test_cpu.py": {"file_type": "user", "copyto": "test_cpu.py"}})
    else:
        options["verilator_options"] += ["--main", "--exe"]
    return {
        "name": "booley:public:cpu_cache:1",
        "filesets": {
            "rtl": {"files": [{"picorv32.v": {"file_type": "verilogSource"}}]},
            "tb": {"files": tb_files, "tags": ["tb"]},
        },
        "targets": {
            "sim": {
                "flow": "sim",
                "filesets": ["rtl", "tb"],
                "toplevel": "top",
                "flow_options": options,
            }
        },
    }


def stats() -> dict[str, int]:
    result = subprocess.run(
        ["ccache", "--print-stats"], capture_output=True, text=True, timeout=10, check=True
    )
    return {
        parts[0]: int(parts[1])
        for line in result.stdout.splitlines()
        if len(parts := line.split()) == 2
    }


def instrument(root: Path) -> Path:
    """Measure generated C++ Make separately from setup, Verilation, and run."""
    tooling = root / ".booley_project" / ".runtime" / "cache-measurement" / uuid.uuid4().hex
    tooling.mkdir(parents=True)
    make = tooling / "make"
    make.write_bytes(Path(__file__).with_name("compiler_cache_make_probe.py").read_bytes())
    make.chmod(0o755)
    os.environ["PATH"] = str(tooling) + os.pathsep + os.environ["PATH"]
    log = tooling / "make.jsonl"
    os.environ["CACHE_ACCEPTANCE_MAKE_LOG"] = str(log)
    return log


def measure(args: argparse.Namespace) -> dict:
    write_design(
        args.project,
        args.rtl,
        args.revision,
        args.harness,
        max_size=args.max_size,
        disabled=args.disabled,
        debug=args.debug,
    )
    os.environ["EXPECTED_REVISION"] = str(args.revision)
    # Native plusargs select only the independent testbench expectation.
    data = args.project / ".booley_project"
    if args.harness != "cocotb":
        (data / "tests.toml").write_text(
            f'[sim]\ntests=["check"]\nselect="+REVISION={args.revision}"\n'
        )
    handle = TargetCatalog.build(args.project).select("sim", for_flow="sim")
    options = SimulationOptions(timeout_ms=10000, trace=args.trace)
    durations = []
    outputs = []

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        start = time.monotonic()
        result = subprocess.run(
            command,
            cwd=args.project,
            capture_output=True,
            text=True,
            timeout=min(timeout + 120, 300),
            check=False,
        )
        durations.append(time.monotonic() - start)
        outputs.append(result.stdout + result.stderr)
        return SubprocessResult(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    make_log = instrument(args.project)
    previous = set(args.project.glob("**/Vtop*.cpp"))
    before = stats()
    start = time.monotonic()
    _run_simulation(args, handle, options, invoke)
    elapsed = time.monotonic() - start
    return _record(args, elapsed, durations, outputs, before, make_log, previous)


def _run_simulation(args, handle, options, invoke) -> None:
    if args.coverage:
        result = collect_target_coverage(
            handle,
            selected_tests=("check",),
            artifact_root=args.project / "campaign",
            invoke=invoke,
            options=options,
        )
        assert result.status == "complete", str(result)
        assert all(test.simulation_verdict == "pass" for test in result.runs), str(result)
    else:
        result = SimulationExecution(invoke=invoke, options=options).run(
            handle, NamedTests(("check",))
        )
        assert result.passed, str(result)


def _record(args, elapsed, durations, outputs, before, make_log, previous) -> dict:
    after = stats()
    output = "\n".join(outputs)
    builds = [
        int(value) / 1000 for value in re.findall(r"BOOLEY_BUILD_MILLISECONDS: (\d+)", output)
    ]
    assert builds, output
    values = [int(value) for value in re.findall(r"VALUE=(\d+)", output)]
    assert values and all(value == (5 ^ args.revision) for value in values), output
    generated = {
        str(path.relative_to(args.project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in set(args.project.glob("**/Vtop*.cpp")) - previous
    }
    return {
        "make": [json.loads(line) for line in make_log.read_text().splitlines()],
        "source_sha256": hashlib.sha256(args.rtl.read_bytes()).hexdigest(),
        "revision": args.revision,
        "build_s": builds,
        "total_s": elapsed,
        "process_s": durations,
        "values": values,
        "stats_delta": {key: after[key] - before.get(key, 0) for key in after},
        "generated": generated,
        "compiler_commands": [
            line
            for line in output.splitlines()
            if re.match(r"(?:ccache )?(?:g\+\+|clang\+\+) ", line)
        ],
        "harness": args.harness,
        "trace": args.trace,
        "coverage": args.coverage,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtl", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--revision", type=int, default=0)
    parser.add_argument("--harness", choices=("generated", "cocotb"), default="generated")
    parser.add_argument("--max-size", default="5G")
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    print(json.dumps(measure(args)))


if __name__ == "__main__":
    main()
