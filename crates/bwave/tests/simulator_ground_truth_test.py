"""
Simulator Ground Truth Test for bwave
======================================

Validates that the bwave Rust tool produces correct output by comparing
against a ground truth VCD oracle that directly parses raw VCD files.

Architecture:
  1. VcdOracle  — minimal, independent VCD parser (the reference implementation)
  2. Simulator runners — compile+simulate designs with Icarus/Verilator
  3. Test comparisons — structured diffs between oracle and bwave output

Each (design, simulator) pair is tested for:
  - list: signal names and widths
  - at-cycle: snapshot values at specific post-reset cycles
  - find: cycle numbers where a signal holds a given value
  - stats: transition counts per signal
  - wave: horizontal waveform table for a cycle range

Run with:  python -m pytest simulator_ground_truth_test.py -v
      or:  python simulator_ground_truth_test.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_VCD_PARSER_DIR = _THIS_DIR.parent
_RTL_FIXTURES = _THIS_DIR / "rtl_fixtures"
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_BWAVE_EXE = _VCD_PARSER_DIR / "target" / "debug" / f"bwave{_EXE_SUFFIX}"
_BWAVE_RELEASE = _VCD_PARSER_DIR / "target" / "release" / f"bwave{_EXE_SUFFIX}"

# Prefer the explicitly installed Session Runtime binary, then a release build,
# then the local debug build.
_BWAVE_CONFIGURED = os.environ.get("BOOLEY_BWAVE_BIN")
BWAVE_BIN = str(
    Path(_BWAVE_CONFIGURED)
    if _BWAVE_CONFIGURED
    else (_BWAVE_RELEASE if _BWAVE_RELEASE.exists() else _BWAVE_EXE)
)

# Simulator environment for Icarus (oss-cad-suite)
# Platform-aware: read tool roots from env vars with OS-appropriate defaults.
_OSS_CAD_ROOT = Path(
    os.environ.get(
        "OSS_CAD_SUITE",
        r"C:\oss-cad-suite" if sys.platform == "win32" else "/opt/oss-cad-suite",
    )
)
_PATH_SEP = ";" if sys.platform == "win32" else ":"

if sys.platform == "win32":
    ICARUS_ENV = {
        "PATH": _PATH_SEP.join(
            [
                str(_OSS_CAD_ROOT / "bin"),
                str(_OSS_CAD_ROOT / "lib"),
                os.environ.get("PATH", ""),
            ]
        ),
        "YOSYSHQ_ROOT": str(_OSS_CAD_ROOT) + os.sep,
        "TEMP": os.environ.get("TEMP", tempfile.gettempdir()),
        "TMP": os.environ.get("TMP", tempfile.gettempdir()),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\WINDOWS"),
        "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
    }
else:
    ICARUS_ENV = {
        "PATH": _PATH_SEP.join(
            [
                str(_OSS_CAD_ROOT / "bin"),
                str(_OSS_CAD_ROOT / "lib"),
                os.environ.get("PATH", ""),
            ]
        ),
        "HOME": os.environ.get("HOME", ""),
    }


# Verilator environment — inherit full OS env (g++ needs SYSTEMROOT, WINDIR, etc.)
# then override PATH/TEMP for our toolchain.
# Discover VERILATOR_ROOT from the binary location when oss-cad-suite isn't present
# (e.g. Docker images where verilator is installed to /usr/local).
def _find_verilator_root() -> str:
    oss_cad_root = _OSS_CAD_ROOT / "share" / "verilator"
    if oss_cad_root.is_dir():
        return str(oss_cad_root)
    vbin = shutil.which("verilator_bin")
    if vbin is not None:
        prefix = Path(vbin).resolve().parent.parent  # bin/../ -> prefix
        candidate = prefix / "share" / "verilator"
        if candidate.is_dir():
            return str(candidate)
    return str(oss_cad_root)


_VERILATOR_ROOT = _find_verilator_root()

if sys.platform == "win32":
    _MSYS64_UCRT = os.environ.get("MSYS64_UCRT", r"C:\msys64\ucrt64\bin")
    VERILATOR_ENV = {
        **os.environ,
        "PATH": _PATH_SEP.join(
            [
                str(_OSS_CAD_ROOT / "bin"),
                str(_OSS_CAD_ROOT / "lib"),
                _MSYS64_UCRT,
                os.environ.get("PATH", ""),
            ]
        ),
        "VERILATOR_ROOT": _VERILATOR_ROOT,
        "TEMP": os.environ.get("TEMP", tempfile.gettempdir()),
        "TMP": os.environ.get("TMP", tempfile.gettempdir()),
    }
else:
    VERILATOR_ENV = {
        **os.environ,
        "PATH": _PATH_SEP.join(
            [
                str(_OSS_CAD_ROOT / "bin"),
                str(_OSS_CAD_ROOT / "lib"),
                os.environ.get("PATH", ""),
            ]
        ),
        "VERILATOR_ROOT": _VERILATOR_ROOT,
    }

# Detect tool availability once at import time
_IVERILOG_AVAILABLE = shutil.which("iverilog", path=ICARUS_ENV["PATH"]) is not None
_VERILATOR_AVAILABLE = shutil.which("verilator_bin", path=ICARUS_ENV["PATH"]) is not None
_BWAVE_AVAILABLE = Path(BWAVE_BIN).exists()


# ═══════════════════════════════════════════════════════════════════════════
# Part 1: VCD Oracle Parser
# ═══════════════════════════════════════════════════════════════════════════


class VcdOracle:
    """Minimal, independent VCD parser for ground truth extraction.

    Parses a VCD file and provides methods to query signal metadata, values
    at specific times, transition lists, and rising edges.  Intentionally
    simple so correctness is obvious by inspection.

    Same-value rewrites are dropped at record time (see
    ``_record_transition``): the FST store keeps value changes only, so the
    oracle counts must use the same definition of "transition".
    """

    def __init__(self, vcd_path: str) -> None:
        self._signals: dict[str, int] = {}  # full_name -> bit_width
        self._id_to_names: dict[str, list[str]] = {}  # vcd_id -> [full_name, ...]
        self._id_to_width: dict[str, int] = {}  # vcd_id -> width
        self._transitions: dict[str, list[tuple[int, str]]] = {}  # vcd_id -> [(time, raw_value)]
        self._timestamps: list[int] = []
        self._events: list[tuple[int, str, str]] = []  # (time, vcd_id, raw_value) in body order
        self._current_time: int = 0
        self._timescale_ns: float = 1.0

        with Path(vcd_path).open(encoding="utf-8", errors="replace") as f:
            text = f.read()

        self._parse(text)

    # ── Public API ──────────────────────────────────────────────────────

    def signals(self) -> dict[str, int]:
        """Return {full_hierarchical_name: bit_width} for every $var."""
        return dict(self._signals)

    def value_at_time(self, signal_name: str, time: int) -> str:
        """Last known value of *signal_name* at or before *time*.

        Returns the raw binary/scalar string (e.g. "0101" or "1").
        """
        vcd_id = self._name_to_id(signal_name)
        if vcd_id is None:
            raise KeyError(f"Signal not found: {signal_name}")
        return self._value_at_time_by_id(vcd_id, time)

    def transitions(self, signal_name: str) -> list[tuple[int, str]]:
        """All (timestamp, raw_value) pairs for *signal_name*."""
        vcd_id = self._name_to_id(signal_name)
        if vcd_id is None:
            raise KeyError(f"Signal not found: {signal_name}")
        return list(self._transitions.get(vcd_id, []))

    def timestamps(self) -> list[int]:
        """All VCD timestamp markers in body order."""
        return list(self._timestamps)

    def rising_edges(self, clock_pattern: str) -> list[int]:
        """Timestamps where a signal matching *clock_pattern* goes 0->1."""
        import fnmatch

        matched_ids: list[str] = []
        for vcd_id, names in self._id_to_names.items():
            for name in names:
                if (
                    fnmatch.fnmatch(name.lower(), clock_pattern.lower())
                    and vcd_id not in matched_ids
                ):
                    matched_ids.append(vcd_id)

        edges: list[int] = []
        for vcd_id in matched_ids:
            prev = "x"
            for t, val in self._transitions.get(vcd_id, []):
                scalar = self._to_scalar(val)
                if prev == "0" and scalar == "1":
                    edges.append(t)
                prev = scalar
        edges.sort()
        return edges

    def signal_names(self) -> list[str]:
        """All full hierarchical signal names in declaration order."""
        return list(self._signals.keys())

    # ── Internal parsing ────────────────────────────────────────────────

    def _parse(self, text: str) -> None:
        # Strip $comment ... $end sections
        text = re.sub(r"\$comment\b.*?\$end", "", text, flags=re.DOTALL)

        # Split into header and body at $enddefinitions
        parts = re.split(r"\$enddefinitions\s+\$end", text, maxsplit=1)
        if len(parts) < 2:
            raise ValueError("No $enddefinitions found in VCD")
        header, body = parts[0], parts[1]

        self._parse_header(header)
        self._parse_body(body)

    def _parse_header(self, header: str) -> None:
        scope_stack: list[str] = []

        # Parse timescale
        ts_match = re.search(r"\$timescale\s+(.*?)\s*\$end", header, re.DOTALL)
        if ts_match:
            self._timescale_ns = self._parse_timescale(ts_match.group(1).strip())

        # Tokenize: find all $keyword ... $end blocks
        # Process sequentially to maintain scope context
        pos = 0
        while pos < len(header):
            m = re.search(r"\$(scope|upscope|var)\b", header[pos:])
            if m is None:
                break
            keyword = m.group(1)
            start = pos + m.start()

            # Find matching $end
            end_m = re.search(r"\$end\b", header[start + len(keyword) + 1 :])
            if end_m is None:
                break
            end_pos = start + len(keyword) + 1 + end_m.end()
            block = header[start:end_pos]

            if keyword == "scope":
                # $scope <type> <name> $end
                parts = block.split()
                if len(parts) >= 3:
                    scope_stack.append(parts[2])

            elif keyword == "upscope":
                if scope_stack:
                    scope_stack.pop()

            elif keyword == "var":
                # $var <type> <width> <id> <name> [range] $end
                # Remove the $end, then split
                inner = re.sub(r"\$end\s*$", "", block).strip()
                parts = inner.split()
                if len(parts) >= 5:
                    # parts[0] = "$var", parts[1] = type, parts[2] = width,
                    # parts[3] = id, parts[4] = name
                    width = int(parts[2])
                    vcd_id = parts[3]
                    leaf_name = parts[4]

                    full_name = ".".join([*scope_stack, leaf_name]) if scope_stack else leaf_name

                    self._signals[full_name] = width
                    self._id_to_names.setdefault(vcd_id, []).append(full_name)
                    self._id_to_width[vcd_id] = width

                    # Initialize transition list
                    if vcd_id not in self._transitions:
                        self._transitions[vcd_id] = []

            pos = end_pos

    def _parse_body(self, body: str) -> None:
        """Parse value-change dump body."""
        in_dumpvars = False
        in_dumpoff = False

        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Timestamp
            if stripped.startswith("#"):
                time_str = stripped[1:].strip()
                if time_str.isdigit():
                    self._current_time = int(time_str)
                    self._timestamps.append(self._current_time)
                continue

            # Section markers
            in_dumpvars, in_dumpoff = self._parse_body_line(
                stripped,
                in_dumpvars,
                in_dumpoff,
            )

    def _parse_body_line(
        self,
        line: str,
        in_dumpvars: bool,
        in_dumpoff: bool,
    ) -> tuple[bool, bool]:
        """Parse a single non-timestamp body line, return updated flags."""
        if line.startswith("$"):
            return self._handle_directive(line, in_dumpvars, in_dumpoff)
        if not in_dumpoff:
            self._parse_value_change(line)
        return in_dumpvars, in_dumpoff

    def _handle_directive(
        self,
        line: str,
        in_dumpvars: bool,
        in_dumpoff: bool,
    ) -> tuple[bool, bool]:
        """Process a VCD $ directive, return updated (in_dumpvars, in_dumpoff)."""
        if line.startswith("$dumpvars"):
            return True, in_dumpoff
        if line.startswith("$dumpoff"):
            return in_dumpvars, True
        if line.startswith("$dumpon"):
            return in_dumpvars, False
        if line.startswith("$end") and in_dumpvars:
            return False, in_dumpoff
        return in_dumpvars, in_dumpoff

    def _parse_value_change(self, line: str) -> None:
        """Parse a single value-change line and record the transition."""
        # Vector: b<bits> <id> / B<bits> <id>; Real: r<val> <id> / R<val> <id>
        if line[0] in "bBrR":
            parts = line.split()
            if len(parts) >= 2:
                self._record_transition(parts[1], parts[0][1:], is_real=line[0] in "rR")
        # Scalar: <value><id> where value is 0/1/x/z/X/Z
        elif line[0] in "01xzXZ" and len(line) >= 2:
            self._record_transition(line[1:], line[0])

    def _canon_value(self, vcd_id: str, value: str, is_real: bool) -> str:
        """Width-extended lowercase form, for same-value rewrite detection."""
        if is_real:
            try:
                return repr(float(value))
            except ValueError:
                return value.lower()
        v = value.lower()
        width = self._id_to_width.get(vcd_id, len(v))
        if len(v) < width:
            fill = v[0] if v and v[0] in "xz" else "0"
            v = fill * (width - len(v)) + v
        elif len(v) > width:
            v = v[-width:]
        return v

    def _record_transition(
        self,
        vcd_id: str,
        value: str,
        is_real: bool = False,
    ) -> None:
        if vcd_id not in self._transitions:
            return
        prior = self._transitions[vcd_id]
        # The FST store keeps value *changes*: same-value VCD rewrites
        # (combinational re-evals, $dumpon re-dumps) are dropped at write
        # time. The oracle mirrors that so transition counts are comparable.
        if prior and self._canon_value(
            vcd_id,
            prior[-1][1],
            is_real,
        ) == self._canon_value(vcd_id, value, is_real):
            return
        prior.append((self._current_time, value))
        self._events.append((self._current_time, vcd_id, value))

    def _value_at_time_by_id(self, vcd_id: str, time: int) -> str:
        """Binary search for last transition at or before *time*."""
        transitions = self._transitions.get(vcd_id, [])
        if not transitions:
            return "x"

        # Linear scan (transitions are in order)
        result = "x"
        for t, val in transitions:
            if t <= time:
                result = val
            else:
                break
        return result

    def _name_to_id(self, signal_name: str) -> str | None:
        for vcd_id, names in self._id_to_names.items():
            if signal_name in names:
                return vcd_id
        return None

    @staticmethod
    def _to_scalar(val: str) -> str:
        """Reduce a value to its scalar interpretation (for edge detection)."""
        if len(val) == 1:
            return val.lower()
        # Multi-bit: check if it's all-zeros or has a 1
        if all(c == "0" for c in val):
            return "0"
        if any(c == "1" for c in val):
            return "1"
        return "x"

    @staticmethod
    def _parse_timescale(ts: str) -> float:
        """Parse e.g. '1ns' -> 1.0, '10ns' -> 10.0, '1ps' -> 0.001."""
        m = re.match(r"(\d+)\s*(s|ms|us|ns|ps|fs)", ts)
        if not m:
            return 1.0
        factor = int(m.group(1))
        unit = m.group(2)
        unit_to_ns = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1.0, "ps": 1e-3, "fs": 1e-6}
        return factor * unit_to_ns.get(unit, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Part 2: Simulator Runners
# ═══════════════════════════════════════════════════════════════════════════


def run_icarus(design_v: str, tb_v: str, workdir: str) -> str:
    """Compile and simulate with Icarus Verilog, return VCD path."""
    vvp_path = str(Path(workdir) / "sim.vvp")
    vcd_path = str(Path(workdir) / "trace.vcd")

    # Compile
    result = subprocess.run(
        ["iverilog", "-o", vvp_path, design_v, tb_v],
        env=ICARUS_ENV,
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"iverilog compilation failed:\n{result.stderr}")

    # Simulate
    result = subprocess.run(
        ["vvp", vvp_path],
        env=ICARUS_ENV,
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 and "Finshed" not in result.stdout:
        # vvp may return non-zero on $finish; check VCD exists
        pass

    if not Path(vcd_path).exists():
        raise RuntimeError(
            f"VCD not generated. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return vcd_path


def _run_step(cmd: list[str], env: dict, cwd: str, label: str, timeout: int = 120) -> None:
    """Run a subprocess step, raise with details on failure."""
    result = subprocess.run(
        cmd, env=env, capture_output=True, text=True, cwd=cwd, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Verilator build failed at '{label}'.\n"
            f"cmd: {' '.join(cmd)}\nrc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def run_verilator(design_v: str, tb_v: str, workdir: str) -> str:
    """Compile and simulate with Verilator, return VCD path.

    All steps use Python subprocess directly (no bash). This avoids
    MSYS2/Git Bash env-passing issues with g++ on Windows.
    """
    work = Path(workdir)
    vcd_path = str(work / "trace.vcd")
    obj_dir = str(work / "obj_vl")
    tb_name = Path(tb_v).stem

    # ucrt64 MUST come before oss-cad-suite on Windows — DLL conflicts break g++
    if sys.platform == "win32":
        _msys_ucrt = os.environ.get("MSYS64_UCRT", r"C:\msys64\ucrt64\bin")
        env = {
            **os.environ,
            "PATH": _PATH_SEP.join(
                [
                    _msys_ucrt,
                    str(_OSS_CAD_ROOT / "bin"),
                    str(_OSS_CAD_ROOT / "lib"),
                    os.environ.get("PATH", ""),
                ]
            ),
            "VERILATOR_ROOT": _VERILATOR_ROOT,
        }
    else:
        env = {
            **os.environ,
            "PATH": _PATH_SEP.join(
                [
                    str(_OSS_CAD_ROOT / "bin"),
                    str(_OSS_CAD_ROOT / "lib"),
                    os.environ.get("PATH", ""),
                ]
            ),
            "VERILATOR_ROOT": _VERILATOR_ROOT,
        }

    vr = str(Path(env["VERILATOR_ROOT"]) / "include")

    # Write sim_main.cpp for Verilator (--timing requires contextp->time advance)
    sim_main = str(work / "sim_main.cpp")
    with Path(sim_main).open("w") as f:
        f.write(
            f'#include "V{tb_name}.h"\n'
            '#include "verilated.h"\n'
            '#include "verilated_vcd_c.h"\n'
            "double sc_time_stamp() { return 0; }\n"
            "int main(int argc, char** argv) {\n"
            "    const std::unique_ptr<VerilatedContext> ctxp(new VerilatedContext);\n"
            "    ctxp->commandArgs(argc, argv);\n"
            "    ctxp->traceEverOn(true);\n"
            f"    const std::unique_ptr<V{tb_name}> top(new V{tb_name}(ctxp.get()));\n"
            "    while (!ctxp->gotFinish()) {\n"
            "        top->eval();\n"
            "        ctxp->timeInc(1);\n"
            "    }\n"
            "    top->final();\n"
            "    return 0;\n"
            "}\n"
        )

    # Step 1: Generate C++ from Verilog
    verilator_bin = shutil.which("verilator_bin", path=env["PATH"])
    if verilator_bin is None:
        raise RuntimeError("verilator_bin not found on PATH")
    _run_step(
        [
            verilator_bin,
            "--cc",
            "--exe",
            "--trace",
            "--timing",
            "--top-module",
            tb_name,
            "--Mdir",
            "obj_vl",
            design_v,
            tb_v,
            sim_main,
        ],
        env,
        workdir,
        "verilator --cc",
    )

    # Step 2: Compile Verilator runtime
    cflags = [
        "-Os",
        "-I.",
        f"-I{vr}",
        f"-I{vr}/vltstd",
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

    for src in ["verilated", "verilated_vcd_c", "verilated_timing", "verilated_threads"]:
        obj = str(Path(obj_dir) / f"{src}.o")
        if not Path(obj).exists():
            _run_step(
                ["g++", *cflags, "-c", "-o", f"{src}.o", str(Path(vr) / f"{src}.cpp")],
                env,
                obj_dir,
                f"compile {src}",
            )

    # Step 3: Merge design sources via verilator_includer
    includer = str(Path(env["VERILATOR_ROOT"]) / "bin" / "verilator_includer")
    cpp_files = list(Path(obj_dir).glob(f"V{tb_name}*.cpp"))
    cpp_basenames = [f.name for f in cpp_files]
    includer_result = subprocess.run(
        [sys.executable, includer, "-DVL_INCLUDE_OPT=include", *cpp_basenames],
        capture_output=True,
        text=True,
        cwd=obj_dir,
        timeout=30,
        check=False,
    )
    if includer_result.returncode != 0:
        raise RuntimeError(f"verilator_includer failed:\n{includer_result.stderr}")
    all_cpp = Path(obj_dir) / f"V{tb_name}__ALL.cpp"
    with all_cpp.open("w") as f:
        f.write(includer_result.stdout)

    # Step 4: Compile design + sim_main
    _run_step(
        ["g++", *cflags, "-c", "-o", f"V{tb_name}__ALL.o", f"V{tb_name}__ALL.cpp"],
        env,
        obj_dir,
        "compile design",
    )
    _run_step(
        ["g++", *cflags, "-c", "-o", "sim_main.o", str(work / "sim_main.cpp")],
        env,
        obj_dir,
        "compile sim_main",
    )

    # Step 5: Link
    objs = [
        "verilated.o",
        "verilated_vcd_c.o",
        "verilated_timing.o",
        "verilated_threads.o",
        f"V{tb_name}__ALL.o",
        "sim_main.o",
    ]
    exe_name = f"V{tb_name}{_EXE_SUFFIX}"
    _run_step(
        ["g++", *objs, "-pthread", "-lpthread", "-latomic", "-o", exe_name], env, obj_dir, "link"
    )

    # Step 6: Run simulation
    exe_path = str(Path(obj_dir) / exe_name)
    _run_step([exe_path], env, workdir, "simulate", timeout=60)

    if not Path(vcd_path).exists():
        raise RuntimeError("VCD not generated by Verilator simulation")
    return vcd_path


# ═══════════════════════════════════════════════════════════════════════════
# Part 3: Helpers for running bwave and parsing output
# ═══════════════════════════════════════════════════════════════════════════


def _bwave_for_vcd(vcd_path: str) -> str:
    """Build or reuse an .fst store for *vcd_path* under tempdir."""
    import hashlib

    src = Path(vcd_path)
    stat = src.stat()
    key = hashlib.sha1(
        f"{src.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode(),
    ).hexdigest()[:16]
    cache_dir = Path(tempfile.gettempdir()) / "bwave_oracle_tests"
    cache_dir.mkdir(parents=True, exist_ok=True)
    bwave_path = cache_dir / f"{src.stem}_{key}.fst"
    if bwave_path.exists() and bwave_path.stat().st_mtime_ns >= stat.st_mtime_ns:
        return str(bwave_path)

    result = subprocess.run(
        [BWAVE_BIN, "build", str(src), "-o", str(bwave_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"bwave build failed for {src}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return str(bwave_path)


def _replace_vcd_inputs(args: list[str]) -> list[str]:
    """Replace VCD path arguments with freshly-built .fst store paths."""
    rewritten: list[str] = []
    for arg in args:
        p = Path(arg)
        if p.suffix.lower() == ".vcd" and p.exists():
            rewritten.append(_bwave_for_vcd(arg))
        else:
            rewritten.append(arg)
    return rewritten


def run_bwave(args: list[str], timeout: int = 30) -> tuple[str, str]:
    """Run current bwave v0.2 subcommand args, return (stdout, stderr)."""
    cmd = [BWAVE_BIN, *_replace_vcd_inputs(args)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"bwave command failed rc={result.returncode}\n"
            f"cmd: {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout, result.stderr


def strip_bit_range(name: str) -> str:
    """Strip VCD bit-range suffix: 'counter[15:0]' -> 'counter'."""
    return re.sub(r"\[\d+:\d+\]$", "", name)


def parse_list_signals_output(stdout: str) -> dict[str, int]:
    """Parse --list output into {name: width}.

    bwave may output a flat list or a tree with indentation:
        dut                            (6 signals)
          clk                          wire     1-bit
          count[3:0]                   reg      4-bit
        clk                            reg      1-bit
    Bit ranges stripped. Scope tracked via indentation.
    """
    signals: dict[str, int] = {}
    scope_stack: list[str] = []

    for line in stdout.splitlines():
        raw = line.rstrip()
        if not raw or raw.lstrip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip())
        level = indent // 2

        # Module line: "  modname                       (N signals)"
        mod_match = re.match(r"^(\s*)(\S+)\s+\(\d+ signals?\)$", raw)
        if mod_match:
            scope_stack = scope_stack[:level]
            scope_stack.append(mod_match.group(2))
            continue

        # Signal line: "  signame                       wire     4-bit"
        sig_match = re.match(r"^(\s*)(\S+)\s+\S+\s+(\d+)-bit$", raw)
        if sig_match:
            name = strip_bit_range(sig_match.group(2))
            width = int(sig_match.group(3))
            prefix_parts = scope_stack[:level]
            full_name = ".".join([*prefix_parts, name]) if prefix_parts else name
            signals[full_name] = width

    return signals


def parse_at_cycle_output(stdout: str) -> dict[str, str]:
    """Parse --at-cycle output into {signal_name: value}.

    Output format:
        # Snapshot at cycle 5
        clk                                      = 1
        count[15:0]                              = 3
    Bit ranges stripped from names.
    """
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        m = re.match(r"^(\S+)\s+=\s+(\S+)$", stripped)
        if m:
            values[strip_bit_range(m.group(1))] = m.group(2)
    return values


def parse_find_value_output(stdout: str) -> list[tuple[int, str, str]]:
    """Parse --find output into [(cycle, signal_name, value), ...].

    Sync output format:
        cycle 4 enable 1
        cycle 5 enable 1
    """
    results: list[tuple[int, str, str]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        m = re.match(r"^cycle\s+(\d+)\s+(\S+)\s+(\S+)$", stripped)
        if m:
            results.append((int(m.group(1)), strip_bit_range(m.group(2)), m.group(3)))
    return results


def parse_stats_output(stdout: str) -> dict[str, int]:
    """Parse --stats output into {signal_name: transition_count}.

    Output format:
        clk  1-bit  95 transitions  2 unique values
        counter[15:0]  16-bit  47 transitions  47 unique values
    Bit ranges stripped from names.
    """
    stats: dict[str, int] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("  ") or not stripped:
            continue
        m = re.match(r"^(\S+)\s+\d+-bit;?\s+(\d+)\s+transitions?;?\s+", stripped)
        if m:
            stats[strip_bit_range(m.group(1))] = int(m.group(2))
    return stats


def _cache_clock_signal(oracle: VcdOracle) -> str | None:
    """Return the clock signal B-wave's cache builder would auto-detect."""
    candidates = [
        name
        for name, width in oracle.signals().items()
        if width == 1 and "clk" in name.split("[", 1)[0].lower()
    ]
    candidates.sort(key=lambda name: (name.count("."), name))
    return candidates[0] if candidates else None


def _cache_reset_signal(oracle: VcdOracle) -> str | None:
    """Return the reset signal B-wave's cache builder would auto-detect."""
    candidates = [
        name
        for name, width in oracle.signals().items()
        if width == 1 and "rst" in name.split("[", 1)[0].lower()
    ]
    candidates.sort(key=lambda name: (name.count("."), name))
    return candidates[0] if candidates else None


def _infer_cache_sim_range(oracle: VcdOracle) -> tuple[int, int]:  # noqa: PLR0912, PLR0915 — reimplements the cache builder's edge-scan heuristic; many sequential branches over VCD ticks
    """Infer the cache sim_start/sim_end ticks used by async stats.

    B-wave's cache builder starts stats at the first detected clock rising edge
    after reset is inactive.  It records sim_end as the largest VCD timestamp.
    """
    timestamps = oracle.timestamps()
    if not timestamps:
        all_ticks = [
            tick for name in oracle.signal_names() for tick, _value in oracle.transitions(name)
        ]
        end_tick = max(all_ticks) if all_ticks else 0
        return 0, end_tick

    clock_name = _cache_clock_signal(oracle)
    reset_name = _cache_reset_signal(oracle)
    clock_id = oracle._name_to_id(clock_name) if clock_name is not None else None
    reset_id = oracle._name_to_id(reset_name) if reset_name is not None else None

    reset_active_low = False
    if reset_name is not None:
        reset_leaf = reset_name.split("[", 1)[0].split(".")[-1].lower()
        reset_active_low = reset_leaf.endswith("n") or "_n" in reset_leaf

    events_by_tick: dict[int, list[tuple[str, str]]] = {}
    for tick, vcd_id, value in oracle._events:
        events_by_tick.setdefault(tick, []).append((vcd_id, value))

    current_tick = 0
    sim_start_tick = 0
    sim_end_tick = 0
    first_timestamp_seen = False
    cycle_count = 0
    rising_edge_pending = False
    clock_prev = "x"
    reset_active = True

    for tick in timestamps:
        if first_timestamp_seen:
            prev_tick = current_tick
            if reset_id is not None and reset_active:
                rising_edge_pending = False
            elif rising_edge_pending:
                rising_edge_pending = False
                cycle_count += 1
                if cycle_count == 1:
                    sim_start_tick = prev_tick

        current_tick = tick
        sim_end_tick = max(sim_end_tick, tick)
        if not first_timestamp_seen:
            first_timestamp_seen = True
            sim_start_tick = tick

        for vcd_id, value in events_by_tick.get(tick, []):
            scalar = VcdOracle._to_scalar(value)
            if vcd_id == clock_id:
                if scalar == "1" and clock_prev == "0":
                    rising_edge_pending = True
                clock_prev = scalar
            if vcd_id == reset_id and scalar in {"0", "1"}:
                is_asserted = scalar == "0" if reset_active_low else scalar == "1"
                reset_active = is_asserted

    if rising_edge_pending and not (reset_id is not None and reset_active):
        cycle_count += 1
        if cycle_count == 1:
            sim_start_tick = current_tick

    return sim_start_tick, sim_end_tick


def cache_transition_count(oracle: VcdOracle, signal_name: str) -> int:
    """Expected count for B-wave async stats after cache sim-window rebasing."""
    start_tick, end_tick = _infer_cache_sim_range(oracle)
    return sum(
        1 for tick, _value in oracle.transitions(signal_name) if start_tick <= tick <= end_tick
    )


def parse_wave_output(stdout: str) -> dict[int, dict[str, str]]:
    """Parse --wave output into {cycle: {signal_name: value}}.

    bwave v0.2 outputs a horizontal table:
            cycle  1  2  3
        counter[15:0]  0  1  2
    Bit ranges stripped from signal names.
    """
    data: dict[int, dict[str, str]] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if not parts:
            continue
        if parts[0] == "cycle":
            cycles = [int(p) for p in parts[1:] if p.isdigit()]
            for cycle in cycles:
                data.setdefault(cycle, {})
            continue
        if data and len(parts) >= 2:
            cycles = list(data.keys())
            sig_name = strip_bit_range(parts[0])
            for cycle, value in zip(cycles, parts[1:], strict=False):
                data[cycle][sig_name] = value
    return data


def normalize_value(val: str) -> str:
    """Normalize a value for comparison.

    Handles hex/binary format differences between oracle and bwave:
    - Oracle stores raw binary; bwave displays as uppercase hex
    - Both may have leading zeros
    - x/z values kept as-is
    """
    val = val.strip()

    # x/z — keep as-is (case-insensitive compare later)
    if any(c in val for c in "xzXZ"):
        return val.lower()

    # Try to parse as integer for canonical comparison
    try:
        # If it's hex (all hex chars)
        if all(c in "0123456789abcdefABCDEF" for c in val) and len(val) > 0:
            return format(int(val, 16), "X") if int(val, 16) != 0 else "0"
    except ValueError:
        pass

    return val


def bin_to_hex(binary_str: str) -> str:
    """Convert a binary string to uppercase hex, stripping leading zeros."""
    if not binary_str or not all(c in "01" for c in binary_str):
        return binary_str
    val = int(binary_str, 2)
    return format(val, "X") if val != 0 else "0"


def oracle_value_to_hex(val: str, width: int) -> str:
    """Convert oracle raw value to the hex format bwave uses.

    - 1-bit scalars stay as "0"/"1"/"x"/"z"
    - Multi-bit binary -> uppercase hex (e.g. "00001010" -> "A")
    - x/z containing values stay as raw binary
    """
    if any(c in val for c in "xzXZ"):
        return val.lower()
    if width == 1:
        return val
    return bin_to_hex(val)


# ═══════════════════════════════════════════════════════════════════════════
# Test infrastructure
# ═══════════════════════════════════════════════════════════════════════════

# Design registry: (design_stem, design_file, tb_file)
DESIGNS = [
    ("counter_4bit", "counter_4bit.v", "counter_4bit_tb.v"),
    ("fsm_traffic", "fsm_traffic.v", "fsm_traffic_tb.v"),
    ("lfsr_8bit", "lfsr_8bit.v", "lfsr_8bit_tb.v"),
    ("sync_fifo", "sync_fifo.v", "sync_fifo_tb.v"),
    ("arbiter", "arbiter.v", "arbiter_tb.v"),
]


class SimulationCache:
    """Cache simulated VCD files across test methods.

    Running simulators is expensive; we run each (design, simulator) pair
    once and reuse the VCD + oracle for all comparison tests.
    """

    _cache: ClassVar[
        dict[str, tuple[str, str, VcdOracle]]
    ] = {}  # key -> (workdir, vcd_path, oracle)

    @classmethod
    def get(cls, design: str, simulator: str) -> tuple[str, VcdOracle]:
        """Return (vcd_path, oracle) for design+simulator, simulating if needed."""
        key = f"{design}_{simulator}"
        if key in cls._cache:
            _, vcd_path, oracle = cls._cache[key]
            return vcd_path, oracle

        design_info = next(d for d in DESIGNS if d[0] == design)
        design_v = str(_RTL_FIXTURES / design_info[1])
        tb_v = str(_RTL_FIXTURES / design_info[2])

        workdir = tempfile.mkdtemp(prefix=f"vcd_test_{key}_")
        try:
            if simulator == "icarus":
                vcd_path = run_icarus(design_v, tb_v, workdir)
            elif simulator == "verilator":
                vcd_path = run_verilator(design_v, tb_v, workdir)
            else:
                raise ValueError(f"Unknown simulator: {simulator}")

            oracle = VcdOracle(vcd_path)
            cls._cache[key] = (workdir, vcd_path, oracle)
            return vcd_path, oracle
        except Exception:
            # Keep workdir on failure for debugging
            print(f"  [KEEP workdir for debugging: {workdir}]", file=sys.stderr)
            raise

    @classmethod
    def cleanup(cls) -> None:
        """Remove all temp directories (call on success)."""
        for _key, (workdir, _, _) in cls._cache.items():
            try:
                shutil.rmtree(workdir)
            except OSError as e:
                print(f"  [WARN] Failed to clean {workdir}: {e}", file=sys.stderr)
        cls._cache.clear()


def _build_bwave_if_missing() -> None:
    """Build bwave from source if the binary doesn't exist."""
    if Path(BWAVE_BIN).exists():
        return
    print("Building bwave...", file=sys.stderr)
    result = subprocess.run(
        ["cargo", "build"],
        cwd=str(_VCD_PARSER_DIR),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cargo build failed:\n{result.stderr}")
    if not Path(BWAVE_BIN).exists():
        raise RuntimeError(f"Build succeeded but {BWAVE_BIN} not found")


# ═══════════════════════════════════════════════════════════════════════════
# Test Cases
# ═══════════════════════════════════════════════════════════════════════════


def _find_reset_edge(oracle: VcdOracle) -> int:
    """Return the rising-edge index just after reset deasserts (0 if no reset found)."""
    oracle_signals = oracle.signals()
    rising = oracle.rising_edges("*clk*")
    reset_sig = _find_signal_by_leaf(oracle_signals, lambda leaf: "rst" in leaf)
    if reset_sig is None:
        return 0
    for i, t in enumerate(rising):
        val = oracle.value_at_time(reset_sig, t)
        if VcdOracle._to_scalar(val) == "1":
            return i + 1
    return 0


def _parse_scope_from_stderr(stderr: str) -> str:
    """Extract scope prefix from bwave stderr (e.g. 'tb.dut.')."""
    for line in stderr.splitlines():
        m = re.match(r"^# scope:\s+(\S+)", line)
        if m:
            return m.group(1) + "."
    return ""


def _find_signal_by_leaf(
    signals: dict[str, int],
    predicate: object,  # Callable[[str], bool]
) -> str | None:
    """Return the first signal whose leaf name satisfies *predicate*."""
    for name in signals:
        leaf = name.split(".")[-1].lower()
        if predicate(leaf):  # type: ignore[operator]
            return name
    return None


def _make_test_class(design: str, simulator: str) -> type:
    """Dynamically create a test class for a (design, simulator) pair."""

    skip_sim = (simulator == "icarus" and not _IVERILOG_AVAILABLE) or (
        simulator == "verilator" and not _VERILATOR_AVAILABLE
    )
    skip_extract = not _BWAVE_AVAILABLE

    class _Tests(unittest.TestCase):
        """Ground truth tests for {design} with {simulator}."""

        vcd_path: str = ""
        oracle: VcdOracle | None = None

        @classmethod
        def setUpClass(cls) -> None:
            if skip_sim or skip_extract:
                return
            _build_bwave_if_missing()
            cls.vcd_path, cls.oracle = SimulationCache.get(design, simulator)

        # ── list ────────────────────────────────────────────────────────

        @unittest.skipIf(skip_sim, f"{simulator} not available")
        @unittest.skipIf(skip_extract, "bwave not built")
        def test_list_signals(self) -> None:
            """Signal names and widths match between oracle and bwave."""
            oracle = self.__class__.oracle
            assert oracle is not None

            stdout, stderr = run_bwave(["list", self.vcd_path])
            extract_signals = parse_list_signals_output(stdout)

            oracle_signals = oracle.signals()

            # The oracle has full hierarchical names (e.g. "counter_4bit_tb.dut.count")
            # while bwave strips the common scope prefix.
            scope = _parse_scope_from_stderr(stderr)

            # Build oracle lookup after stripping common prefix
            oracle_stripped: dict[str, int] = {}
            for name, width in oracle_signals.items():
                stripped = name[len(scope) :] if name.startswith(scope) else name
                oracle_stripped[stripped] = width

            # Compare: every extract signal should exist in oracle with same width
            for name, width in extract_signals.items():
                self.assertIn(
                    name,
                    oracle_stripped,
                    f"bwave signal '{name}' not in oracle. "
                    f"Oracle has: {sorted(oracle_stripped.keys())}",
                )
                self.assertEqual(
                    width,
                    oracle_stripped[name],
                    f"Width mismatch for '{name}': extract={width}, oracle={oracle_stripped[name]}",
                )

            # Every oracle signal should be in extract output
            for name, _width in oracle_stripped.items():
                self.assertIn(
                    name, extract_signals, f"Oracle signal '{name}' missing from bwave output"
                )

        # ── at-cycle ────────────────────────────────────────────────────

        @unittest.skipIf(skip_sim, f"{simulator} not available")
        @unittest.skipIf(skip_extract, "bwave not built")
        def test_at_cycle(self) -> None:
            """Signal values at specific cycles match oracle ground truth."""
            oracle = self.__class__.oracle
            assert oracle is not None

            oracle_signals = oracle.signals()
            rising = oracle.rising_edges("*clk*")
            self.assertGreater(len(rising), 5, "Not enough clock edges")

            reset_deassert_edge = _find_reset_edge(oracle)

            for cycle_num in [1, 5, 10, 15]:
                edge_idx = reset_deassert_edge + cycle_num - 1
                if edge_idx >= len(rising):
                    continue
                self._check_at_cycle(
                    oracle,
                    oracle_signals,
                    rising[edge_idx],
                    cycle_num,
                )

        def _check_at_cycle(
            self,
            oracle: VcdOracle,
            oracle_signals: dict[str, int],
            timestamp: int,
            cycle_num: int,
        ) -> None:
            """Compare bwave snapshot at *cycle_num* against oracle."""
            # bwave displays the value stable BEFORE the rising edge
            sample_t = timestamp - 1
            oracle_values: dict[str, str] = {}
            for name, width in oracle_signals.items():
                raw_val = oracle.value_at_time(name, sample_t)
                oracle_values[name] = oracle_value_to_hex(raw_val, width)

            stdout, stderr = run_bwave(["value", self.vcd_path, "--at", str(cycle_num)])
            extract_values = parse_at_cycle_output(stdout)
            scope = _parse_scope_from_stderr(stderr)

            for ext_name, ext_val in extract_values.items():
                if "clk" in ext_name.lower():
                    continue
                full_name = scope + ext_name if scope else ext_name
                if full_name not in oracle_values:
                    continue
                oracle_val = oracle_values[full_name]
                norm_ext = normalize_value(ext_val)
                norm_oracle = normalize_value(oracle_val)
                self.assertEqual(
                    norm_ext,
                    norm_oracle,
                    f"Cycle {cycle_num}, signal '{ext_name}': "
                    f"extract='{ext_val}' oracle='{oracle_val}' "
                    f"(normalized: {norm_ext} vs {norm_oracle})",
                )

        @staticmethod
        def _oracle_find_cycles(
            oracle: VcdOracle,
            rising: list[int],
            reset_edge: int,
            signal: str,
            value: str,
        ) -> set[int]:
            """Return set of post-reset cycle numbers where *signal* == *value*."""
            cycles: set[int] = set()
            for cycle_num in range(1, len(rising) - reset_edge + 1):
                edge_idx = reset_edge + cycle_num - 1
                if edge_idx >= len(rising):
                    break
                val = oracle.value_at_time(signal, rising[edge_idx] - 1)
                if VcdOracle._to_scalar(val) == value:
                    cycles.add(cycle_num)
            return cycles

        # ── find ────────────────────────────────────────────────────────

        @unittest.skipIf(skip_sim, f"{simulator} not available")
        @unittest.skipIf(skip_extract, "bwave not built")
        def test_find_value(self) -> None:
            """Cycle numbers where a signal matches a value agree with oracle."""
            oracle = self.__class__.oracle
            assert oracle is not None

            oracle_signals = oracle.signals()
            rising = oracle.rising_edges("*clk*")

            # Pick a suitable signal to search: prefer known control signals
            target_value = "1"
            target_signal = _find_signal_by_leaf(
                oracle_signals,
                lambda leaf: leaf in {"enable", "wr_en", "walk_request"},
            )
            if target_signal is None:
                # Fall back to any 1-bit signal that isn't clock/reset
                for _name in oracle_signals:
                    _leaf = _name.split(".")[-1].lower()
                    if oracle_signals[_name] == 1 and "clk" not in _leaf and "rst" not in _leaf:
                        target_signal = _name
                        break

            if target_signal is None:
                self.skipTest("No suitable 1-bit signal found for --find test")
                return

            reset_deassert_edge = _find_reset_edge(oracle)
            oracle_cycles = self._oracle_find_cycles(
                oracle,
                rising,
                reset_deassert_edge,
                target_signal,
                target_value,
            )

            if not oracle_cycles:
                self.skipTest(f"Signal '{target_signal}' never equals '{target_value}' post-reset")
                return

            leaf_name = target_signal.split(".")[-1]
            stdout, _stderr = run_bwave(
                [
                    "find",
                    self.vcd_path,
                    f"*{leaf_name}*",
                    target_value,
                ]
            )
            extract_cycles = {r[0] for r in parse_find_value_output(stdout)}

            # Exclude boundary cycles (oracle can't see past last rising edge)
            max_oracle_cycle = max(oracle_cycles) if oracle_cycles else 0
            extract_interior = {c for c in extract_cycles if c <= max_oracle_cycle}

            for ec in extract_interior:
                self.assertIn(
                    ec,
                    oracle_cycles,
                    f"bwave found cycle {ec} for '{leaf_name}'={target_value} "
                    f"but oracle disagrees. Oracle cycles: {sorted(oracle_cycles)}",
                )
            for oc in oracle_cycles:
                self.assertIn(
                    oc,
                    extract_interior,
                    f"Oracle found cycle {oc} for '{leaf_name}'={target_value} "
                    f"but bwave missed it. Extract cycles: {sorted(extract_interior)}",
                )

        # ── stats ───────────────────────────────────────────────────────

        @unittest.skipIf(skip_sim, f"{simulator} not available")
        @unittest.skipIf(skip_extract, "bwave not built")
        def test_stats(self) -> None:
            """Cache-windowed transition counts match bwave async stats."""
            oracle = self.__class__.oracle
            assert oracle is not None

            # Async stats count decoded cache records from the cache sim_start
            # through sim_end.  Sync stats intentionally rebase after reset.
            stdout, stderr = run_bwave(["stats", self.vcd_path, "--async", "-s", "*"])
            extract_stats = parse_stats_output(stdout)

            scope = _parse_scope_from_stderr(stderr)

            oracle_signals = oracle.signals()
            oracle_stats: dict[str, int] = {}
            for name in oracle_signals:
                stripped = name[len(scope) :] if name.startswith(scope) else name
                oracle_stats[stripped] = cache_transition_count(oracle, name)

            for ext_name, ext_count in extract_stats.items():
                if ext_name not in oracle_stats:
                    continue
                oracle_count = oracle_stats[ext_name]
                self.assertEqual(
                    ext_count,
                    oracle_count,
                    f"Transition count mismatch for '{ext_name}': "
                    f"extract={ext_count}, oracle={oracle_count}",
                )

        # ── wave ────────────────────────────────────────────────────────

        @unittest.skipIf(skip_sim, f"{simulator} not available")
        @unittest.skipIf(skip_extract, "bwave not built")
        def test_wave(self) -> None:
            """Waveform values match oracle for a cycle range."""
            oracle = self.__class__.oracle
            assert oracle is not None

            oracle_signals = oracle.signals()
            rising = oracle.rising_edges("*clk*")
            reset_deassert_edge = _find_reset_edge(oracle)

            # Pick a specific signal for wave comparison
            wave_signal = _find_signal_by_leaf(
                oracle_signals,
                lambda leaf: leaf in {"count", "lfsr_out", "state"},
            )
            if wave_signal is None:
                # Fall back to any multi-bit non-clock/reset signal
                for _name in oracle_signals:
                    if oracle_signals[_name] > 1:
                        _leaf = _name.split(".")[-1].lower()
                        if "clk" not in _leaf and "rst" not in _leaf:
                            wave_signal = _name
                            break

            if wave_signal is None:
                self.skipTest("No suitable signal found for wave test")
                return

            leaf_name = wave_signal.split(".")[-1]
            wave_width = oracle_signals[wave_signal]

            stdout, _stderr = run_bwave(
                [
                    "wave",
                    self.vcd_path,
                    "-t",
                    "1:5",
                    "-s",
                    f"*{leaf_name}",
                ]
            )
            wave_data = parse_wave_output(stdout)

            if not wave_data:
                self.skipTest("No wave output produced")
                return

            self._check_wave_data(
                oracle,
                wave_signal,
                wave_width,
                leaf_name,
                wave_data,
                rising,
                reset_deassert_edge,
            )

        def _check_wave_data(
            self,
            oracle: VcdOracle,
            wave_signal: str,
            wave_width: int,
            leaf_name: str,
            wave_data: dict[int, dict[str, str]],
            rising: list[int],
            reset_deassert_edge: int,
        ) -> None:
            """Compare wave data against oracle for each cycle."""
            for cycle_num, signals in wave_data.items():
                if cycle_num < 1 or cycle_num > 5:
                    continue
                edge_idx = reset_deassert_edge + cycle_num - 1
                if edge_idx >= len(rising):
                    continue
                timestamp = rising[edge_idx]

                for sig_name, ext_val in signals.items():
                    if leaf_name not in sig_name and sig_name != leaf_name:
                        continue
                    oracle_raw = oracle.value_at_time(wave_signal, timestamp - 1)
                    oracle_hex = oracle_value_to_hex(oracle_raw, wave_width)
                    norm_ext = normalize_value(ext_val)
                    norm_oracle = normalize_value(oracle_hex)
                    self.assertEqual(
                        norm_ext,
                        norm_oracle,
                        f"Wave cycle {cycle_num}, signal '{sig_name}': "
                        f"extract='{ext_val}' oracle='{oracle_hex}' "
                        f"(normalized: {norm_ext} vs {norm_oracle})",
                    )

    # Set a meaningful class name
    class_name = f"Test_{design}_{simulator}"
    _Tests.__name__ = class_name
    _Tests.__qualname__ = class_name
    _Tests.__doc__ = f"Ground truth tests for {design} with {simulator}."
    return _Tests


# ═══════════════════════════════════════════════════════════════════════════
# Dynamically create test classes for each (design, simulator) pair
# ═══════════════════════════════════════════════════════════════════════════

# Generate test classes and inject into module namespace
for _design_name, _, _ in DESIGNS:
    for _sim in ("icarus", "verilator"):
        _cls = _make_test_class(_design_name, _sim)
        globals()[_cls.__name__] = _cls

# Clean up loop vars
del _design_name, _sim, _cls


# ═══════════════════════════════════════════════════════════════════════════
# VcdOracle standalone tests (no simulators needed)
# ═══════════════════════════════════════════════════════════════════════════


class TestVcdOracleUnit(unittest.TestCase):
    """Unit tests for the VcdOracle parser itself, using synthetic fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture = _THIS_DIR / "fixtures" / "small_clocked.vcd"
        if not fixture.exists():
            raise unittest.SkipTest(f"Fixture not found: {fixture}")
        cls.oracle = VcdOracle(str(fixture))

    def test_signal_count(self) -> None:
        """Oracle finds all 10 signals in small_clocked.vcd."""
        sigs = self.oracle.signals()
        self.assertEqual(
            len(sigs), 10, f"Expected 10 signals, got {len(sigs)}: {list(sigs.keys())}"
        )

    def test_signal_widths(self) -> None:
        """Signal widths are correct."""
        sigs = self.oracle.signals()
        expected = {
            "tb.dut.clk": 1,
            "tb.dut.rstn": 1,
            "tb.dut.data_a": 8,
            "tb.dut.data_b": 8,
            "tb.dut.flag": 1,
            "tb.dut.counter": 16,
            "tb.dut.state": 4,
            "tb.dut.enable": 1,
            "tb.dut.addr": 8,
            "tb.dut.done": 1,
        }
        for name, width in expected.items():
            self.assertIn(name, sigs, f"Signal '{name}' not found")
            self.assertEqual(sigs[name], width, f"Width mismatch for '{name}'")

    def test_rising_edges(self) -> None:
        """Clock rising edges are at expected timestamps (5, 15, 25, ...)."""
        edges = self.oracle.rising_edges("*clk*")
        self.assertGreater(len(edges), 0)
        # First rising edge should be at time 5
        self.assertEqual(edges[0], 5)
        # Edges are 10ns apart (5ns half-period)
        for i in range(1, min(len(edges), 10)):
            self.assertEqual(edges[i] - edges[i - 1], 10, f"Non-uniform clock period at edge {i}")

    def test_value_at_time(self) -> None:
        """Value lookup returns correct data."""
        # At time 0, rstn should be 0 (reset asserted)
        val = self.oracle.value_at_time("tb.dut.rstn", 0)
        self.assertEqual(val, "0")

        # At time 25, rstn should be 1 (deasserted at cycle 3 rising edge = time 25)
        val = self.oracle.value_at_time("tb.dut.rstn", 25)
        self.assertEqual(val, "1")

    def test_transitions_clk(self) -> None:
        """Clock has many transitions (toggles every 5ns)."""
        transitions = self.oracle.transitions("tb.dut.clk")
        self.assertGreater(len(transitions), 50, "Clock should have many transitions")

    def test_dumpvars_section(self) -> None:
        """Oracle handles VCD files without $dumpvars (small_clocked has none)."""
        # small_clocked.vcd sets initial values directly (no $dumpvars block)
        # The oracle should still parse initial values at #0
        val = self.oracle.value_at_time("tb.dut.counter", 0)
        self.assertEqual(val, "0000000000000000")


class TestVcdOracleLargeFixture(unittest.TestCase):
    """Test oracle against large_multiwidth.vcd which has $dumpvars."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture = _THIS_DIR / "fixtures" / "large_multiwidth.vcd"
        if not fixture.exists():
            raise unittest.SkipTest(f"Fixture not found: {fixture}")
        cls.oracle = VcdOracle(str(fixture))

    def test_signal_count(self) -> None:
        """Oracle finds all 30 signals in large_multiwidth.vcd."""
        sigs = self.oracle.signals()
        self.assertEqual(len(sigs), 30)

    def test_wide_signal(self) -> None:
        """256-bit signal parsed correctly."""
        sigs = self.oracle.signals()
        self.assertIn("tb.dut.huge_val", sigs)
        self.assertEqual(sigs["tb.dut.huge_val"], 256)

    def test_dumpvars_initial(self) -> None:
        """$dumpvars section sets correct initial values."""
        # stuck_one is initialized to 1 in $dumpvars
        val = self.oracle.value_at_time("tb.dut.stuck_one", 0)
        self.assertEqual(val, "1")

    def test_xz_values(self) -> None:
        """Signals with x/z values are handled."""
        sigs = self.oracle.signals()
        self.assertIn("tb.dut.stuck_x", sigs)
        # stuck_x should be x at time 0
        val = self.oracle.value_at_time("tb.dut.stuck_x", 0)
        self.assertTrue(all(c in "xX" for c in val), f"Expected all x, got '{val}'")


# ═══════════════════════════════════════════════════════════════════════════
# Cross-validation: oracle vs bwave on synthetic fixtures
# ═══════════════════════════════════════════════════════════════════════════


@unittest.skipIf(not _BWAVE_AVAILABLE, "bwave not built")
class TestOracleVsExtractSynthetic(unittest.TestCase):
    """Compare oracle against bwave on the synthetic test VCDs.

    These don't need simulators — the VCDs are pre-generated fixtures.
    """

    @classmethod
    def setUpClass(cls) -> None:
        fixture = _THIS_DIR / "fixtures" / "small_clocked.vcd"
        if not fixture.exists():
            raise unittest.SkipTest(f"Fixture not found: {fixture}")
        cls.vcd_path = str(fixture)
        cls.oracle = VcdOracle(cls.vcd_path)
        _build_bwave_if_missing()

    def test_list_signals_match(self) -> None:
        """--list output matches oracle for small_clocked.vcd."""
        stdout, stderr = run_bwave(["list", self.vcd_path])
        extract_sigs = parse_list_signals_output(stdout)

        scope = _parse_scope_from_stderr(stderr)

        oracle_sigs = self.oracle.signals()
        oracle_stripped = {}
        for name, width in oracle_sigs.items():
            stripped = name[len(scope) :] if name.startswith(scope) else name
            oracle_stripped[stripped] = width

        self.assertEqual(
            set(extract_sigs.keys()), set(oracle_stripped.keys()), "Signal name mismatch"
        )
        for name in extract_sigs:
            self.assertEqual(
                extract_sigs[name], oracle_stripped[name], f"Width mismatch for '{name}'"
            )

    def test_stats_transitions_match(self) -> None:
        """Transition counts match for small_clocked.vcd.

        Async stats count decoded cache records inside B-wave's cache sim window.
        """
        stdout, stderr = run_bwave(["stats", self.vcd_path, "--async", "-s", "*"])
        extract_stats = parse_stats_output(stdout)

        scope = _parse_scope_from_stderr(stderr)

        for ext_name, ext_count in extract_stats.items():
            full_name = scope + ext_name if scope else ext_name
            oracle_count = cache_transition_count(self.oracle, full_name)
            self.assertEqual(
                ext_count,
                oracle_count,
                f"Transition count for '{ext_name}': extract={ext_count} oracle={oracle_count}",
            )

    def test_at_cycle_5_match(self) -> None:
        """Snapshot at cycle 5 matches oracle for small_clocked.vcd."""
        stdout, stderr = run_bwave(["value", self.vcd_path, "--at", "5"])
        extract_vals = parse_at_cycle_output(stdout)

        scope = _parse_scope_from_stderr(stderr)
        rising = self.oracle.rising_edges("*clk*")
        reset_deassert_edge = _find_reset_edge(self.oracle)

        # Cycle 5 timestamp
        edge_idx = reset_deassert_edge + 5 - 1
        self.assertLess(edge_idx, len(rising))
        timestamp = rising[edge_idx]

        for ext_name, ext_val in extract_vals.items():
            if "clk" in ext_name.lower():
                continue
            full_name = scope + ext_name if scope else ext_name
            oracle_raw = self.oracle.value_at_time(full_name, timestamp - 1)
            width = self.oracle.signals().get(full_name, 1)
            oracle_hex = oracle_value_to_hex(oracle_raw, width)

            norm_ext = normalize_value(ext_val)
            norm_oracle = normalize_value(oracle_hex)

            self.assertEqual(
                norm_ext,
                norm_oracle,
                f"Cycle 5 mismatch for '{ext_name}': extract='{ext_val}' oracle='{oracle_hex}'",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Virtual signal ground truth: oracle-computed vs bwave --virtual
# ═══════════════════════════════════════════════════════════════════════════


@unittest.skipIf(not _BWAVE_AVAILABLE, "bwave not built")
class TestVirtualSignalGroundTruth(unittest.TestCase):
    """Cross-validate virtual signal results against Python oracle.

    Uses large_multiwidth.vcd which has counter (16-bit), status (8-bit),
    valid/ready (1-bit), and x/z values.
    """

    @classmethod
    def setUpClass(cls) -> None:
        fixture = _THIS_DIR / "fixtures" / "large_multiwidth.vcd"
        if not fixture.exists():
            raise unittest.SkipTest(f"Fixture not found: {fixture}")
        cls.vcd_path = str(fixture)
        cls.oracle = VcdOracle(cls.vcd_path)
        _build_bwave_if_missing()
        # The source fixture may live on a read-only bind mount (as it does in
        # the Docker smoke test), so keep generated stores in writable temp
        # space instead of beside the fixture.
        temp_dir = tempfile.TemporaryDirectory(prefix="bwave-virtual-")
        cls.addClassCleanup(temp_dir.cleanup)
        bwave_path = str(Path(temp_dir.name) / "large_multiwidth.test_virtual.fst")
        result = subprocess.run(
            [BWAVE_BIN, "build", cls.vcd_path, "-o", bwave_path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"bwave build failed:\n{result.stderr}")
        cls.bwave_path = bwave_path
        # Get clock edges for cycle-to-tick mapping
        cls.clock_edges = cls.oracle.rising_edges("*clk*")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "bwave_path"):
            Path(cls.bwave_path).unlink(missing_ok=True)

    def _oracle_value_at_cycle(self, sig_name: str, cycle: int) -> str:
        """Get oracle value at a specific cycle (tick of rising edge)."""
        if cycle < len(self.clock_edges):
            tick = self.clock_edges[cycle]
            return self.oracle.value_at_time(sig_name, tick)
        return "x"

    def _oracle_hex_at_cycle(self, sig_name: str, cycle: int) -> int | None:
        """Get oracle value as int at cycle. None for x/z."""
        raw = self._oracle_value_at_cycle(sig_name, cycle)
        if any(c in raw for c in "xzXZ"):
            return None
        try:
            if all(c in "01" for c in raw):
                return int(raw, 2)
            return int(raw, 16)
        except ValueError:
            return None

    def _bwave_find_virtual(self, virtual_def: str, value: str = "'h1") -> set[int]:
        """Run bwave --virtual DEF --find NAME VALUE on the pre-built .fst store."""
        name = virtual_def.split("=", maxsplit=1)[0].strip()
        cmd = [
            BWAVE_BIN,
            "find",
            self.bwave_path,
            name,
            value,
            "--with-reset",
            "--virtual",
            virtual_def,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # bwave uses 1-based cycle numbering; oracle uses 0-based
        return {r[0] - 1 for r in parse_find_value_output(result.stdout)}

    def test_gt_counter(self) -> None:
        """Virtual GT on counter matches oracle computation."""
        threshold = 50
        virtual_def = f"vgt_counter = *counter* > 'd{threshold}"

        # Oracle: compute expected cycles where counter > threshold
        oracle_cycles = set()
        for cycle in range(len(self.clock_edges)):
            val = self._oracle_hex_at_cycle("tb.dut.counter", cycle)
            if val is not None and val > threshold:
                oracle_cycles.add(cycle)

        bwave_cycles = self._bwave_find_virtual(virtual_def)

        self.assertEqual(
            oracle_cycles,
            bwave_cycles,
            f"GT {threshold} mismatch: oracle has {len(oracle_cycles)} cycles, "
            f"bwave has {len(bwave_cycles)}",
        )

    def test_slice_status_msb(self) -> None:
        """Virtual SLICE on status MSB matches oracle."""
        virtual_def = "vslice_msb = *status*[7]"

        # Oracle: bit 7 of status
        oracle_cycles = set()
        for cycle in range(len(self.clock_edges)):
            val = self._oracle_hex_at_cycle("tb.dut.status", cycle)
            if val is not None and (val >> 7) & 1:
                oracle_cycles.add(cycle)

        bwave_cycles = self._bwave_find_virtual(virtual_def)

        self.assertEqual(
            oracle_cycles,
            bwave_cycles,
            f"SLICE 7 mismatch: oracle {len(oracle_cycles)} vs bwave {len(bwave_cycles)}",
        )

    def test_sig_to_sig_equal(self) -> None:
        """Virtual signal-to-signal EQUAL matches oracle."""
        virtual_def = "veq_vr = *valid* == *ready*"

        # Skip reset period — virtual signals are only evaluated at
        # transition points, so steady-state during reset is invisible.
        # Start from the cycle where reset deasserts (reset_edge - 1) since
        # bwave evaluates virtual signals at that transition.
        reset_edge = _find_reset_edge(self.oracle)
        start = max(0, reset_edge - 1)

        oracle_cycles = set()
        for cycle in range(start, len(self.clock_edges)):
            v = self._oracle_hex_at_cycle("tb.dut.valid", cycle)
            r = self._oracle_hex_at_cycle("tb.dut.ready", cycle)
            if v is not None and r is not None and v == r:
                oracle_cycles.add(cycle)

        bwave_cycles = self._bwave_find_virtual(virtual_def)

        self.assertEqual(
            oracle_cycles,
            bwave_cycles,
            f"EQUAL mismatch: oracle {len(oracle_cycles)} vs bwave {len(bwave_cycles)}",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Cleanup and main
# ═══════════════════════════════════════════════════════════════════════════


def teardown_module() -> None:
    """Clean up simulation temp dirs after all tests pass."""
    # Only clean up if all tests passed — pytest calls this after all tests
    SimulationCache.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
