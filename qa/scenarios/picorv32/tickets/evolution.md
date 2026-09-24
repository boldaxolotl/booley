# Ticket 2 creation packet

Render every declared typed substitution into this asset, retain the resolved bytes, and
stage those exact bytes under the ignored Project-data path
`tmp/qa-inputs/<run-id>/ticket-2/packet.md`. Retain the source asset, resolved packet,
typed non-secret substitutions, staged Sandbox path, SHA-256, and byte count. The resolved
and staged hashes must match immediately before submission; recheck the staged hash after
the attempt.

Invoke exactly one supported client form with that Sandbox path:

- Codex: `$booley-ticket-create --agent --no-confirm --input-file <project-data-path>`
- Claude: `/booley-ticket-create --agent --no-confirm --input-file <project-data-path>`

These are skill invocations, not shell commands. Retain the literal invocation separately
from the packet. Ticket Create owns live dependency, guidance, selector, Target, test, and
FPGA Flow resolution. It also owns deriving and authoring the applicable Temporal Target
definitions and their owned test tables; this packet deliberately does not pre-author
those definitions.

```markdown
---
summary: Implement RV32 Zbb through registered PCPI execution
type: feature
branch: {{ outer_destination_branch }}
project_destination_ref: {{ project_destination_ref }}
scope:
  - picorv32.v
  - testbench.v
  - testbench_wb.v
  - Makefile
  - tests/zbb.S [new]
spec: /opt/riscv-docs/riscv-isa-manual.html
dependencies: [dhrystone-self-checking-cycle-contract]
priority: medium
on_success: [triage_report, review, merge, cleanup]
CRITERIA_MANDATORY:
  ELAB:
    sim_core_zbb (temp): pass
    sim_axi_zbb (temp): pass
    sim_wb_zbb (temp): pass
    sim_zbb_disabled (temp): pass
  ELAB_STANDALONE:
    - sim_core_zbb (temp)
    - sim_axi_zbb (temp)
    - sim_wb_zbb (temp)
    - sim_zbb_disabled (temp)
  LINT:
    lint_core_zbb (temp): clean
  SIM:
    sim_core: {main: pass, axi: pass}
    sim_wb: {wb: pass}
    sim_dhry_checked: {dhry: pass}
    sim_core_zbb (temp): {zbb_core: fail -> pass}
    sim_axi_zbb (temp): {zbb_axi: fail -> pass}
    sim_wb_zbb (temp): {zbb_wb: fail -> pass}
    sim_zbb_disabled (temp): {zbb_disabled: fail -> pass}
  MUTATION:
    sim_core_zbb (temp): {scope: [picorv32.v], min_detected: 14, total: 15}
  SYNTH:
    synth_core_zbb (temp):
      baseline: synth_core
      cell_count_increase_at_most: 11%
      critical_path_ps_increase_at_most: 3%
{{ configured_fpga_criterion }}
  REVIEW:
    rtl:
      bugs: [done, clean]
      protocol: done
      spec: done
      code_style: done
      optimization: done
      security: done
    tb: {quality: done}
---

## Description

### Current State

The pinned PicoRV32 Project lacks the agreed RV32 Zbb execution path and directed wrapper
coverage. Ticket 1 provides the refreshed persistent `sim_dhry_checked` regression.

### Required Changes

Implement `ANDN`, `ORN`, `XNOR`, `CLZ`, `CTZ`, `CPOP`, `MIN`, `MINU`, `MAX`, `MAXU`,
`SEXT.B`, `SEXT.H`, `ZEXT.H`, `ROL`, `ROR`, `RORI`, `ORC.B`, and `REV8`. Add
`ENABLE_ZBB`, defaulting to `0`, across the core, AXI, and Wishbone wrappers. Use an
internal registered PCPI implementation with a fixed one-cycle response.

Exercise enabled execution through all three wrappers. In the disabled test, arm a
distinct MMIO marker immediately before the first Zbb encoding and require the ensuing
illegal-instruction trap; no unrelated trap may count. Preserve the existing `sim_core`
`main` and `axi` tests, `sim_wb` `wb` test, lint and physical-synthesis behavior, and
genuinely execute `sim_dhry_checked` `dhry`.

Create the Temporal Targets named by the Criteria: `sim_core_zbb`, `sim_axi_zbb`,
`sim_wb_zbb`, `sim_zbb_disabled`, `lint_core_zbb`, and `synth_core_zbb`, plus
`fpga_core_zbb` only when the rendered FPGA Criterion is present. Register the owned tests
named by the Criteria. `sim_axi_zbb` uses the existing `testbench` top whose wrapper
instantiates `picorv32_axi`. The mutation set targets Zbb decode, result generation, PCPI
handshake, and enable gating. The applicable Target definitions and their unambiguously
owned test tables remain through execution and are removed at acceptance.

### Affected Interfaces

The `ENABLE_ZBB` parameter is added consistently to the core, AXI wrapper, and Wishbone
wrapper. Existing behavior remains unchanged when it is `0`.
```
