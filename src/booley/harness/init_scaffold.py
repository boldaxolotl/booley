"""``booley init --scaffold <name>`` — a runnable starter IP for a fresh repo.

Everything else in init assumes an *existing* RTL project being ported to
Booley; this step covers the opposite case, designing an IP from scratch. It
interviews the user (simulator, testbench style, lint EDA tool, ASIC/FPGA flows)
and emits a minimal but *working* project — parameterized counter RTL, a
self-checking testbench, a `.core` with one Target per enabled flow, and
populated ``booley.toml``/``tests.toml`` — so ``booley doctor`` and the
simulate/lint/synth flows are green before the user writes a single line.

Placeholder-shaped but real on purpose: empty stubs cannot pass doctor's core
audit or the sim-verdict check, so a from-scratch user's first contact with
Booley would be a wall of red. A tiny green design they mutate is the better
starting point.

The scaffold refuses to run when the repo already contains design files
(``.core``/RTL) or a populated config — scaffolding into a live project is a
porting job, and ``docs/SETUP.md`` owns that path.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from booley.harness.init_common import (
    InitContext,
    err,
    info,
    ok,
    warn,
)
from booley.harness.init_plan import (
    FilesystemMutation,
    InitFilesystemRequest,
    InitPlan,
    InitPlanBlockedError,
    InitPreconditionError,
    InitTarget,
    apply_init_plan,
    plan_init_filesystem,
)
from booley.runtime.project_dir import reset_cache

# Wizard vocabulary. `sv` = a plain self-checking SystemVerilog TB scored by
# stdout sentinels; `cocotb` = a Python TB per ADR 0034 (verdict from
# results.xml).
SIM_EDA_TOOLS = ("verilator", "icarus")
TB_STYLES = ("sv", "cocotb")
LINT_EDA_TOOLS = ("verilator", "verible")

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class ScaffoldChoices:
    """Resolved wizard answers — the full input of :func:`scaffold_files`."""

    name: str
    sim_eda_tool: str  # one of SIM_EDA_TOOLS
    tb_style: str  # one of TB_STYLES
    lint_eda_tool: str  # one of LINT_EDA_TOOLS
    asic: bool  # emit asic_synthesize Target + SDC
    fpga_part: str | None  # Vivado part → emit fpga Target + XDC; None = skip


class _ScaffoldHostProbe:
    def is_tracked(self, _root: Path, _path: Path) -> bool:
        return False


def _plan_scaffold_files(ctx: InitContext, files: dict[str, str]) -> InitPlan:
    targets: list[InitTarget] = []
    config_paths = frozenset({".booley_project/booley.toml", ".booley_project/tests.toml"})
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
            "and follow docs/SETUP.md."
        )
        ctx.record("scaffold", "err", "existing design files")
        return False
    for cfg_name in ("booley.toml", "tests.toml"):
        cfg = ctx.project_root / ".booley_project" / cfg_name
        if _config_is_populated(cfg):
            err(
                f"{cfg} is already populated — scaffold would overwrite deliberate "
                "config. Remove it first if you really want a fresh start."
            )
            ctx.record("scaffold", "err", f"{cfg_name} already populated")
            return False
    return True


# ---------------------------------------------------------------------------
# Wizard prompts
# ---------------------------------------------------------------------------


def _ask_choice(question: str, options: list[tuple[str, str]], default: str) -> str:
    """Numbered-menu prompt; empty input, EOF, or a bad TTY yields the default."""
    print(f"  {question}")
    for idx, (value, blurb) in enumerate(options, 1):
        marker = "  (default)" if value == default else ""
        print(f"    {idx}. {value} — {blurb}{marker}")
    while True:
        try:
            raw = input(f"  choice [{default}]: ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        for value, _ in options:
            if raw == value:
                return value
        print("    answer with a number or an option name")


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


def _ask_text(question: str, default: str = "") -> str:
    try:
        raw = input(f"  {question}: ").strip()
    except EOFError:
        return default
    return raw or default


def gather_scaffold_choices(args, ctx: InitContext) -> ScaffoldChoices | None:
    """Resolve wizard answers from CLI flags, prompts, or defaults.

    A flag always wins over a prompt (agent/CI runs pass everything as flags);
    unanswered questions fall back to prompts only on an interactive TTY
    outside ``--check-only`` — otherwise the documented defaults apply, with a
    note, matching init's "prompts are skipped when non-interactive" contract.

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

    interactive = ctx.interactive and not ctx.check_only

    def resolve(flag: str, options: list[tuple[str, str]], default: str, question: str) -> str:
        flagged = getattr(args, flag, None)
        if flagged:
            return str(flagged)
        if interactive:
            return _ask_choice(question, options, default)
        info(f"{question}: {default} (non-interactive default; set --{flag.replace('_', '-')})")
        return default

    sim_eda_tool = resolve(
        "sim_eda_tool",
        [
            ("verilator", "fast compiled sim, runs in the Booley sandbox"),
            ("icarus", "event-driven interpreter, runs in the Booley sandbox"),
        ],
        "verilator",
        "Which simulator?",
    )
    if sim_eda_tool not in SIM_EDA_TOOLS:
        err(
            f"simulator {sim_eda_tool!r} is not supported by the Session Runtime; "
            "choose verilator or icarus (Xcelium/VCS are future roadmap work)"
        )
        return None

    tb_style = resolve(
        "tb_style",
        [
            ("sv", "self-checking SystemVerilog TB, verdict via stdout sentinel"),
            ("cocotb", "Python TB (ADR 0034), verdict via cocotb results.xml"),
        ],
        "sv",
        "Which testbench style?",
    )

    lint_eda_tool = resolve(
        "lint_eda_tool",
        [
            ("verilator", "verilator --lint-only: structural/semantic checks"),
            ("verible", "verible style lint: naming/formatting conventions"),
        ],
        "verilator",
        "Which lint EDA tool?",
    )
    if lint_eda_tool not in LINT_EDA_TOOLS:
        err(
            f"lint EDA tool {lint_eda_tool!r} is not supported; "
            f"choose one of: {', '.join(LINT_EDA_TOOLS)}"
        )
        return None

    asic_flag = getattr(args, "asic", None)
    if asic_flag is not None:
        asic = bool(asic_flag)
    elif interactive:
        asic = _ask_yes_no("Enable ASIC synthesis (yosys + SDC constraints)?", default=True)
    else:
        info("ASIC synthesis: enabled (non-interactive default; set --no-asic to skip)")
        asic = True

    fpga_part = getattr(args, "fpga_part", None)
    if fpga_part is None and interactive:
        if _ask_yes_no(
            "Enable FPGA implementation (Vivado Session Runtime policy)?", default=False
        ):
            fpga_part = _ask_text("Vivado part (e.g. xc7a200tfbg484-1)")
            if not fpga_part:
                info("no part given — skipping the fpga flow")
                fpga_part = None
    elif fpga_part is None:
        info("FPGA implementation: skipped (non-interactive default; set --fpga-part to enable)")

    return ScaffoldChoices(
        name=name,
        sim_eda_tool=sim_eda_tool,
        tb_style=tb_style,
        lint_eda_tool=lint_eda_tool,
        asic=asic,
        fpga_part=fpga_part or None,
    )


# ---------------------------------------------------------------------------
# Refusal gate — scaffold only ever runs in a design-free repo
# ---------------------------------------------------------------------------

_RTL_SUFFIXES = {".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl"}


def existing_design_files(root: Path, limit: int = 5) -> list[Path]:
    """Design files that make the repo a *porting* job, not a scaffold target.

    Hidden trees (``.git/``, ``.booley_project/`` worktrees, editor state) are
    skipped — except ``.booley_project/cores/``, whose stealth ``.core`` files
    are real design description (ADR 0036).
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        hidden = any(part.startswith(".") for part in rel.parts[:-1])
        stealth_core = rel.parts[:2] == (".booley_project", "cores") and path.suffix == ".core"
        if hidden and not stealth_core:
            continue
        if not path.is_file() or rel.name.startswith("."):
            continue
        if path.suffix == ".core" or path.suffix.lower() in _RTL_SUFFIXES:
            found.append(rel)
            if len(found) >= limit:
                break
    return found


def _config_is_populated(path: Path) -> bool:
    """True when a config file holds real (or unparseable) content.

    ``booley init`` scaffolds comment-only skeletons for ``booley.toml`` /
    ``tests.toml``; those parse to an empty document and are fair game to
    replace. Anything that parses non-empty was authored on purpose.
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
        return tomllib.loads(content) != {}
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return True  # unreadable/broken — treat as user content, refuse


# ---------------------------------------------------------------------------
# File generators (pure: choices in, {relative path: content} out)
# ---------------------------------------------------------------------------


def _rtl_source(c: ScaffoldChoices) -> str:
    return f"""\
// {c.name} — starter IP scaffolded by `booley init --scaffold`.
//
// A parameterized synchronous counter: small enough to read in a minute, real
// enough to exercise every configured flow (simulate / lint / elaborate /
// synth). Replace it with your design and keep the surrounding wiring — the
// `.core` Targets, tests.toml entries, and testbench layout are the pattern.
//
// The timescale must match the testbench's: Verilator 5 promotes a
// mixed-presence timescale (TIMESCALEMOD) to an error under --timing.
`timescale 1ns / 1ps

module {c.name} #(
    parameter int unsigned WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,  // asynchronous active-low reset
    input  logic             en,     // count enable
    output logic [WIDTH-1:0] count
);

  // Named constant instead of a bare 1'b1 so the adder operands share a width
  // (a width-expansion lint finding on line one of a fresh project is a bad
  // first impression).
  localparam logic [WIDTH-1:0] STEP = 1;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= '0;
    end else if (en) begin
      count <= count + STEP;
    end
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
// test name in `.booley_project/tests.toml` `[sim] tests = [...]`.
`timescale 1ns / 1ps

module tb_{c.name} #(
    parameter int unsigned WIDTH = 8
);

  // 10 ns period = 100 MHz; keep in sync with constraints/{c.name}.sdc.
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
# sentinels. One @cocotb.test() per behavior; `.booley_project/tests.toml`
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


def _core_file(c: ScaffoldChoices) -> str:
    # Testbench filesets use the Source-Isolation ``tb`` tag. The scaffold only
    # emits Session-Runtime-supported simulators, so no vendor-specific tags or
    # broker-specific wrapper plumbing are needed.
    tb_tags = "[tb]"
    if c.tb_style == "cocotb":
        tb_fileset = f"""\
  tb:
    files:
      # Python TBs stage into the build root via copyto (ADR 0034 decision 4);
      # the basename MUST match flow_options.cocotb_module below.
      - tb/test_{c.name}.py: {{file_type: user, copyto: test_{c.name}.py}}
    tags: {tb_tags}
"""
    else:
        tb_fileset = f"""\
  tb:
    files:
      - tb/tb_{c.name}.sv: {{file_type: systemVerilogSource}}
    tags: {tb_tags}
"""

    # Sim target: one shape per (EDA tool, tb_style) cell of the wizard matrix.
    sim_opts = [f"      tool: {c.sim_eda_tool}"]
    sim_opts.extend(["      booley:", "        doctor: [sim, elab]"])
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
            # --timing: event-driven TB (Verilator 5 NEEDTIMINGOPT otherwise);
            # --main/--exe: generate + build the C++ main wrapper.
            sim_opts.append("      verilator_options: [--timing, --main, --exe]")
        elif c.sim_eda_tool == "icarus":
            sim_opts.append("      iverilog_options: [-g2012]")
        sim_toplevel = f"tb_{c.name}"  # a sim target's toplevel is its TB top
    sim_flow_options = "\n".join(sim_opts)

    constraint_filesets = ""
    if c.asic:
        constraint_filesets += f"""\
  constraints:
    files:
      - constraints/{c.name}.sdc: {{file_type: SDC}}
"""
    if c.fpga_part:
        constraint_filesets += f"""\
  xdc:
    files:
      - constraints/{c.name}.xdc: {{file_type: xdc}}
"""

    # Lint flow options mirror the sim branch's language-mode rule: the
    # scaffold's RTL is SystemVerilog throughout, and iverilog defaults to
    # Verilog-2005 — an icarus-driven target without -g2012 dies on the first
    # `logic`/`always_ff` with a syntax error that reads like a design bug.
    # LINT_EDA_TOOLS offers no icarus entry today, but non-sim targets used to be
    # exactly where this flag got forgotten, so the rule lives here (keyed on
    # the EDA tool, like the sim branch) rather than trusting the EDA-tool wizard menu.
    sv_lang_flag = ", iverilog_options: [-g2012]" if c.lint_eda_tool == "icarus" else ""
    lint_flow_options = f"{{tool: {c.lint_eda_tool}{sv_lang_flag}, booley: {{doctor: [lint]}}}}"

    targets = f"""\
  sim:
    flow: sim
    flow_options:
{sim_flow_options}
    filesets: [rtl, tb]
    toplevel: {sim_toplevel}
    parameters: [WIDTH]
  lint:
    flow: lint
    flow_options: {lint_flow_options}
    filesets: [rtl]
    toplevel: {c.name}
"""
    if c.asic:
        targets += f"""\
  synth:  # asic_synthesize
    flow: generic
    # arch is REQUIRED: edalize's yosys backend refuses to configure without
    # it. Booley's synth script overrides the pass, but keep the field.
    flow_options: {{tool: yosys, arch: xilinx, booley: {{doctor: [synth]}}}}
    filesets: [rtl, constraints]
    toplevel: {c.name}
    parameters: [WIDTH]
"""
    if c.fpga_part:
        targets += f"""\
  fpga:  # fpga_impl
    flow: generic
    flow_options: {{tool: vivado, part: {c.fpga_part}, out_of_context: true}}
    filesets: [rtl, xdc]
    toplevel: {c.name}
    parameters: [WIDTH]
"""

    return f"""\
CAPI=2:
name: ::{c.name}:0
description: {c.name} starter IP (scaffolded by booley init --scaffold)

filesets:
  rtl:
    files:
      - rtl/{c.name}.sv: {{file_type: systemVerilogSource}}
{tb_fileset}{constraint_filesets}
parameters:
  WIDTH: {{datatype: int, paramtype: vlogparam, default: 8}}

targets:
{targets}"""


def _booley_toml(c: ScaffoldChoices) -> str:
    # ADR 0039: one builder (the built-in FuseSoC/Edalize flow), and every Flow
    # executes inside the Session Runtime. ``enabled = false`` turns a Flow off.
    lines = [
        f"# booley.toml — generated by `booley init --scaffold` for the '{c.name}' starter IP.",
        "# Knob reference: docs/CONFIG.md (in the Booley repo / .booley/docs).",
        "",
        "[project]",
        f'name = "{c.name}"',
        "",
        "[stealth]",
        "# Off for a scaffolded project: this repo exists to exercise Booley, so the",
        "# commit-message sanitizer (which redacts product names like 'booley' and strips",
        "# AI-attribution trailers) would only mangle honest history. Delete this",
        "# section (stealth defaults to on) if the repo grows into IP whose commit",
        "# log must not reveal agent involvement.",
        "enabled = false",
        "",
        "[flows]",
        "",
        "[flows.sim]",
    ]
    lines += [
        "# A per-test non-RTL build step (e.g. compiling a firmware image before",
        "# each run) goes here — shell lines run inside the Session Runtime, per test:",
        '#   pre_run_commands = ["make -C tests build CASE=$BOOLEY_TEST_NAME"]',
        "",
        "[flows.lint]",
        "",
        "[flows.elab]",
    ]

    lines += ["", "[flows.synth]"]
    if not c.asic:
        lines += [
            "# Disabled at scaffold time. To enable: add a `synth` Target plus an SDC",
            "# fileset and doctor: [synth] metadata to the .core (ADR 0031 — no SDC",
            "# is a hard error), then drop the `enabled` line:",
            "enabled = false",
        ]

    if c.fpga_part:
        lines += [
            "",
            "[flows.fpga]",
            "# Register and grant the built-in Vivado policy with `booley eda` before",
            "# running this Flow. The Target uses out_of_context because the scaffolded",
            "# XDC has no board pins yet — drop it once you add LOCs.",
        ]

    return "\n".join(lines) + "\n"


def _tests_toml(c: ScaffoldChoices) -> str:
    if c.tb_style == "cocotb":
        return f"""\
# tests.toml — per-Target verification-intent test lists (generated by --scaffold).

[sim]
# Exact @cocotb.test() function names in tb/test_{c.name}.py. No `select` —
# cocotb tests are chosen by name; a select plusarg on a Cocotb Target is a
# setup-time error.
tests = ["test_reset", "test_count"]
"""
    return """\
# tests.toml — per-Target verification-intent test lists (generated by --scaffold).

[sim]
tests = ["reset", "count"]
# {index} = the 0-based position in `tests`, so the TB receives +test_id=<n>.
select = "+test_id={index}"
"""


def _sdc_file(c: ScaffoldChoices) -> str:
    return f"""\
# constraints/{c.name}.sdc — timing constraints for the `synth` Target.
# ADR 0031: a synth Target without an SDC fileset is a hard error — no silent
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
# constraints/{c.name}.xdc — FPGA constraints for the `fpga` Target.
# Scaffolded for out-of-context implementation (no board pins). When targeting
# a real board, add pin LOCs/IOSTANDARDs here and drop `out_of_context` from
# [flows.fpga] in .booley_project/booley.toml.
create_clock -name clk -period 10.000 [get_ports clk]
"""


def scaffold_files(c: ScaffoldChoices) -> dict[str, str]:
    """All scaffolded files as {repo-relative POSIX path: content}."""
    files = {
        f"rtl/{c.name}.sv": _rtl_source(c),
        f"{c.name}.core": _core_file(c),
        ".booley_project/booley.toml": _booley_toml(c),
        ".booley_project/tests.toml": _tests_toml(c),
    }
    if c.tb_style == "cocotb":
        files[f"tb/test_{c.name}.py"] = _cocotb_testbench(c)
    else:
        files[f"tb/tb_{c.name}.sv"] = _sv_testbench(c)
    if c.asic:
        files[f"constraints/{c.name}.sdc"] = _sdc_file(c)
    if c.fpga_part:
        files[f"constraints/{c.name}.xdc"] = _xdc_file(c)
    return files


# ---------------------------------------------------------------------------
# The init step
# ---------------------------------------------------------------------------


def step_scaffold(ctx: InitContext, args) -> bool:
    """Run the scaffold wizard and write the starter project.

    Returns True when init should proceed to the regular steps; False aborts
    the run (refusal or invalid answers) after recording an err result.
    """

    ctx.step_banner("scaffold a new IP from scratch")

    choices = gather_scaffold_choices(args, ctx)
    if choices is None:
        ctx.record("scaffold", "err", "invalid scaffold options")
        return False

    # Refusal gate: an existing design means this is a *porting* job.
    if not _scaffold_inputs_available(ctx):
        return False

    files = scaffold_files(choices)

    plan = _plan_scaffold_files(ctx, files)
    if plan.blockers:
        for action in plan.blockers:
            err(f"scaffold would replace user-owned path {action.path}")
        ctx.record("scaffold", "err", "filesystem ownership conflict")
        return False

    if ctx.check_only:
        pending = [
            action for action in plan.actions if action.mutation is not FilesystemMutation.NONE
        ]
        for action in pending:
            warn(f"would write {action.path.relative_to(ctx.project_root).as_posix()}")
        ctx.record("scaffold", "warn", f"would write {len(pending)} file(s)")
        return True

    if not _apply_scaffold_files(ctx, plan):
        return False
    reset_cache()  # .booley_project may have just come into existence

    summary = (
        f"sim={choices.sim_eda_tool}/{choices.tb_style} lint={choices.lint_eda_tool} "
        f"asic={'on' if choices.asic else 'off'} "
        f"fpga={choices.fpga_part or 'off'}"
    )
    info(f"scaffolded '{choices.name}' ({summary})")
    if choices.fpga_part:
        warn(
            "fpga needs a registered and granted Vivado installation; "
            "the scaffolded XDC has no board pins"
        )
    info("commit the scaffolded files — doctor flags .core-referenced files a clone would lack")
    info("after init finishes: `booley doctor --deep` should be green, then try")
    info(f"  a sim run of test 'reset' on target 'sim' — then replace {choices.name}.")
    ctx.record("scaffold", "ok", summary)
    return True
