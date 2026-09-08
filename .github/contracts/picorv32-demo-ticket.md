---
summary: Add opt-in RV32 Zbb PCPI co-processor
type: feature
branch: main
project_destination_ref: refs/heads/main
scope:
  - picorv32.v
  - testbench.v
  - testbench_wb.v
  - Makefile
  - tests/zbb.S [new]
spec: /opt/riscv-docs/riscv-isa-manual.html
on_success:
  destination: review
  merge: true
  cleanup: true
  triage_report: true
priority: medium
---

## Criteria

### Mandatory

- **lint_clean**: `lint_core`
- **sim_pass**: `testbench.v @ sim_core @ main @ pass -> pass`, `testbench.v @ sim_core @ axi @ pass -> pass`, `testbench_wb.v @ sim_wb @ wb @ pass -> pass`
- **review_rtl_bugs**
- **review_tb_quality**
- **synthesis_ok**: `json:{"targets":[{"baseline":"synth_core","candidate":"synth_core"}],"cell_count_increase_at_most":"11%","critical_path_ps_increase_at_most":"3%"}`

### Optional

- **review_rtl_spec**
- **mutation_score**: `json:[{"target":"sim_core","scope":["picorv32.v"],"min_detected":14,"total":15}]`

## Description

### Current State

PicoRV32 implements internal PCPI multiplier and divider units in `picorv32.v`, with arbitration in the core. The AXI4-Lite and Wishbone wrappers forward core configuration parameters. The existing self-checking firmware regressions cover the plain/AXI and Wishbone paths. No Zbb support exists.

### Required Changes

Implement ratified RV32 Zbb 1.0 as an internal `picorv32_pcpi_zbb` co-processor using standard instruction encodings. Support ANDN, ORN, XNOR, CLZ, CTZ, CPOP, MIN, MINU, MAX, MAXU, SEXT.B, SEXT.H, ZEXT.H, ROL, ROR, RORI, ORC.B, and REV8.

Expose an `ENABLE_ZBB` parameter, defaulting to `0`, through the core and both wrappers. When enabled, recognized Zbb instructions must complete through PCPI with a registered fixed one-cycle response. When disabled, the same encodings must remain unsupported and take the existing illegal-instruction trap path.

Update native decode/trap handling so standard Zbb encodings that overlap base ALU opcode classes are routed to PCPI rather than accepted as base instructions. Preserve RV32IMC behavior when Zbb is disabled.

Add a directed Zbb assembly regression, build it with Zbb assembler support, and run it on both existing bus testbenches. Use the public Project's existing simulation, lint, and synthesis Targets; this CI-only Ticket must not require private Target configuration.

### Affected Interfaces

`ENABLE_ZBB` is a new opt-in public parameter on `picorv32`, `picorv32_axi`, and `picorv32_wb`. No external PCPI ports change.

## Implementation Plan

### Approach

Add a `picorv32_pcpi_zbb` module in the flat RTL source and integrate it into existing internal PCPI arbitration. Decode only ratified RV32 Zbb encodings, hold and complete via the standard PCPI protocol, and return one registered result cycle after acceptance. Keep the parameter disabled by default; the existing regression testbenches explicitly enable it in their scoped test configurations.

### Implementation Steps

1. Add `ENABLE_ZBB` parameter plumbing to the core and AXI/Wishbone wrappers.
2. Implement Zbb decode, result logic, PCPI handshake, and arbitration in `picorv32.v`; adjust base decoder classification so Zbb encodings reach PCPI.
3. Enable Zbb in the AXI/plain and Wishbone regression instantiations while retaining the default parameter value of zero.
4. Add `tests/zbb.S`, covering every RV32 Zbb instruction and specified boundary operands, and update the Makefile to assemble it with Zbb support.
5. Run the recorded lint, simulation, synthesis, review, and mutation criteria.

### Interface Changes

New `ENABLE_ZBB` boolean parameter, default `0`, propagated through all public wrappers. Internal PCPI wires connect the new co-processor; no external port changes.

### Edge Cases & Risks

Count operations must return 32 for zero inputs where specified. Rotate amounts use RV32 low five-bit semantics, including 0 and 31. Signed/unsigned min/max must differ correctly at sign boundaries. Byte/halfword extensions, `orc.b`, and `rev8` require exact lane behavior. Zbb priority/arbitration must not interfere with M-extension PCPI units or external PCPI use.

### Verification

Run enabled-mode regressions on the plain/AXI and Wishbone paths; lint the wrapper; review RTL bugs, specification compliance, and testbench quality; compare the public synthesis Target against its frozen baseline with no more than 11% cell-count growth and no more than 3% critical-path increase; require at least 14 of 15 mutations to be detected.

### Open Questions

None.
