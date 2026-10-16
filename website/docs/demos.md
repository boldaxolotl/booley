---
hide:
  - navigation
  - toc
---

# Demos

Open-source designs already set up for Booley. Clone one, open it in VS Code,
and give the agent a task.

## PicoRV32

A small RISC-V CPU core ([YosysHQ/picorv32](https://github.com/YosysHQ/picorv32)).
Start here: it is the quickest way to try Booley on your own machine. Clone the
configured demo, enter Goal Mode with `/booley-goal`, and run your own change.

[Open the PicoRV32 demo](https://github.com/boldaxolotl/booley-prj-picorv32/blob/e53c1896ab02595aa4ebb3a56b5aa7bd109177f9/README.md)

## Caliptra

The open-source hardware root of trust
([chipsalliance/caliptra-rtl](https://github.com/chipsalliance/caliptra-rtl)):
a full SoC with a RISC-V core and firmware. The project wires Caliptra's
Verilator testbench, firmware tests, lint, and ASIC synthesis into Booley
Flows, so the agent can simulate the whole SoC and inspect its waveforms.
It is a heavier setup: the Sandbox uses the RISC-V image and asks for 28 GB
of memory.

[Open the Caliptra project](https://github.com/boldaxolotl/booley-prj-caliptra#readme)
