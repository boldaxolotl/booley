#!/usr/bin/env python3
"""Create compressed real-world VCD fixtures for B-wave tests.

The committed fixture is produced from an externally supplied cache-controller
RTL file, with this deterministic testbench providing repeatable traffic. Raw
VCDs are intentionally not checked in; run this script with the RTL path to
rebuild the compressed fixture and manifest after the source or stimulus changes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ICARUS_FIXTURE_NAME = "cache_controller_small"
VERILATOR_FIXTURE_NAME = "cache_controller_verilator_small"
ITERATIONS = 8500

HERE = Path(__file__).resolve().parent


TESTBENCH_TEMPLATE = """\
`timescale 1ns/1ps

module cache_controller_real_trace_tb;
  localparam int ITERATIONS = __ITERATIONS__;
  localparam int NUM_LINES = 32;

  logic        clk;
  logic        reset;
  logic [4:0]  address;
  logic [31:0] write_data;
  logic        read;
  logic        write;
  wire [31:0]  read_data;
  wire         hit;
  wire         miss;
  wire         mem_write;
  wire [31:0]  mem_address;
  wire [31:0]  mem_write_data;
  logic [31:0] mem_read_data;
  logic        mem_ready;

  int cycle_count;
  int read_count;
  int write_count;
  int reset_count;
  logic [31:0] lfsr;

  cache_controller dut (
    .clk(clk),
    .reset(reset),
    .address(address),
    .write_data(write_data),
    .read(read),
    .write(write),
    .read_data(read_data),
    .hit(hit),
    .miss(miss),
    .mem_write(mem_write),
    .mem_address(mem_address),
    .mem_write_data(mem_write_data),
    .mem_read_data(mem_read_data),
    .mem_ready(mem_ready)
  );

  initial clk = 1'b0;
  always #5 clk = ~clk;

  always @(posedge clk) begin
    cycle_count <= cycle_count + 1;
  end

  function automatic logic [31:0] pattern_data(input logic [4:0] addr, input int salt);
    pattern_data = {27'b0, addr} ^ (32'h9E37_79B9 * (salt + 1));
  endfunction

  function automatic logic [31:0] next_lfsr(input logic [31:0] cur);
    next_lfsr = {cur[30:0], cur[31] ^ cur[21] ^ cur[1] ^ cur[0]};
  endfunction

  task automatic drive_idle(input int cycles);
    begin
      for (int i = 0; i < cycles; i++) begin
        @(negedge clk);
        read <= 1'b0;
        write <= 1'b0;
        mem_ready <= 1'b0;
        address <= '0;
        write_data <= '0;
        mem_read_data <= '0;
      end
    end
  endtask

  task automatic apply_reset(input int cycles);
    begin
      reset_count++;
      @(negedge clk);
      reset <= 1'b1;
      read <= 1'b0;
      write <= 1'b0;
      mem_ready <= 1'b0;
      address <= '0;
      write_data <= '0;
      mem_read_data <= '0;
      repeat (cycles) @(negedge clk);
      reset <= 1'b0;
      drive_idle(2);
    end
  endtask

  task automatic issue_read(input logic [4:0] addr, input int latency, input int salt);
    begin
      read_count++;
      @(negedge clk);
      address <= addr;
      write_data <= '0;
      read <= 1'b1;
      write <= 1'b0;
      mem_read_data <= pattern_data(addr, salt);
      mem_ready <= (latency == 0);
      @(negedge clk);
      for (int wait_cycle = 1; wait_cycle <= latency; wait_cycle++) begin
        address <= addr;
        read <= 1'b1;
        write <= 1'b0;
        mem_read_data <= pattern_data(addr, salt);
        mem_ready <= (wait_cycle == latency);
        @(negedge clk);
      end
      read <= 1'b0;
      mem_ready <= 1'b0;
      drive_idle(1);
    end
  endtask

  task automatic issue_write(input logic [4:0] addr, input logic [31:0] data);
    begin
      write_count++;
      @(negedge clk);
      address <= addr;
      write_data <= data;
      read <= 1'b0;
      write <= 1'b1;
      mem_read_data <= pattern_data(addr, write_count);
      mem_ready <= 1'b0;
      @(negedge clk);
      write <= 1'b0;
      drive_idle(1);
    end
  endtask

  initial begin
    $dumpfile("__RAW_VCD_NAME__");
    $dumpvars(0, cache_controller_real_trace_tb);

    cycle_count = 0;
    read_count = 0;
    write_count = 0;
    reset_count = 0;
    lfsr = 32'hACE1_0001;
    reset = 1'b0;
    read = 1'b0;
    write = 1'b0;
    address = '0;
    write_data = '0;
    mem_ready = 1'b0;
    mem_read_data = '0;

    apply_reset(4);

    for (int iter = 0; iter < ITERATIONS; iter++) begin
      logic [4:0] addr;
      logic [31:0] data;
      int latency;

      lfsr = next_lfsr(lfsr);
      addr = lfsr[4:0] ^ iter[4:0];
      data = pattern_data(addr, iter) ^ lfsr;
      latency = int'(lfsr[7:5] % 4);

      if ((iter % 257) == 0) begin
        apply_reset(2 + int'(lfsr[9:8]));
      end else if ((iter % 5) == 0) begin
        issue_write(addr, data);
      end else begin
        issue_read(addr, latency, iter);
      end

      if ((iter % 17) == 0) begin
        issue_read(addr, 0, iter + 1);
      end
    end

    drive_idle(8);
    $display("[SIM_RESULT] PASSED reads=%0d writes=%0d resets=%0d cycles=%0d",
             read_count, write_count, reset_count, cycle_count);
    $finish;
  end
endmodule
"""


def _testbench(raw_vcd_name: str) -> str:
    return TESTBENCH_TEMPLATE.replace("__ITERATIONS__", str(ITERATIONS)).replace(
        "__RAW_VCD_NAME__", raw_vcd_name
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _tool_env() -> dict[str, str]:
    env = os.environ.copy()
    if sys.platform == "win32":
        oss_root = Path(env.get("OSS_CAD_SUITE", r"C:\oss-cad-suite"))
        env["PATH"] = os.pathsep.join(
            [
                str(oss_root / "bin"),
                str(oss_root / "lib"),
                env.get("PATH", ""),
            ]
        )
        env.setdefault("VERILATOR_ROOT", str(oss_root / "share" / "verilator"))
        env.setdefault("SYSTEMROOT", r"C:\WINDOWS")
    return env


def _count_vcd(raw_vcd: Path) -> tuple[int, int]:
    signal_count = 0
    transition_count = 0
    with raw_vcd.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            stripped = line.strip()
            if stripped.startswith("$var "):
                signal_count += 1
            elif stripped and stripped[0] in "01xzXZbBrR":
                transition_count += 1
    return signal_count, transition_count


def _normalize_vcd(raw_vcd: Path) -> None:
    text = raw_vcd.read_text(encoding="utf-8", errors="replace")
    text = re.sub(
        r"\$date\b.*?\$end",
        "$date\n    2026-06-03T00:00:00Z\n$end",
        text,
        count=1,
        flags=re.DOTALL,
    )
    raw_vcd.write_text(text, encoding="utf-8", newline="\n")


def _write_gzip(raw_vcd: Path, compressed: Path) -> None:
    with (
        raw_vcd.open("rb") as src,
        compressed.open("wb") as dst,
        gzip.GzipFile(filename="", mode="wb", fileobj=dst, compresslevel=9, mtime=0) as gz,
    ):
        shutil.copyfileobj(src, gz)


def _manifest_entry(
    *,
    name: str,
    raw_vcd: Path,
    compressed: Path,
    signal_count: int,
    transition_count: int,
    simulator: str,
    simulator_command: str,
) -> str:
    return f"""\
[[fixture]]
name = "{name}"
format = "vcd.gz"
source_design = "external cache_controller.sv supplied to generate.py"
top_module = "cache_controller_real_trace_tb"
testbench = "created by generate.py"
simulator = "{simulator}"
simulator_command = "{simulator_command}"
stimulus = "deterministic LFSR traffic, {ITERATIONS} operations, periodic resets"
raw_vcd = "{raw_vcd.name}"
compressed = "{compressed.name}"
raw_bytes = {raw_vcd.stat().st_size}
compressed_bytes = {compressed.stat().st_size}
raw_sha256 = "{_sha256(raw_vcd)}"
compressed_sha256 = "{_sha256(compressed)}"
signal_count = {signal_count}
transition_count = {transition_count}
"""


def _write_manifest(entries: list[str]) -> None:
    (HERE / "MANIFEST.toml").write_text("\n".join(entries), encoding="utf-8", newline="\n")


def _generate_icarus(work: Path, env: dict[str, str], rtl_path: Path) -> str:
    iverilog = shutil.which("iverilog", path=_tool_env().get("PATH"))
    vvp = shutil.which("vvp", path=_tool_env().get("PATH"))
    if iverilog is None or vvp is None:
        raise RuntimeError("iverilog/vvp not found")

    name = ICARUS_FIXTURE_NAME
    raw_vcd_name = f"{name}.vcd"
    compressed = HERE / f"{raw_vcd_name}.gz"
    tb_path = work / "cache_controller_real_trace_tb.sv"
    rtl_copy = work / "cache_controller.sv"
    sim_path = work / "sim.vvp"
    raw_vcd = work / raw_vcd_name

    tb_path.write_text(_testbench(raw_vcd_name), encoding="utf-8", newline="\n")
    shutil.copy2(rtl_path, rtl_copy)

    _run(
        [
            iverilog,
            "-g2012",
            "-DBWAVE_REAL_TRACE_FIXTURE",
            "-o",
            str(sim_path),
            str(rtl_copy),
            str(tb_path),
        ],
        work,
        env,
    )
    _run([vvp, str(sim_path)], work, env)

    if not raw_vcd.exists():
        raise RuntimeError(f"Icarus simulation completed but did not create {raw_vcd}")

    _normalize_vcd(raw_vcd)
    _write_gzip(raw_vcd, compressed)
    signal_count, transition_count = _count_vcd(raw_vcd)
    _print_generated(compressed, raw_vcd, signal_count, transition_count)
    return _manifest_entry(
        name=name,
        raw_vcd=raw_vcd,
        compressed=compressed,
        signal_count=signal_count,
        transition_count=transition_count,
        simulator="Icarus Verilog",
        simulator_command="iverilog -g2012 -DBWAVE_REAL_TRACE_FIXTURE -o sim.vvp cache_controller.sv cache_controller_real_trace_tb.sv && vvp sim.vvp",
    )


def _write_verilator_sim_main(path: Path, top_module: str) -> None:
    path.write_text(
        f"""\
#include "V{top_module}.h"
#include "verilated.h"

double sc_time_stamp() {{ return 0; }}

int main(int argc, char** argv) {{
    const std::unique_ptr<VerilatedContext> ctxp(new VerilatedContext);
    ctxp->commandArgs(argc, argv);
    ctxp->traceEverOn(true);
    const std::unique_ptr<V{top_module}> top(new V{top_module}(ctxp.get()));
    while (!ctxp->gotFinish()) {{
        top->eval();
        ctxp->timeInc(1);
    }}
    top->final();
    return 0;
}}
""",
        encoding="utf-8",
        newline="\n",
    )


def _verilator_env(env: dict[str, str]) -> dict[str, str]:
    if sys.platform != "win32":
        return env
    msys_ucrt = Path(env.get("MSYS64_UCRT", r"C:\msys64\ucrt64\bin"))
    return {**env, "PATH": os.pathsep.join([str(msys_ucrt), env["PATH"]])}


def _verilator_cflags(include_dir: Path) -> list[str]:
    return [
        "-Os",
        "-I.",
        f"-I{include_dir}",
        f"-I{include_dir / 'vltstd'}",
        "-DVERILATOR=1",
        "-DVM_COVERAGE=0",
        "-DVM_SC=0",
        "-DVM_TIMING=1",
        "-DVM_TRACE=1",
        "-DVM_TRACE_FST=0",
        "-DVM_TRACE_VCD=1",
        "-DVM_TRACE_SAIF=0",
        "-faligned-new",
        "-fcf-protection=none",
        "-fcoroutines",
    ]


def _run_verilator_frontend(
    *,
    verilator_bin: str,
    top_module: str,
    rtl_copy: Path,
    tb_path: Path,
    sim_main: Path,
    work: Path,
    env: dict[str, str],
) -> None:
    _run(
        [
            verilator_bin,
            "--cc",
            "--exe",
            "--trace",
            "--timing",
            "-Wno-fatal",
            "-Wno-INITIALDLY",
            "--top-module",
            top_module,
            "--Mdir",
            "obj_vl",
            str(rtl_copy),
            str(tb_path),
            str(sim_main),
        ],
        work,
        env,
    )


def _compile_verilator_runtime(
    *, gxx: str, cflags: list[str], include_dir: Path, obj_dir: Path, env: dict[str, str]
) -> None:
    for src in ["verilated", "verilated_vcd_c", "verilated_timing", "verilated_threads"]:
        _run([gxx, *cflags, "-c", "-o", f"{src}.o", str(include_dir / f"{src}.cpp")], obj_dir, env)


def _compile_verilator_model(
    *,
    gxx: str,
    cflags: list[str],
    verilator_root: Path,
    top_module: str,
    sim_main: Path,
    obj_dir: Path,
    env: dict[str, str],
) -> None:
    includer = verilator_root / "bin" / "verilator_includer"
    cpp_files = [path.name for path in obj_dir.glob(f"V{top_module}*.cpp")]
    included = subprocess.run(
        [sys.executable, str(includer), "-DVL_INCLUDE_OPT=include", *cpp_files],
        cwd=obj_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if included.returncode != 0:
        raise RuntimeError(f"verilator_includer failed:\n{included.stderr}")
    all_cpp = obj_dir / f"V{top_module}__ALL.cpp"
    all_cpp.write_text(included.stdout, encoding="utf-8", newline="\n")

    _run([gxx, *cflags, "-c", "-o", f"V{top_module}__ALL.o", all_cpp.name], obj_dir, env)
    _run([gxx, *cflags, "-c", "-o", "sim_main.o", str(sim_main)], obj_dir, env)


def _link_verilator_model(
    *, gxx: str, cflags: list[str], top_module: str, obj_dir: Path, env: dict[str, str]
) -> Path:
    exe_name = f"V{top_module}{'.exe' if sys.platform == 'win32' else ''}"
    objs = [
        "verilated.o",
        "verilated_vcd_c.o",
        "verilated_timing.o",
        "verilated_threads.o",
        f"V{top_module}__ALL.o",
        "sim_main.o",
    ]
    _run([gxx, *objs, "-pthread", "-lpthread", "-latomic", "-o", exe_name], obj_dir, env)
    return obj_dir / exe_name


def _generate_verilator(work: Path, env: dict[str, str], rtl_path: Path) -> str:
    name = VERILATOR_FIXTURE_NAME
    raw_vcd_name = f"{name}.vcd"
    compressed = HERE / f"{raw_vcd_name}.gz"
    top_module = "cache_controller_real_trace_tb"
    obj_dir = work / "obj_vl"
    tb_path = work / f"{top_module}.sv"
    rtl_copy = work / "cache_controller.sv"
    sim_main = work / "sim_main.cpp"
    raw_vcd = work / raw_vcd_name

    env = _verilator_env(env)

    verilator_bin = shutil.which("verilator_bin", path=env["PATH"])
    gxx = shutil.which("g++", path=env["PATH"])
    if verilator_bin is None or gxx is None:
        raise RuntimeError("verilator_bin/g++ not found")

    tb_path.write_text(_testbench(raw_vcd_name), encoding="utf-8", newline="\n")
    shutil.copy2(rtl_path, rtl_copy)
    _write_verilator_sim_main(sim_main, top_module)

    _run_verilator_frontend(
        verilator_bin=verilator_bin,
        top_module=top_module,
        rtl_copy=rtl_copy,
        tb_path=tb_path,
        sim_main=sim_main,
        work=work,
        env=env,
    )

    verilator_root = Path(env["VERILATOR_ROOT"])
    include_dir = verilator_root / "include"
    cflags = _verilator_cflags(include_dir)
    _compile_verilator_runtime(
        gxx=gxx, cflags=cflags, include_dir=include_dir, obj_dir=obj_dir, env=env
    )
    _compile_verilator_model(
        gxx=gxx,
        cflags=cflags,
        verilator_root=verilator_root,
        top_module=top_module,
        sim_main=sim_main,
        obj_dir=obj_dir,
        env=env,
    )
    exe_path = _link_verilator_model(
        gxx=gxx, cflags=cflags, top_module=top_module, obj_dir=obj_dir, env=env
    )
    _run([str(exe_path)], work, env)

    if not raw_vcd.exists():
        raise RuntimeError(f"Verilator simulation completed but did not create {raw_vcd}")

    _normalize_vcd(raw_vcd)
    _write_gzip(raw_vcd, compressed)
    signal_count, transition_count = _count_vcd(raw_vcd)
    _print_generated(compressed, raw_vcd, signal_count, transition_count)
    return _manifest_entry(
        name=name,
        raw_vcd=raw_vcd,
        compressed=compressed,
        signal_count=signal_count,
        transition_count=transition_count,
        simulator="Verilator",
        simulator_command="verilator_bin --cc --exe --trace --timing -Wno-fatal -Wno-INITIALDLY --top-module cache_controller_real_trace_tb --Mdir obj_vl cache_controller.sv cache_controller_real_trace_tb.sv sim_main.cpp && g++ link && ./Vcache_controller_real_trace_tb",
    )


def _print_generated(
    compressed: Path, raw_vcd: Path, signal_count: int, transition_count: int
) -> None:
    print(
        textwrap.dedent(f"""\
        wrote {compressed}
          raw bytes:        {raw_vcd.stat().st_size}
          compressed bytes: {compressed.stat().st_size}
          signals:          {signal_count}
          transitions:      {transition_count}
    """).rstrip()
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rtl_path", type=Path, help="path to cache_controller.sv")
    return parser.parse_args()


def main() -> int:
    rtl_path = _parse_args().rtl_path.resolve()
    if not rtl_path.is_file():
        print(f"ERROR: cache-controller RTL not found: {rtl_path}", file=sys.stderr)
        return 1

    HERE.mkdir(parents=True, exist_ok=True)
    env = _tool_env()
    entries = []
    with tempfile.TemporaryDirectory(prefix="bwave_real_trace_icarus_") as temp:
        entries.append(_generate_icarus(Path(temp), env, rtl_path))
    with tempfile.TemporaryDirectory(prefix="bwave_real_trace_verilator_") as temp:
        entries.append(_generate_verilator(Path(temp), env, rtl_path))
    _write_manifest(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
