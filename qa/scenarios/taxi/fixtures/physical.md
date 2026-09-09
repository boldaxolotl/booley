## Physical synthesis contract

Taxi requires both slang/Yosys logical synthesis and slang/Yosys/OpenROAD physical
synthesis. The logical path remains required for Setup, continuity, Ticket2 and
final regression, with a 0% cell-count increase limit. The separate physical
Target has both 0% cell-count and 0% critical-path increase limits.
`estimated_fmax_mhz` is not measured physical timing.

### Target and constraints

Create Project-owned persistent Target `synth_mac_10g_physical` during Setup, with the same complete canonical source closure, top `taxi_eth_mac_10g`, and every parameter in the representative MAC table. Keep `frontend: slang`; set `tool: yosys`, `synth_mode: physical`, `ppa_profile: balanced`, `flatten: true` explicitly in Target `flow_options`. Use the published Booley Session Image's physical synthesis resources and record exact Yosys/OpenROAD, liberty/technology and recipe identities. No backend-specific override or alternative library is introduced by this design. The same frozen recipe, source closure, parameter set, library and SDC apply to both sides of every comparison.

`qa` here names the suite, not a hardcoded framework Project path. Author the SDC within the run's Project-owned configuration repository (the directory resolved by `booley.runtime.project_dir`), e.g. `constraints/taxi_mac_10g.sdc`, and reference it through the new Target's fileset with `file_type: SDC`. No existing Taxi file is changed. [The documented synthesis recipe](../../../../docs/user/CONFIG.md) requires explicit Target-owned SDC for physical synthesis and adds no generated clocks.

The [pinned MAC](https://github.com/fpganinja/taxi/blob/cc70b270b910d369ab1ad7b3855e76399fd461f1/src/eth/rtl/taxi_eth_mac_10g.sv) exposes `rx_clk`, `tx_clk`, `stat_clk`, `ptp_clk`, `ptp_sample_clk`. The [pinned testbench, lines 49–73](https://github.com/fpganinja/taxi/blob/cc70b270b910d369ab1ad7b3855e76399fd461f1/src/eth/tb/taxi_eth_mac_10g/test_taxi_eth_mac_10g.py#L49) supplies 6.4 ns for the first four under 64-bit operation (156.25 MHz), and 8 ns for `ptp_sample_clk` (125 MHz). Freeze these as the physical regression constraint fixture:

Use [taxi_mac_10g.sdc](taxi_mac_10g.sdc), the executable constraint authority. It excludes clock ports from data-input delays and applies the fixed zero-delay policy below.

This is a reproducible clocked-block regression fixture, not board integration/signoff. Do not change the explicit zero external-delay assumption, blank out clocks, tie enabled PTP/statistics away, or add broad false paths to obtain a pass. The approved fixture assumes zero external input/output delay against each named clock, using explicit `-add_delay` to retain all clock-relative constraints. This is a conservative relative-comparison fixture, not a board interface budget. No asynchronous-clock exemption is implied. Record timing-path coverage and any remaining unconstrained paths explicitly. Common phase for the four equal-period clocks follows the testbench's startup pattern as a regression modeling choice, not a claim that production clocks are phase-related. Any change to clock relations, CDC exceptions or I/O timing requires a reviewed constraint-fixture amendment. Retain the original unqualified 0% timing gate semantics; do not silently strengthen it to five per-clock percentage gates. Require valid per-clock timing coverage for every clock that survives the complete enabled-design synthesis, and prove why any absent clock was optimized out instead of silently accepting missing timing.

### Setup prompt

Use the complete [Setup payload](../prompts/setup.md), which includes both synthesis Targets and their frozen constraints.

### Ordered work and Criteria

1. Setup creates and validates the fourth Target and five-clock SDC. Discovery/refresh checks cover all four Targets; explicit source and parameter equivalence compares logical/physical Targets.
2. The unchanged continuity phase runs fresh logical **and physical** synthesis before Interactive Mode. Archive physical reports and artifacts alongside the existing baseline; they belong to the same live run.
3. Verification Ticket1's implementation Scope and Criteria remain unchanged. The new physical Target is already Project-owned, so it is available in the accepted state and the later seeded Basis. Preserve it through mutation restoration.
4. Bug Fix Ticket2 retains required logical `synth_mac_10g` with 0% cell increase. Apply the critical-path comparison to a directed Acceptance Basis/candidate `synth_mac_10g_physical` `synthesis_ok` Criterion with **`cell_count_increase_at_most: 0%` and `critical_path_ps_increase_at_most: 0%`**. Both sides use the seeded Ticket Basis/candidate as usual; do not substitute the earlier clean continuity result for the declared Basis measurement. The repair Scope stays the one RTL file. No Target Plan change or new hardware edit is authorized by adding this already-existing Target to Criteria.
5. Final regression reruns both logical and fresh physical synthesis against repaired merged RTL. Archive result, normalized metrics, timing directory and source/constraint identities before cleanup; remove the additional run-owned Target/SDC with the Project.

The whole run remains eight hours. Physical work runs within the existing approved baseline, Ticket2 and final-regression phase ceilings (with the Setup addition inside its Setup ceiling); no image preparation, baseline or comparison is moved outside the clock, and no old artifact supplies a fresh required pass. Time exhaustion blocks missing physical work and makes qualification incomplete unless a trustworthy failure already makes it failed.

### Checks selected for both Taxi core platforms

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.physical.target` | Core | Resolve new physical Target; prove same full MAC sources, top and approved parameters as logical Target; explicit slang/physical/balanced/flatten configuration. | Both resolved recipes, source hashes and parameter comparison during Setup. |
| `taxi-10g-mac-port-evolution.physical.constraints` | Core | Load Project-owned SDC; create four 6.4ns clocks and one 8ns clock on exact ports. No silent timing exception or parameter change. | SDC bytes/hash, timing engine clock inventory and consumed files. |
| `taxi-10g-mac-port-evolution.physical.recipe-identity` | Core | Baseline/candidate/final use same selected library/technology and physical recipe. | Exact image/tool/library/recipe identities and pair comparison. |
| `taxi-10g-mac-port-evolution.physical.discovery-doctor` | Core | Include fourth Target in CLI/MCP identity checks and deep Doctor. | Discovery comparison and warning-free Doctor results. |
| `taxi-10g-mac-port-evolution.physical.clean-baseline` | Core | Fresh physical run on unchanged baseline completes with artifact-backed physical timing/structural/PPA evidence. | Normalized completion fields, placement/optimization/STA artifacts and source identity. |
| `taxi-10g-mac-port-evolution.physical.clock-coverage` | Core | Validate surviving clock timing coverage and expose unconstrained I/O/path limits; no fabricated missing-clock pass. | Per-clock report, optimized clock-net evidence where applicable, unconstrained-path report. |
| `taxi-10g-mac-port-evolution.physical.ticket2-cells` | Core | Directed seeded Acceptance Basis/candidate physical pair has cell-count increase at most 0%. | Fresh paired cells, Basis identity and exact Criterion comparison. |
| `taxi-10g-mac-port-evolution.ticket2.synthesis-path` | Core | Same physical pair has critical-path increase at most 0% using published unqualified timing metric semantics. | Fresh paired physical `per_clock` timing, selected representative metric and exact comparison. |
| `taxi-10g-mac-port-evolution.physical.final` | Core | Rerun successful fresh physical synthesis on final repaired merged state. | Physical result/artifacts, source/SDC/library identities and timing coverage. |
| `taxi-10g-mac-port-evolution.physical.archive-cleanup` | Core | Preserve physical reports/timing artifacts before deleting run-owned Target/SDC/Project. | Archive manifest and configuration/resource cleanup proof. |
