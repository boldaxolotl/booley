---
title: Why AI agents need good tools for hardware debugging
description: >-
  The same agent, the same Caliptra bug, with and without Booley. Both fixed
  it every time; with Booley the agent finished in 21 minutes instead of 56.
date: 2026-10-16
slug: harness-matters
draft: true
# Application-note number, shown in the title strip and the footer.
number: AN-001
# Exhibits listed under the contents sidebar (id within this page, label).
exhibits:
  - [f1, "Figure 1. Time per bug fix"]
  - [f2, "Figure 2. Seeing inside the design, b3"]
  - [f3, "Figure 3. Build setup, b1"]
  - [f4, "Figure 4. Sleeping on finished jobs, b10"]
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
      <p>I gave the same agent the same hardware bug, with and without Booley. Both setups produced a fix and a reproducing test in every trial. With Booley, the agent finished in 21 minutes on average; without it, 56 minutes. Most of the difference came from the work around the fix: building a test that exposed the bug, inspecting what happened inside the processor, and getting results back promptly.</p>
    </section>

<!-- more -->

Modern AI agents are very good at reasoning about code and finding bugs, including in hardware designs. But even smart models need good tools to work efficiently.

I use AI agents in my chip-design work daily, and I'm building [Booley](https://github.com/boldaxolotl/booley), an open-source development environment for AI-assisted FPGA and ASIC work. The environment and interfaces around an agent are often called its *harness*. For hardware development, they let the agent build the design, run simulations, inspect internal signals, and act on the results.

## A real bug in a real processor

I first tried bug-fixing tasks from a subset of the CVDP benchmark. That subset was already too easy for the agents I tested. I needed harder problems to learn whether better debugging tools would make a difference.

So I looked for the largest, high-quality open-source hardware description, or RTL, codebase I could find. I chose [Caliptra](https://github.com/chipsalliance/caliptra-rtl), a hardware root of trust: a security subsystem with its own processor and firmware.

Even in Caliptra, finding a suitable bug took longer than I expected. I screened about 16 candidates, including historical fixes reverted back into the code and still-open issues. An agent identified each bug by reading the code, usually within a few minutes. I wanted a task that also required simulation and debugging.

I settled on a historical bug in VeeR EL2, the RISC-V processor inside Caliptra, and reintroduced it by reverting an upstream fix from the [VeeR EL2 1.4 release](https://github.com/chipsalliance/Cores-VeeR-EL2/commit/fb3354352be70781450ea70963c44d4b8f88ae9b).

During startup, the processor checks the contents of its read-only program memory, or ROM. Under a particular sequence of reads and writes, that check fails even though the stored program is correct. The first word read from ROM has the wrong value.

The agent's task was to find and fix the cause in the RTL, then add a test that fails before the fix and passes afterward.

<details open markdown>
<summary>The bug report and exact RTL fault</summary>
<div class="details__body" markdown>

The agent received an issue-style report describing this behavior:

> Early in boot, the firmware recomputes a digest over its own ROM image and compares it with a constant fixed at build time. Normally the check passes. In one case the digest comes out wrong, even though the ROM is unchanged and the constant is correct. No fault, no exception, no ECC event, fully deterministic. The trigger is a coding pattern: the failing routine reads ROM word by word and, in the same tight loop, writes partial results to a scratch buffer in DCCM. Keep the accumulator in registers, or move the buffer out of DCCM, and the digest is correct. Only the first word of the ROM input looks corrupted.
>
> Find and fix the RTL root cause. Add a test that reproduces the problem and passes after the fix.

DCCM is the processor's tightly coupled data memory. In `el2_lsu_bus_intf.sv`, a missing `& lsu_busreq_r` guard allows a store to DCCM to forward into a subsequent load from ROM address zero. The load returns `0x0` instead of the ROM word. The upstream [release notes](https://github.com/chipsalliance/Cores-VeeR-EL2/blob/fb3354352be70781450ea70963c44d4b8f88ae9b/release-notes.md) describe the fix as "Store data collision with DCCM DMA error when address is 0x0".

</div>
</details>

## Why reproducing it was harder than finding it

The processor uses *store-to-load forwarding*: a read can take its value directly from a recent write instead of waiting for that write to reach memory. A missing condition lets it select unrelated write data for a read from ROM.

In both setups, the agent usually identified the faulty line within five minutes. It then had to write a test that failed on the original design and passed after the fix. That would demonstrate the fault and help catch it if it returned later.

Two details made that test difficult to write:

- The test template marked ROM as a region where accesses may have side effects. That setting disables forwarding and hides the bug.
- Fetching instructions from uncached ROM was so slow that the write and read didn't overlap inside the processor's pipeline. The test had to create that overlap to expose the fault.

Most of the work went into building a test, simulating it, inspecting what happened, and adjusting it until it failed for the right reason.

## What I compared

I ran ten trials with **Claude Code alone**, using a shell and Caliptra's existing Makefiles and test scripts, and ten with **Claude Code plus Booley**. Both used Claude Code as the agent environment.

The Booley project was configured before the trials. The timings cover debugging, excluding the initial work of adapting Caliptra to Booley.

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

Booley supplied the simulation interface, waveform queries, and build configuration:

- The `sim` Flow builds and runs a simulation, then reports its result. Its interface stays the same across projects, so the agent doesn't have to learn each project's shell commands.
- B-Wave lets the agent query waveforms: recordings of how internal signals change during a simulation. The agent can search a recording and read signal values at particular cycles.
- FuseSoC, Booley's build system, organizes the source files, simulator settings, and firmware build steps. The Caliptra setup described these in FuseSoC core files and a test list.

## The result: 21 minutes versus 56

Both setups completed the task in all ten trials. Mean completion time was **21.3 minutes with Booley** and **55.8 minutes with Claude Code alone** (Figure 1). The medians were 21.4 and 53.3 minutes respectively.

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
          <rect x="120" y="302.5" width="110" height="57.5" fill="#2f5d8a"/>
          <rect x="120" y="279.5" width="110" height="23" fill="#7fbcb3"/>
          <rect x="120" y="277.5" width="110" height="2" fill="#e3b23c"/>
          <rect x="120" y="253.5" width="110" height="24" fill="#c9ced4"/>
          <text class="v" x="175" y="336" text-anchor="middle" style="fill:#fff">11.5</text>
          <text class="v" x="175" y="296" text-anchor="middle">4.6</text>
          <text class="v" x="175" y="270" text-anchor="middle">4.8</text>
          <path d="M120 278.5 H100" stroke="var(--ink-2)"/>
          <text class="ax" x="96" y="283" text-anchor="end">0.4</text>
          <text class="tot" x="175" y="242" text-anchor="middle">21.3 min</text>
          <text class="bar" x="175" y="388" text-anchor="middle">With Booley</text>

          <!-- Claude Code alone -->
          <rect x="300" y="261.5" width="110" height="98.5" fill="#2f5d8a"/>
          <rect x="300" y="191.5" width="110" height="70" fill="#7fbcb3"/>
          <rect x="300" y="178" width="110" height="13.5" fill="#b8402c"/>
          <rect x="300" y="120" width="110" height="58" fill="#e3b23c"/>
          <rect x="300" y="81.5" width="110" height="38.5" fill="#c9ced4"/>
          <text class="v" x="355" y="315" text-anchor="middle" style="fill:#fff">19.7</text>
          <text class="v" x="355" y="231" text-anchor="middle">14.0</text>
          <text class="v" x="355" y="189" text-anchor="middle" style="fill:#fff;font-size:12px">2.7</text>
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
      <figcaption id="f1cap"><b>Figure 1.</b> Mean time per bug fix, split by activity. Ten paired Caliptra bug-fix trials, all accepted. Segment values in minutes.</figcaption>
    </figure>

The chart separates builds and simulations, signal observation, waits after jobs had finished, and other work. The three sources of delay below recurred across the trials. I examined them in the run histories; I didn't run separate experiments to isolate each feature's contribution.

## Seeing why a test keeps passing

<figure id="f2" aria-labelledby="f2cap">
      <div class="frame tl" style="--grid:22.2222%">
        <div class="tl__row">
          <p class="tl__lab"><b>Claude Code alone</b>, trial b3 <span class="tl__tot">85 min</span></p>
          <div class="tl__track" role="img" aria-label="Claude Code alone, trial b3: 85 min in total. Observing internal signals: 37.8 min. Segments in minutes: other work 0.0&ndash;1.4; build and simulation 1.4&ndash;8.2; other work 8.2&ndash;8.7; observing signals 8.7&ndash;10.3; other work 10.3&ndash;10.8; observing signals 10.8&ndash;17.1; other work 17.1&ndash;17.6; observing signals 17.6&ndash;23.8; other work 23.8&ndash;24.2; observing signals 24.2&ndash;33.2; other work 33.2&ndash;33.7; observing signals 33.7&ndash;36.0; other work 36.0&ndash;36.5; observing signals 36.5&ndash;40.8; other work 40.8&ndash;41.6; observing signals 41.6&ndash;46.8; build and simulation 46.8&ndash;84.4; other work 84.4&ndash;84.7.">
            <i class="s else" style="left:0.00%;width:1.54%"></i>
            <i class="s build" style="left:1.54%;width:7.54%"></i>
            <i class="s else" style="left:9.09%;width:0.61%"></i>
            <i class="s obs" style="left:9.70%;width:1.72%"></i>
            <i class="s else" style="left:11.42%;width:0.54%"></i>
            <i class="s obs" style="left:11.97%;width:7.07%"></i>
            <i class="s else" style="left:19.03%;width:0.54%"></i>
            <i class="s obs" style="left:19.58%;width:6.87%"></i>
            <i class="s else" style="left:26.44%;width:0.48%"></i>
            <i class="s obs" style="left:26.92%;width:10.01%"></i>
            <i class="s else" style="left:36.93%;width:0.48%"></i>
            <i class="s obs" style="left:37.41%;width:2.61%"></i>
            <i class="s else" style="left:40.02%;width:0.54%"></i>
            <i class="s obs" style="left:40.57%;width:4.80%"></i>
            <i class="s else" style="left:45.37%;width:0.82%"></i>
            <i class="s obs" style="left:46.19%;width:5.83%"></i>
            <i class="s build" style="left:52.02%;width:41.77%"></i>
            <i class="s else" style="left:93.79%;width:0.28%"></i>
            <i class="mk-tri" style="left:9.39%"></i>
            <i class="mk-tri" style="left:11.69%"></i>
            <i class="mk-tri" style="left:19.31%"></i>
            <i class="mk-tri" style="left:26.69%"></i>
            <i class="mk-tri" style="left:37.18%"></i>
            <i class="mk-tri" style="left:40.30%"></i>
            <i class="mk-tri" style="left:45.96%"></i>
          </div>
          <p class="tl__note">Observing internal signals: 37.8 min</p>
        </div>
        <div class="tl__row">
          <p class="tl__lab"><b>With Booley</b>, trial b3 <span class="tl__tot">27 min</span></p>
          <div class="tl__track" role="img" aria-label="With Booley, trial b3: 27 min in total. Observing internal signals: 9.8 min. Segments in minutes: other work 0.0&ndash;1.6; build and simulation 1.6&ndash;5.4; other work 5.4&ndash;6.0; observing signals 6.0&ndash;12.9; build and simulation 12.9&ndash;13.2; observing signals 13.2&ndash;14.7; build and simulation 14.7&ndash;15.5; observing signals 15.5&ndash;17.1; build and simulation 17.1&ndash;25.3; other work 25.3&ndash;26.7.">
            <i class="s else" style="left:0.00%;width:1.74%"></i>
            <i class="s build" style="left:1.74%;width:4.26%"></i>
            <i class="s else" style="left:6.00%;width:0.69%"></i>
            <i class="s obs" style="left:6.69%;width:7.68%"></i>
            <i class="s build" style="left:14.37%;width:0.34%"></i>
            <i class="s obs" style="left:14.71%;width:1.58%"></i>
            <i class="s build" style="left:16.29%;width:0.89%"></i>
            <i class="s obs" style="left:17.18%;width:1.79%"></i>
            <i class="s build" style="left:18.97%;width:9.19%"></i>
            <i class="s else" style="left:28.16%;width:1.51%"></i>
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

When a test passes despite a suspected bug, you need to see what happened inside the design. In pair b3 (Figure 2), Claude Code alone added debug prints with `$display`, rebuilt the simulator, reran the test, and searched the output. It repeated that loop seven times, spending 37.8 minutes on signal observation.

With Booley, the agent built once with waveform recording enabled and asked B-Wave 16 questions about the recorded signals. That work took 9.8 minutes.

In trial b1, the agent searched the waveform to find whether the faulty address comparison ever fired. Listing 1 shows equivalent command lines, with output trimmed.

<figure id="l1" aria-labelledby="l1cap">
<pre><code><span class="p">$ </span>bwave find trace.fst '*bus_intf.ld_addr_rhit_lo_lo' 1
cycle 4687 caliptra_top_tb.caliptra_top_dut.rvtop.veer.lsu.bus_intf.ld_addr_rhit_lo_lo 1
cycle 4696 caliptra_top_tb.caliptra_top_dut.rvtop.veer.lsu.bus_intf.ld_addr_rhit_lo_lo 1
...
<span class="c"># 32 matches found</span></code></pre>
      <figcaption id="l1cap"><b>Listing 1.</b> B-Wave search for the faulty address comparison, trial b1. Output trimmed.</figcaption>
    </figure>

The comparison fired 32 times, yet the test still passed. The agent then asked for the surrounding signal values at the first match (Listing 2):

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

The read matched a write that should not have been eligible for forwarding. But the test's memory setting (`is_sideeffects_m = 1`) blocked forwarding (`ld_full_hit_m = 0`) and hid the bug.

The agent changed that setting in the test. Half a minute later, the test failed on the buggy design.

The agent could ask follow-up questions of the same recording without rebuilding. Across all ten pairs, it spent **9.4 minutes less on average** observing signals with Booley.

*The b3 chart's final segment (Figure 2), roughly 47 to 85 minutes, is a sequence of final tests. That time isn't counted as signal observation.*

## Making the build repeatable

<figure id="f3" aria-labelledby="f3cap">
      <div class="frame tl" style="--grid:12.5000%">
        <div class="tl__row">
          <p class="tl__lab"><b>Claude Code alone</b>, trial b1 <span class="tl__tot">74 min</span></p>
          <div class="tl__track" role="img" aria-label="Claude Code alone, trial b1: 74 min in total. Build setup and recovery: 36.0 min. Segments in minutes: other work 0.0&ndash;3.8; hunting env vars 3.8&ndash;10.2; build and simulation 10.2&ndash;11.8; other work 11.8&ndash;12.5; build and simulation 12.5&ndash;16.1; other work 16.1&ndash;16.3; build and simulation 16.3&ndash;17.6; sim hung 17.6&ndash;37.0; build and simulation 37.0&ndash;48.3; other work 48.3&ndash;48.5; build and simulation 48.5&ndash;56.8; other work 56.8&ndash;57.7; build and simulation 57.7&ndash;59.9; other work 59.9&ndash;60.7; sim hung 60.7&ndash;65.8; build and simulation 65.8&ndash;66.2; other work 66.2&ndash;66.4; sim hung 66.4&ndash;71.4; build and simulation 71.4&ndash;74.3.">
            <i class="s else" style="left:0.00%;width:4.78%"></i>
            <i class="s hunt" style="left:4.78%;width:8.04%"></i>
            <i class="s build" style="left:12.81%;width:1.98%"></i>
            <i class="s else" style="left:14.79%;width:0.79%"></i>
            <i class="s build" style="left:15.57%;width:4.55%"></i>
            <i class="s else" style="left:20.12%;width:0.26%"></i>
            <i class="s build" style="left:20.39%;width:1.58%"></i>
            <i class="s hung" style="left:21.96%;width:24.24%"></i>
            <i class="s build" style="left:46.20%;width:14.16%"></i>
            <i class="s else" style="left:60.36%;width:0.20%"></i>
            <i class="s build" style="left:60.56%;width:10.46%"></i>
            <i class="s else" style="left:71.03%;width:1.06%"></i>
            <i class="s build" style="left:72.09%;width:2.76%"></i>
            <i class="s else" style="left:74.85%;width:1.05%"></i>
            <i class="s hung" style="left:75.90%;width:6.33%"></i>
            <i class="s build" style="left:82.23%;width:0.53%"></i>
            <i class="s else" style="left:82.75%;width:0.20%"></i>
            <i class="s hung" style="left:82.95%;width:6.33%"></i>
            <i class="s build" style="left:89.28%;width:3.55%"></i>
          </div>
          <p class="tl__note">Build setup and recovery: 36.0 min</p>
        </div>
        <div class="tl__row">
          <p class="tl__lab"><b>With Booley</b>, trial b1 <span class="tl__tot">22 min</span></p>
          <div class="tl__track" role="img" aria-label="With Booley, trial b1: 22 min in total. Build setup and recovery: 0.0 min. Segments in minutes: other work 0.0&ndash;1.8; build and simulation 1.8&ndash;7.3; other work 7.3&ndash;8.0; build and simulation 8.0&ndash;11.7; other work 11.7&ndash;13.2; build and simulation 13.2&ndash;13.9; other work 13.9&ndash;14.4; build and simulation 14.4&ndash;20.4; other work 20.4&ndash;21.5.">
            <i class="s else" style="left:0.00%;width:2.27%"></i>
            <i class="s build" style="left:2.27%;width:6.91%"></i>
            <i class="s else" style="left:9.19%;width:0.85%"></i>
            <i class="s build" style="left:10.04%;width:4.61%"></i>
            <i class="s else" style="left:14.65%;width:1.79%"></i>
            <i class="s build" style="left:16.44%;width:0.91%"></i>
            <i class="s else" style="left:17.35%;width:0.66%"></i>
            <i class="s build" style="left:18.01%;width:7.51%"></i>
            <i class="s else" style="left:25.53%;width:1.37%"></i>
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
          <li><i class="sw build"></i>Build and simulation</li>
          <li><i class="sw else"></i>Everything else (reading, reasoning, editing)</li>
        </ul>
      </div>
      <figcaption id="f3cap"><b>Figure 3.</b> Getting the build to work at all, trial pair b1. Claude Code alone rediscovered the build environment variables, then lost them between shell calls three times; the Booley run spent no time on setup.</figcaption>
    </figure>

Caliptra's simulation Makefiles need environment variables that point to dependencies. All ten runs with Claude Code alone hit missing configuration; most recovered within a few minutes.

In b1 (Figure 3), the agent worked out which variables to set and exported them in a shell call. Those values didn't carry over to later calls in this run. The firmware build then failed to produce `program.hex`, and the simulation started without a program. It waited until its timeout. This happened three times before the agent put the configuration in a single `run.sh` script.

The run spent about 36 minutes dealing with setup, or 30 minutes under a stricter count of directly attributable time.

The Booley project's FuseSoC core files already held the dependency paths, so the agent didn't have to rediscover the Makefile variables. Each simulation invocation supplied its build configuration and firmware build step, without relying on exports from a previous shell call. If the firmware build failed, Booley stopped the run and reported the build error before starting a simulation without its program.

The Booley run in pair b1 spent no time repairing setup. Across all ten pairs, the average setup saving was **3.6 minutes**; b1 was the unusually costly case. Configuring the project once avoided repeating that work during debugging.

## Getting the result when the job finishes

<figure id="f4" aria-labelledby="f4cap">
      <div class="frame tl" style="--grid:15.3846%">
        <div class="tl__row">
          <p class="tl__lab"><b>Claude Code alone</b>, trial b10 <span class="tl__tot">60 min</span></p>
          <div class="tl__track" role="img" aria-label="Claude Code alone, trial b10: 60 min in total. Sleeping after the job already finished: 30.7 min. Segments in minutes: other work 0.0&ndash;4.0; build and simulation 4.0&ndash;4.4; sleeping on a finished job 4.4&ndash;13.5; build and simulation 13.5&ndash;17.7; sleeping on a finished job 17.7&ndash;23.2; build and simulation 23.2&ndash;24.7; sleeping on a finished job 24.7&ndash;33.2; other work 33.2&ndash;34.4; build and simulation 34.4&ndash;34.8; other work 34.8&ndash;34.9; build and simulation 34.9&ndash;43.8; sleeping on a finished job 43.8&ndash;51.6; build and simulation 51.6&ndash;56.1; other work 56.1&ndash;60.1.">
            <i class="s else" style="left:0.00%;width:6.23%"></i>
            <i class="s build" style="left:6.23%;width:0.51%"></i>
            <i class="s sleep" style="left:6.74%;width:13.98%"></i>
            <i class="s build" style="left:20.72%;width:6.46%"></i>
            <i class="s sleep" style="left:27.18%;width:8.54%"></i>
            <i class="s build" style="left:35.72%;width:2.22%"></i>
            <i class="s sleep" style="left:37.94%;width:13.11%"></i>
            <i class="s else" style="left:51.05%;width:1.83%"></i>
            <i class="s build" style="left:52.88%;width:0.63%"></i>
            <i class="s else" style="left:53.51%;width:0.12%"></i>
            <i class="s build" style="left:53.63%;width:13.74%"></i>
            <i class="s sleep" style="left:67.37%;width:12.02%"></i>
            <i class="s build" style="left:79.38%;width:6.97%"></i>
            <i class="s else" style="left:86.35%;width:6.08%"></i>
          </div>
          <p class="tl__note">Sleeping after the job already finished: 30.7 min</p>
        </div>
        <div class="tl__row">
          <p class="tl__lab"><b>With Booley</b>, trial b10 <span class="tl__tot">13 min</span></p>
          <div class="tl__track" role="img" aria-label="With Booley, trial b10: 13 min in total. Sleeping after the job already finished: 0.0 min. Segments in minutes: other work 0.0&ndash;2.0; build and simulation 2.0&ndash;5.3; other work 5.3&ndash;5.7; build and simulation 5.7&ndash;6.7; other work 6.7&ndash;6.8; build and simulation 6.8&ndash;12.8.">
            <i class="s else" style="left:0.00%;width:3.08%"></i>
            <i class="s build" style="left:3.08%;width:5.06%"></i>
            <i class="s else" style="left:8.14%;width:0.57%"></i>
            <i class="s build" style="left:8.71%;width:1.57%"></i>
            <i class="s else" style="left:10.28%;width:0.20%"></i>
            <i class="s build" style="left:10.48%;width:9.23%"></i>
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
      <figcaption id="f4cap"><b>Figure 4.</b> Waiting on jobs that are already done, trial pair b10. Claude Code alone polled with fixed <code>sleep</code> calls; Booley returned each result when the job ended.</figcaption>
    </figure>

The agent also needs to know when a build finishes. In several runs with Claude Code alone, it started work in the background and used a fixed sleep before checking the result. In b10 (Figure 4), a build started at 4.3 minutes and failed one second later. The agent had already requested `sleep 540`, a nine-minute wait, and didn't check the result until 13.4 minutes.

It made similar waits four times in that run, spending 30.7 of its 60 minutes asleep after jobs had finished.

Booley's simulation interface waits for completion and returns the result. In the release used here, a job lasting more than four minutes returned a run ID. The agent could then call `booley_poll`, which waited again and returned when the job ended, without an arbitrary sleep.

The Booley run in pair b10 took 13 minutes in total. Across all ten pairs, it spent **11.2 minutes less on average** waiting on finished jobs. These waits affected six pairs. I counted only waits where I could establish that the job had finished, so the estimate is a lower bound.

## What this experiment shows

The agent found the likely RTL fault quickly in either setup. Most of its work went into reproducing the failure and testing the fix. Booley shortened that work with waveform queries, a repeatable build, and job results that arrived when the job finished.

This was one bug, one design, one model, and ten trials per setup. I chose a bug that needed substantial simulation work and used an already configured Booley project. These results don't establish the same speedup for other tasks or models, or tell us which capability would help most on its own. I also built Booley and ran the experiment myself.

## Try it on a design

Start with the smaller [PicoRV32 demo](../../demos.md#picorv32). The processor project is already configured for Booley: give the agent a change to make, then inspect its work.

For the design used here, see the [Caliptra project](https://github.com/boldaxolotl/booley-prj-caliptra). Its README explains the setup. The [reproduction details below](#reproduce) contain the recorded versions, original prompt, and patches.

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
            <tr><th scope="row">Booley Caliptra project</th><td>Recorded commit <code>e9dd79e7a31067c438d821b7e54efe397f019015</code>, mounted at <code>.booley_project</code>. This historical commit is absent from the current public <a href="https://github.com/boldaxolotl/booley-prj-caliptra">booley-prj-caliptra</a> history. Recreating the exact Booley environment requires the archived project snapshot; using the current public project is a different setup.</td></tr>
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
