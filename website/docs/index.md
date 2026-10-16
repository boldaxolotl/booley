---
hide:
  - navigation
  - toc
---

<div class="manpage" markdown>

<p class="man-head"><span>BOOLEY(1)</span><span>Booley Manual</span><span>BOOLEY(1)</span></p>

<h1 class="wordmark">booley<span class="cursor" aria-hidden="true"></span></h1>

## NAME

booley - open-source agentic RTL IDE for FPGA and ASIC work

## SYNOPSIS

```console
$ pipx install booley-rtl
$ booley bootstrap
```

Runs on Linux and Windows. Needs Python 3.11+, Git, Docker, VS Code, and
Claude Code or Codex. Then point it at your RTL with the
[setup guide](https://github.com/boldaxolotl/booley/blob/main/docs/user/SETUP.md).

## DESCRIPTION

Booley runs Claude Code or Codex as a Developer Agent inside a Docker sandbox
and hands it real EDA tools. Each flow is one command that returns a
structured result, pass or fail plus metrics such as area, timing, or cycle
counts, so the agent never greps a log. The interface stays the same across
tools and across projects; flows are built on
[FuseSoC](https://github.com/olofk/fusesoc).

In Goal Mode you set the acceptance criteria up front: passing tests, area and
timing budgets, cycle counts, coverage. Booley checks them in code, against
recorded results, not against the agent's claim that it is done.

## FLOWS

<dl class="man-dl">
<dt>sim</dt><dd>Simulation with Verilator or Icarus Verilog. cocotb testbenches work as they are.</dd>
<dt>lint</dt><dd>Verilator and Verible.</dd>
<dt>synth</dt><dd>Yosys, plus OpenROAD for physical timing on Nangate45.</dd>
<dt>fpga</dt><dd>AMD Vivado 2025.2, from your own licensed install.</dd>
<dt>bwave</dt><dd>Waveform queries for the agent: find, sample, snapshot, measure.</dd>
</dl>

## EXAMPLE

Instead of adding `$display` and rebuilding, the agent asks the waveform.
Output from one of Booley's test traces:

```text
$ bwave trace.fst wave -s '*enable' -s '*state*' -s '*counter*%d' -t 0:12
        cycle  0  1  2  3  4  5  6  7  8  9  10  11  12
counter[15:0]  0  0  1  2  3  4  5  6  7  8   9  10  11
   state[3:0]  0  0  0  1  2  3  4  5  6  7   0   1   2
       enable  0  0  0  0  1  1  1  1  1  1   1   1   1
# scope: tb.dut
# 3 signals, 13 columns
$ bwave trace.fst find '*done' 1
cycle 44 done 1
# scope: tb.dut
# 1 matches found
```

## SCREENSHOT

<div class="screenshot-pending" markdown>
Screenshot coming soon. Until then, the videos below show Booley running end
to end.
</div>

## SEE ALSO

<dl class="man-dl">
<dt><a href="https://github.com/boldaxolotl/booley">source</a></dt><dd>Code, issues, and the full documentation on GitHub.</dd>
<dt><a href="https://github.com/boldaxolotl/booley-prj-picorv32">demo</a></dt><dd>A PicoRV32 project already set up for Booley. Clone it and run your own change.</dd>
<dt><a href="https://youtu.be/zHuvU4QJbvE">video</a></dt><dd>Design Optimization (12:43)</dd>
<dt><a href="https://youtu.be/hsYHHZcx82w">video</a></dt><dd>Finding and Fixing Bugs (9:40)</dd>
<dt><a href="blog/">blog</a></dt><dd>Experiments with agents on real hardware.</dd>
</dl>

## LICENSE

Apache-2.0. Free to use, change, and ship.

<p class="man-foot"><span>booley</span><span>October 2026</span><span>BOOLEY(1)</span></p>

</div>
