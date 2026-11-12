# Goalset: repair

Repair the UART's conformance, attempt 1 of 2. An independent check found a
mismatch in the finished implementation; start from the integrated finished
commit. You have the original public contract and at most five diagnostic
excerpts. Reproduce each failure first, then add a regression test and fix the
RTL or testbench. Keep all the original Goals and never weaken a check; the
evaluator itself stays hidden. After Finish, the full frozen case list is rerun
with the same seed. This is a repair, not a place to finish incomplete feature
work.

## Goals

```json
[
  {
    "family": "ELAB",
    "target": "sim_uart",
    "origin": "repair"
  },
  {
    "family": "SIM",
    "target": "sim_uart",
    "origin": "repair"
  },
  {
    "family": "LINT",
    "target": "lint_uart",
    "origin": "repair"
  },
  {
    "family": "SYNTH",
    "target": "synth_uart",
    "origin": "repair"
  },
  {
    "family": "REVIEW",
    "review": "rtl_bugs",
    "verdict": "clean",
    "origin": "repair",
    "target": "sim_uart"
  },
  {
    "family": "REVIEW",
    "review": "rtl_protocol",
    "verdict": "clean",
    "origin": "repair",
    "target": "sim_uart"
  },
  {
    "family": "REVIEW",
    "review": "rtl_spec",
    "verdict": "clean",
    "spec": "spec/contract.md",
    "origin": "repair",
    "target": "sim_uart"
  },
  {
    "family": "REVIEW",
    "review": "tb_quality",
    "verdict": "clean",
    "origin": "repair",
    "target": "sim_uart"
  }
]
```
