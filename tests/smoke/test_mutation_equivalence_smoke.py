"""Real Verilator builds through the Mutation Flow's equivalent-Mutant check.

Runs inside the Booley Sandbox Image (CI coverage-release step): the host tests fake
Verilator's output, so this is the one place real generated C++ reaches the comparator
through Booley's own build paths (ADR 0088, amendment 2026-11-13).

The RTL calls ``$error``, so Verilator writes each build's absolute source path into the
C++ (``VL_STOP_MT``), and a Mutant builds from another directory than the baseline.  A
default assignment that every ``case`` branch overwrites gives Mutants that change nothing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from booley.flows.mutation.flow import MutationFlow
from booley.flows.mutation.generator import generate_mutants
from booley.flows.mutation.request import MutationRequest
from booley.runtime.booley_dir import reset_cache

pytestmark = [
    pytest.mark.skipif(
        any(shutil.which(tool) is None for tool in ("verilator", "make", "g++", "fusesoc")),
        reason="real smoke runs inside the pinned Booley Sandbox Image",
    ),
    pytest.mark.timeout(900),
]

_DUT = """\
module dut (
    input  logic [1:0] sel,
    input  logic [3:0] a,
    input  logic [3:0] b,
    output logic [3:0] y
);
  always_comb begin
    y = 4'd0; // default that every branch below overwrites
    case (sel)
      2'd0: y = a & b;
      2'd1: y = a | b;
      2'd2: y = a ^ b;
      default: y = ~a;
    endcase
    if (sel == 2'd3 && a == 4'd15 && b == 4'd15) $error("illegal input combination");
  end
endmodule
"""
_DEFAULT_LINE = next(
    number for number, line in enumerate(_DUT.splitlines(), start=1) if "default that" in line
)
_TESTBENCH = """\
module top;
  logic [1:0] sel;
  logic [3:0] a, b, y, expected;
  dut d (.sel(sel), .a(a), .b(b), .y(y));
  integer failures = 0;

  initial begin
    for (int s = 0; s < 4; s++)
      for (int i = 0; i < 16; i++)
        for (int j = 0; j < 16; j++) begin
          if (s == 3 && i == 15 && j == 15) continue; // the illegal combination
          sel = s[1:0]; a = i[3:0]; b = j[3:0];
          case (sel)
            2'd0: expected = a & b;
            2'd1: expected = a | b;
            2'd2: expected = a ^ b;
            default: expected = ~a;
          endcase
          #1;
          if (y !== expected) failures++;
        end
    if (failures == 0) $display("[SIM_RESULT] PASSED");
    else $display("[SIM_RESULT] FAILED");
    $finish;
  end
endmodule
"""
_COUNT = 8


def _write_project(root: Path) -> None:
    """A one-Target Verilator Project whose single test checks `dut` exhaustively."""
    data = root / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text('[project]\nname = "mutation-equivalence-smoke"\n')
    (data / "tests.toml").write_text('[sim]\ntests = ["check"]\n')
    (root / "rtl").mkdir()
    (root / "rtl" / "dut.sv").write_bytes(_DUT.encode())
    (root / "top.sv").write_bytes(_TESTBENCH.encode())
    core = {
        "name": "booley:smoke:mutation_equivalence:1",
        "filesets": {
            "rtl": {"files": [{"rtl/dut.sv": {"file_type": "systemVerilogSource"}}]},
            "tb": {"files": [{"top.sv": {"file_type": "systemVerilogSource"}}], "tags": ["tb"]},
        },
        "targets": {
            "sim": {
                "flow": "sim",
                "filesets": ["rtl", "tb"],
                "toplevel": "top",
                "flow_options": {
                    "tool": "verilator",
                    "make_options": ["-j2"],
                    "verilator_options": ["--timing", "-Wno-fatal", "--main", "--exe"],
                },
            }
        },
    }
    (root / "smoke.core").write_text("CAPI=2:\n" + yaml.safe_dump(core))


def _seed_with_a_default_mutant_in_the_selection(root: Path) -> int:
    """A seed whose first ``_COUNT`` candidates include a Mutant on the default line."""
    for seed in range(50):
        first = generate_mutants(root, ["rtl/dut.sv"], _COUNT, seed).mutants
        if any(mutant.line == _DEFAULT_LINE for mutant in first):
            return seed
    pytest.fail("no seed puts a default-line Mutant among the first candidates")


def test_real_verilator_build_drops_the_mutant_that_changes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    reset_cache()
    _write_project(tmp_path)
    seed = _seed_with_a_default_mutant_in_the_selection(tmp_path)
    candidates = generate_mutants(tmp_path, ["rtl/dut.sv"], 10_000, seed).mutants
    default_line_ids = {m.id for m in candidates if m.line == _DEFAULT_LINE}

    result = MutationFlow().execute(
        MutationRequest(
            target="sim",
            work_dir=tmp_path,
            scope="rtl/dut.sv",
            count=_COUNT,
            seed=seed,
            min_detected=0,
            timeout_ms=900_000,
            report_dir=tmp_path / "reports",
        )
    )

    detail = result.outcome.detail
    assert result.exit_code == 0, result.outcome.report_text
    classified_ids = {row["id"] for row in detail["classified"]}
    assert len(detail["classified"]) == _COUNT  # replacements filled the dropped slots
    # The Mutants that change the default (a constant and a deletion) build the baseline's
    # C++ except for the private path in `$error`'s VL_STOP_MT: dropped, not scored.
    dropped = set(detail["equivalent_dropped"])
    assert dropped, result.outcome.report_text
    assert dropped & default_line_ids
    assert not dropped & classified_ids
    assert not default_line_ids & classified_ids  # no Mutant of the dead default is scored
    # A Mutant that changes the behavior was built, simulated and detected.
    assert any(row["status"] == "detected" for row in detail["classified"])
    assert detail["identity"]["verilator_version"].startswith("Verilator ")
    for mutant_id in dropped:
        assert mutant_id not in result.outcome.report_text
