# FPGA implementation (`fpga`)

`fpga` runs FPGA implementation through host-provisioned AMD Vivado. The Target
owns the FPGA part, top module, parameters, and XDC constraints.

```bash
booley flow fpga --target fpga_soc
booley flow fpga --target fpga_soc --ppa-profile max_frequency --baseline main
```

| Option | Effect |
|---|---|
| `--baseline <git-ref>` | Compare implementation metrics with another revision. |
| `--ppa-profile <compact\|balanced\|max_frequency>` | Override the Target's optimization intent for this call (default: Target `flow_options.ppa_profile`, then `balanced`). Applies to baseline and candidate alike. |
| `--no-cache` | Force a fresh implementation instead of reusing a matching cached result. |

| Profile | Vivado strategies | Intent |
|---|---|---|
| `compact` | `Flow_AreaOptimized_high` / `Area_Explore` | Smaller resource use. |
| `balanced` | Vivado defaults | Default trade-off. |
| `max_frequency` | `Flow_PerfOptimized_high` / `Performance_ExplorePostRoutePhysOpt` | Higher Fmax. |

A profile states intent, not a guaranteed result. Target `synth` and `pnr`
fields select Edalize engines; they are not Vivado strategy overrides.

**Verdict.** The report gives LUT/FF/BRAM/DSP utilization, routed per-clock
timing and Fmax, and critical conditions (latches, combinational loops,
multi-driven nets). `FPGA:{target}` is satisfied only when metrics are
complete, timing and configured thresholds pass, and no critical condition is
present. Power is not measured.

The run ends with one table with a row per Target: LUT, FF, BRAM, DSP, WNS,
WHS, Fmax, Time, and Status (`PASS`, `FAIL` or `ERROR`). With `--baseline`, the
change against the baseline is shown on a line under the Target's row (`Δ +1.0%`
in the LUT, FF, BRAM, and DSP columns). A metric the run did not produce shows
`—`, and a Target name over 40 characters is cut with `…`. Notes (critical
conditions, subprocess output, log paths) are listed under the table, then
`RESULT:`. On a terminal the table is coloured; the report, MCP and piped
output carry the same text without colour codes.

## Configuration (`[flows.fpga]`)

FPGA implementation runs through Vivado inside the Sandbox. The
Linux host-provisioned policy obtains a read-only registered Vivado 2025.2 release
through `[eda.vivado]`; it does not require Vivado on the Sandbox `PATH` from
the host.

`[flows.fpga]` contains execution policy. Build inputs and target selection
belong to the invocation and `.core` Target: put `part`, `out_of_context`, and
the portable `ppa_profile` under its `flow_options`, and XDC constraints in a
`file_type: xdc` fileset.

Name Booley-authored implementation Targets `fpga` or `fpga_<subject>`; that
axis declares that the FPGA Flow can drive them. `flow_options.tool` still
controls FuseSoC resolution, including `tool_<name>` conditional files, but it
does not choose the implementation backend: the FPGA Flow always builds its
own Vivado EDAM. Use `tool: vivado` normally; another Sandbox-available tool is
valid when a project deliberately needs its resolution conditions. Unprefixed
vendored Targets fall back to `tool: vivado` because their names are immutable.

```toml
[flows.fpga]
timeout_ms = 7200000
```

```yaml
# <design>.core
filesets:
  fpga_constraints:
    files:
      - constraints/top.xdc: {file_type: xdc}
      - constraints/clocks.xdc: {file_type: xdc}
targets:
  fpga:
    flow_options:
      tool: vivado
      part: xc7a100tcsg324-1
      ppa_profile: balanced     # compact | balanced | max_frequency
      out_of_context: true
    filesets: [rtl, fpga_constraints]
    toplevel: top
```

`ppa_profile` expresses portable optimization intent. It resolves per-call
`--ppa-profile`, then Target `flow_options.ppa_profile`, then `balanced`.
Booley maps `compact` to Vivado's area-oriented synthesis and implementation
strategies, leaves the existing Vivado defaults untouched for `balanced`, and
maps `max_frequency` to its characterized performance strategies. Target
`synth` and `pnr` fields are Edalize engine selectors, not strategy overrides;
the built-in FPGA Flow neither forwards nor reinterprets them as an expert
profile surface.

The `file_type: xdc` fileset is the sole constraints source and is mandatory.
There is no `[flows.fpga].xdc` key. Keeping constraints and the device part on
the Target prevents one global Flow section from applying the wrong design
intent to another Target.

Repeated runs use a Booley-owned content cache, not Make timestamps. The cache
fingerprint covers the resolved Target/EDAM, portable profile and concrete
Vivado mapping, source/header/constraint bytes,
top, part, parameters/defines, flow options, and the supported Vivado plus
Edalize/FuseSoC identities. Reuse also re-hashes the routed report set and, for
a non-OOC Target, its bitstream; a hit is reported explicitly as
`cached: true`. A miss invokes Make with `-B`, so `Nothing to be done` cannot
turn prior reports into either a false pass or a false failure.

Profiles name optimization intent, not guaranteed outcomes. FPGA area is
represented by utilization/resource counts; this Flow does not currently
normalize or compare power.
