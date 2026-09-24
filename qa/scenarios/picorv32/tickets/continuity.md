# Ticket 1 creation packet

Render the declared substitutions into this asset, retain the resolved bytes, and stage
those exact bytes under the ignored Project-data path
`tmp/qa-inputs/<run-id>/ticket-1/packet.md`. Retain the resolved and staged SHA-256 and
byte count before submission, then recheck the staged hash after the attempt.

Invoke exactly one supported client form with that Sandbox path:

- Codex: `$booley-ticket-create --agent --no-confirm --input-file <project-data-path>`
- Claude: `/booley-ticket-create --agent --no-confirm --input-file <project-data-path>`

These are skill invocations, not shell commands. Retain the literal invocation separately
from the packet. Ticket Create owns deriving and authoring the New Target definition and
its owned test table from the live Project.

```markdown
---
summary: Add a self-checking Dhrystone cycle contract
type: verification
branch: {{ outer_destination_branch }}
project_destination_ref: {{ project_destination_ref }}
scope:
  - dhrystone/dhry_1.c
  - dhrystone/testbench.v
spec: ""
dependencies: []
priority: medium
on_success: [triage_report, review, merge, cleanup]
CRITERIA_MANDATORY:
  ELAB:
    sim_dhry_checked (new): pass
  SIM:
    sim_dhry_checked (new): {dhry: pass}
  CYCLE_COUNT:
    sim_dhry_checked (new): {dhry: {cycle_count_max: 110000}}
  REVIEW:
    tb: {quality: done}
---

## Description

### Coverage Gaps

The fixed 100-iteration Dhrystone demo can report success without checking its final
result, and its calibrated cycle count is not an acceptance condition.

### Verification Strategy

Validate the deterministic final result in firmware. A mismatch prints an error and
traps before success or cycle reporting. Preserve success magic `123456789` at MMIO
address `0x20000000`; only the validated path may pass. Emit exactly
`[SIM_CYCLES] dhry <User_Time>` after validation with a deterministic timeout. The
xPack GCC 15.2 calibration is 109734 cycles; acceptance uses the inclusive 110000 cap.
Create the New Target `sim_dhry_checked` and register its `dhry` test. It remains
selectable after acceptance and is exported to the dependent Ticket.

### RTL Boundary

Do not modify RTL. Limit changes to `dhrystone/dhry_1.c` and
`dhrystone/testbench.v`.
```
