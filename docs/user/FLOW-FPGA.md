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

## Configuration (`[flows.fpga]`)

FPGA implementation runs through Vivado inside the Sandbox. Vivado is
provisioned from the host; see the
[Vivado host-provisioning policy](SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy).

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

The `file_type: xdc` fileset is the sole constraints source and is mandatory.

Repeat runs reuse a cached result when every input (sources, constraints, part,
profile, parameters, flow options and tool versions) is identical; the report
then says `cached: true`. `--no-cache` forces a fresh run.
