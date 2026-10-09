# Goalset: feature

Implement the documented standalone UART from the frozen public corpus and
MMIO/timing addenda. Author owned RTL and self-checking SystemVerilog TB assets;
preserve interface, full required behavior and Target contracts. Use only the
mission's allowed documentation and ordinary Project inspection; retrieve no
existing UART RTL or tests. Report corpus conflicts instead of inventing rules.
`spec/contract.md` links the immutable corpus and MMIO contract for spec
review. Require fresh netlist/reports for synthesis. Do not invent relative PPA,
hidden coverage or mutation gates. Operator conformance evaluation is separate
from Finish; the child sees no evaluator implementation, seed or generated cases.

## Goals

All Goals are mandatory. Copy this file into the resolved Project's `goalsets/`
before entry. Follow the candidate's `booley-goal/SKILL.md` and USAGE Goal Mode.

```json
[
  {
    "family": "elab",
    "target": "sim_uart",
    "origin": "feature"
  },
  {
    "family": "sim",
    "target": "sim_uart",
    "origin": "feature"
  },
  {
    "family": "lint",
    "target": "lint_uart",
    "origin": "feature"
  },
  {
    "family": "synth",
    "target": "synth_uart",
    "origin": "feature"
  },
  {
    "family": "review",
    "review": "rtl_bugs",
    "verdict": "clean",
    "origin": "feature"
  },
  {
    "family": "review",
    "review": "rtl_protocol",
    "verdict": "clean",
    "origin": "feature"
  },
  {
    "family": "review",
    "review": "rtl_spec",
    "verdict": "clean",
    "spec": "spec/contract.md",
    "origin": "feature"
  },
  {
    "family": "review",
    "review": "tb_quality",
    "verdict": "clean",
    "origin": "feature"
  }
]
```
