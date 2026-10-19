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

Requirements, upgrades, and troubleshooting are in the
[installation guide](https://github.com/boldaxolotl/booley#installation).

## DESCRIPTION

Booley turns Claude Code or Codex into a capable RTL assistant. It runs the
Developer Agent in a sandbox, hands it real EDA tools, and checks its work
against your acceptance criteria: passing tests, area and timing budgets,
cycle counts, coverage, and more. You design; it does the grunt work.

## FEATURES

### Integrated development environment

RTL development is fragmented across editors, tool-specific commands, build environments, logs, and waveform viewers. Booley brings that workflow together in one reproducible VS Code workspace.

- **A typed interface for each Booley Flow:** a Flow is one command (`sim`, `lint`, `synth`, or `fpga`) that runs an EDA job end to end and returns a structured result: pass/fail plus metrics such as area, timing, or cycle counts, instead of a raw log to grep. Each Flow's interface stays the same across EDA tools and across projects: `sim` stays `sim` whether it runs Verilator today or Xcelium<sup>*</sup> tomorrow, and on every project you work on. Flows are built on [FuseSoC](https://github.com/olofk/fusesoc), so there's no per-repo EDA glue to learn or maintain.
- **Reproducible team environment:** configure the project once, and its Docker environment supplies the same pinned EDA stack, agent tooling, and system dependencies to every team member. Nobody has to rebuild the toolchain independently or debug "works on my machine" differences ([why Docker](https://github.com/boldaxolotl/Booley/blob/main/docs/internals/WHY.md#why-docker)).
- **One Window:** RTL, the Developer Agent, terminals, EDA runs, results, and waveform viewing live in a single VS Code window. You can move from editing to simulation to waveform debugging to synthesis without switching between separate applications.

<sub>* Xcelium support is a work in progress.</sub>

### Built for agentic workflows

The mental model behind Booley is simple: treat the Developer Agent like a talented junior engineer. It can write RTL and testbenches, but it is inexperienced with EDA tools, prone to questionable design decisions, and too risky to give unrestricted host access—it could, for example, force-push to your Git repository and rewrite its history. Booley gives it a constrained workspace, explicit specifications, automated checks, and human review.

- **Sandboxed for autonomous execution:** the Developer Agent and every command it launches run inside a Docker container with restricted mounts and network access. You can delegate long-running tasks to Developer Agents without approving every bash tool call and without worrying about your files and git history ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/user/FEATURES.md#docker-sandboxing), [security model](https://github.com/boldaxolotl/Booley/blob/main/docs/internals/ARCHITECTURE.md#security--trust-model)).
- **Goal Mode — guardrails for long autonomous work:** you set mandatory Goals at entry. **Booley checks whether each Goal is met in deterministic code**, using recorded results from Booley Flows and Specialists rather than the LLM's claim of success. Area and cycle-count Goals (`SYNTH:synth_fifo (area increase ≤ 10%)`, `CYCLE_COUNT:sim_fifo (≤ 1,000 cycles, test smoke)`) keep the Developer Agent within your PPA budget, while coverage and mutation-testing Goals (`COVERAGE:sim_fifo (line ≥ 90%, tests all)`, `MUTATION:sim_fifo (≥ 10/10 mutations detected, scope rtl/fifo.sv)`) help it write stronger testbenches. Saved Goals and evidence keep long sessions on track, including across context compaction; completion requires fresh evidence for every Goal. At review time, one briefing shows achieved Goals and their evidence, approved Goal changes, open review findings, and Target and constraint edits, so you can see what passed and what needs attention ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/user/USAGE.md#goal-mode)).
- **Waveform-aware debugging:** `bwave` lets the Developer Agent query real traces instead of guessing from RTL. Ask "How many `i_ready`/`o_valid` handshakes occurred between 1,000 and 2,000 ns?" or "When did `data_o` equal `0xDEADBEEF`?" The Developer Agent answers from actual simulation data instead of spending minutes reasoning from code (and getting it wrong) ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/user/FEATURES.md#waveform-based-debug)).

See [FEATURES.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/FEATURES.md) for the full list of capabilities.

## FLOWS

<dl class="man-dl">
<dt>sim</dt><dd>Simulation with Verilator or Icarus Verilog. cocotb testbenches work as they are.</dd>
<dt>lint</dt><dd>Verilator and Verible.</dd>
<dt>synth</dt><dd>Yosys, plus OpenROAD for physical timing on Nangate45. A PPA estimate, not tape-out.</dd>
<dt>fpga</dt><dd>AMD Vivado 2025.2, from your own licensed install.</dd>
<dt>bwave</dt><dd>Waveform queries for the agent: find, sample, snapshot, measure.</dd>
</dl>

Synopsys VCS and Cadence Xcelium are in progress. Versions and platform
details: [supported EDA tools](https://github.com/boldaxolotl/booley/blob/main/docs/user/SUPPORTED-EDA-TOOLS.md).

## LIMITATIONS

- **The IDE shell is stock VS Code today.** Booley brings its Developer Agent chat, reproducible environment, EDA Flows, and waveform tooling together inside VS Code; it does not yet ship custom editor chrome or a standalone IDE. Native VS Code UI and, longer term, a VS Code fork are planned ([roadmap](https://github.com/boldaxolotl/Booley/blob/main/docs/internals/ROADMAP.md#native-ide-surface-in-vs-code)).
- **Booley will not design hardware for you.** You design the architecture and write the specs; Booley handles the grunt work. Force multiplier, not replacement.
- **You need prior digital design experience.** Even the most advanced LLM is useless without electronic engineering fundamentals; Booley assumes you can read RTL, judge a waveform, and know what a sane result looks like.
- **SystemVerilog and Verilog only.** VHDL and UVM are not supported.
- **Setup can take effort.** I've tried to make the setup process as streamlined as possible, but every build system is different; complex flows or heavy licensed EDA tools may still need project-specific work. It's a price you pay once, though. After that, every Goal and every session builds on it, and development speeds up significantly.
- **Work in progress.** Expect occasional bugs and rough edges in the UI. I'm actively on it, and things keep getting better.

## HOW IT IS BUILT

Booley was designed and is maintained by a hardware engineer, not a career software engineer. Its first-party code was written by Claude and Codex, but this is not vibe-coding: I define the architecture and specifications, weigh design tradeoffs, review implementation plans and code, direct revisions, and make the final engineering decisions. Every change also goes through separate agent and human reviews, including QA passes aimed specifically at finding bugs.

Development follows Booley's [coding principles](https://github.com/boldaxolotl/Booley/blob/main/docs/internals/CODING_PRINCIPLES.md), with isolated branches, pull-request review, type checking, linting, automated tests, coverage requirements, and CI. The project is still young and hasn't yet had extensive review or long-term maintenance from experienced software engineers; those contributions are especially welcome.

</div>
