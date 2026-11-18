# Request: feature

Implement the standalone UART described by the frozen public corpus and the
MMIO and timing addenda. Write your own RTL and a self-checking SystemVerilog
testbench, and keep the interface, all required behavior and the Target names.
Use only the allowed documentation and the Project itself; don't fetch any
existing UART RTL or tests. Where the corpus contradicts itself, report the
conflict instead of inventing a rule.

`spec/contract.md` holds the frozen register definitions and MMIO addendum for
the spec review. Synthesis needs a fresh netlist and reports. Error recovery is
required: after a framing error or a break, with parity on or off, the receiver
must go back to receiving valid characters as the corpus Reception and error
sections describe; test that in the testbench.

The mutation run below is the only mutation gate. It mutates `rtl/qa_uart.sv`,
the scaffold's design file, so keep the UART implementation there. Don't add
relative-PPA or hidden coverage gates. Conformance is checked separately; you
won't see that evaluator, its seed or its cases.

Before you give me the commit, run the RTL bugs, protocol and spec (against
`spec/contract.md`) Reviewers and a TB-quality Reviewer on `sim_uart`, and run
`booley flow mutation --target sim_uart --scope rtl/qa_uart.sv --count 10
--min-detected 8`; report their results. Give me a Measured Commit.
