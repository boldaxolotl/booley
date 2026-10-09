# Goalset: continuity

Implement a Dhrystone self-checking cycle contract. Keep the fixed 100 iterations.
Validate the deterministic final result in firmware; a mismatch prints an error
and traps before success or cycle reporting. Preserve magic `123456789` at
MMIO `0x20000000` only on the validated path. Emit exactly
`[SIM_CYCLES] dhry <User_Time>` after validation, with a deterministic timeout.
The xPack GCC 15.2 calibration is 109734; the inclusive cap is 110000.

Change only `dhrystone/dhry_1.c` and `dhrystone/testbench.v` plus the ordinary
Project Target/test definitions needed for `sim_dhry_checked` and its registered
`dhry` test. Keep that Target selectable for subsequent work. Do not edit RTL.

## Goals

```json
[
  {
    "family": "elab",
    "target": "sim_dhry_checked",
    "origin": "continuity"
  },
  {
    "family": "sim",
    "target": "sim_dhry_checked",
    "origin": "continuity"
  },
  {
    "family": "cycle_count",
    "target": "sim_dhry_checked",
    "test": "dhry",
    "thresholds": {
      "cycle_count_max": 110000
    },
    "origin": "continuity"
  },
  {
    "family": "review",
    "review": "tb_quality",
    "verdict": "done",
    "origin": "continuity"
  }
]
```
