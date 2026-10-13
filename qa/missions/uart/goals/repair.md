# Goalset: repair

Repair standalone UART conformance, attempt 1/2, only after an actual independent
mismatch on a finished implementation. Start from the preceding integrated
finished commit. Supply only the original public contract and at most five
bounded diagnostic excerpts. Reproduce first, then author a regression and fix
owned RTL/TB. Retain the same complete Goals. Never weaken checks or reveal
hidden evaluator material. The operator reruns the entire frozen manifest with
the same seed; at most two repairs. Unfinished feature work is not a repair.

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
    "origin": "repair"
  },
  {
    "family": "REVIEW",
    "review": "rtl_protocol",
    "verdict": "clean",
    "origin": "repair"
  },
  {
    "family": "REVIEW",
    "review": "rtl_spec",
    "verdict": "clean",
    "spec": "spec/contract.md",
    "origin": "repair"
  },
  {
    "family": "REVIEW",
    "review": "tb_quality",
    "verdict": "clean",
    "origin": "repair"
  }
]
```
