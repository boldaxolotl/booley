# ASIC synthesis (`synth`)

`synth` maps a Target's RTL to the standard cells of a cell library, the
[open 45 nm library](SUPPORTED-EDA-TOOLS.md), and
reports area, cell count, per-clock timing and Fmax. It also fails the run on
structural problems such as unintended latches and combinational loops. Use it
while you change RTL to see whether a change makes the design smaller or
faster, usually by comparing against another revision. It is an estimate, not
sign-off: there is no clock tree, no routing and a single process corner, wire
delays are estimated from placement, and power is not measured. Trust the
direction and rough size of a change between two runs; expect absolute numbers
to differ from your foundry flow. For what all Flows share (choosing Targets,
options, logs, exit codes), see [Running Flows](FLOW_REFERENCE.md).

## Running synthesis

1. **Check for a synthesis Target.** `booley targets --for synth` lists
   them. Each needs a timing constraints file that defines at least one
   clock. Project Setup normally creates both; to add them yourself, name the
   Target `synth_<subject>` and see [Timing constraints](#timing-constraints)
   and the [Synthesis Target recipe](TARGETS.md#synthesis-target).
2. **Ask the agent**, naming the Target and, to compare, a git revision:

    > *"Synthesize `synth_core` and compare area and Fmax with `main`."*
    >
    > *"Try the `max_frequency` profile on `synth_core` and tell me what Fmax it reaches."*

3. **Read the result.** The agent runs synthesis at the current code and,
   when you gave a revision, again at that revision, then reports the area,
   timing and verdict of each Target with the change in percent.

To reproduce a run yourself inside the Sandbox:

```bash
booley flow synth --target synth_core --baseline main
```

The Target's `flow_options` set how it is built. Most also have an option that
overrides the Target for one run; with `--baseline`, the override applies to
the baseline build too:

| Target setting | Option for one run | Effect |
|---|---|---|
| `synth_mode` | none | `physical` (default) runs placement and timing; `logical` stops after mapping. See [Fast logical estimates](#fast-logical-estimates). |
| `ppa_profile` | `--ppa-profile <compact\|balanced\|max_frequency>` | What to optimize for. Default: `balanced`. See [Optimization profiles](#optimization-profiles). |
| `flatten` | `--flatten` / `--no-flatten` | Flatten the hierarchy before mapping, or keep it. Default: flatten. |
| `frontend` | `--frontend <sv2v\|slang>` | How SystemVerilog is read. Default: `sv2v`; switch only when it fails. See [RTL frontend](#rtl-frontend-sv2v-vs-slang). |
| `slang_options` | none | Options passed unchanged to the `slang` frontend. Add `--single-unit` when the RTL relies on macros from a header included only once at the top of the file list. |
| `advanced_settings_yosys`, `advanced_settings_openroad` | the expert options | Override single settings of the profile. See [Optimization profiles](#optimization-profiles). |
| none | `--baseline <git-ref>` | Also synthesize the Target at that revision and add the change in percent to every row. Its timing reports are kept beside the current run's; the JSON report names both. Shared rules: [Running Flows](FLOW_REFERENCE.md#common-options). |
| none | `--no-cache` | Run OpenROAD even when [its results could be reused](#reusing-earlier-runs), and store the new results instead. |

## Reading results

The run prints one row per Target. Example (Yosys and OpenROAD), with
`--baseline main`:

```text
baseline: main
  Target       Area       Δ       Crit path   Δ       Fmax      Setup slack   Time    Status
  ──────────   ────────   ─────   ─────────   ─────   ───────   ───────────   ─────   ──────
  synth_core   45.2 kGE   +2.7%   2,380 ps    +3.5%   420 MHz   +0.120 ns     61.0s   PASS

  synth_core: 12,345 cells, hold slack +0.020 ns, reg2reg Fmax 420 MHz
  synth_core: log: .booley/flow-reports/synth/3/artifacts/synth_synth_core/run.log

RESULT: PASS
```

| Column | Meaning |
|---|---|
| Area | Cell area in kGE: thousands of NAND2 gates of the same area. |
| Crit path | Delay of the worst path on the slowest clock, in ps. |
| Fmax | Highest frequency that clock's worst path would meet; the clock is named when there are several. |
| Setup slack | Worst setup slack across clocks; negative means the constraints are not met. |
| Δ | With `--baseline`: change from the baseline. |

Below the table come the cell count, worst hold slack, the register-to-register
Fmax, a warning count with the log path, and any failure reason. When the
worst path starts at an input or ends at an output, a note says so: such a
path's slack depends on the input and output delays in your constraints, not on
the logic between registers, so fix those constraints rather than the RTL.

Everything else is in the run's numbered directory (`synth/3/` above; see
[Reading results](FLOW_REFERENCE.md#reading-results) for where it lives):

| File | What it holds |
|---|---|
| `targets/<target>.json` | The Target's results. `area_kge` and `cells`; `per_clock`, one entry per clock with its `period_ns`, setup slack `wns_ns`, hold slack `whs_ns`, `critical_path_ps` and `fmax_mhz`; `reg2reg_fmax_mhz`; `conditions` with the latch, combinational-loop and multi-driven counts; `total_warnings` and `warning_summary`, the warnings grouped with a few sample messages. With `--baseline`: `baseline` (the same metrics at that revision), and `delta_pct` (area) and `timing_delta_pct` (critical path) in percent. A logical-mode run has `estimated_fmax_mhz` instead of clock timing. |
| `artifacts/synth_<target>/run.log` | The full Yosys and OpenROAD output: every warning and error. |
| `artifacts/synth_<target>/timing/` | The STA reports, physical mode only (see [`timing_report_paths`](#configuration-flowssynth)). |
| `artifacts/synth_<target>/baseline_timing/` | With `--baseline`: the baseline run's STA reports. |

For the rest of the EDA tools' output, such as the netlist and OpenROAD's
intermediate files, go to the build directory in your checkout,
`.booley/.runtime/edalize/synth/<target>/`. The next run of the same Target
overwrites it.

`flow-reports/synth_<target>.json`, next to `synth/`, is a copy of the latest
run's `targets/<target>.json`. A VLNV-qualified Target selector becomes a
sanitized file name with a hash suffix.

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | Synthesis finished, the netlist is clean, timing is met. | `0` |
| `WARN` | Timing is violated, the EDA tools reported actionable warnings, or the baseline turned out to be the same source. | `0` |
| `FAIL` | The design cannot be synthesized, or the final netlist has latches beyond `expected_latches`, combinational loops, multi-driven nets, or logic that never became cells; or timing is violated while `fail_on_timing_violation = true`. | `1` |
| `ERROR` | No verdict: missing constraints, bad configuration, timeout, out of memory, or a crash. | `2` |

A timing violation is `WARN` by default, unlike an
[FPGA build](FLOW-FPGA.md#reading-results), where it is `FAIL`: synthesis
timing is an estimate, and early constraints are often placeholders. Set
[`fail_on_timing_violation`](#configuration-flowssynth) to make it `FAIL`.

Warnings known to be harmless, and warnings Yosys reports against your own
source lines, are counted but don't cause `WARN`. A loop or multi-driven
warning is still checked and can fail the run.

## Timing constraints

A synthesis Target reads its timing constraints from SDC files in a
`file_type: SDC` fileset. At least one file must create a clock; Booley adds
no constraints of its own, so the clock periods and I/O delays you write are
what the timing numbers mean. To declare the fileset, see the
[Synthesis Target recipe](TARGETS.md#synthesis-target).

```tcl
# constraints/core.sdc
create_clock -name clk_i -period 2.5 [get_ports clk_i]
set_input_delay  -clock clk_i 0.0 [all_inputs]
set_output_delay -clock clk_i 0.0 [all_outputs]
set_false_path -from [get_ports test_mode_i]
```

Files load in the order the Target lists them. A Target without SDC stops with
an error before synthesis starts, and one whose SDC creates no clock stops
with an `ERROR` when the constraints are read. Booley sets no fanout limit: the library's limit applies, or 50 when it
sets none. If very high-fanout nets dominate timing, set your own with
`set_max_fanout <n> [current_design]`.

## Optimization profiles

A profile states what to optimize for. Set it on the Target as
`ppa_profile`, or try one for a single run with `--ppa-profile`:

| Profile | Intent | Mapping recipe (`--abc-recipe`) | Placement utilization / density |
|---|---|---|---|
| `compact` | Smaller area. | `default` | 40% / 0.65 |
| `balanced` | Default trade-off. | `balanced` | 50% / 0.80 |
| `max_frequency` | Higher Fmax. | `fast` | 50% / 0.80 |

`balanced` and `max_frequency` differ only in the mapping recipe. A
profile states intent, not a guaranteed result; compare runs to see which
suits your design. Name the profile on the Target so results stay comparable over
time. `--ppa-profile` replaces any expert settings on the Target for that run,
so you see the clean profile.

For tuning experiments, expert options override single settings of the
profile. Pass them for one run, or set them on the Target: drop the leading
`--`, write the name with underscores, and nest it under the table the
second column names.

| Option | Target table | What it changes | Default |
|---|---|---|---|
| `--abc-recipe <default\|balanced\|fast>` | `advanced_settings_yosys` | Logic-optimization script used during mapping. | From the profile |
| `--abc-script <script>` | `advanced_settings_yosys` | Your own ABC commands in place of a recipe, starting with `+` (for example `+strash;map`). Cannot be combined with `--abc-recipe`. | None |
| `--generic-abc-before-mapping` / `--no-generic-abc-before-mapping` | `advanced_settings_yosys` | Adds a generic logic-optimization pass before mapping. | Off |
| `--abc-delay-ps <ps>` | `advanced_settings_yosys` | Delay target for mapping, in ps. | None |
| `--utilization-pct <percent>` | `advanced_settings_openroad` | Share of the floorplan that cells fill. | From the profile |
| `--placement-density <fraction>` | `advanced_settings_openroad` | Target density for placement. | From the profile |
| `--repair-setup` / `--no-repair-setup` | `advanced_settings_openroad` | Repairs setup violations after placement. | On |
| `--repair-hold` / `--no-repair-hold` | `advanced_settings_openroad` | Repairs hold violations. | Off |
| `--gate-cloning` / `--no-gate-cloning` | `advanced_settings_openroad` | Lets setup repair duplicate gates. | Off |
| `--setup-margin-ns <ns>` | `advanced_settings_openroad` | Extra setup slack that repair aims for, in ns. | 0 |
| `--repair-tns-percent <percent>` | `advanced_settings_openroad` | Share of violating endpoints that setup repair works on. | OpenROAD's default |

```yaml
flow_options:
  ppa_profile: balanced
  advanced_settings_yosys: {abc_recipe: fast}
  advanced_settings_openroad: {utilization_pct: 60, gate_cloning: true}
```

The `advanced_settings_openroad` options apply in physical mode only; a
logical-mode Target that sets them is rejected. How many paths the timing
reports list is a project setting, not a Target setting; see
[Configuration](#configuration-flowssynth).

After changing the mapping options, rerun the baseline with the same options;
otherwise the comparison mixes two recipes.

## Reusing earlier runs

In physical mode Yosys always runs. OpenROAD, usually the slow part, is skipped
when everything it reads matches this Target's last completed run in this
checkout: the exact netlist Yosys wrote, the generated OpenROAD script, the
library and SDC contents, the OpenROAD build, the Sandbox image, Booley's own
build and OpenROAD's environment. That run's OpenROAD results are then used
as this run's. A comment-only or testbench-only RTL edit usually gives the same
netlist and reuses; a changed clock period, or any logic change that alters
the netlist, runs OpenROAD again.

A reused run prints `<target>: REUSED -- OpenROAD results reused from run <id>
(<time>)`, its `run.log` ends with a `BOOLEY_OPENROAD_REUSED` line, and its
JSON report has an `openroad_reuse` entry naming the producing run. The numbers
are that run's, and the result counts like a fresh one.
SDC files that use Tcl beyond plain constraint commands (variables, `set`,
`expr`, `source`, `;`) always run OpenROAD, as do runs outside the Sandbox,
a library that includes other files, and a preloaded library (`LD_PRELOAD`). The stored results survive
cleaning the Target's build directory. Pass `--no-cache`, or ask the agent
for a fresh run, to run OpenROAD anyway and replace them.

## Fast logical estimates

By default a Target runs physical synthesis: mapping, then placement, timing
repair and timing analysis. Set `synth_mode: logical` on the Target to stop
after mapping. It runs much faster and needs no SDC, and reports mapped area
and an estimated Fmax from the longest chain of mapped logic. Both are usually
optimistic. The Fmax ignores placement and wires; every run prints that
warning, and the Crit path column reads `logical estimate`. The area leaves out
the buffers and larger cells that timing repair adds in physical mode.
Use logical mode for quick area trends on large designs, and physical mode
whenever timing matters. The mode belongs to the Target. There is
no per-call `--synth-mode` option, so keep one Target per mode if you need
both.

## Large memories

`synth` does not synthesize memories. The cell library has no SRAM or
register-file macros, so Yosys would build every RAM out of flip-flops and
logic, and a large one would swamp the area and timing of the rest of the
design. For a design with large memories, `synth` measures the logic around
them instead. Results leave out each memory's own area, contents and internal
timing; add the macro's figures from its datasheet when you need a total.

Project Setup decides how each memory is handled. Its plan (Step 0, see
[Setup](SETUP.md#porting-an-existing-project-plan-then-execute)) lists every
memory it finds in a synthesis Target and picks one of:

- **Kept at the boundary:** the memory is outside the synthesized top, so its
  ports are the top's ports and the SDC constrains them.
- **Replaced by a surrogate:** Setup writes a Memory Timing Surrogate and wires
  it into the synthesis Target (Step 2).
- **Blocked:** no safe way to swap the memory out, for example an asynchronous
  read or a multiport memory with independent clocks. The plan marks that
  synthesis Target red until you resolve it.

You write a surrogate by hand only for a memory added after setup, or ask the
agent to do it with the same rules.

A Memory Timing Surrogate is a small standard-cell stand-in that keeps the
memory's ports, latency, reset behavior and data dependence but not its
capacity. The Target selects it with a define and a synthesis-only fileset,
so simulation and lint keep the real memory:

```yaml
# <design>.core
filesets:
  synth_memory:
    files:
      - synth/ram_surrogate.sv: {file_type: systemVerilogSource}
parameters:
  SYNTH_MEMORY_SURROGATE: {datatype: bool, paramtype: vlogdefine, default: true}
targets:
  synth_core:
    filesets: [rtl, timing_constraints, synth_memory]
    parameters: [SYNTH_MEMORY_SURROGATE]
```

Every read-data bit of a valid surrogate depends, through registers only, on
the write data, address, write enables and byte enables, with no combinational
write-to-read arc. A surrogate whose read data depends only on the address or
on constants lets synthesis delete the surrounding logic, so area and timing
look better than they are. Project Setup checks each surrogate it writes with
a no-pruning probe: synthesis with the surrogate must keep the same flip-flops and nearly the
same logic as synthesis with the memory left out.

## RTL frontend (sv2v vs slang)

Yosys reads the RTL through one of two frontends. Set it on the synthesis
Target as `flow_options.frontend`, or override it for one run with
`--frontend`:

- **`sv2v`** (default): converts SystemVerilog to plain Verilog first, then
  Yosys reads that.
- **`slang`**: Yosys reads SystemVerilog directly. It needs Yosys 0.67 or
  newer, which the standard image meets; see
  [the image versions](SUPPORTED-EDA-TOOLS.md#versions-in-the-sandbox-image).

Stay on `sv2v` unless it fails. Use `slang` when the design puts
parameterized interfaces on module ports, or reads an interface's parameters
hierarchically (`localparam KEEP_W = s_axis_tx.KEEP_W`); sv2v cannot convert
either. Both frontends feed the same mapping and timing steps, so the choice
does not change how area and timing are measured.

Neither frontend handles everything, so when one rejects the design, try the
other. Known failures and workarounds:
[under `slang`](TROUBLESHOOTING.md#rtl-simulates-cleanly-but-synth-rejects-it-under-slang)
and [after `sv2v`](TROUBLESHOOTING.md#yosys-rejects-transpiled-verilog-after-sv2v-succeeds).

Assertions: `sv2v` drops SVA, and `slang` reads it but Booley removes it before
mapping, so neither puts assertions into the netlist. If the design can switch
its assertions off with a define (such as `NO_ASSERTIONS`), set that define on
the synthesis Target anyway; it keeps the frontend's work small.

## Configuration (`[flows.synth]`)

Besides the shared `enabled` and `timeout_ms` (default `1800000`, 30 minutes
per Target), synthesis has three settings:

```toml
[flows.synth]
timeout_ms = 5400000
expected_latches = 0               # intentional latches allowed before FAIL
fail_on_timing_violation = false   # true: timing violations are FAIL (exit 1)
timing_report_paths = 10           # worst setup paths listed per timing report
```

Keep `fail_on_timing_violation = false` while the constraints are
placeholders. Turn it on once they are real, so a CI step that reads only the
exit code doesn't treat a design missing timing as success.

`timing_report_paths` (integer, at least 1; a smaller value stops the run
with a configuration error) sets how many setup paths the timing reports in
`artifacts/synth_<target>/timing/` list: `overall.rpt`, `overall.csv.rpt`, the `pre_repair`
pair (written only when timing repair runs), and `reg2reg.rpt` (only when the
design has registers). Each lists the worst path to each of the N worst
endpoints, worst first; a design with fewer paths lists those it has. Worst slack, critical path, Fmax, and the verdict
still come from the single worst path.

Everything about how the design is built lives on the Target in the `.core`
file, not in `booley.toml`: see the [Target settings](#running-synthesis) and
the full [Synthesis Target recipe](TARGETS.md#synthesis-target), with its SDC
fileset.
