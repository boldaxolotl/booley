"""``booley init --scaffold <name>`` — a runnable starter IP for a fresh repo.

Everything else in init assumes an *existing* RTL project being ported to
Booley; this step covers the opposite case, designing an IP from scratch. It
asks nothing: the design choices (simulator, testbench style, lint EDA tool,
ASIC/FPGA flows, Stealth) arrive as flags — ``/booley-setup new`` asks the user
and passes the answers — and a missing flag takes the default init reports. It
emits a minimal but *working* project — parameterized counter RTL, a
self-checking testbench, a `.core` with one Target per enabled flow, and
populated ``booley.toml``/``tests.toml`` — so ``booley doctor`` and the
simulate/lint/synth flows are green before the user writes a single line.

Placeholder-shaped but real on purpose: empty stubs cannot pass doctor's core
audit or the sim-verdict check, so a from-scratch user's first contact with
Booley would be a wall of red. A tiny green design they mutate is the better
starting point.

The scaffold refuses to run when the repo already contains design files
(``.core``/RTL) or a populated config — scaffolding into a live project is a
porting job, and ``docs/user/SETUP.md`` owns that path. What a completed plain
``booley init`` wrote (the ``[agent]`` table, the Stealth footprint) does not
count: the scaffold keeps ``[agent]`` and, for an open scaffold, removes the
still-empty hidden layout so ``.booley/`` can be committed normally.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from booley.core.booley_dir import BOOLEY_DIR_NAME
from booley.harness.setup.common import (
    InitContext,
    err,
    info,
    ok,
    warn,
)
from booley.harness.setup.plan import (
    FilesystemMutation,
    InitFilesystemRequest,
    InitPlan,
    InitPlanBlockedError,
    InitPreconditionError,
    InitTarget,
    apply_init_plan,
    plan_init_filesystem,
)
from booley.harness.setup.project_origin import record_scaffold_origin
from booley.runtime.booley_dir import reset_cache
from booley.runtime.docs_links import docs_url, sandbox_doc_path
from booley.runtime.git import git_exclude_entries, remove_git_excludes
from booley.runtime.project_repositories import is_standalone_git_repository, run_git

# Choice vocabulary. `sv` = a plain self-checking SystemVerilog TB scored by
# stdout sentinels; `cocotb` = a Python TB per ADR 0034 (verdict from
# results.xml).
SIM_EDA_TOOLS = ("verilator", "icarus")
TB_STYLES = ("sv", "cocotb")
LINT_EDA_TOOLS = ("verilator", "verible")

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")

# The ``description:`` texts only the scaffold authors. Doctor recognises a
# scaffold-authored core by these (shared, so renderer and recognizer cannot
# drift), never by path or name.
_DESIGN_DESCRIPTION = "{name} starter IP (scaffolded by booley init --scaffold)"
_SELFTEST_DESCRIPTION = "Doctor-only fail-path fixture for {name}"
_SCAFFOLD_DESCRIPTIONS = tuple(
    re.compile(re.escape(template).replace(re.escape("{name}"), "[a-z][a-z0-9_]*") + r"\Z")
    for template in (_DESIGN_DESCRIPTION, _SELFTEST_DESCRIPTION)
)

# Where a Stealth scaffold puts Booley-authored cores (ADR 0036): the one
# scanned subtree of the Booley Directory, spelled repository-root-relative.
_STEALTH_CORE_DIR = ".booley/cores/"


def is_scaffold_core_description(description: object) -> bool:
    """True when *description* is exactly one of the texts the scaffold renders."""
    return isinstance(description, str) and any(
        pattern.match(description) for pattern in _SCAFFOLD_DESCRIPTIONS
    )


@dataclass(frozen=True)
class ScaffoldChoices:
    """Resolved scaffold answers — the full input of :func:`scaffold_files`."""

    name: str
    sim_eda_tool: str  # one of SIM_EDA_TOOLS
    tb_style: str  # one of TB_STYLES
    lint_eda_tool: str  # one of LINT_EDA_TOOLS
    asic: bool  # emit asic_synthesize Target
    fpga_part: str | None  # Vivado part → emit fpga Target + XDC; None = skip
    synth_mode: str = "physical"
    stealth: bool = False  # --stealth: hidden footprint, cores under .booley/cores/


class _ScaffoldHostProbe:
    def is_tracked(self, _root: Path, _path: Path) -> bool:
        return False


def _plan_scaffold_files(ctx: InitContext, files: dict[str, str]) -> InitPlan:
    targets: list[InitTarget] = []
    config_paths = frozenset({".booley/booley.toml", ".booley/tests.toml"})
    for rel, content in sorted(files.items()):
        target = ctx.project_root / rel
        recognized: tuple[str, ...] = ()
        if rel in config_paths and target.is_file():
            recognized = (hashlib.sha256(target.read_bytes()).hexdigest(),)
        targets.append(
            InitTarget.write_file(
                target,
                content.encode(),
                owner_marker=None,
                reason="write the selected starter project",
                recognized_legacy_digests=recognized,
            )
        )
    return plan_init_filesystem(
        InitFilesystemRequest(root=ctx.project_root, targets=tuple(targets)),
        _ScaffoldHostProbe(),
    )


def _apply_scaffold_files(ctx: InitContext, plan: InitPlan) -> bool:
    try:
        apply_init_plan(plan)
    except (InitPlanBlockedError, InitPreconditionError) as exc:
        err(f"scaffold filesystem changed before apply: {exc}")
        ctx.record("scaffold", "err", "filesystem precondition changed")
        return False
    for action in plan.actions:
        if action.mutation is not FilesystemMutation.NONE:
            ok(f"wrote {action.path.relative_to(ctx.project_root).as_posix()}")
    return True


def _scaffold_inputs_available(ctx: InitContext) -> bool:
    design_files = existing_design_files(ctx.project_root)
    if design_files:
        listing = ", ".join(str(path) for path in design_files)
        err(
            f"repo already contains design files ({listing}) — scaffold only runs in "
            "a fresh repo. To onboard an existing project, run plain `booley init` "
            f"and follow {docs_url('SETUP.md')}"
        )
        ctx.record("scaffold", "err", "existing design files")
        return False
    for cfg_name in ("booley.toml", "tests.toml"):
        cfg = ctx.project_root / ".booley" / cfg_name
        # A completed plain `booley init` wrote `[agent]` into booley.toml.
        if _config_is_populated(
            cfg, ignored_tables=("agent",) if cfg_name == "booley.toml" else ()
        ):
            err(
                f"{cfg} is already populated — scaffold would overwrite deliberate "
                "config. Remove it first if you really want a fresh start."
            )
            ctx.record("scaffold", "err", f"{cfg_name} already populated")
            return False
    return True


# ---------------------------------------------------------------------------
# Choices — flags win; a missing flag takes the default init reports
# ---------------------------------------------------------------------------


def _ask_yes_no(question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"  {question} [{hint}]: ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("    answer y or n")


def _resolve_synth_mode(args, asic: bool) -> str | None:
    explicit = getattr(args, "synth_mode", None)
    if explicit is not None and not asic:
        err("--synth-mode conflicts with --no-asic; enable ASIC synthesis or omit --synth-mode")
        return None
    if asic and explicit is None:
        info("Synthesis mode: physical (default; set --synth-mode)")
    return "physical" if explicit is None else explicit


def _flag_or_default(args, flag: str, default: str, label: str) -> str:
    """The value of ``--<flag>``, or *default* with a line saying so."""
    flagged = getattr(args, flag, None)
    if flagged:
        return str(flagged)
    info(f"{label}: {default} (default; set --{flag.replace('_', '-')})")
    return default


def gather_scaffold_choices(args, ctx: InitContext) -> ScaffoldChoices | None:
    """Resolve the scaffold answers from CLI flags and the documented defaults.

    Init asks no design questions (``/booley-setup new`` does, and passes every
    answer as a flag). A flag always wins; each missing one takes its default
    and init prints which default it used, so a bare ``--scaffold`` run is
    never a silent guess.

    Returns ``None`` (after printing the error) on an invalid name or a
    flag-forced combination the runtime rejects.
    """
    name = str(getattr(args, "scaffold", "") or "")
    if not _NAME_RE.fullmatch(name):
        err(
            f"scaffold name {name!r} is not a valid module name — "
            "use lowercase [a-z][a-z0-9_]* (it becomes the RTL module, VLNV, and file names)"
        )
        return None

    sim_eda_tool = _flag_or_default(args, "sim_eda_tool", "verilator", "Simulator")
    if sim_eda_tool not in SIM_EDA_TOOLS:
        err(
            f"simulator {sim_eda_tool!r} is not supported by the Sandbox; "
            "choose verilator or icarus (Xcelium/VCS are future roadmap work)"
        )
        return None

    tb_style = _flag_or_default(args, "tb_style", "sv", "Testbench style")

    lint_eda_tool = _flag_or_default(args, "lint_eda_tool", "verilator", "Lint EDA tool")
    if lint_eda_tool not in LINT_EDA_TOOLS:
        err(
            f"lint EDA tool {lint_eda_tool!r} is not supported; "
            f"choose one of: {', '.join(LINT_EDA_TOOLS)}"
        )
        return None

    asic_flag = getattr(args, "asic", None)
    if asic_flag is not None:
        asic = bool(asic_flag)
    else:
        info("ASIC synthesis: enabled (default; set --no-asic to skip)")
        asic = True

    synth_mode = _resolve_synth_mode(args, asic)
    if synth_mode is None:
        return None

    fpga_part = getattr(args, "fpga_part", None)
    if fpga_part is None:
        info("FPGA implementation: skipped (default; set --fpga-part to enable)")

    stealth = bool(getattr(args, "stealth", False))
    if not stealth:
        info("Stealth: off (default; set --stealth)")

    return ScaffoldChoices(
        name=name,
        sim_eda_tool=sim_eda_tool,
        tb_style=tb_style,
        lint_eda_tool=lint_eda_tool,
        asic=asic,
        fpga_part=fpga_part or None,
        synth_mode=synth_mode,
        stealth=stealth,
    )


# ---------------------------------------------------------------------------
# Refusal gate — scaffold only ever runs in a design-free repo
# ---------------------------------------------------------------------------

_RTL_SUFFIXES = {".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl"}


def existing_design_files(root: Path, limit: int = 5) -> list[Path]:
    """Design files that make the repo a *porting* job, not a scaffold target.

    Hidden trees (``.git/``, ``.booley/`` worktrees, editor state) are
    skipped — except ``.booley/cores/``, whose stealth ``.core`` files
    are real design description (ADR 0036).
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        hidden = any(part.startswith(".") for part in rel.parts[:-1])
        stealth_core = rel.parts[:2] == (".booley", "cores") and path.suffix == ".core"
        if hidden and not stealth_core:
            continue
        if not path.is_file() or rel.name.startswith("."):
            continue
        if path.suffix == ".core" or path.suffix.lower() in _RTL_SUFFIXES:
            found.append(rel)
            if len(found) >= limit:
                break
    return found


def _config_is_populated(path: Path, ignored_tables: tuple[str, ...] = ()) -> bool:
    """True when a config file holds real (or unparseable) content.

    ``booley init`` scaffolds comment-only skeletons for ``booley.toml`` /
    ``tests.toml``; those parse to an empty document and are fair game to
    replace. Anything that parses non-empty was authored on purpose — except
    the *ignored_tables* plain init itself writes (``[agent]``).
    """
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        # Recognize the init-created comment-only skeleton directly. This is
        # independent of newline convention and avoids treating a CRLF copy as
        # authored configuration on Windows.
        if not any(
            line.strip() and not line.lstrip().startswith("#") for line in content.splitlines()
        ):
            return False
        document = tomllib.loads(content)
        return any(key not in ignored_tables for key in document)
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return True  # unreadable/broken — treat as user content, refuse


# ---------------------------------------------------------------------------
# File generators (pure: choices in, {relative path: content} out)
# ---------------------------------------------------------------------------


def _counter_header(c: ScaffoldChoices, *, doctor_bad: bool) -> str:
    if doctor_bad:
        return f"""\
// Doctor-only known-bad replacement for rtl/{c.name}.sv.
//
// This file is intentionally wrong: reset loads all ones instead of zero. It
// is copied over the staged good RTL only for Doctor's bad simulation run, so
// a clean scaffold proves that its Simulation Flow detects a behavioral bug.
"""
    flows = ["simulate", "lint"]
    if c.asic:
        flows.append("synth")
    if c.fpga_part:
        flows.append("fpga")
    enabled = " / ".join(flows)
    return f"""\
// {c.name} — starter IP scaffolded by `booley init --scaffold`.
//
// A parameterized synchronous counter: small enough to read in a minute, real
// enough to exercise every configured flow ({enabled}).
// Replace it with your design and keep the surrounding wiring — the
// `.core` Targets, tests.toml entries, and testbench layout are the pattern.
//
// The timescale must match the testbench's: Verilator 5 promotes a
// mixed-presence timescale (TIMESCALEMOD) to an error under --timing.
"""


def _counter_body(c: ScaffoldChoices, *, doctor_bad: bool) -> str:
    reset = "'1;  // Deliberate Doctor self-test defect." if doctor_bad else "'0;"
    return f"""\
`timescale 1ns / 1ps

module {c.name} #(
    parameter int unsigned WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,  // asynchronous active-low reset
    input  logic             en,     // count enable
    output logic [WIDTH-1:0] count
);

  // A named constant keeps the adder operands at the same width.
  localparam logic [WIDTH-1:0] STEP = 1;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= {reset}
    end else if (en) begin
      count <= count + STEP;
    end
  end

endmodule
"""


def _counter_rtl(c: ScaffoldChoices, *, doctor_bad: bool) -> str:
    """Render the shared counter, injecting only Doctor's deliberate reset defect."""
    return _counter_header(c, doctor_bad=doctor_bad) + _counter_body(c, doctor_bad=doctor_bad)


def _rtl_source(c: ScaffoldChoices) -> str:
    return _counter_rtl(c, doctor_bad=False)


def _sim_bad_overlay(c: ScaffoldChoices) -> str:
    """A compiling RTL replacement whose reset behavior fails both scaffold tests."""
    return _counter_rtl(c, doctor_bad=True)


def _lint_bad_source(c: ScaffoldChoices) -> str:
    """A known-bad source graded as a design finding by either scaffold linter."""
    return f"""\
// Doctor-only known-bad source for the lint_selftest_bad Target.
//
// The untyped parameter is a default Verible finding; Verilator rejects the
// undeclared RHS. Keeping both in one tiny file lets either supported scaffold
// linter prove that its Lint Flow rejects a known-bad design.
module {c.name}_lint_selftest_bad #(
    parameter WIDTH = 1
) (
    input  logic clk,
    output logic value
);

  always_ff @(posedge clk) begin
    value <= undeclared_rhs;
  end

endmodule
"""


def _sv_testbench(c: ScaffoldChoices) -> str:
    return f"""\
// tb_{c.name} — self-checking testbench for the scaffold counter.
//
// Booley decides pass/fail by scanning stdout: print exactly ONE
// `[SIM_RESULT] PASSED` or `[SIM_RESULT] FAILED` line, then $finish.
// Test selection arrives as `+test_id=<index>` — the 0-based position of the
// test name in `.booley/tests.toml` `[sim_{c.name}] tests = [...]`.
`timescale 1ns / 1ps

module tb_{c.name} #(
    parameter int unsigned WIDTH = 8
);

  // 10 ns period = 100 MHz scaffold stimulus clock.
  localparam int unsigned CLK_HALF_NS = 5;

  logic             clk;
  logic             rst_n;
  logic             en;
  logic [WIDTH-1:0] count;

  int unsigned n_errors = 0;

  // The DUT instance MUST be named `dut` — coverage tooling derives the
  // `<tb_module>.dut.*` hierarchy from it (tb_style_guide.md §12).
  {c.name} #(
      .WIDTH(WIDTH)
  ) dut (
      .clk  (clk),
      .rst_n(rst_n),
      .en   (en),
      .count(count)
  );

  initial clk = 1'b0;
  always #(CLK_HALF_NS) clk = ~clk;

  // Watchdog: a hung test must FAIL, never stall the harness silently.
  initial begin
    #1ms;
    $display("ERROR: watchdog timeout");
    $display("[SIM_RESULT] FAILED");
    $finish;
  end

  // Stimulus is driven on the falling edge so it can never race the DUT's
  // rising-edge sampling. (The style guide prefers a clocking block; negedge
  // driving is the portable equivalent that Icarus also handles.)
  task automatic apply_reset();
    en    = 1'b0;
    rst_n = 1'b0;
    repeat (3) @(negedge clk);
    rst_n = 1'b1;
    @(negedge clk);
  endtask

  task automatic check(input bit cond, input string what);
    if (!cond) begin
      n_errors++;
      $display("ERROR: %s (count=%0d)", what, count);
    end
  endtask

  // Test 0 — deterministic: reset clears the counter and it holds while idle.
  task automatic test_reset();
    apply_reset();
    check(count == '0, "count not zero after reset");
    repeat (5) @(negedge clk);
    check(count == '0, "count changed while en=0");
  endtask

  // Test 1 — randomized enable pattern against a reference model, long enough
  // to cover the wrap at 2**WIDTH (random vectors AND the edge case, §8).
  task automatic test_count();
    logic [WIDTH-1:0] expected;
    apply_reset();
    expected = '0;
    for (int i = 0; i < 600; i++) begin
      en = (($urandom % 4) != 0);  // ~75% enable duty
      @(negedge clk);
      if (en) expected = expected + {{{{(WIDTH - 1) {{1'b0}}}}, 1'b1}};
      check(count == expected, "count diverged from reference model");
    end
  endtask

  initial begin
    int unsigned test_id;
    if (!$value$plusargs("test_id=%d", test_id)) test_id = 0;

    case (test_id)
      0: test_reset();
      1: test_count();
      default: begin
        n_errors++;
        $display("ERROR: unknown test_id %0d", test_id);
      end
    endcase

    $display("tb_{c.name}: test_id=%0d finished with %0d error(s)", test_id, n_errors);
    if (n_errors == 0) $display("[SIM_RESULT] PASSED");
    else $display("[SIM_RESULT] FAILED");
    $finish;
  end

endmodule
"""


def _cocotb_testbench(c: ScaffoldChoices) -> str:
    return f'''\
# test_{c.name} — cocotb testbench for the scaffold counter (Cocotb Target,
# ADR 0034). The verdict comes from cocotb's results.xml: a test passes when
# its coroutine returns and fails when it raises — assert, never print
# sentinels. One @cocotb.test() per behavior; `.booley/tests.toml`
# lists exactly these function names.

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge


async def init(dut):
    """Shared bring-up: clock, reset, defaults."""
    # Positional time unit — the keyword was renamed between cocotb 1.x/2.x.
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.en.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    # Drive on falling edges from here on, so stimulus never races the DUT's
    # rising-edge sampling.
    await FallingEdge(dut.clk)


@cocotb.test()
async def test_reset(dut):
    """Reset clears the counter and it holds at zero while disabled."""
    await init(dut)
    assert int(dut.count.value) == 0, "count not zero after reset"
    await ClockCycles(dut.clk, 5)
    assert int(dut.count.value) == 0, "count changed while en=0"


@cocotb.test()
async def test_count(dut):
    """Randomized enable pattern against a reference model, covering the wrap
    at 2**WIDTH. cocotb seeds `random` (RANDOM_SEED), so runs are reproducible."""
    await init(dut)
    width = len(dut.count)
    expected = 0
    for _ in range(600):
        en = random.random() < 0.75
        dut.en.value = int(en)
        await FallingEdge(dut.clk)
        if en:
            expected = (expected + 1) % (1 << width)
        assert int(dut.count.value) == expected, "count diverged from reference model"
'''


def _tb_fileset(c: ScaffoldChoices) -> str:
    """Render the Source-Isolation-tagged testbench fileset."""
    if c.tb_style == "cocotb":
        return f"""\
  tb:
    files:
      # Python TBs stage into the build root via copyto (ADR 0034 decision 4);
      # the basename MUST match flow_options.cocotb_module below.
      - tb/test_{c.name}.py: {{file_type: user, copyto: test_{c.name}.py}}
    tags: [tb]
"""
    return f"""\
  tb:
    files:
      - tb/tb_{c.name}.sv: {{file_type: systemVerilogSource}}
    tags: [tb]
"""


def _owns_sim_main(c: ScaffoldChoices) -> bool:
    return c.sim_eda_tool == "verilator" and c.tb_style == "sv"


def _sim_main_fileset(c: ScaffoldChoices) -> str:
    if not _owns_sim_main(c):
        return ""
    return """\
  tb_cpp:
    files:
      - tb/sim_main.cpp: {file_type: cppSource}
    tags: [tb]
"""


def _sim_main(c: ScaffoldChoices) -> str:
    return f"""// The SV testbench owns clock and stimulus. Honor Booley's tracefile argument.
// Coverage builds must retain the exactly-once write hook while the model lives.
#include "Vtb_{c.name}.h"
#include "verilated.h"
#if VM_TRACE
#include "verilated_vcd_c.h"
#endif
#if VM_COVERAGE
#include "booley_coverage.h"
#endif

int main(int argc, char** argv) {{
    VerilatedContext context;
    context.commandArgs(argc, argv);
#if VM_TRACE
    context.traceEverOn(true);
#endif
    Vtb_{c.name} top{{&context}};
#if VM_TRACE
    VerilatedVcdC trace;
    top.trace(&trace, 99);
    const char* argument = context.commandArgsPlusMatch("tracefile=");
    const char* destination = argument[0] ? argument + sizeof("+tracefile=") - 1 : "";
    if (destination[0]) trace.open(destination);
    else trace.open("dump.vcd");
#endif
    while (!context.gotFinish()) {{
        top.eval();
#if VM_TRACE
        trace.dump(context.time());
#endif
        if (context.gotFinish() || !top.eventsPending()) break;
        context.time(top.nextTimeSlot());
    }}
    top.final();
#if VM_COVERAGE
    booley_coverage_write();
#endif
#if VM_TRACE
    trace.close();
#endif
    return context.gotFinish() ? 0 : 1;
}}
"""


def _sim_flow_options(c: ScaffoldChoices) -> tuple[str, str]:
    """Return rendered simulation options and the matching toplevel."""
    sim_opts = [f"      tool: {c.sim_eda_tool}"]
    sim_opts.extend(["      booley:", "        doctor: [sim]"])
    if c.tb_style == "cocotb":
        sim_opts.append(f"      cocotb_module: test_{c.name}")
        sim_opts.append("      timescale: 1ns/1ps")
        if c.sim_eda_tool == "verilator":
            sim_opts.append("      verilator_options: [--timing, -Wno-fatal]")
        else:  # icarus; SV sources need -g2012
            sim_opts.append("      iverilog_options: [-g2012]")
        sim_toplevel = c.name  # cocotb drives the DUT directly — no HDL wrapper
    else:
        if c.sim_eda_tool == "verilator":
            sim_opts.extend(["        coverage:", "          custom_main_hooks: [write_hook]"])
            # The SV TB owns timing; the authored main owns tracing and cleanup.
            sim_opts.append("      verilator_options: [--timing, --exe]")
        elif c.sim_eda_tool == "icarus":
            sim_opts.append("      iverilog_options: [-g2012]")
        sim_toplevel = f"tb_{c.name}"  # a sim target's toplevel is its TB top
    return "\n".join(sim_opts), sim_toplevel


def _constraint_filesets(c: ScaffoldChoices) -> str:
    rendered = ""
    if c.asic and c.synth_mode == "physical":
        rendered += f"""\
  constraints:
    files:
      - constraints/{c.name}.sdc: {{file_type: SDC}}
"""
    if c.fpga_part:
        rendered += f"""\
  xdc:
    files:
      - constraints/{c.name}.xdc: {{file_type: xdc}}
"""
    return rendered


def _lint_language_option(c: ScaffoldChoices) -> str:
    """Keep SystemVerilog mode explicit for any future Icarus lint Target."""
    sv_lang_flag = ", iverilog_options: [-g2012]" if c.lint_eda_tool == "icarus" else ""
    return sv_lang_flag


def _sim_target(c: ScaffoldChoices) -> str:
    flow_options, toplevel = _sim_flow_options(c)
    filesets = "rtl, tb, tb_cpp" if _owns_sim_main(c) else "rtl, tb"
    return f"""\
  sim_{c.name}:
    flow: sim
    flow_options:
{flow_options}
    filesets: [{filesets}]
    toplevel: {toplevel}
    parameters: [WIDTH]
"""


def _lint_target(c: ScaffoldChoices) -> str:
    language_option = _lint_language_option(c)
    return f"""\
  lint_{c.name}:
    flow: lint
    flow_options: {{tool: {c.lint_eda_tool}{language_option}, booley: {{doctor: [lint]}}}}
    filesets: [rtl]
    toplevel: {c.name}
"""


def _synth_target(c: ScaffoldChoices) -> str:
    if not c.asic:
        return ""
    filesets = "rtl, constraints" if c.synth_mode == "physical" else "rtl"
    return f"""\
  synth_{c.name}:  # asic_synthesize
    flow: generic
    # arch is required by Edalize's Yosys backend even though Booley overrides the pass.
    flow_options: {{tool: yosys, arch: xilinx, synth_mode: {c.synth_mode}, booley: {{doctor: [synth]}}}}
    filesets: [{filesets}]
    toplevel: {c.name}
    parameters: [WIDTH]
"""


def _fpga_target(c: ScaffoldChoices) -> str:
    if not c.fpga_part:
        return ""
    return f"""\
  fpga_{c.name}:  # fpga_impl
    flow: generic
    flow_options: {{tool: vivado, part: {c.fpga_part}, ppa_profile: balanced, out_of_context: true, booley: {{doctor: [fpga]}}}}
    filesets: [rtl, xdc]
    toplevel: {c.name}
    parameters: [WIDTH]
"""


def _core_file(c: ScaffoldChoices) -> str:
    """Render the public scaffold design description."""
    filesets = _tb_fileset(c) + _sim_main_fileset(c) + _constraint_filesets(c)
    targets = _sim_target(c) + _lint_target(c) + _synth_target(c) + _fpga_target(c)
    return f"""\
CAPI=2:
name: ::{c.name}:0
description: {_DESIGN_DESCRIPTION.format(name=c.name)}

filesets:
  rtl:
    files:
      # copyto gives Doctor's bad overlay a stable staged destination.
      - rtl/{c.name}.sv: {{file_type: systemVerilogSource, copyto: {c.name}.sv}}
{filesets}parameters:
  WIDTH: {{datatype: int, paramtype: vlogparam, default: 8}}

targets:
{targets}"""


def _lint_selftest_core(c: ScaffoldChoices) -> str:
    """Render Doctor's tracked, non-public lint fail-path Target."""
    language_option = _lint_language_option(c)
    fixture_dir = _STEALTH_CORE_DIR if c.stealth else ""  # root-relative under Stealth
    return f"""\
CAPI=2:
name: ::{c.name}_booley_doctor_selftest:0
description: {_SELFTEST_DESCRIPTION.format(name=c.name)}

filesets:
  lint_selftest_bad:
    files:
      - {fixture_dir}booley_doctor_selftest/lint_bad.sv: {{file_type: systemVerilogSource}}

targets:
  lint_selftest_bad:
    flow: lint
    flow_options: {{tool: {c.lint_eda_tool}{language_option}, booley: {{doctor_selftest: true}}}}
    filesets: [lint_selftest_bad]
    toplevel: {c.name}_lint_selftest_bad
"""


def _stealth_toml_lines(c: ScaffoldChoices) -> list[str]:
    """The ``[stealth]`` comment and switch: off by default, on for ``--stealth``."""
    if c.stealth:
        return [
            "# Requested with `--stealth`: Booley-authored cores live under .booley/cores/",
            "# (outside the RTL repository) and commit messages with banned terms are rejected.",
            "enabled = true",
        ]
    return [
        "# Off for a scaffolded project: this repo exists to exercise Booley, so the",
        "# commit-message policy (which rejects messages naming protected terms and",
        "# machine-attribution footers) would only get in the way of honest history. Set this",
        "# to true if the repo grows into IP whose commit log must not reveal agent",
        "# involvement (a missing key also means off).",
        "enabled = false",
    ]


def _booley_toml(c: ScaffoldChoices) -> str:
    # ADR 0039: one builder (the built-in FuseSoC/Edalize flow), and every Flow
    # executes inside the Sandbox. ``enabled = false`` turns a Flow off.
    lines = [
        f"# booley.toml — generated by `booley init --scaffold` for the '{c.name}' starter IP.",
        f"# Knob reference: {sandbox_doc_path('CONFIG.md')} (Sandbox), {docs_url('CONFIG.md')} (online)",
        "",
        "[project]",
        f'name = "{c.name}"',
        "",
        "[stealth]",
        *_stealth_toml_lines(c),
        "",
        "[sandbox]",
        "# Sizes the Memory Budget that Jobs are admitted against (ADR 0087).",
        'memory = "8g"',
        "",
        "[flows]",
        "",
        "[flows.sim]",
    ] + (['trace_files = ["dump.vcd"]'] if _owns_sim_main(c) else [])
    lines += [
        "# A per-test non-RTL build step (e.g. compiling a firmware image before",
        "# each run) goes here — shell lines run inside the Sandbox, per test:",
        '#   pre_run_commands = ["make -C tests build CASE=$BOOLEY_TEST_NAME"]',
        "",
        "[flows.lint]",
    ]

    lines += ["", "[flows.synth]"]
    if not c.asic:
        lines += [
            f"# Disabled at scaffold time. To enable: add a `synth_{c.name}` Target with",
            "# doctor: [synth] metadata; physical mode also requires an SDC fileset.",
            "# Then drop the `enabled` line:",
            "enabled = false",
        ]

    if c.fpga_part:
        lines += [
            "",
            "[flows.fpga]",
            "# Register and grant the built-in Vivado policy with `booley eda` before",
            "# running this Flow. The Target uses out_of_context because the scaffolded",
            "# XDC has no board pins yet — drop it once you add LOCs.",
            "",
            "[jobs.seats]",
            "# How many Vivado licence seats you own; Booley never guesses it. Raise it",
            "# if you own more than one.",
            "vivado = 1",
        ]

    else:
        lines += ["", "[flows.fpga]", "enabled = false"]

    return "\n".join(lines) + "\n"


def _tests_toml(c: ScaffoldChoices) -> str:
    if c.tb_style == "cocotb":
        return f"""\
# tests.toml — per-Target verification-intent test lists (generated by --scaffold).

[sim_{c.name}]
# Exact @cocotb.test() function names in tb/test_{c.name}.py. No `select` —
# cocotb tests are chosen by name; a select plusarg on a Cocotb Target is a
# setup-time error.
tests = ["test_reset", "test_count"]
# Each Test Run is seeded through cocotb's seed variable, and a run that names
# no seed uses the Default Seed, 1. Rerun one seed with `--test test_reset@42`;
# set `default_seed = <n>` here to change the default.
"""
    return f"""\
# tests.toml — per-Target verification-intent test lists (generated by --scaffold).

[sim_{c.name}]
tests = ["reset", "count"]
# {{index}} = the 0-based position in `tests`, so the TB receives +test_id=<n>.
select = "+test_id={{index}}"
# Each Test Run is seeded: Verilator receives +verilator+seed+<seed>, and a
# run that names no seed uses the Default Seed, 1. Rerun one seed with
# `--test reset@42`; set `default_seed = <n>` here to change the default.
"""


def _sdc_file(c: ScaffoldChoices) -> str:
    return f"""\
# constraints/{c.name}.sdc — timing constraints for the `synth_{c.name}` Target.
# Physical synthesis requires a Target-owned SDC fileset — no silent
# default clock. 10 ns = 100 MHz scaffold clock; keep in sync with the TB.
create_clock -name clk -period 10.0 [get_ports clk]
# Only synchronous data ports carry clock-relative I/O delay. rst_n is an
# asynchronous assertion/deassertion input in this starter and is intentionally
# excluded from recovery/removal timing; replace this assumption when the reset
# crosses into a real clock domain.
set_input_delay  -clock clk 0.0 [get_ports en]
set_output_delay -clock clk 0.0 [get_ports count]
set_false_path -from [get_ports rst_n]
"""


def _xdc_file(c: ScaffoldChoices) -> str:
    return f"""\
# constraints/{c.name}.xdc — FPGA constraints for the `fpga_{c.name}` Target.
# Scaffolded for out-of-context implementation (no board pins). When targeting
# a real board, add pin LOCs/IOSTANDARDs here and drop `out_of_context` from
# [flows.fpga] in .booley/booley.toml.
create_clock -name clk -period 10.000 [get_ports clk]
"""


def scaffold_files(c: ScaffoldChoices) -> dict[str, str]:
    """All scaffolded files as {repo-relative POSIX path: content}."""
    # Open layout: cores sit in the repository. Stealth: under .booley/cores/.
    core_dir = _STEALTH_CORE_DIR if c.stealth else ""
    selftest_dir = _STEALTH_CORE_DIR if c.stealth else "verif/"
    files = {
        f"rtl/{c.name}.sv": _rtl_source(c),
        f"{core_dir}{c.name}.core": _core_file(c),
        f"{selftest_dir}booley_doctor_selftest.core": _lint_selftest_core(c),
        f"{selftest_dir}booley_doctor_selftest/lint_bad.sv": _lint_bad_source(c),
        ".booley/booley.toml": _booley_toml(c),
        ".booley/tests.toml": _tests_toml(c),
        f".booley/selftest/sim/bad-overlay/{c.name}.sv": _sim_bad_overlay(c),
    }
    if c.tb_style == "cocotb":
        files[f"tb/test_{c.name}.py"] = _cocotb_testbench(c)
    else:
        files[f"tb/tb_{c.name}.sv"] = _sv_testbench(c)
    if _owns_sim_main(c):
        files["tb/sim_main.cpp"] = _sim_main(c)
    if c.asic and c.synth_mode == "physical":
        files[f"constraints/{c.name}.sdc"] = _sdc_file(c)
    if c.fpga_part:
        files[f"constraints/{c.name}.xdc"] = _xdc_file(c)
    return files


# ---------------------------------------------------------------------------
# Hidden layout — what a default plain `booley init` left for an open scaffold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HiddenFootprint:
    """The Stealth footprint an open scaffold removes: ``.booley/.git`` and exclusions."""

    inner_repo: Path | None = None  # the empty inner repository's .git directory
    exclude_entries: tuple[str, ...] = ()  # `/.booley` lines in the honored info/exclude

    @property
    def present(self) -> bool:
        return self.inner_repo is not None or bool(self.exclude_entries)


def _inner_repo_has_history(booley_dir: Path) -> bool:
    """True unless ``booley_dir`` is a standalone repository with no commits.

    An unreadable or non-standalone repository counts as history: removing it
    could destroy something this code cannot see.
    """
    if not is_standalone_git_repository(booley_dir):
        return True
    heads = run_git(booley_dir, "rev-list", "-n", "1", "--all")
    return heads.returncode != 0 or bool(heads.stdout.strip())


def _inspect_hidden_footprint(
    ctx: InitContext, choices: ScaffoldChoices
) -> _HiddenFootprint | None:
    """Find the footprint to remove, or ``None`` after refusing (nothing changed).

    ``--stealth`` keeps the hidden layout. A symlinked (pinned) ``.booley``
    belongs to the relocated Booley Directory family and is left alone.
    """
    if choices.stealth:
        return _HiddenFootprint()
    booley_dir = ctx.project_root / BOOLEY_DIR_NAME
    exclude_entries = git_exclude_entries(ctx.project_root, (BOOLEY_DIR_NAME,))
    inner_git = booley_dir / ".git"
    if booley_dir.is_symlink():
        if exclude_entries or inner_git.exists():
            warn(f"{booley_dir} is a symlink; leaving its hidden layout as is")
        return _HiddenFootprint()
    if not inner_git.exists() and not inner_git.is_symlink():
        return _HiddenFootprint(None, exclude_entries)
    if inner_git.is_symlink() or _inner_repo_has_history(booley_dir):
        err(
            f"{booley_dir} is its own git repository with history, so an open scaffold "
            "would commit an embedded repository. Rerun with `--stealth` to keep the "
            f"hidden layout, or move or remove {inner_git} and rerun."
        )
        ctx.record("scaffold", "err", "inner repository has history")
        return None
    return _HiddenFootprint(inner_git, exclude_entries)


def _remove_hidden_footprint(ctx: InitContext, footprint: _HiddenFootprint) -> bool:
    """Remove the footprint (inner repo first, so a failure keeps the exclusion); report each part."""
    try:
        if footprint.inner_repo is not None:
            shutil.rmtree(footprint.inner_repo)
            ok(f"removed empty inner git repository {footprint.inner_repo}")
        if footprint.exclude_entries:
            remove_git_excludes(ctx.project_root, footprint.exclude_entries)
            ok("removed /.booley from git info/exclude")
    except OSError as exc:
        err(f"could not remove the hidden layout: {exc}; fix it and rerun the scaffold")
        ctx.record("scaffold", "err", "hidden layout removal failed")
        return False
    return True


def _report_hidden_footprint(footprint: _HiddenFootprint) -> None:
    if footprint.inner_repo is not None:
        warn(f"would remove empty inner git repository {footprint.inner_repo}")
    if footprint.exclude_entries:
        warn("would remove /.booley from git info/exclude")


def _keep_agent_table(files: dict[str, str], existing_config: Path) -> None:
    """Append the plain-init ``[agent]`` table verbatim to the rendered booley.toml."""
    try:
        agent = tomlkit.parse(existing_config.read_text(encoding="utf-8")).get("agent")
    except (OSError, ValueError):
        return  # unreadable config was already refused by the input gate
    if agent is None:
        return
    # Re-emit as a real `[agent]` table: an inline `agent = {...}` or dotted
    # key appended after `[flows.fpga]` would land inside that table instead.
    agent_table = tomlkit.table()
    agent_table.update(agent)
    kept = tomlkit.document()
    kept["agent"] = agent_table
    files[".booley/booley.toml"] += "\n" + kept.as_string()


# ---------------------------------------------------------------------------
# The init step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PreparedScaffold:
    """Everything the scaffold decided before touching the disk."""

    choices: ScaffoldChoices
    footprint: _HiddenFootprint
    plan: InitPlan


def _prepare_scaffold(ctx: InitContext, args) -> _PreparedScaffold | None:
    """Run every refusal in order; ``None`` means refused with nothing changed."""
    choices = gather_scaffold_choices(args, ctx)
    if choices is None:
        ctx.record("scaffold", "err", "invalid scaffold options")
        return None

    # Refusal gate: an existing design means this is a *porting* job.
    if not _scaffold_inputs_available(ctx):
        return None

    footprint = _inspect_hidden_footprint(ctx, choices)
    if footprint is None:
        return None

    files = scaffold_files(choices)
    _keep_agent_table(files, ctx.project_root / ".booley" / "booley.toml")

    plan = _plan_scaffold_files(ctx, files)
    if plan.blockers:
        for action in plan.blockers:
            err(f"scaffold would replace user-owned path {action.path}")
        ctx.record("scaffold", "err", "filesystem ownership conflict")
        return None
    return _PreparedScaffold(choices, footprint, plan)


def step_scaffold(ctx: InitContext, args) -> bool:
    """Write the starter project from the resolved choices.

    Returns True when init should proceed to the regular steps; False aborts
    the run (refusal or invalid answers) after recording an err result.
    Refusals all precede the first destructive step, so a refused run changes
    nothing.
    """

    ctx.step_banner("scaffold a new IP from scratch")

    prepared = _prepare_scaffold(ctx, args)
    if prepared is None:
        return False
    choices, footprint, plan = prepared.choices, prepared.footprint, prepared.plan

    if ctx.check_only:
        pending = [
            action for action in plan.actions if action.mutation is not FilesystemMutation.NONE
        ]
        for action in pending:
            warn(f"would write {action.path.relative_to(ctx.project_root).as_posix()}")
        _report_hidden_footprint(footprint)
        ctx.record("scaffold", "warn", f"would write {len(pending)} file(s)")
        return True

    if footprint.present and not _remove_hidden_footprint(ctx, footprint):
        return False
    if not _apply_scaffold_files(ctx, plan) or not _publish_scaffold_origin(ctx):
        return False
    reset_cache()  # .booley may have just come into existence

    summary = (
        f"sim={choices.sim_eda_tool}/{choices.tb_style} lint={choices.lint_eda_tool} "
        f"asic={'on' if choices.asic else 'off'} "
        f"synth_mode={choices.synth_mode if choices.asic else 'disabled'} "
        f"fpga={choices.fpga_part or 'off'}"
    )
    info(f"scaffolded '{choices.name}' ({summary})")
    if choices.fpga_part:
        warn(
            "fpga needs a registered and granted Vivado installation; "
            "the scaffolded XDC has no board pins"
        )
    info("commit the scaffolded files — doctor flags .core-referenced files a clone would lack")
    if choices.stealth:
        info("  commit .booley/ (incl. cores/) in its own git repository, not the RTL repo")
    info("`/booley-setup new` runs the Doctor gate in the container; then replace")
    info(f"  {choices.name} (try a sim run of test 'reset' on target 'sim_{choices.name}' first).")
    ctx.record("scaffold", "ok", summary)
    return True


def _publish_scaffold_origin(ctx: InitContext) -> bool:
    try:
        record_scaffold_origin(ctx.project_root / ".booley/booley.toml")
    except OSError as exc:
        err(f"could not record successful scaffold origin: {exc}")
        ctx.record("scaffold", "err", str(exc))
        return False
    return True
