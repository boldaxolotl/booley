# Goalset: feature

Implement the documented standalone UART from the frozen public corpus and
MMIO/timing addenda. Author owned RTL and self-checking SystemVerilog TB assets;
preserve interface, full required behavior and Target contracts. Use only the
mission's allowed documentation and ordinary Project inspection; retrieve no
existing UART RTL or tests. Report corpus conflicts instead of inventing rules.
`spec/contract.md` contains the frozen register definitions and MMIO addendum
for spec review. Require fresh netlist/reports for synthesis. Error recovery is
required behavior: after a framing error or a break, with parity enabled or
disabled, the receiver starts the next character only on a START edge (idle high,
then 1→0), as the corpus Reception section defines. The mutation Goal below is the
only mutation gate; do not invent relative PPA or hidden coverage gates. Operator
conformance evaluation is separate from Finish; the child sees no evaluator
implementation, seed or generated cases.

## Goals

```json
[
  {
    "family": "ELAB",
    "target": "sim_uart",
    "origin": "feature"
  },
  {
    "family": "SIM",
    "target": "sim_uart",
    "origin": "feature"
  },
  {
    "family": "LINT",
    "target": "lint_uart",
    "origin": "feature"
  },
  {
    "family": "SYNTH",
    "target": "synth_uart",
    "origin": "feature"
  },
  {
    "family": "REVIEW",
    "review": "rtl_bugs",
    "verdict": "clean",
    "origin": "feature",
    "target": "sim_uart"
  },
  {
    "family": "REVIEW",
    "review": "rtl_protocol",
    "verdict": "clean",
    "origin": "feature",
    "target": "sim_uart"
  },
  {
    "family": "REVIEW",
    "review": "rtl_spec",
    "verdict": "clean",
    "spec": "spec/contract.md",
    "origin": "feature",
    "target": "sim_uart"
  },
  {
    "family": "MUTATION",
    "target": "sim_uart",
    "scope": ["rtl/qa_uart.sv"],
    "min_detected": 8,
    "total": 10,
    "origin": "feature"
  },
  {
    "family": "REVIEW",
    "review": "tb_quality",
    "verdict": "clean",
    "origin": "feature",
    "target": "sim_uart"
  }
]
```
