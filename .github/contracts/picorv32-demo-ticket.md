---
summary: Add opt-in RV32 Zbb PCPI co-processor
type: feature
branch: main
scope:
  - picorv32.v
  - testbench.v
  - testbench_wb.v
  - testbench_zbb_disabled.v [new]
  - Makefile
  - tests/zbb.S [new]
spec: /opt/riscv-docs/riscv-isa-manual.html
on_success:
  destination: review
  merge: true
  cleanup: true
  triage_report: true
priority: medium
created: "2026-08-27T09:18:38Z"
base_sha: a473fc8fca393771d83b0ffcf0b14db3393339d8
target_contract:
  schema: 3
  outer_sha: a473fc8fca393771d83b0ffcf0b14db3393339d8
  project_sha: 0c7329ccfeaeac012a05ff71bb1b53561950c37c
  surface_digest: 2ed7e14ee7006521bbb5dc5ee5c1a0a0609f19a9fb234bdb0a5ed33db35d3834
  targets: [lint_core, sim_core, sim_wb, sim_zbb_disabled, synth_core, synth_core_zbb]
  bindings: [{flow: lint, criterion: lint_clean, baseline: "booley::picorv32-impl:0#lint_core", candidate: "booley::picorv32-impl:0#lint_core"}, {flow: sim, criterion: mutation_score, baseline: "booley::picorv32:0#sim_core", candidate: "booley::picorv32:0#sim_core"}, {flow: sim, criterion: sim_pass, baseline: "booley::picorv32:0#sim_core", candidate: "booley::picorv32:0#sim_core"}, {flow: sim, criterion: sim_pass, baseline: "booley::picorv32:0#sim_wb", candidate: "booley::picorv32:0#sim_wb"}, {flow: sim, criterion: sim_pass, baseline: "booley::picorv32:0#sim_zbb_disabled", candidate: "booley::picorv32:0#sim_zbb_disabled"}, {flow: synth, criterion: synthesis_ok, baseline: "booley::picorv32-impl:0#synth_core", candidate: "booley::picorv32-impl:0#synth_core_zbb"}]
  participants: [{role: outer, sealed_sha: a473fc8fca393771d83b0ffcf0b14db3393339d8, ticket_ref: refs/heads/main, destination_ref: refs/heads/main, destination_sha: a473fc8fca393771d83b0ffcf0b14db3393339d8}, {role: project, sealed_sha: 0c7329ccfeaeac012a05ff71bb1b53561950c37c, ticket_ref: refs/heads/ci/agent-ticket-contract, destination_ref: refs/heads/main, destination_sha: 27da6589e5d69701085806806f56c5861a09d62d}]
  surface_entries: [{path: .booley_project/booley.toml, kind: target-selection, sha256: fe90ccabd2407f49e7d2e0636bebfe107f6cbc7ba90c7970da6582aa2f9d29e8}, {path: .booley_project/cores/constraints/asic_core.sdc, kind: constraint, sha256: 3b887fed090b9b7be4386659c6f9bb11f1f7177468695f77f6a540aa10246e01}, {path: .booley_project/cores/constraints/fpga_core.xdc, kind: constraint, sha256: b09e9cb7ff4a56c82dbc8dc73c7a1f0d6e04eaa0c0706a69fd9fce23ef06848a}, {path: .booley_project/cores/picorv32_impl.core, kind: core, sha256: af48c851363f39e85f265362a3c383b3960512fd7709e3196b49da6de1bf26fc}, {path: .booley_project/cores/picorv32_sim.core, kind: core, sha256: ec7bd90200dc7605a2b8e5d3004eb76eb9783c7c1c5b2e9236685d62e9ab2e0e}, {path: .booley_project/tests.toml, kind: tests, sha256: 43789a73df781ab10edbc9184628656573181cc1307e644bef4a930c4b910b85}]
---

## Criteria

### Mandatory

- **lint_clean**: `lint_core`
- **sim_pass**: `testbench.v @ sim_core @ main @ pass -> pass`, `testbench.v @ sim_core @ axi @ pass -> pass`, `testbench_wb.v @ sim_wb @ wb @ pass -> pass`, `testbench_zbb_disabled.v @ sim_zbb_disabled @ zbb_disabled @ pass -> pass`
- **review_rtl_bugs**
- **review_tb_quality**
- **synthesis_ok**: `json:{"targets":[{"baseline":"synth_core","candidate":"synth_core_zbb"}],"cell_count_increase_at_most":11,"critical_path_ps_increase_at_most":3}`

### Optional

- **review_rtl_spec**
- **mutation_score**: `json:[{"target":"sim_core","scope":["picorv32.v"],"min_detected":14,"total":15}]`

## Description

### Current State

PicoRV32 implements internal PCPI multiplier and divider units in `picorv32.v`, with arbitration in the core. The AXI4-Lite and Wishbone wrappers forward core configuration parameters. The existing self-checking firmware regressions cover the plain/AXI and Wishbone paths. No Zbb support, disabled-mode testbench, Zbb-specific Target, or enabled-mode QoR configuration exists.

### Required Changes

Implement ratified RV32 Zbb 1.0 as an internal `picorv32_pcpi_zbb` co-processor using standard instruction encodings. Support ANDN, ORN, XNOR, CLZ, CTZ, CPOP, MIN, MINU, MAX, MAXU, SEXT.B, SEXT.H, ZEXT.H, ROL, ROR, RORI, ORC.B, and REV8.

Expose an `ENABLE_ZBB` parameter, defaulting to `0`, through the core and both wrappers. When enabled, recognized Zbb instructions must complete through PCPI with a registered fixed one-cycle response. When disabled, the same encodings must remain unsupported and take the existing illegal-instruction trap path.

Update native decode/trap handling so standard Zbb encodings that overlap base ALU opcode classes are routed to PCPI rather than accepted as base instructions. Preserve RV32IMC behavior when Zbb is disabled.

Add a directed Zbb assembly regression, build it with Zbb assembler support, and run it on both existing bus testbenches. Add a standalone self-checking disabled-mode testbench that executes a valid Zbb encoding with Zbb disabled and passes only on the expected illegal-instruction trap. The sealed Target contract supplies the `sim_zbb_disabled` Target and the Zbb-enabled synthesis Target.

### Affected Interfaces

`ENABLE_ZBB` is a new opt-in public parameter on `picorv32`, `picorv32_axi`, and `picorv32_wb`. No external PCPI ports change.

## Implementation Plan

### Approach

Add a `picorv32_pcpi_zbb` module in the flat RTL source and integrate it into existing internal PCPI arbitration. Decode only ratified RV32 Zbb encodings, hold and complete via the standard PCPI protocol, and return one registered result cycle after acceptance. Keep the parameter disabled by default; the existing regression testbenches and the sealed `synth_core_zbb` Target explicitly enable it.

### Implementation Steps

1. Add `ENABLE_ZBB` parameter plumbing to the core and AXI/Wishbone wrappers.
2. Implement Zbb decode, result logic, PCPI handshake, and arbitration in `picorv32.v`; adjust base decoder classification so Zbb encodings reach PCPI.
3. Enable Zbb in the AXI/plain and Wishbone regression instantiations while retaining the default parameter value of zero.
4. Add `tests/zbb.S`, covering every RV32 Zbb instruction and specified boundary operands, and update the Makefile to assemble it with Zbb support.
5. Add `testbench_zbb_disabled.v`, checking that a valid Zbb word traps when disabled.
6. Run the sealed lint, simulation, synthesis, review, and mutation criteria.

### Interface Changes

New `ENABLE_ZBB` boolean parameter, default `0`, propagated through all public wrappers. Internal PCPI wires connect the new co-processor; no external port changes.

### Edge Cases & Risks

Count operations must return 32 for zero inputs where specified. Rotate amounts use RV32 low five-bit semantics, including 0 and 31. Signed/unsigned min/max must differ correctly at sign boundaries. Byte/halfword extensions, `orc.b`, and `rev8` require exact lane behavior. Zbb priority/arbitration must not interfere with M-extension PCPI units or external PCPI use.

### Verification

Run enabled-mode regressions on the plain/AXI and Wishbone paths; run the disabled-mode trap regression; lint the wrapper; review RTL bugs, specification compliance, and testbench quality; compare the Zbb-enabled candidate against the frozen baseline with no more than 11% cell-count growth and no more than 3% critical-path increase; require at least 14 of 15 mutations to be detected.

### Open Questions

None.
