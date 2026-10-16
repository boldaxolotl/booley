---
date: 2026-10-16
slug: harness-matters
draft: true
---

# Why AI agents need good tools for hardware debugging

Modern AI agents are very good at reasoning about code and finding bugs, including in hardware designs. But even smart models need good tools to work efficiently.

I use AI agents in my chip-design work daily, and I'm building [Booley](https://github.com/boldaxolotl/booley), an open-source development environment for AI-assisted FPGA and ASIC work.

To see how much those tools matter, I gave the same agent the same hardware bug, with and without Booley. Both setups produced a fix and a reproducing test in every trial. With Booley, the agent finished in **21 minutes on average**; without it, **56 minutes**.

Most of the difference came from the work around the fix: building a test that exposed the bug, inspecting what happened inside the processor, and getting results back promptly.

<!-- more -->

The environment and interfaces around an agent are often called its *harness*. For hardware development, they let the agent build the design, run simulations, inspect internal signals, and act on the results.

## A real bug in a real processor

I first tried bug-fixing tasks from a subset of the CVDP benchmark. That subset was already too easy for the agents I tested. I needed harder problems to learn whether better debugging tools would make a difference.

So I looked for the largest, high-quality open-source hardware description, or RTL, codebase I could find. I chose [Caliptra](https://github.com/chipsalliance/caliptra-rtl), a hardware root of trust: a security subsystem with its own processor and firmware.

Even in Caliptra, finding a suitable bug took longer than I expected. I screened about 16 candidates, including historical fixes reverted back into the code and still-open issues. An agent identified each bug by reading the code, usually within a few minutes. I wanted a task that also required simulation and debugging.

I settled on a historical bug in VeeR EL2, the RISC-V processor inside Caliptra, and reintroduced it by reverting an upstream fix from the [VeeR EL2 1.4 release](https://github.com/chipsalliance/Cores-VeeR-EL2/commit/fb3354352be70781450ea70963c44d4b8f88ae9b).

During startup, the processor checks the contents of its read-only program memory, or ROM. Under a particular sequence of reads and writes, that check fails even though the stored program is correct. The first word read from ROM has the wrong value.

The agent's task was to find and fix the cause in the RTL, then add a test that fails before the fix and passes afterward.

<details markdown="1">
<summary>The bug report and exact RTL fault</summary>

The agent received an issue-style report describing this behavior:

> Early in boot, the firmware recomputes a digest over its own ROM image and compares it with a constant fixed at build time. Normally the check passes. In one case the digest comes out wrong, even though the ROM is unchanged and the constant is correct. No fault, no exception, no ECC event, fully deterministic. The trigger is a coding pattern: the failing routine reads ROM word by word and, in the same tight loop, writes partial results to a scratch buffer in DCCM. Keep the accumulator in registers, or move the buffer out of DCCM, and the digest is correct. Only the first word of the ROM input looks corrupted.
>
> Find and fix the RTL root cause. Add a test that reproduces the problem and passes after the fix.

DCCM is the processor's tightly coupled data memory. In `el2_lsu_bus_intf.sv`, a missing `& lsu_busreq_r` guard allows a store to DCCM to forward into a subsequent load from ROM address zero. The load returns `0x0` instead of the ROM word. The upstream [release notes](https://github.com/chipsalliance/Cores-VeeR-EL2/blob/fb3354352be70781450ea70963c44d4b8f88ae9b/release-notes.md) describe the fix as "Store data collision with DCCM DMA error when address is 0x0".

</details>

## Why reproducing it was harder than finding it

The processor uses *store-to-load forwarding*: a read can take its value directly from a recent write instead of waiting for that write to reach memory. A missing condition lets it select unrelated write data for a read from ROM.

In both setups, the agent usually identified the faulty line within five minutes. It then had to write a test that failed on the original design and passed after the fix. That would demonstrate the fault and help catch it if it returned later.

Two details made that test difficult to write:

- The test template marked ROM as a region where accesses may have side effects. That setting disables forwarding and hides the bug.
- Fetching instructions from uncached ROM was so slow that the write and read didn't overlap inside the processor's pipeline. The test had to create that overlap to expose the fault.

Most of the work went into building a test, simulating it, inspecting what happened, and adjusting it until it failed for the right reason.

## What I compared

I ran ten trials with **Claude Code alone**, using a shell and Caliptra's existing Makefiles and test scripts, and ten with **Claude Code plus Booley**. The charts label these setups "without harness" and "with harness" respectively; both used Claude Code as the agent environment.

The Booley project was configured before the trials. The timings cover debugging, excluding the initial work of adapting Caliptra to Booley.

<details markdown="1">
<summary>Experiment settings and versions</summary>

- **Agent:** Claude Code 2.1.289, Sonnet 5.5, medium effort.
- **Booley:** published release 0.3.0.
- **Design:** Caliptra at commit `e379800c`, with the historical VeeR fix reverted.
- **Environment:** the same container image, 8 CPUs, 10 GB RAM, no internet access.
- **Task:** the same prompt and a 90-minute elapsed-time limit for every trial.
- **Trials:** ten per setup, numbered b1 through b10. Matching numbers identify the pairs shown in the charts.

</details>

Booley supplied the simulation interface, waveform queries, and build configuration:

- The `sim` Flow builds and runs a simulation, then reports its result. Its interface stays the same across projects, so the agent doesn't have to learn each project's shell commands.
- B-Wave lets the agent query waveforms: recordings of how internal signals change during a simulation. The agent can search a recording and read signal values at particular cycles.
- FuseSoC, Booley's build system, organizes the source files, simulator settings, and firmware build steps. The Caliptra setup described these in FuseSoC core files and a test list.

## The result: 21 minutes versus 56

![Mean completion time: 21.3 minutes with Booley and 55.8 minutes with Claude Code alone, split by activity](harness-matters/time-split.png)

Both setups completed the task in all ten trials. Mean completion time was **21.3 minutes with Booley** and **55.8 minutes with Claude Code alone**. The medians were 21.4 and 53.3 minutes respectively.

The chart separates builds and simulations, signal observation, waits after jobs had finished, and other work. The three sources of delay below recurred across the trials. I examined them in the run histories; I didn't run separate experiments to isolate each feature's contribution.

## Seeing why a test keeps passing

![Trial pair b3: repeated debug-print builds compared with waveform queries](harness-matters/factor-waveform.png)

When a test passes despite a suspected bug, you need to see what happened inside the design. In pair b3, Claude Code alone added debug prints with `$display`, rebuilt the simulator, reran the test, and searched the output. It repeated that loop seven times, spending 37.8 minutes on signal observation.

With Booley, the agent built once with waveform recording enabled and asked B-Wave 16 questions about the recorded signals. That work took 9.8 minutes.

In trial b1, the agent searched the waveform to find whether the faulty address comparison ever fired. Here are equivalent command lines, with output trimmed:

```text
$ bwave find trace.fst '*bus_intf.ld_addr_rhit_lo_lo' 1
cycle 4687 caliptra_top_tb.caliptra_top_dut.rvtop.veer.lsu.bus_intf.ld_addr_rhit_lo_lo 1
cycle 4696 caliptra_top_tb.caliptra_top_dut.rvtop.veer.lsu.bus_intf.ld_addr_rhit_lo_lo 1
...
# 32 matches found
```

The comparison fired 32 times, yet the test still passed. The agent then asked for the surrounding signal values at the first match:

```text
$ bwave value trace.fst --at 4687 -s '*bus_intf.ld_*' -s '*bus_intf.lsu_busreq*' -s '*bus_intf.is_sideeffects_m' ...
# Snapshot at cycle 4687
lsu_busreq_m                             = 1
is_sideeffects_m                         = 1
lsu_busreq_r                             = 0
ld_addr_rhit_lo_lo                       = 1
ld_full_hit_m                            = 0
```

The read matched a write that should not have been eligible for forwarding. But the test's memory setting (`is_sideeffects_m = 1`) blocked forwarding (`ld_full_hit_m = 0`) and hid the bug.

The agent changed that setting in the test. Half a minute later, the test failed on the buggy design.

The agent could ask follow-up questions of the same recording without rebuilding. Across all ten pairs, it spent **9.4 minutes less on average** observing signals with Booley.

*The b3 chart's final segment, roughly 47 to 85 minutes, is a sequence of final tests. That time isn't counted as signal observation.*

## Making the build repeatable

![Trial pair b1: build configuration errors and simulation timeouts](harness-matters/factor-setup.png)

Caliptra's simulation Makefiles need environment variables that point to dependencies. All ten runs with Claude Code alone hit missing configuration; most recovered within a few minutes.

In b1, the agent worked out which variables to set and exported them in a shell call. Those values didn't carry over to later calls in this run. The firmware build then failed to produce `program.hex`, and the simulation started without a program. It waited until its timeout. This happened three times before the agent put the configuration in a single `run.sh` script.

The run spent about 36 minutes dealing with setup, or 30 minutes under a stricter count of directly attributable time.

The Booley project's FuseSoC core files already held the dependency paths, so the agent didn't have to rediscover the Makefile variables. Each simulation invocation supplied its build configuration and firmware build step, without relying on exports from a previous shell call. If the firmware build failed, Booley stopped the run and reported the build error before starting a simulation without its program.

The Booley run in pair b1 spent no time repairing setup. Across all ten pairs, the average setup saving was **3.6 minutes**; b1 was the unusually costly case. Configuring the project once avoided repeating that work during debugging.

## Getting the result when the job finishes

![Trial pair b10: time spent sleeping after build jobs had already finished](harness-matters/factor-oversleep.png)

The agent also needs to know when a build finishes. In several runs with Claude Code alone, it started work in the background and used a fixed sleep before checking the result. In b10, a build started at 4.3 minutes and failed one second later. The agent had already requested `sleep 540`, a nine-minute wait, and didn't check the result until 13.4 minutes.

It made similar waits four times in that run, spending 30.7 of its 60 minutes asleep after jobs had finished.

Booley's simulation interface waits for completion and returns the result. In the release used here, a job lasting more than four minutes returned a run ID. The agent could then call `booley_poll`, which waited again and returned when the job ended, without an arbitrary sleep.

The Booley run in pair b10 took 13 minutes in total. Across all ten pairs, it spent **11.2 minutes less on average** waiting on finished jobs. These waits affected six pairs. I counted only waits where I could establish that the job had finished, so the estimate is a lower bound.

## What this experiment shows

The agent found the likely RTL fault quickly in either setup. Most of its work went into reproducing the failure and testing the fix. Booley shortened that work with waveform queries, a repeatable build, and job results that arrived when the job finished.

This was one bug, one design, one model, and ten trials per setup. I chose a bug that needed substantial simulation work and used an already configured Booley project. These results don't establish the same speedup for other tasks or models, or tell us which capability would help most on its own. I also built Booley and ran the experiment myself.

## Try it on a design

Start with the smaller [PicoRV32 demo](../../demos.md#picorv32). The processor project is already configured for Booley: give the agent a change to make, then inspect its work.

For the design used here, see the [Caliptra project](https://github.com/boldaxolotl/booley-prj-caliptra). Its README explains the setup. The [reproduction details below](#reproduce) contain the recorded versions, original prompt, and patches.

<details id="reproduce" markdown="1">
<summary>How to reproduce (spoilers)</summary>

These are the inputs used in the experiment. Prepare the bug before starting the agent, and give it only the prompt below. The patch and the rest of this article reveal the answer.

**Pinned versions**

- Caliptra: `e379800c5467379386f96a9a51a539bd662b608a` in [chipsalliance/caliptra-rtl](https://github.com/chipsalliance/caliptra-rtl).
- Adams Bridge, at `submodules/adams-bridge`: `2f15ffb1a2e9bd99652fa04d434a48ffcdc23f42` in [chipsalliance/adams-bridge](https://github.com/chipsalliance/adams-bridge).
- Booley: published package `booley-rtl==0.3.0`, source commit `f9ec6d5602f3fcf47f48b678a3ba821fb82c859c` in [boldaxolotl/booley](https://github.com/boldaxolotl/booley).
- Booley Caliptra project: recorded commit `e9dd79e7a31067c438d821b7e54efe397f019015`, mounted at `.booley_project`. This historical commit is absent from the current public [booley-prj-caliptra](https://github.com/boldaxolotl/booley-prj-caliptra) history. Recreating the exact Booley environment requires the archived project snapshot; using the current public project is a different setup.
- Agent: Claude Code `2.1.289`, model ID `claude-sonnet-5-5`, effort `medium`.
- Per trial: 8 CPUs, 10 GB RAM, a 4,096-process limit, and a 5,400-second time limit. Network access was restricted to the model API; web search, web fetching, and subagents were disabled.

**Prepare the source**

Check out the Caliptra revision above and initialize its submodules, then pin Adams Bridge to the recorded revision:

```sh
git clone https://github.com/chipsalliance/caliptra-rtl.git caliptra-source
cd caliptra-source
git checkout --detach e379800c5467379386f96a9a51a539bd662b608a
git submodule update --init --recursive
git -C submodules/adams-bridge checkout --detach 2f15ffb1a2e9bd99652fa04d434a48ffcdc23f42
```

Both setups also received the same [tracing patch](harness-matters/trace-harness.patch). It makes waveform recording opt-in with `+trace` and changes the trace depth from 24 to 4. This is a testbench adjustment, separate from the RTL bug below. Download it and the [bug mutation](harness-matters/seed.patch) outside the source directory, then apply them from the Caliptra root:

```sh
patch --dry-run --fuzz=0 -p1 < ../trace-harness.patch
patch --fuzz=0 -p1 < ../trace-harness.patch
patch --dry-run --fuzz=0 -p1 < ../seed.patch
patch --fuzz=0 -p1 < ../seed.patch
```

The agent received a source snapshot without Git history or the saved patches. The Booley setup also contained the pinned `.booley_project`, with root-level `AGENTS.md` and `CLAUDE.md` symlinked to `.booley_project/AGENTS.md`. The other setup contained Caliptra's native build scripts without that project directory or guidance. Start each attempt from a fresh copy of the prepared snapshot.

These steps recreate the source mutation and task. The reported timing comparison also used frozen container images and the archived Booley project, so a run with a different toolchain or project revision is not an exact repeat of that environment.

**Exact code mutation**

The mutation removes `& lsu_busreq_r` from all four address-match expressions in `src/riscv_core/veer_el2/rtl/lsu/el2_lsu_bus_intf.sv`. Restoring the removed guards fixes the bug. This diff shows the four changed lines; the downloadable patch also includes the surrounding context.

```diff
--- a/src/riscv_core/veer_el2/rtl/lsu/el2_lsu_bus_intf.sv
+++ b/src/riscv_core/veer_el2/rtl/lsu/el2_lsu_bus_intf.sv
@@ -241,4 +241,4 @@
-   assign ld_addr_rhit_lo_lo = (lsu_addr_m[31:2] == lsu_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m & lsu_busreq_r;
-   assign ld_addr_rhit_lo_hi = (end_addr_m[31:2] == lsu_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m & lsu_busreq_r;
-   assign ld_addr_rhit_hi_lo = (lsu_addr_m[31:2] == end_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m & lsu_busreq_r;
-   assign ld_addr_rhit_hi_hi = (end_addr_m[31:2] == end_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m & lsu_busreq_r;
+   assign ld_addr_rhit_lo_lo = (lsu_addr_m[31:2] == lsu_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m;
+   assign ld_addr_rhit_lo_hi = (end_addr_m[31:2] == lsu_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m;
+   assign ld_addr_rhit_hi_lo = (lsu_addr_m[31:2] == end_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m;
+   assign ld_addr_rhit_hi_hi = (end_addr_m[31:2] == end_addr_r[31:2]) & lsu_pkt_r.valid & lsu_pkt_r.store & lsu_busreq_m;
```

The downloadable `seed.patch` has SHA-256:

```text
247a8a96d300af8c5b243a9fffaef2ce2762df317d477f547c94fc98d0aea0b0
```

**Exact agent prompt**

Both setups received this text unchanged. [Download the prompt](harness-matters/agent-prompt.txt) to preserve the original line breaks.

```text
Bug report against this Caliptra RTL snapshot.

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
and passes after your fix, then run the relevant verification checks.
```

The downloadable `agent-prompt.txt` has SHA-256:

```text
55fc823860c02e841b3c7b1ee46d81a014220b8244304dd5d7d04148ed731ea7
```

</details>
