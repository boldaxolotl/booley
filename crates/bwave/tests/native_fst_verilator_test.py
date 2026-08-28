#!/usr/bin/env python3
"""Native Verilator FST validation (FST migration plan, deferred check).

Proves bwave reads FST files written by a *foreign* writer — Verilator's
embedded GTKWave fstapi (zlib/fastlz-packed blocks, its own hierarchy and
alias emission) — not just files produced by bwave's own fst-writer.

Method: simulate the same testbench twice with Verilator (`--binary`),
once with `--trace-fst` (native FST, no VCD anywhere in the loop) and once
with `--trace` (VCD), convert the VCD with `bwave build`, then diff every
query command's output between the native store and the converted store.
Same simulator + same semantics on both runs, so any difference is a
writer/reader artifact by construction.

A separate authored C++ fixture compiles and runs ``VerilatedFstC`` with the
matching ``--trace-fst`` and ``VM_TRACE_FMT_FST`` options, then requires B-Wave
to list its counter. That locks the compiler/runtime-object/harness contract
that a prerecorded FST alone cannot exercise.

Normalization is shared with differential_fst.py (same accepted deltas:
x/z minimal forms, real float text, no-op transitions), plus one specific
to this comparison: var-type labels are collapsed, because Verilator's FST
writer declares SystemVerilog types (``logic``, ``parameter``) while its
VCD writer downgrades everything to ``wire`` — the two dumps genuinely
carry different type metadata and bwave reports each faithfully.

Exit codes: 0 = all identical, 1 = differences, 2 = environment missing.

Run on a host with Verilator (the Booley sandbox container qualifies):
    python3 native_fst_verilator_test.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from differential_fst import COMMANDS, normalize, run

HERE = Path(__file__).resolve().parent
RTL = HERE / "rtl_fixtures"

DESIGNS = ["counter_4bit", "fsm_traffic", "lfsr_8bit"]

# Var-type tokens as rendered by `bwave list` (see var_type_str in fst.rs).
VAR_TYPE_RE = re.compile(
    r"\b(wire|reg|logic|bit|integer|parameter|real|realtime|time|int|"
    r"supply[01]|tri(?:and|or|reg|[01])?|wand|wor|event|port)\b"
)


def collapse_var_types(text: str) -> str:
    """See module docstring: Verilator FST/VCD type metadata differs."""
    return VAR_TYPE_RE.sub("<type>", text)


def find_bwave() -> str | None:
    configured = os.environ.get("BOOLEY_BWAVE_BIN")
    if configured and Path(configured).is_file():
        return configured
    for profile in ("release", "debug"):
        cand = (
            HERE.parent
            / "target"
            / profile
            / ("bwave.exe" if sys.platform == "win32" else "bwave")
        )
        if cand.exists():
            return str(cand)
    installed = Path("/usr/local/libexec/booley/bwave")
    if installed.is_file():
        return str(installed)
    return None


def verilate_and_run(design: str, trace_flag: str, workdir: Path) -> Path:
    """Build tb with Verilator --binary and run it; return the dump path.

    The testbenches name their dump 'trace.vcd'; with --trace-fst Verilator
    writes FST *content* to that name, so the caller renames it.
    """
    tb = f"{design}_tb"
    workdir.mkdir(parents=True, exist_ok=True)
    build = subprocess.run(
        [
            "verilator",
            "--binary",
            trace_flag,
            "--timing",
            "-Wno-fatal",
            "--top-module",
            tb,
            "--Mdir",
            "obj_dir",
            str(RTL / f"{design}.v"),
            str(RTL / f"{tb}.v"),
        ],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=300,
        check=False,
    )
    if build.returncode != 0:
        raise RuntimeError(
            f"verilator --binary {trace_flag} failed for {design}:\n{build.stdout}\n{build.stderr}"
        )
    sim = subprocess.run(
        [str(workdir / "obj_dir" / f"V{tb}")],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
        check=False,
    )
    if sim.returncode != 0:
        raise RuntimeError(
            f"simulation failed for {design} ({trace_flag}):\n{sim.stdout}\n{sim.stderr}"
        )
    dump = workdir / "trace.vcd"
    if not dump.exists():
        raise RuntimeError(f"no dump produced for {design} ({trace_flag})")
    return dump


def build_and_run_authored_fst_main(workdir: Path) -> Path:
    """Compile a real VerilatedFstC harness and return its native FST output."""
    top = "authored_fst_top"
    workdir.mkdir(parents=True, exist_ok=True)
    build = subprocess.run(
        [
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "--trace-fst",
            "--timing",
            "-Wno-fatal",
            "--top-module",
            top,
            "--Mdir",
            "obj_dir",
            "-CFLAGS",
            "-DVM_TRACE_FMT_FST",
            str(RTL / f"{top}.sv"),
            str(RTL / "authored_fst_main.cpp"),
        ],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=300,
        check=False,
    )
    if build.returncode != 0:
        raise RuntimeError(f"authored VerilatedFstC build failed:\n{build.stdout}\n{build.stderr}")
    store = workdir / "authored.fst"
    sim = subprocess.run(
        [str(workdir / "obj_dir" / f"V{top}"), str(store)],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
        check=False,
    )
    if sim.returncode != 0 or "[SIM_RESULT] PASSED" not in sim.stdout:
        raise RuntimeError(f"authored VerilatedFstC run failed:\n{sim.stdout}\n{sim.stderr}")
    if not store.is_file() or store.stat().st_size == 0:
        raise RuntimeError("authored VerilatedFstC run produced no nonempty FST")
    return store


def validate_authored_fst_main(exe: str, workdir: Path) -> bool:
    """Build, run, and query the authored C++ native-FST harness."""
    store = build_and_run_authored_fst_main(workdir)
    stdout, stderr, returncode = run(exe, ["list"], str(store))
    if returncode != 0 or "count[3:0]" not in stdout:
        print(f"FAIL authored VerilatedFstC harness: rc={returncode}\n{stdout}\n{stderr}")
        return False
    print(f"{'authored VerilatedFstC harness':<40} OK")
    return True


def main() -> int:
    exe = find_bwave()
    if exe is None:
        print("SKIP: bwave executable not found (cargo build first)")
        return 2
    if shutil.which("verilator") is None:
        print("SKIP: verilator not on PATH")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="bwave_native_fst_"))
    failures = 0 if validate_authored_fst_main(exe, tmp / "authored_fst_main") else 1
    total = 1
    for design in DESIGNS:
        native_dump = verilate_and_run(design, "--trace-fst", tmp / f"{design}_fst")
        native_store = native_dump.with_name("native.fst")
        native_dump.rename(native_store)

        vcd = verilate_and_run(design, "--trace", tmp / f"{design}_vcd")
        conv_store = tmp / f"{design}_vcd" / "converted.fst"
        p = subprocess.run(
            [exe, "build", str(vcd), "-o", str(conv_store)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if p.returncode != 0:
            print(f"FAIL {design}: bwave build rc={p.returncode}\n{p.stderr}")
            failures += 1
            continue

        design_fail = 0
        for cmd in COMMANDS:
            total += 1
            so_n, _, rc_n = run(exe, cmd, str(native_store))
            so_c, _, rc_c = run(exe, cmd, str(conv_store))
            nn = collapse_var_types(normalize(so_n, str(native_store)))
            nc = collapse_var_types(normalize(so_c, str(conv_store)))
            if nn != nc or rc_n != rc_c:
                design_fail += 1
                failures += 1
                print(f"DIFF {design}: {' '.join(cmd)} (rc {rc_n} vs {rc_c})")
                for i, (ln, lc) in enumerate(zip(nn.splitlines(), nc.splitlines(), strict=False)):
                    if ln != lc:
                        print(f"    line {i}: native={ln!r}")
                        print(f"    line {i}:   conv={lc!r}")
                        break
                else:
                    print(f"    line-count {len(nn.splitlines())} vs {len(nc.splitlines())}")
        status = "OK" if design_fail == 0 else f"{design_fail} DIFFS"
        print(f"{design:<40} {status}")

    print(f"\n{total} command pairs, {failures} failures")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
