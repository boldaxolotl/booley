# Implement the documented standalone UART

Feature Ticket: **Implement the documented standalone UART**, type Feature, scoped to owned `rtl/` and `tb/` assets. Its payload is:

> Implement the complete standalone UART from the frozen documentation and MMIO addendum, authoring both RTL and self-checking SystemVerilog verification. Preserve the interface, full required behavior and Target contracts. Use only allowed documentation and ordinary Project inspection; retrieve no existing UART RTL or tests. Complete the bound Criteria and retain evidence. Report documentation conflicts rather than inventing replacement requirements.

Mandatory Criteria: Elaboration Check and complete Simulation on `sim_uart`; clean lint on `lint_uart`; successful logical synthesis on `synth_uart`; clean RTL-bugs, protocol, specification and TB-quality reviews. Bind the specification review to immutable corpus/addendum paths. Require fresh netlist/reports for synthesis. Do not invent a relative PPA improvement, hidden coverage Criterion or mutation-score gate.

Success disposition is `done`, with local merge, Workspace cleanup and triage report enabled. Save Scope, Criteria, Targets, Ticket Baseline, Board transitions, reports and the accepted commit under `evidence/`. **Independent conformance is graded separately by the operator in the mission's `evaluate` area; Ticket acceptance alone does not show the UART conforms.**
