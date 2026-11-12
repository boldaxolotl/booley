# ASIC synthesis (`synth`)

`synth` maps a Target's RTL to standard cells on an
[open 45 nm library](SUPPORTED-EDA-TOOLS.md) and
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

1. Check that the Project has a synthesis Target (`booley targets --for synth`
   lists them) and that it has a timing constraints file defining at least one
   clock. Project Setup normally creates both; see
   [Timing constraints](#timing-constraints) to add them yourself.
2. Ask the Developer Agent, naming the Target and, to compare, a git revision:

    > *"Synthesize `synth_core` and compare area and Fmax with `main`."*
    >
    > *"Try the `max_frequency` profile on `synth_core` and tell me what Fmax it reaches."*

3. The agent runs synthesis at the current code and, when you gave a revision,
   again at that revision, then reports the area and timing of each with the
   change in percent.

To reproduce a run yourself inside the Sandbox:

```bash
booley flow synth --target synth_core --baseline main
```

| Option | Effect |
|---|---|
| `--baseline <git-ref>` | Also synthesize the Target at that revision and add the change in percent to every row. Its timing reports are kept beside the current run's; the JSON report names both. Shared rules: [Running Flows](FLOW_REFERENCE.md#common-options). |
| `--ppa-profile <compact\|balanced\|max_frequency>` | Use this [optimization profile](#optimization-profiles) for this run. Default: the Target's `ppa_profile`, else `balanced`. |
| `--flatten` / `--no-flatten` | Flatten the hierarchy before mapping, or keep it. Default: flatten. |
| `--frontend <sv2v\|slang>` | Pick how SystemVerilog is read; switch only when the default fails. See [synth RTL frontend](SUPPORTED-EDA-TOOLS.md#synth-rtl-frontend-sv2v-vs-slang). |
| `--fresh-openroad` | Run OpenROAD even when [its results could be reused](#reusing-openroad-results), and store the new results instead. MCP: `fresh_openroad`. |

## Reading results

The run prints one row per Target, for example with `--baseline main`:

```text
baseline: main
  Target       Area       Δ       Crit path   Δ       Fmax      Setup slack   Time    Status
  ──────────   ────────   ─────   ─────────   ─────   ───────   ───────────   ─────   ──────
  synth_core   45.2 kGE   +2.7%   2,380 ps    +3.5%   420 MHz   +0.120 ns     61.0s   PASS

  synth_core: 12,345 cells, hold slack +0.020 ns, reg2reg Fmax 400 MHz
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
| `targets/<target>.json` | The Target's results. `area_kge` and `cells`; `per_clock`, one entry per clock with its `period_ns`, setup slack `wns_ns`, hold slack `whs_ns`, `critical_path_ps` and `fmax_mhz`; `reg2reg_fmax_mhz`; `conditions` with the latch, combinational-loop and multiply-driven counts; `total_warnings` and `warning_summary`, the warnings grouped with a few sample messages. With `--baseline`: `baseline` (the same metrics at that revision), and `delta_pct` (area) and `timing_delta_pct` (critical path) in percent. A logical-mode run has `estimated_fmax_mhz` instead of clock timing. |
| `artifacts/synth_<target>/run.log` | The full Yosys and OpenROAD output: every warning and error. |
| `artifacts/synth_<target>/timing/` | The STA reports, physical mode only (see [`timing_report_paths`](#configuration-flowssynth)). |
| `artifacts/synth_<target>/baseline_timing/` | With `--baseline`: the baseline run's STA reports. |

`flow-reports/synth_<target>.json`, next to `synth/`, is a copy of the latest
run's `targets/<target>.json`. A VLNV-qualified Target selector becomes a
sanitized file name with a hash suffix.

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | Synthesis finished, the netlist is clean, timing is met. | `0` |
| `WARN` | Timing is violated, the EDA tools reported actionable warnings, or the baseline turned out to be the same source. | `0` |
| `FAIL` | The design cannot be synthesized, or the final netlist has latches beyond `expected_latches`, combinational loops, multiply-driven nets, or logic that never became cells; or timing is violated while `fail_on_timing_violation = true`. | `1` |
| `ERROR` | No verdict: missing constraints, bad configuration, timeout, out of memory, or a crash. | `2` |

Warnings known to be harmless, and warnings the Yosys frontends report
against your source lines, are counted but don't cause `WARN`. A
`SYNTH:{target}` Goal is met when the run finishes with a clean netlist and
every area or timing threshold set on the Goal passes; see
Goal Mode (host doc, not shipped).

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

## Fast logical estimates

By default a Target runs physical synthesis: mapping, then placement, timing
repair and timing analysis. Set `synth_mode: logical` on the Target to stop
after mapping. It runs much faster and needs no SDC, and reports mapped area
and an estimated Fmax from the longest chain of mapped logic. That Fmax ignores
placement and wires and is usually optimistic; every run prints that warning,
and the Crit path column reads `logical estimate`.
Use logical mode for quick area trends on large designs, and physical mode
whenever timing matters. The mode belongs to the Target. There is
no per-call `--synth-mode` option, so keep one Target per mode if you need
both.

## Reusing OpenROAD results

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
are that run's, and the result counts like a fresh one, including for Goals.
SDC files that use Tcl beyond plain constraint commands (variables, `set`,
`expr`, `source`, `;`) always run OpenROAD, as do runs outside the Sandbox,
a library that includes other files, and a preloaded library (`LD_PRELOAD`). The stored results survive
cleaning the Target's build directory; `--fresh-openroad` runs OpenROAD anyway
and replaces them.

## Optimization profiles

A profile states what to optimize for. Set it on the Target as
`ppa_profile`, or try one for a single run with `--ppa-profile`:

| Profile | Intent | Placement utilization / density |
|---|---|---|
| `compact` | Smaller area. | 40% / 0.65 |
| `balanced` | Default trade-off. | 50% / 0.80 |
| `max_frequency` | Higher Fmax. | 50% / 0.80 |

Each profile also uses its own logic-optimization script during mapping. A
profile states intent, not a guaranteed result; compare runs to see which
suits your design. Name the profile on the Target so results stay comparable over
time. `--ppa-profile` replaces any expert settings on the Target for that run,
so you see the clean profile.

Expert options for tuning experiments; each one also exists as a Target
setting under the same name with underscores:

- Mapping: `--abc-recipe <default|balanced|fast>` or `--abc-script <script>`,
  `--generic-abc-before-mapping` / `--no-generic-abc-before-mapping`,
  `--abc-delay-ps <ps>`.
- Placement and timing repair (physical mode only): `--utilization-pct <percent>`,
  `--placement-density <fraction>`, `--repair-setup` / `--no-repair-setup`,
  `--repair-hold` / `--no-repair-hold`, `--gate-cloning` / `--no-gate-cloning`,
  `--setup-margin-ns <ns>`, `--repair-tns-percent <percent>`. How many paths the
  timing reports list is a project setting, not a Target setting; see
  [Configuration](#configuration-flowssynth).

After changing the mapping options, rerun the baseline with the same options;
otherwise the comparison mixes two recipes.

## Large memories

Synthesizing a large RAM as flip-flops makes area and timing meaningless, and
leaving it out lets synthesis delete the logic around it. Instead, a synthesis
Target can swap in a Memory Timing Surrogate: a small standard-cell stand-in
that keeps the memory's latency, reset behaviour and data dependence but not
its capacity. The Target selects it with a define and a synthesis-only
fileset, so simulation and lint keep the real memory:

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
look better than they are.
Project Setup checks each surrogate with a no-pruning probe against
synthesis with the memory left out. Results then exclude the memory's own
area, contents and internal timing; add the macro's figures from its
datasheet when you need a total.

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
placeholders. Turn it on once they are real, so a CI step or Goal that reads
only the exit code doesn't treat a design missing timing as success.

`timing_report_paths` (integer, at least 1; a smaller value stops the run
with a configuration error) sets how many setup paths the timing reports in
`artifacts/synth_<target>/timing/` list: `overall.rpt`, `overall.csv.rpt`, the `pre_repair`
pair (written only when timing repair runs), and `reg2reg.rpt` (only when the
design has registers). Each lists the worst path to each of the N worst
endpoints, worst first; a design with fewer paths lists those it has. Worst slack, critical path, Fmax, and the verdict
still come from the single worst path.

Everything about how the design is built lives on the Target in the `.core`
file, not in `booley.toml`; the full Target, with its SDC fileset, is the
[Synthesis Target recipe](TARGETS.md#synthesis-target). Besides `tool`, the
`flow_options` this Flow reads:

- `synth_mode`: `physical` (default) or `logical`; see
  [Fast logical estimates](#fast-logical-estimates).
- `ppa_profile`: `compact`, `balanced` (default) or `max_frequency`; see
  [Optimization profiles](#optimization-profiles).
- `flatten`: `true` (default) or `false`.
- `frontend`: `sv2v` (default) or `slang`. `slang_options` passes options to
  the `slang` frontend unchanged. Add `--single-unit` when the RTL relies on
  macros from a header included only once at the top of the file list.
- `advanced_settings_yosys` and `advanced_settings_openroad`: the expert
  options of [Optimization profiles](#optimization-profiles). Setting
  `advanced_settings_openroad` on a logical Target is an error, since it would
  have no effect.
