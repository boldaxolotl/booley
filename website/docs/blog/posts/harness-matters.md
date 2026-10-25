---
title: "Why AI agents struggle with RTL debugging, and what helps"
description: >-
  A case study on Caliptra, a large open-source system on chip: an AI agent
  found a processor bug within minutes, then spent most of an hour reproducing
  it. Invisible internal signals and long-running simulations explain much of
  that time. Tools aimed at them, plus a repeatable build, cut the mean time to
  a verified fix from 56 minutes to 21.
date: 2026-10-16
slug: harness-matters
draft: false
# Application-note number, shown in the title strip and the footer.
number: AN-001
# Exhibits listed under the contents sidebar (id within this page, label).
exhibits:
  - [f1, "Figure 1. Time per bug fix"]
  - [f2, "Figure 2. Invisible internal state, b3"]
  - [f3, "Figure 3. Waiting on long jobs, b10"]
  - [f4, "Figure 4. Fragile build state, b1"]
  - [t1, "Table 1. Test conditions"]
  - [t2, "Table 2. Pinned versions"]
  - [l1, "Listing 1. B-Wave search"]
  - [l2, "Listing 2. B-Wave signal snapshot"]
  - [l3, "Listing 3. Check out sources"]
  - [l4, "Listing 4. Apply patches"]
  - [l5, "Listing 5. Bug mutation"]
  - [l6, "Listing 6. Agent prompt"]
# Files in docs/blog/posts/harness-matters/, offered in the sidebar. The blog
# plugin publishes them beside the post, so each path is just the file name.
downloads:
  - [prompt, agent-prompt.txt]
  - [seed.patch, seed.patch]
  - [trace-harness.patch, trace-harness.patch]
---

<section class="abstract" aria-labelledby="abs">
      <div class="abstract__h" id="abs">Abstract</div>
      <p>I asked an AI coding agent to fix a bug in the <span class="term" tabindex="0">RISC-V<span class="term__tip" role="tooltip">An open instruction set architecture. Many open-source processor cores implement it.</span></span> processor inside <span class="term" tabindex="0">Caliptra<span class="term__tip" role="tooltip">A large open-source system on chip from CHIPS Alliance. It is a hardware root of trust: a security block with its own RISC-V processor and firmware.</span></span>. It usually found the faulty line of <span class="term" tabindex="0">RTL<span class="term__tip" role="tooltip">Register-transfer level: the code that describes a chip’s hardware, usually written in Verilog or SystemVerilog. A simulator runs it; synthesis tools turn it into gates.</span></span> within five minutes. Proving the fix took much longer, because the agent first had to make the bug show up in simulation. Over ten trials, two hardware-specific problems took most of that time: the internal signals that explain a failure are invisible unless the simulation records them, and simulations run long enough that the agent has to guess when they finish. A third problem, a build that depends on settings the agent's shell doesn't keep, isn't specific to hardware but is common in RTL projects. With tools aimed at all three, the same agent's mean time to a verified fix fell from 56 minutes to 21.</p>
    </section>

<!-- more -->

Coding agents are built mostly for software work. Their working loop assumes that a test runs in seconds, that a failure prints a useful message, and that a command is cheap to rerun. Hardware design breaks all three. Chips are described in <span class="term" tabindex="0">RTL<span class="term__tip" role="tooltip">Register-transfer level: the code that describes a chip’s hardware, usually written in Verilog or SystemVerilog. A simulator runs it; synthesis tools turn it into gates.</span></span> code and tested in a simulator. A simulation of a large design takes minutes to compile and run, so every rerun is expensive, and the state that explains a failure lives in internal signals that nobody printed.

Current models reason well about RTL. This note looks at what happens when that reasoning meets a real hardware debug loop. It asks where the agent's time goes, which of that time comes from hardware work rather than from agent setups in general, and what the agent's <span class="term" tabindex="0">harness<span class="term__tip" role="tooltip">The environment around an AI agent: the tools it can call, how its commands run, and the instructions it gets.</span></span> can do about it.

The scope is debugging: finding a bug, reproducing it, and proving the fix. Other RTL tasks, such as writing new design code or building verification environments, run into different problems. Later notes will cover those.

To answer it, I ran the same agent on the same bug in two setups: a plain shell, and [Booley](https://github.com/boldaxolotl/booley), an open-source agent harness for FPGA and ASIC work. I build Booley and ran this experiment myself, so weigh the comparison with that in mind. The problems described below, though, come from the plain-shell runs and apply to any agent setup for RTL.

## The case: a forwarding bug in a RISC-V core

I wanted a bug where the environment, not the model's reasoning, would limit the agent. I started with bug-fixing tasks from a subset of [CVDP](https://github.com/NVlabs/cvdp_benchmark), NVIDIA's benchmark of hardware design and verification problems. Those tasks were too small for this question: agents solved them quickly with any tooling, so the environment never had a chance to matter. I needed a design where reproducing a bug takes real simulation work.

So I looked for the largest high-quality open-source RTL codebase I could find. I chose [Caliptra](https://github.com/chipsalliance/caliptra-rtl), a hardware root of trust: a security subsystem with its own processor and firmware.

I settled on a historical bug in VeeR EL2, the RISC-V processor inside Caliptra, and reintroduced it by reverting an upstream fix from the [VeeR EL2 1.4 release](https://github.com/chipsalliance/Cores-VeeR-EL2/commit/fb3354352be70781450ea70963c44d4b8f88ae9b).

In the scenario the bug report describes, firmware checks the contents of the processor's <span class="term" tabindex="0">ROM<span class="term__tip" role="tooltip">Read-only memory. Here it holds the processor’s boot program.</span></span> during startup. Under a particular sequence of reads and writes, that check fails even though the stored program is correct: the word at ROM address 0 reads back as zero.

The agent's task was to find and fix the cause in the RTL, then add a test that fails before the fix and passes afterward.

<details open markdown>
<summary>The bug report</summary>
<div class="details__body" markdown>

The agent received an issue-style report describing this behavior:

> Early in boot, the firmware recomputes a digest over its own ROM image and compares it with a constant fixed at build time. Normally the check passes. In one case the digest comes out wrong, even though the ROM is unchanged and the constant is correct. No fault, no exception, no ECC event, fully deterministic. The trigger is a coding pattern: the failing routine reads ROM word by word and, in the same tight loop, writes partial results to a scratch buffer in <span class="term" tabindex="0">DCCM<span class="term__tip" role="tooltip">Data closely coupled memory: the processor’s fast on-chip data memory, reached without going over the system bus.</span></span>. Keep the accumulator in registers, or move the buffer out of DCCM, and the digest is correct. Only the first word of the ROM input looks corrupted.
>
> Find and fix the RTL root cause. Add a test that reproduces the problem and passes after the fix.

The upstream [release notes](https://github.com/chipsalliance/Cores-VeeR-EL2/blob/fb3354352be70781450ea70963c44d4b8f88ae9b/release-notes.md) describe the fix as "Store data collision with DCCM DMA error when address is 0x0".

</div>
</details>

## Why reproducing it was harder than finding it

The processor uses *store-to-load forwarding*: a read can take its value directly from a recent write to the same address instead of waiting for memory. To save power, it also blanks the address and data of a write that goes to its on-chip data memory (DCCM), setting both to zero. The forwarding check was missing its "this write goes out on the bus" condition, so it compared each read against that blanked address of zero. A read from ROM address 0, issued right after an on-chip write, "matched", skipped the real ROM read, and got the blanked value: zero.

In both setups, the agent usually identified the faulty line within five minutes. It then had to write a test that failed on the original design and passed after the fix. That would demonstrate the fault and help catch it if it returned later.

Two details made that test difficult to write:

- The test template marked ROM as a region where accesses may have side effects. That setting disables forwarding and hides the bug.
- The processor works on several instructions at once, each at a different stage of its *pipeline*. The bug needs the write and the read back to back, one stage apart. Fetching the test's instructions from ROM over the bus left gaps between them, so the test had to run its loop from the processor's fast on-chip instruction memory (ICCM) instead.

Most of the work went into building a test, simulating it, inspecting what happened, and adjusting it until it failed for the right reason.

Finding the bug took reading, and current models read code well. Reproducing it took repeated experiments: build, simulate, look at the result, change the test. Hardware and software differ most in that loop, so the rest of this note measures it.

## Method

I ran ten trials in each of two setups, with the same agent, model, prompt, and container:

- **Claude Code alone:** a shell plus Caliptra's existing Makefiles and test scripts. This is the default when you point an agent at an RTL repository.
- **Claude Code plus Booley:** the same agent with a harness designed for RTL work, configured for Caliptra before the trials. The timings cover debugging only, not the one-time work of adapting Caliptra to Booley.

 The Booley setup's `AGENTS.md` (also linked as `CLAUDE.md`) explains how to use Booley's tools and has two lines describing the repository; it contains no build commands, environment variables, or debugging hints.

<details markdown>
<summary>Experiment settings and versions</summary>
<div class="details__body" markdown>

<p class="cap cap--top" id="t1"><b>Table 1.</b> Test conditions, identical for both setups unless noted</p>

<table>
          <tbody>
            <tr><th scope="row">Agent</th><td>Claude Code 2.1.289, Sonnet 5.5, medium effort</td></tr>
            <tr><th scope="row">Booley</th><td>Published release 0.3.0 (with-Booley setup only)</td></tr>
            <tr><th scope="row">Design</th><td>Caliptra at commit <code>e379800c</code>, with the historical VeeR fix reverted</td></tr>
            <tr><th scope="row">Environment</th><td>Same container image, 8 CPUs, 10 GB RAM, no internet access</td></tr>
            <tr><th scope="row">Task</th><td>Same prompt, 90-minute elapsed-time limit per trial</td></tr>
            <tr><th scope="row">Trials</th><td>Ten per setup, b1 through b10; matching numbers form pairs</td></tr>
          </tbody>
        </table>

</div>
</details>

The harness changed three things, each aimed at one of the problems described below:

- **Waveform queries.** Both setups could record a <span class="term" tabindex="0">waveform<span class="term__tip" role="tooltip">A recording of how internal signals change, cycle by cycle, during a simulation.</span></span>; only the Booley setup had a tool, B-Wave, to search it and read signal values at particular cycles.
- **One blocking simulation command.** Booley's `sim` Flow builds and runs a simulation, then returns its result when the job ends. It works the same way in every project, so the agent doesn't have to learn each project's shell commands.
- **Build configuration in files.** [FuseSoC](https://github.com/olofk/fusesoc), the open-source build tool Booley uses, described the source files, simulator settings, dependency paths, and firmware build steps in <span class="term" tabindex="0">core files<span class="term__tip" role="tooltip">FuseSoC description files, one per component, listing its source files, dependencies, and build settings.</span></span> and a test list.

## Where the time went

Both setups produced an accepted fix and a reproducing test in all ten trials, so the comparison is about time. Mean completion time was **55.8 minutes with Claude Code alone** and **21.3 minutes with Booley** (Figure 1); the medians were 53.3 and 21.4 minutes. Average cost per trial at Sonnet 5.5 API prices was $2.33 and $1.98.

<figure id="f1" aria-labelledby="f1cap">
      <div class="frame">
        <svg class="chart" viewBox="0 40 640 365" role="img" aria-labelledby="f1t f1d">
          <title id="f1t">Mean time per bug fix, split by activity</title>
          <desc id="f1d">With Booley: build and simulation 11.5 minutes, observing signals 4.6, sleeping on finished jobs 0.4, everything else 4.8, total 21.3. Claude Code alone: build and simulation 19.7, observing signals 14.0, simulation hung 2.7, sleeping on finished jobs 11.6, everything else 7.7, total 55.8.</desc>
          <!-- y scale: 5 px per minute, zero at y=360 -->
          <g class="grid"><path d="M60 360 H420 M60 310 H420 M60 260 H420 M60 210 H420 M60 160 H420 M60 110 H420 M60 60 H420"/></g>
          <g class="ax" text-anchor="end">
            <text x="50" y="364">0</text><text x="50" y="314">10</text><text x="50" y="264">20</text><text x="50" y="214">30</text><text x="50" y="164">40</text><text x="50" y="114">50</text><text x="50" y="64">60 min</text>
          </g>
          <path d="M60 360 H420" stroke="var(--ink)" stroke-width="1.2"/>

          <!-- With Booley -->
          <rect x="120" y="302.5" width="110" height="57.5" class="sg build"/>
          <rect x="120" y="279.5" width="110" height="23" class="sg obs"/>
          <rect x="120" y="277.5" width="110" height="2" class="sg sleep"/>
          <rect x="120" y="253.5" width="110" height="24" class="sg else"/>
          <text class="v on-dark" x="175" y="336" text-anchor="middle">11.5</text>
          <text class="v" x="175" y="296" text-anchor="middle">4.6</text>
          <text class="v" x="175" y="270" text-anchor="middle">4.8</text>
          <path d="M120 278.5 H100" stroke="var(--ink-2)"/>
          <text class="ax" x="96" y="283" text-anchor="end">0.4</text>
          <text class="tot" x="175" y="242" text-anchor="middle">21.3 min</text>
          <text class="bar" x="175" y="388" text-anchor="middle">With Booley</text>

          <!-- Claude Code alone -->
          <rect x="300" y="261.5" width="110" height="98.5" class="sg build"/>
          <rect x="300" y="191.5" width="110" height="70" class="sg obs"/>
          <rect x="300" y="178" width="110" height="13.5" class="sg hung"/>
          <rect x="300" y="120" width="110" height="58" class="sg sleep"/>
          <rect x="300" y="81.5" width="110" height="38.5" class="sg else"/>
          <text class="v on-dark" x="355" y="315" text-anchor="middle">19.7</text>
          <text class="v" x="355" y="231" text-anchor="middle">14.0</text>
          <text class="v on-dark" x="355" y="189" text-anchor="middle" style="font-size:12px">2.7</text>
          <text class="v" x="355" y="153" text-anchor="middle">11.6</text>
          <text class="v" x="355" y="105" text-anchor="middle">7.7</text>
          <text class="tot" x="355" y="70" text-anchor="middle">55.8 min</text>
          <text class="bar" x="355" y="388" text-anchor="middle">Claude Code alone</text>

          <!-- direct labels -->
          <g class="k">
            <text x="424" y="315">Build and simulation</text>
            <text x="424" y="231">Observing internal signals</text>
            <text x="424" y="189">Sim hung: env vars lost</text>
            <text x="424" y="153">Sleeping on finished jobs</text>
            <text x="424" y="105">Everything else</text>
          </g>
        </svg>
      </div>
      <figcaption id="f1cap"><b>Figure 1.</b> Mean time per bug fix, split by activity. Ten paired Caliptra bug-fix trials, all accepted. Segment values in minutes. Each minute counts in one category only; a wait after a job had finished counts as waiting even when it followed a hung simulation.</figcaption>
    </figure>

In the plain-shell runs, only about a third of the time went into building and simulating. The rest went to observing internal signals, sleeping on jobs that had already finished, and recovering from broken builds. Each recurred across the trials. The first two follow from how hardware is simulated; the third is a general agent problem that showed up here too. I identified them from the run histories; I didn't run separate experiments to isolate the contribution of each fix.

## Problem 1: internal state is invisible by default

When a software test fails, it usually prints what went wrong, and a debugger can stop the program anywhere. In RTL simulation, the state that explains a failure is spread across thousands of internal signals, and none of them are visible unless the testbench records a waveform or prints them. Adding a print means editing the design or testbench and recompiling. Caliptra simulates with Verilator, which compiles the design into a C++ program, so each recompile and rerun takes several minutes.

<figure id="f2" aria-labelledby="f2cap">
      <div class="frame tl" style="--grid:22.2222%">
        <div class="tl__row">
          <p class="tl__lab"><b>Claude Code alone</b>, trial b3 <span class="tl__tot">85 min</span></p>
          <div class="tl__track" role="img" aria-label="Claude Code alone, trial b3: 85 min in total. Observing internal signals: 37.8 min. Segments in minutes: other work 0.0&ndash;1.4; build and simulation 1.4&ndash;8.1; other work 8.1&ndash;8.5; observing signals 8.5&ndash;10.4; build and simulation 10.4&ndash;10.6; observing signals 10.6&ndash;40.7; other work 40.7&ndash;40.9; build and simulation 40.9&ndash;41.1; observing signals 41.1&ndash;46.8; build and simulation 46.8&ndash;84.3; other work 84.3&ndash;84.6.">
            <i class="s else" style="left:0.00%;width:1.56%"></i>
            <i class="s build" style="left:1.56%;width:7.44%"></i>
            <i class="s else" style="left:9.00%;width:0.46%"></i>
            <i class="s obs" style="left:9.46%;width:2.14%"></i>
            <i class="s build" style="left:11.60%;width:0.22%"></i>
            <i class="s obs" style="left:11.82%;width:33.43%"></i>
            <i class="s else" style="left:45.26%;width:0.19%"></i>
            <i class="s build" style="left:45.44%;width:0.19%"></i>
            <i class="s obs" style="left:45.63%;width:6.38%"></i>
            <i class="s build" style="left:52.01%;width:41.66%"></i>
            <i class="s else" style="left:93.67%;width:0.33%"></i>
            <i class="mk-tri" style="left:9.46%"></i>
            <i class="mk-tri" style="left:11.70%"></i>
            <i class="mk-tri" style="left:19.30%"></i>
            <i class="mk-tri" style="left:26.68%"></i>
            <i class="mk-tri" style="left:37.13%"></i>
            <i class="mk-tri" style="left:40.27%"></i>
            <i class="mk-tri" style="left:45.90%"></i>
          </div>
          <p class="tl__note">Observing internal signals: 37.8 min</p>
        </div>
        <div class="tl__row">
          <p class="tl__lab"><b>With Booley</b>, trial b3 <span class="tl__tot">27 min</span></p>
          <div class="tl__track" role="img" aria-label="With Booley, trial b3: 27 min in total. Observing internal signals: 9.8 min. Segments in minutes: other work 0.0&ndash;1.6; build and simulation 1.6&ndash;5.3; other work 5.3&ndash;6.0; observing signals 6.0&ndash;12.9; build and simulation 12.9&ndash;13.2; observing signals 13.2&ndash;14.6; build and simulation 14.6&ndash;15.5; observing signals 15.5&ndash;17.0; build and simulation 17.0&ndash;25.3; other work 25.3&ndash;26.6.">
            <i class="s else" style="left:0.00%;width:1.78%"></i>
            <i class="s build" style="left:1.78%;width:4.11%"></i>
            <i class="s else" style="left:5.89%;width:0.78%"></i>
            <i class="s obs" style="left:6.67%;width:7.67%"></i>
            <i class="s build" style="left:14.33%;width:0.33%"></i>
            <i class="s obs" style="left:14.67%;width:1.56%"></i>
            <i class="s build" style="left:16.22%;width:1.00%"></i>
            <i class="s obs" style="left:17.22%;width:1.67%"></i>
            <i class="s build" style="left:18.89%;width:9.22%"></i>
            <i class="s else" style="left:28.11%;width:1.44%"></i>
            <i class="mk-dot" style="left:10.73%;width:0.96%"></i>
            <i class="mk-dot" style="left:11.90%;width:2.54%"></i>
            <i class="mk-dot" style="left:14.78%;width:1.72%"></i>
            <i class="mk-dot" style="left:17.89%;width:1.72%"></i>
          </div>
          <p class="tl__note">Observing internal signals: 9.8 min</p>
        </div>
        <div class="tl__axis" aria-hidden="true">
          <span style="left:0.00%">0</span>
          <span style="left:22.22%">20</span>
          <span style="left:44.44%">40</span>
          <span style="left:66.67%">60</span>
          <span style="left:88.89%">80</span>
        </div>
        <p class="tl__axh">Minutes since the agent started</p>
        <ul class="tl__key">
          <li><i class="sw obs"></i>Observing internal signals</li>
          <li><i class="sw build"></i>Build and simulation</li>
          <li><i class="sw else"></i>Everything else (reading, reasoning, editing)</li>
          <li><i class="mk-tri"></i>Rebuild to add debug prints (7&times;)</li>
          <li><i class="mk-dot"></i>Waveform query (16&times;)</li>
        </ul>
      </div>
      <figcaption id="f2cap"><b>Figure 2.</b> Seeing inside the design, trial pair b3. Claude Code alone added <code>$display</code> prints, rebuilt, reran and searched the output seven times; with Booley the agent recorded a waveform once and queried it 16 times.</figcaption>
    </figure>

This matters most when a test passes despite a suspected bug, which happened repeatedly here, and the agent needs to see why the bug didn't fire.

**What the agent did with only a shell.** It debugged the simulator the way it would debug a software program: with prints. In pair b3 (Figure 2), it added <span class="term" tabindex="0"><code>$display</code><span class="term__tip" role="tooltip">Verilog’s print statement, similar to printf in C.</span></span> statements, rebuilt the simulator, reran the test, and searched the output. Every new question meant another recompile. It repeated that loop seven times and spent 37.8 minutes on signal observation.

Recording a waveform was an option in this setup too, but a recording only helps if the agent can read it. Caliptra's waveforms are in FST, a compressed binary format that takes a dedicated library to parse, and a quick Python script is too slow on a dump this size.

**What worked: record once, then query.** With Booley, the agent built once with waveform recording enabled and asked B-Wave 16 questions about the recorded signals. That work took 9.8 minutes.

In trial b1, the agent searched the waveform to find whether the faulty address comparison ever fired. Listing 1 shows equivalent command lines, with output trimmed.

<figure id="l1" aria-labelledby="l1cap">
<pre><code><span class="p">$ </span>bwave find trace.fst '*bus_intf.ld_addr_rhit_lo_lo' 1
cycle 4687 caliptra_top_tb.caliptra_top_dut.rvtop.veer.lsu.bus_intf.ld_addr_rhit_lo_lo 1
cycle 4696 caliptra_top_tb.caliptra_top_dut.rvtop.veer.lsu.bus_intf.ld_addr_rhit_lo_lo 1
...
<span class="c"># 32 matches found</span></code></pre>
      <figcaption id="l1cap"><b>Listing 1.</b> B-Wave search for the faulty address comparison, trial b1. Output trimmed.</figcaption>
    </figure>

The comparison fired 32 times, yet the test still passed. The agent then asked for the surrounding signal values at the first match (Listing 2). Signal names end in their pipeline stage: `_m` for the load, `_r` for the store one stage ahead of it.

<figure id="l2" aria-labelledby="l2cap">
<pre><code><span class="p">$ </span>bwave value trace.fst --at 4687 -s '*bus_intf.ld_*' -s '*bus_intf.lsu_busreq*' -s '*bus_intf.is_sideeffects_m' ...
<span class="c"># Snapshot at cycle 4687</span>
lsu_busreq_m                             = 1
<span class="hl">is_sideeffects_m                         = 1</span>
lsu_busreq_r                             = 0
ld_addr_rhit_lo_lo                       = 1
<span class="hl">ld_full_hit_m                            = 0</span></code></pre>
      <figcaption id="l2cap"><b>Listing 2.</b> Signal values at the first match, cycle 4687. Output trimmed; the two lines that hid the bug are shaded.</figcaption>
    </figure>

The read matched a write that should not have been eligible for forwarding: a DCCM store (`lsu_busreq_r = 0`) whose address had been blanked to 0. But the test's memory setting (`is_sideeffects_m = 1`) blocked forwarding (`ld_full_hit_m = 0`) and hid the bug.

The agent changed that setting in the test. Half a minute later, the test failed on the buggy design.

The agent could ask follow-up questions of the same recording without rebuilding. Across all ten pairs, it spent **9.4 minutes less on average** observing signals with Booley.

*The b3 chart's final segment (Figure 2), roughly 47 to 85 minutes, is a sequence of final tests. That time isn't counted as signal observation.*

In hardware debugging, a new question about the design should be answerable from an existing recording. A tool that searches a recorded waveform by signal name and reads values at a given cycle gives an agent roughly what a debugger gives it in software.

## Problem 2: simulations are long, and the agent guesses how long

Agents' habits come from commands that finish in seconds. In hardware, minutes are normal: compiling a design like Caliptra for simulation and running a firmware test takes several, and synthesis or a full regression can take hours. So the agent runs the job in the background and has to decide when to check on it. If it guesses, it either checks too early or sleeps long after the job has finished.

<figure id="f3" aria-labelledby="f3cap">
      <div class="frame tl" style="--grid:15.3846%">
        <div class="tl__row">
          <p class="tl__lab"><b>Claude Code alone</b>, trial b10 <span class="tl__tot">60 min</span></p>
          <div class="tl__track" role="img" aria-label="Claude Code alone, trial b10: 60 min in total. Sleeping after the job already finished: 30.7 min. Segments in minutes: other work 0.0&ndash;4.0; build and simulation 4.0&ndash;4.4; sleeping on a finished job 4.4&ndash;13.4; other work 13.4&ndash;13.5; build and simulation 13.5&ndash;17.7; sleeping on a finished job 17.7&ndash;23.2; build and simulation 23.2&ndash;24.7; sleeping on a finished job 24.7&ndash;33.2; other work 33.2&ndash;34.4; build and simulation 34.4&ndash;34.8; other work 34.8&ndash;34.9; build and simulation 34.9&ndash;43.8; sleeping on a finished job 43.8&ndash;51.5; build and simulation 51.5&ndash;56.1; other work 56.1&ndash;60.0.">
            <i class="s else" style="left:0.00%;width:6.15%"></i>
            <i class="s build" style="left:6.15%;width:0.62%"></i>
            <i class="s sleep" style="left:6.77%;width:13.85%"></i>
            <i class="s else" style="left:20.62%;width:0.15%"></i>
            <i class="s build" style="left:20.77%;width:6.46%"></i>
            <i class="s sleep" style="left:27.23%;width:8.46%"></i>
            <i class="s build" style="left:35.69%;width:2.31%"></i>
            <i class="s sleep" style="left:38.00%;width:13.08%"></i>
            <i class="s else" style="left:51.08%;width:1.85%"></i>
            <i class="s build" style="left:52.92%;width:0.62%"></i>
            <i class="s else" style="left:53.54%;width:0.15%"></i>
            <i class="s build" style="left:53.69%;width:13.69%"></i>
            <i class="s sleep" style="left:67.38%;width:11.85%"></i>
            <i class="s build" style="left:79.23%;width:7.08%"></i>
            <i class="s else" style="left:86.31%;width:6.00%"></i>
          </div>
          <p class="tl__note">Sleeping after the job already finished: 30.7 min</p>
        </div>
        <div class="tl__row">
          <p class="tl__lab"><b>With Booley</b>, trial b10 <span class="tl__tot">13 min</span></p>
          <div class="tl__track" role="img" aria-label="With Booley, trial b10: 13 min in total. Sleeping after the job already finished: 0.0 min. Segments in minutes: other work 0.0&ndash;2.0; build and simulation 2.0&ndash;5.2; other work 5.2&ndash;5.7; build and simulation 5.7&ndash;6.6; other work 6.6&ndash;6.8; build and simulation 6.8&ndash;12.8.">
            <i class="s else" style="left:0.00%;width:3.08%"></i>
            <i class="s build" style="left:3.08%;width:4.92%"></i>
            <i class="s else" style="left:8.00%;width:0.77%"></i>
            <i class="s build" style="left:8.77%;width:1.38%"></i>
            <i class="s else" style="left:10.15%;width:0.31%"></i>
            <i class="s build" style="left:10.46%;width:9.23%"></i>
          </div>
          <p class="tl__note">Sleeping after the job already finished: 0.0 min</p>
        </div>
        <div class="tl__axis" aria-hidden="true">
          <span style="left:0.00%">0</span>
          <span style="left:15.38%">10</span>
          <span style="left:30.77%">20</span>
          <span style="left:46.15%">30</span>
          <span style="left:61.54%">40</span>
          <span style="left:76.92%">50</span>
          <span style="left:92.31%">60</span>
        </div>
        <p class="tl__axh">Minutes since the agent started</p>
        <ul class="tl__key">
          <li><i class="sw sleep"></i>Sleeping after the job already finished</li>
          <li><i class="sw build"></i>Build and simulation</li>
          <li><i class="sw else"></i>Everything else (reading, reasoning, editing)</li>
        </ul>
      </div>
      <figcaption id="f3cap"><b>Figure 3.</b> Waiting on jobs that are already done, trial pair b10. Claude Code alone polled with fixed <code>sleep</code> calls; Booley returned each result when the job ended.</figcaption>
    </figure>

**What the agent did with only a shell.** In several runs, it started work in the background and used a fixed sleep before checking the result. In b10 (Figure 3), a build started at 4.3 minutes and failed one second later. The failure was hidden: the command ended with `; tail`, so it reported exit code 0. Seconds later the agent requested `sleep 540`, a nine-minute wait, and didn't check the result until 13.4 minutes.

It made similar waits four times in that run, spending 30.7 of its 60 minutes asleep after jobs had finished.

**What worked: return when the job ends.** Booley's simulation command waits for the job and returns the result. For long jobs it returns a run ID instead, and the agent calls a poll tool (`booley_poll`) that returns as soon as the job ends, so there is no fixed sleep to guess.

The Booley run in pair b10 took 13 minutes in total. Across all ten pairs, it spent **11.2 minutes less on average** waiting on finished jobs. These waits affected six pairs. I counted only waits where I could establish that the job had finished, so the estimate is a lower bound. The sleeping is an agent habit. Hardware makes it expensive because the jobs are long, so the guessed waits are long too. There is a cheap partial fix: one line in `CLAUDE.md` telling the agent to wait for the process instead of sleeping would likely recover much of this time. Hiding exit codes behind pipes, as in `; tail`, makes it worse, because a failed build looks like a running one.

## Not hardware-specific: the build is fragile, and the shell forgets

This problem isn't specific to hardware. Any project whose build depends on undocumented environment variables trips agents up the same way, and RTL projects often have such builds. Caliptra's simulation needs the simulator, toolchain paths, and a firmware image compiled by a separate cross-compiler and loaded into the simulated memory, and its Makefiles locate these through environment variables. An engineer sets them once in a terminal and forgets about them. An agent works through a series of separate shell calls, and state set in one call doesn't necessarily survive to the next. When a step fails, the next one often runs anyway: a simulation without its firmware doesn't crash; it runs until it times out.

<figure id="f4" aria-labelledby="f4cap">
      <div class="frame tl" style="--grid:12.5000%">
        <div class="tl__row">
          <p class="tl__lab"><b>Claude Code alone</b>, trial b1 <span class="tl__tot">74 min</span></p>
          <div class="tl__track" role="img" aria-label="Claude Code alone, trial b1: 74 min in total. Build setup and recovery: 30.1 min. Segments in minutes: other work 0.0&ndash;3.9; hunting env vars 3.9&ndash;4.2; sleeping on a finished job 4.2&ndash;8.0; hunting env vars 8.0&ndash;10.3; build and simulation 10.3&ndash;11.8; other work 11.8&ndash;12.5; build and simulation 12.5&ndash;16.1; other work 16.1&ndash;16.4; build and simulation 16.4&ndash;17.6; sim hung 17.6&ndash;32.6; sleeping on a finished job 32.6&ndash;34.7; sim hung 34.7&ndash;37.0; build and simulation 37.0&ndash;48.3; other work 48.3&ndash;48.5; build and simulation 48.5&ndash;49.7; sleeping on a finished job 49.7&ndash;56.8; other work 56.8&ndash;57.8; build and simulation 57.8&ndash;59.9; other work 59.9&ndash;60.8; sim hung 60.8&ndash;65.9; build and simulation 65.9&ndash;66.0; other work 66.0&ndash;66.4; sim hung 66.4&ndash;71.5; build and simulation 71.5&ndash;74.3.">
            <i class="s else" style="left:0.00%;width:4.88%"></i>
            <i class="s hunt" style="left:4.88%;width:0.38%"></i>
            <i class="s sleep" style="left:5.25%;width:4.75%"></i>
            <i class="s hunt" style="left:10.00%;width:2.88%"></i>
            <i class="s build" style="left:12.88%;width:1.88%"></i>
            <i class="s else" style="left:14.75%;width:0.87%"></i>
            <i class="s build" style="left:15.62%;width:4.50%"></i>
            <i class="s else" style="left:20.12%;width:0.37%"></i>
            <i class="s build" style="left:20.50%;width:1.50%"></i>
            <i class="s hung" style="left:22.00%;width:18.75%"></i>
            <i class="s sleep" style="left:40.75%;width:2.63%"></i>
            <i class="s hung" style="left:43.38%;width:2.87%"></i>
            <i class="s build" style="left:46.25%;width:14.12%"></i>
            <i class="s else" style="left:60.38%;width:0.25%"></i>
            <i class="s build" style="left:60.62%;width:1.50%"></i>
            <i class="s sleep" style="left:62.13%;width:8.87%"></i>
            <i class="s else" style="left:71.00%;width:1.25%"></i>
            <i class="s build" style="left:72.25%;width:2.63%"></i>
            <i class="s else" style="left:74.88%;width:1.12%"></i>
            <i class="s hung" style="left:76.00%;width:6.38%"></i>
            <i class="s build" style="left:82.38%;width:0.12%"></i>
            <i class="s else" style="left:82.50%;width:0.50%"></i>
            <i class="s hung" style="left:83.00%;width:6.37%"></i>
            <i class="s build" style="left:89.38%;width:3.50%"></i>
          </div>
          <p class="tl__note">Build setup and recovery: 30.1 min</p>
        </div>
        <div class="tl__row">
          <p class="tl__lab"><b>With Booley</b>, trial b1 <span class="tl__tot">22 min</span></p>
          <div class="tl__track" role="img" aria-label="With Booley, trial b1: 22 min in total. Build setup and recovery: 0.0 min. Segments in minutes: other work 0.0&ndash;1.8; build and simulation 1.8&ndash;7.4; other work 7.4&ndash;8.1; build and simulation 8.1&ndash;11.7; other work 11.7&ndash;13.2; build and simulation 13.2&ndash;13.9; other work 13.9&ndash;14.4; build and simulation 14.4&ndash;20.4; other work 20.4&ndash;21.5.">
            <i class="s else" style="left:0.00%;width:2.25%"></i>
            <i class="s build" style="left:2.25%;width:7.00%"></i>
            <i class="s else" style="left:9.25%;width:0.87%"></i>
            <i class="s build" style="left:10.12%;width:4.50%"></i>
            <i class="s else" style="left:14.62%;width:1.88%"></i>
            <i class="s build" style="left:16.50%;width:0.88%"></i>
            <i class="s else" style="left:17.38%;width:0.62%"></i>
            <i class="s build" style="left:18.00%;width:7.50%"></i>
            <i class="s else" style="left:25.50%;width:1.38%"></i>
          </div>
          <p class="tl__note">Build setup and recovery: 0.0 min</p>
        </div>
        <div class="tl__axis" aria-hidden="true">
          <span style="left:0.00%">0</span>
          <span style="left:12.50%">10</span>
          <span style="left:25.00%">20</span>
          <span style="left:37.50%">30</span>
          <span style="left:50.00%">40</span>
          <span style="left:62.50%">50</span>
          <span style="left:75.00%">60</span>
          <span style="left:87.50%">70</span>
          <span style="left:100.00%">80</span>
        </div>
        <p class="tl__axh">Minutes since the agent started</p>
        <ul class="tl__key">
          <li><i class="sw hunt"></i>Hunting undocumented build env vars</li>
          <li><i class="sw hung"></i>Sim hung: env vars lost between shell calls</li>
          <li><i class="sw sleep"></i>Sleeping after the job already finished</li>
          <li><i class="sw build"></i>Build and simulation</li>
          <li><i class="sw else"></i>Everything else (reading, reasoning, editing)</li>
        </ul>
      </div>
      <figcaption id="f4cap"><b>Figure 4.</b> Getting the build to work at all, trial pair b1. Claude Code alone rediscovered the build environment variables, then lost them between shell calls three times; the Booley run spent no time on setup.</figcaption>
    </figure>

**What the agent did with only a shell.** All ten plain-shell runs hit missing build configuration; most recovered within a few minutes. In b1 (Figure 4), the agent worked out which variables to set and exported them in a shell call. Those values didn't carry over to later calls in this run. The firmware build then failed to produce `program.hex`, and the simulation started without a program. It waited until its timeout. This happened three times before the agent put the configuration in a single `run.sh` script.

The run spent 30 minutes dealing with setup. Sleeps after jobs had already finished, including two minutes after the first hung simulation was killed, are counted separately (Figure 4), as in Figure 1.

**What worked: configuration in files, prerequisites checked.** The Booley project's FuseSoC core files already held the dependency paths, so the agent didn't have to rediscover the Makefile variables. Each simulation invocation supplied its build configuration and firmware build step, without relying on exports from a previous shell call. If the firmware build failed, Booley stopped the run and reported the build error before starting a simulation without its program.

The Booley run in pair b1 spent no time repairing setup. Across all ten pairs, the average setup saving was **3.6 minutes**; b1 was the unusually costly case. Configuring the project once avoided repeating that work during debugging.

Build configuration should live in files that every run reads, and each simulation run should carry its whole configuration and stop at the first failed prerequisite. A checked-in script that the agent's instructions point to gets part of the way there. The plain-shell agent in b1 arrived at exactly that, a `run.sh`, after 30 minutes.

## What the tools didn't change

Both setups still had to work out how to make the bug appear: noticing that the test template's side-effect setting disabled forwarding, and moving the test loop into ICCM to get the store and the load back to back in the pipeline. That is reasoning about microarchitecture, and the harness didn't do it for the agent. It changed the cost of each experiment the agent ran on the way there, and that was enough to cut the total time by more than half.

## Lessons for agent setups on RTL

This is a case study, not a benchmark: one bug, one design, one model, and ten trials per setup. I chose a bug that needed substantial simulation work and used an already configured Booley project. These results don't establish the same speedup for other models or other debugging tasks, or tell us which fix would help most on its own. They also say little about design or verification work, which have their own problems and will get their own notes.

Still, the plain-shell runs show where a general-purpose agent loses time on an RTL project, and none of it was spent finding the bug. The lessons below don't depend on a particular tool. The first two are about hardware. The third applies to any agent setup, but RTL projects need it often:

1. **Make internal state queryable.** Record waveforms on demand and give the agent a way to search them, so a new question about the design doesn't need a rebuild. This was the largest hardware-specific gap.
2. **Return results when jobs finish.** Use a blocking call or a poll that returns on completion, and don't let a pipeline hide a failed exit code.
3. **Keep build configuration in files.** Each simulation should be self-contained and should fail fast when a prerequisite, such as the firmware image, is missing.
4. **Plan for reproduction.** On this bug, the model found the faulty line quickly; the time went into building a test that made the bug visible. Tooling and time budgets should target that loop.

Booley implements all three tooling fixes, but a project can apply them with any waveform library, build system, and job runner.

## Reproducing this study

The [Caliptra project](https://github.com/boldaxolotl/booley-prj-caliptra) used here is public; its README explains the setup. The [reproduction details below](#reproduce) list the recorded versions, the original prompt, and the patches. To try the same workflow on a smaller design, the [PicoRV32 demo](../../demos.md#picorv32) comes preconfigured.

<details id="reproduce" markdown>
<summary>How to reproduce (spoilers)</summary>
<div class="details__body" markdown>

These are the inputs used in the experiment. Prepare the bug before starting the agent, and give it only the prompt below. The patch and the rest of this article reveal the answer.

<p class="cap cap--top" id="t2"><b>Table 2.</b> Pinned versions</p>

<table>
          <tbody>
            <tr><th scope="row">Caliptra</th><td><code>e379800c5467379386f96a9a51a539bd662b608a</code> in <a href="https://github.com/chipsalliance/caliptra-rtl">chipsalliance/caliptra-rtl</a>.</td></tr>
            <tr><th scope="row">Adams Bridge</th><td>At <code>submodules/adams-bridge</code>: <code>2f15ffb1a2e9bd99652fa04d434a48ffcdc23f42</code> in <a href="https://github.com/chipsalliance/adams-bridge">chipsalliance/adams-bridge</a>.</td></tr>
            <tr><th scope="row">Booley</th><td>Published package <code>booley-rtl==0.3.0</code>, source commit <code>f9ec6d5602f3fcf47f48b678a3ba821fb82c859c</code> in <a href="https://github.com/boldaxolotl/booley">boldaxolotl/booley</a>.</td></tr>
            <tr><th scope="row">Booley Caliptra project</th><td>Tag <a href="https://github.com/boldaxolotl/booley-prj-caliptra/tree/an-001-trial"><code>an-001-trial</code></a> (commit <code>e4b4a20beead7d858f8585d29c8c4619bdfc253b</code>) in <a href="https://github.com/boldaxolotl/booley-prj-caliptra">boldaxolotl/booley-prj-caliptra</a>, mounted at <code>.booley_project</code>. It holds the exact project files used in the trials. The trials recorded them as commit <code>e9dd79e7</code>, which was lost when the project history was squashed for publication.</td></tr>
            <tr><th scope="row">Agent</th><td>Claude Code <code>2.1.289</code>, model ID <code>claude-sonnet-5-5</code>, effort <code>medium</code>.</td></tr>
            <tr><th scope="row">Per trial</th><td>8 CPUs, 10 GB RAM, a 4,096-process limit, and a 5,400-second time limit. Network access was restricted to the model API; web search, web fetching, and subagents were disabled.</td></tr>
          </tbody>
        </table>

### Prepare the source

Check out the Caliptra revision above and initialize its submodules, then pin Adams Bridge to the recorded revision (Listing 3):

<figure id="l3" aria-labelledby="l3cap">
<pre><code><span class="p">$ </span>git clone https://github.com/chipsalliance/caliptra-rtl.git caliptra-source
<span class="p">$ </span>cd caliptra-source
<span class="p">$ </span>git checkout --detach e379800c5467379386f96a9a51a539bd662b608a
<span class="p">$ </span>git submodule update --init --recursive
<span class="p">$ </span>git -C submodules/adams-bridge checkout --detach 2f15ffb1a2e9bd99652fa04d434a48ffcdc23f42</code></pre>
          <figcaption id="l3cap"><b>Listing 3.</b> Check out the pinned sources.</figcaption>
        </figure>

Both setups also received the same [tracing patch](harness-matters/trace-harness.patch){download="trace-harness.patch"}. It makes waveform recording opt-in with `+trace` and changes the trace depth from 24 to 4. This is a testbench adjustment, separate from the RTL bug below. Download it and the [bug mutation](harness-matters/seed.patch){download="seed.patch"} outside the source directory, then apply them from the Caliptra root (Listing 4):

<figure id="l4" aria-labelledby="l4cap">
<pre><code><span class="p">$ </span>patch --dry-run --fuzz=0 -p1 &lt; ../trace-harness.patch
<span class="p">$ </span>patch --fuzz=0 -p1 &lt; ../trace-harness.patch
<span class="p">$ </span>patch --dry-run --fuzz=0 -p1 &lt; ../seed.patch
<span class="p">$ </span>patch --fuzz=0 -p1 &lt; ../seed.patch</code></pre>
          <figcaption id="l4cap"><b>Listing 4.</b> Apply the tracing patch and the bug mutation.</figcaption>
        </figure>

The agent received a source snapshot without Git history or the saved patches. The Booley setup also contained the pinned `.booley_project`, with root-level `AGENTS.md` and `CLAUDE.md` symlinked to `.booley_project/AGENTS.md`. The other setup contained Caliptra's native build scripts without that project directory or guidance. Start each attempt from a fresh copy of the prepared snapshot.

These steps recreate the source mutation and task. The reported timing comparison also used frozen container images and the archived Booley project, so a run with a different toolchain or project revision is not an exact repeat of that environment.

### Exact code mutation

The mutation removes `& lsu_busreq_r` from all four address-match expressions in `src/riscv_core/veer_el2/rtl/lsu/el2_lsu_bus_intf.sv`. Restoring the removed guards fixes the bug. Listing 5 shows the four changed lines; the downloadable patch also includes the surrounding context.

<figure id="l5" aria-labelledby="l5cap">
<pre class="diff"><code><span class="c">--- a/src/riscv_core/veer_el2/rtl/lsu/el2_lsu_bus_intf.sv
+++ b/src/riscv_core/veer_el2/rtl/lsu/el2_lsu_bus_intf.sv
@@ -241,4 +241,4 @@</span>
<span class="del">-   assign ld_addr_rhit_lo_lo = (lsu_addr_m[31:2] == lsu_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m &amp; lsu_busreq_r;</span>
<span class="del">-   assign ld_addr_rhit_lo_hi = (end_addr_m[31:2] == lsu_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m &amp; lsu_busreq_r;</span>
<span class="del">-   assign ld_addr_rhit_hi_lo = (lsu_addr_m[31:2] == end_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m &amp; lsu_busreq_r;</span>
<span class="del">-   assign ld_addr_rhit_hi_hi = (end_addr_m[31:2] == end_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m &amp; lsu_busreq_r;</span>
<span class="add">+   assign ld_addr_rhit_lo_lo = (lsu_addr_m[31:2] == lsu_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m;</span>
<span class="add">+   assign ld_addr_rhit_lo_hi = (end_addr_m[31:2] == lsu_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m;</span>
<span class="add">+   assign ld_addr_rhit_hi_lo = (lsu_addr_m[31:2] == end_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m;</span>
<span class="add">+   assign ld_addr_rhit_hi_hi = (end_addr_m[31:2] == end_addr_r[31:2]) &amp; lsu_pkt_r.valid &amp; lsu_pkt_r.store &amp; lsu_busreq_m;</span></code></pre>
          <figcaption id="l5cap"><b>Listing 5.</b> The bug mutation in <code>el2_lsu_bus_intf.sv</code>. Scroll sideways for the full lines.</figcaption>
        </figure>

The downloadable [`seed.patch`](harness-matters/seed.patch){download="seed.patch"} has SHA-256:

<pre class="sum"><code>247a8a96d300af8c5b243a9fffaef2ce2762df317d477f547c94fc98d0aea0b0</code></pre>

### Exact agent prompt

Both setups received this text unchanged (Listing 6). [Download the prompt](harness-matters/agent-prompt.txt){download="agent-prompt.txt"} to preserve the original line breaks.

<figure id="l6" aria-labelledby="l6cap">
<pre class="prompt"><code>Bug report against this Caliptra RTL snapshot.

Runtime ROM self-integrity digest is wrong, but only when the digest loop uses a DCCM scratch buffer

Early in the boot flow the runtime firmware recomputes a digest over its own
ROM image (a self-integrity check) and compares it against a constant that was
fixed at build time. The check normally passes.

We hit a case where the computed digest is wrong even though the ROM image on
the part is byte-for-byte unchanged and the compare constant is correct. No
error, fault, or status bit is set anywhere; the core reports no exception and
the memories report no ECC or parity events. The wrong digest is fully
deterministic across resets.

While bisecting the firmware we found the trigger is a coding pattern, not the
data: the digest routine that fails reads the ROM image word by word and, in
the same tight loop, writes each partially-accumulated result to a small
scratch buffer that the linker placed in DCCM. A functionally identical
version of the routine that keeps the accumulator only in registers (no scratch
writes in the loop), or that moves the scratch buffer out of DCCM, produces the
correct digest every time. The failing and passing routines compute the same
math and touch the same ROM bytes; only the placement/interleaving of the
scratch writes differs.

The corruption is confined to the very start of the digested region: the
recomputed digest diverges from the expected one as if one word of the ROM
input had been replaced, and everything after that point is consistent with the
true ROM contents.

No existing test reproduces the problem.

Find and correct the RTL root cause. Add a test that reproduces the problem
and passes after your fix, then run the relevant verification checks.</code></pre>
          <figcaption id="l6cap"><b>Listing 6.</b> The agent prompt, identical for both setups.</figcaption>
        </figure>

The downloadable [`agent-prompt.txt`](harness-matters/agent-prompt.txt){download="agent-prompt.txt"} has SHA-256:

<pre class="sum"><code>55fc823860c02e841b3c7b1ee46d81a014220b8244304dd5d7d04148ed731ea7</code></pre>

</div>
</details>
