---
hide:
  - navigation
  - toc
---

<div class="manpage" markdown>

<h1 class="wordmark"><span class="wordmark__text">booley</span><span class="wordmark__cursor" aria-hidden="true"></span></h1>

## NAME

booley - open-source agentic RTL IDE for FPGA and ASIC work

## QUICK START

```console
$ pipx install booley-rtl
$ booley bootstrap
```

Runs on Linux and Windows. Needs Python 3.11+, Git, Docker, VS Code, and
Claude Code or Codex. Then point it at your RTL with the
[setup guide](https://github.com/boldaxolotl/booley/blob/main/docs/user/SETUP.md).

## DESCRIPTION

Booley turns Claude Code or Codex into a capable RTL assistant. It runs the
Developer Agent in a sandbox, hands it real EDA tools, and checks its work
against your acceptance criteria: passing tests, area and timing budgets,
cycle counts, coverage, and more. You design; it does the grunt work.

## FLOWS

<dl class="man-dl">
<dt>sim</dt><dd>Simulation with Verilator or Icarus Verilog. cocotb testbenches work as they are.</dd>
<dt>lint</dt><dd>Verilator and Verible.</dd>
<dt>synth</dt><dd>Yosys, plus OpenROAD for physical timing on Nangate45.</dd>
<dt>fpga</dt><dd>AMD Vivado 2025.2, from your own licensed install.</dd>
<dt>bwave</dt><dd>Waveform queries for the agent: find, sample, snapshot, measure.</dd>
</dl>

## SEE ALSO

<dl class="man-dl">
<dt><a href="https://github.com/boldaxolotl/booley">source</a></dt><dd>Code, issues, and the full documentation on GitHub.</dd>
<dt><a href="https://github.com/boldaxolotl/booley-prj-picorv32">demo</a></dt><dd>A PicoRV32 project already set up for Booley. Clone it and run your own change.</dd>
</dl>

## LICENSE

Apache-2.0. Free to use, change, and ship.

</div>
