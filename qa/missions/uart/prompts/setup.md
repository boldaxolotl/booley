# Greenfield setup choices

Set up this new UART IP. Run plain `booley init` with the provider I picked, then
`/booley-setup new` on the host; it runs `booley init --scaffold` with Verilator for
simulation and lint, a SystemVerilog testbench and ASIC support, and then continues
inside the Sandbox. My choices are already made, so you don't need to ask:

- SystemVerilog HDL testbench, Verilator simulation and lint, and logical Yosys
  synthesis, all on the standard image.
- Set `[stealth] enabled = false` explicitly. Keep the ordinary Project source and
  core locations, and use literal commit messages that name the Project; we keep
  them as evidence.

Before you replace the generated counter, show that it passes and that plain and
deep Doctor are clean.
