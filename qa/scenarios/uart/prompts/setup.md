# Approved greenfield setup

### Project, modes and Ticket payloads

Use a SystemVerilog HDL testbench, Verilator Simulation and Lint, and logical Yosys synthesis on the standard image. Explicitly set `[stealth] enabled = false`. Preserve ordinary Project source/core locations and literal benign Project-identifying commit messages as evidence.

The Setup delegate runs `booley init --scaffold` with the selected provider, Verilator simulator/linter, SystemVerilog TB and ASIC support; then `/booley-setup new` inside the Session Runtime. These choices are supplied in advance. Prove the generated counter and plain/deep Doctor baseline before replacing it.
