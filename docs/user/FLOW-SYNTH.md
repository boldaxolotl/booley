# ASIC synthesis (`synth`)

`synth` gives a fast ASIC quality-of-results estimate (area, timing/Fmax,
structural problems) to iterate RTL against. It is not tape-out synthesis or
sign-off. It uses Yosys and OpenROAD on the built-in Nangate45 technology.

```bash
booley flow synth --target synth_soc
booley flow synth --target synth_soc --baseline main   # compare with another revision
```

**Constraints.** Physical synthesis (the Target's `synth_mode`) needs a
`file_type: SDC` fileset in the Target that creates at least one clock; Booley
adds no timing constraints of its own. Logical synthesis skips STA and needs no
SDC. Physical fanout limits come from OpenROAD's defaults, the library, or the
Project's SDC. OpenROAD uses an implicit maximum fanout of 50 when neither the
library nor the SDC sets a limit. For very large nets, set a suitable limit in
the Target's SDC: `set_max_fanout <n> [current_design]`.

Synthesis mode is Target-owned: there is no per-call `--synth-mode` option.

Everyday options:

| Option | Effect |
|---|---|
| `--baseline <git-ref>` | Compare with the baseline Target at another revision (the two Targets may differ). |
| `--ppa-profile <compact\|balanced\|max_frequency>` | Use a clean built-in PPA profile for this call. It replaces the Target's advanced backend settings; expert flags below still apply on top. |
| `--flatten` / `--no-flatten` | Override the Target's hierarchy flattening. |
| `--frontend <sv2v\|slang>` | Override the Target's RTL frontend, mainly for diagnosis. |

Expert options, for tuning experiments:

- Yosys/ABC: `--abc-recipe <default|balanced|fast>` or `--abc-script <script>`,
  `--generic-abc-before-mapping` / `--no-generic-abc-before-mapping`,
  `--abc-delay-ps <ps>`.
- OpenROAD: `--utilization-pct <percent>`, `--placement-density <fraction>`,
  `--repair-setup` / `--no-repair-setup`, `--repair-hold` / `--no-repair-hold`,
  `--gate-cloning` / `--no-gate-cloning`, `--setup-margin-ns <ns>`,
  `--repair-tns-percent <percent>`.

**Verdict.** The report gives area, cells, per-clock timing and Fmax, latches,
and warning counts. Combinational loops and multiple drivers in the final netlist
fail the run. Other actionable warnings give a `warn` grade but still exit `0`;
known-benign warnings are counted with a rationale. `SYNTH:{target}` is
satisfied only when synthesis completes, the final structural check ran, and
every configured threshold passes.

## Configuration (`[flows.synth]`)

```toml
[flows.synth]
timeout_ms = 5400000       # per-config cap in ms (default 1800000 = 30 min)
expected_latches = 0       # intentional latches to allow
# fail_on_timing_violation = true    # negative slack becomes a design FAIL (exit 1)
```

Persistent build-recipe inputs live on the selected Target:

```yaml
# <design>.core
filesets:
  timing_constraints:
    files:
      - util/syn/sdc/block.sdc: {file_type: SDC}
  synth_memory:
    files:
      - synth/top_ram_timing_surrogate.sv: {file_type: systemVerilogSource}
parameters:
  SYNTH_MEMORY_SURROGATE:
    datatype: bool
    paramtype: vlogdefine
    default: true
targets:
  synth:
    flow_options:
      tool: yosys
      frontend: sv2v            # sv2v (default) | slang
      # slang_options: [--single-unit]
      ppa_profile: balanced     # compact | balanced | max_frequency
      synth_mode: physical      # physical (default) | logical
      flatten: true
      advanced_settings_yosys:
        abc_recipe: balanced
        # generic_abc_before_mapping: false
        # abc_delay_ps: 3333
      advanced_settings_openroad:
        utilization_pct: 50
        placement_density: 0.80
    filesets: [rtl, timing_constraints, synth_memory]
    parameters: [SYNTH_MEMORY_SURROGATE]
    toplevel: top
```

`physical` runs Yosys followed by OpenROAD placement, optimization, parasitic
estimation, and its embedded STA. Its `area_um2` is post-optimization area and
`area_source` is `openroad_post_optimization`. Timing intent belongs in the
Target's SDC fileset. A physical Target without SDC is rejected before EDA
execution, and OpenROAD rejects an SDC that creates no clocks. Booley loads the
authored files in Target order and adds no generated timing constraints.

A memory timing surrogate is an ordinary Verilog/SystemVerilog source. Put it
in a synthesis-only fileset and let the project's RTL select it through a typed
`vlogdefine` owned by the synthesis Target, as above. Simulation and lint
Targets keep the functional memory RTL by omitting both that fileset and that
define. No manifest or custom FuseSoC file type is involved. Every read-data
bit of the surrogate depends, through registers only, on the memory's write
data, address, write enables, and byte-enables, combined bit-wise, with no
combinational write-to-read arc. Read data driven only by the address or by
constants lets synthesis delete the logic around the memory and understates
area and Fmax; Project Setup checks each surrogate with a no-pruning probe
against a black-box synthesis. The surrogate is a timing-boundary
approximation: synthesis results exclude the memory's real capacity, contents,
macro area, power, placement, and internal timing arcs.

`logical` runs only Yosys mapping. It is much faster and gives a useful mapped
area estimate plus `estimated_fmax_mhz`, calculated from ABC's longest mapped
logic delay. The frequency is not STA and is probably inaccurate because it
excludes placement and wire delays; Booley prints that warning with every
logical estimate. Its `area_source` is `yosys_mapped`. SDC is not required or
consumed in this mode. A non-empty
`advanced_settings_openroad` table with `synth_mode: logical` is a configuration
error because those settings could not affect the result.

```tcl
# util/syn/sdc/block.sdc
create_clock -name clk_i -period 25.0 [get_ports clk_i]
set_input_delay  -clock clk_i 0.0 [all_inputs]
set_output_delay -clock clk_i 0.0 [all_outputs]
set_false_path -from [get_ports mode_i]
```

Physical fanout limits come from OpenROAD's defaults, the library, or the
Project's SDC. OpenROAD uses an implicit maximum fanout of 50 when neither the
library nor the SDC sets a limit. For very large nets, set a suitable limit in
the Target's SDC: `set_max_fanout <n> [current_design]`.

For normal use, change `ppa_profile`; select `synth_mode: logical` only when the
fast mapped-area / approximate-frequency trade-off is intentional. Profiles are
EDA-tool-independent intent and can be translated by a future Genus backend as
well as the built-in Yosys/OpenROAD backend. The Target's
`advanced_settings_yosys` and `advanced_settings_openroad` subtables are expert
overrides and deliberately remain backend-specific.

| Profile | Yosys mapping | OpenROAD utilization / density |
| --- | --- | --- |
| `compact` | one default liberty-aware ABC pass | 40% / 0.65 |
| `balanced` | one balanced liberty-aware ABC pass | 50% / 0.80 |
| `max_frequency` | one fast liberty-aware ABC pass | 50% / 0.80 |

All profiles run `synth -noabc`, followed by `dfflibmap` and exactly one
liberty-aware ABC pass. `--ppa-profile` and `--flatten`/`--no-flatten` override
the Target defaults for one MCP tool call. Expert per-call flags override one
backend setting after the profile is selected. An explicit per-call profile
starts from that clean built-in profile (it does not inherit Target-level
advanced-setting overrides); per-call expert flags can then refine it.

`balanced` is the default. Select a profile explicitly for stable PPA.
`compact` uses default liberty mapping and 40%/0.65 controls. Every profile uses
grid-aligned core margins and whole-design buffer removal.
`generic_abc_before_mapping = true` enables two ABC passes; establish a fresh
physical baseline when changing the mapping recipe.

Target `flow_options.slang_options` is passed to `read_slang` verbatim.
`--single-unit` is the
common one: slang compiles each file as its own compilation unit, so a repo whose
macros come from a defines header included once at the top of the filelist (a
convention sv2v and Verilator both honor) hits "unknown macro" errors without it.

**`fail_on_timing_violation`** defaults `false`: synthesis succeeded
structurally, and many projects synthesize against placeholder constraints, so a
violation prints `RESULT: WARN -- timing VIOLATED` and exits 0. Turn it on once
the SDC is real, or an rc-only consumer (Goal evidence consumer, CI step) reads a -2.6 ns
design as success.
