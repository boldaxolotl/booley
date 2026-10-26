# Features

Booley's core move is putting **one agent-native interface over the whole fragmented EDA toolchain**, every EDA tool and the Developer Agent behind the same typed surface, and wrapping the result in a single VS Code window. That makes Developer Agents more capable, but it also gives the engineer a faster, lower-friction RTL workflow. Every feature below builds on that foundation; the reasoning behind the load-bearing choices is in [WHY.md](../internals/WHY.md).

New to Booley's vocabulary (Developer Agent, Specialist, Sandbox, Booley Flow, Target, Goal Mode)? The [context map](../../GLOSSARY-MAP.md) points to each canonical glossary and its rejected synonyms.

- [One Interface Over Every EDA Tool and Developer Agent](#one-interface-over-every-eda-tool-and-developer-agent)
- [The Agentic RTL IDE](#the-agentic-rtl-ide)
- [Open Source and Local-First](#open-source-and-local-first)
- [Interactive Mode](#interactive-mode)
- [Goal Mode](#goal-mode)
- [Structured Booley Flow Contracts](#structured-booley-flow-contracts)
- [Named Targets and Tests](#named-targets-and-tests)
- [Evidence-backed Goals](#evidence-backed-goals)
- [Docker Sandboxing](#docker-sandboxing)
- [Fresh Context per Specialist](#fresh-context-per-specialist)
- [Waveform-Based Debug](#waveform-based-debug)
- [Goal recovery](#goal-recovery)
- [Multi-Category Code Review](#multi-category-code-review)
- [Mutation Testing](#mutation-testing)
- [Lint Triage](#lint-triage)
- [LLM Backend Selection](#llm-backend-selection)
- [Expert-Written RTL Guides](#expert-written-rtl-guides)
- [Guided Project Setup](#guided-project-setup)
- [Extensible Toolkit](#extensible-toolkit)
- [Parallel Instances](#parallel-sessions)
- [Windows Support](#windows-support)
- [Firmware-in-the-Loop Debug](#firmware-in-the-loop-debug)
- [Stealth Mode](#stealth-mode)

## One Interface Over Every EDA Tool and Developer Agent

An RTL flow is a pile of EDA tools that share nothing: Verilator, Icarus, Yosys, plus licensed heavyweight tools, each with its own CLI, flags, and output format. The Developer Agent driving them is a moving part too: today Claude Code, tomorrow Codex. Wire Developer Agents straight into that mess and you get N EDA tools × M Developer Agent clients of brittle glue. Booley collapses it to one interface:

- **One typed surface over every Booley Flow.** Each Flow drives its selected EDA tool through the same structured call and returns the same normalized verdict inside the Sandbox. A tool can be supplied by the standard image or by an authorized read-only host installation under a built-in policy; the Developer Agent learns one interface, not one per EDA tool ([details](#structured-booley-flow-contracts)).
- **Client-agnostic.** The same Booley Flows are exposed as MCP tools, the protocol-level functions Claude Code and Codex both invoke natively. Swap the model; the EDA stack stays identical ([details](#llm-backend-selection)).
- **Extension is linear, not multiplicative.** A new analysis is one custom MCP tool behind the same criteria contract ([details](#extensible-toolkit)); a new Developer Agent client is one MCP client. Neither requires rewiring the other side.

EDA-tool support lives in the [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md). One expectation worth setting up front: the built-in `synth` Flow is a **fast PPA (power/performance/area) estimate to optimize RTL against**, not tape-out synthesis. [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md#built-in-flows) spells out exactly what it does and doesn't cover.

## The Agentic RTL IDE

Booley is not only infrastructure for the Developer Agent. It is workflow infrastructure for the hardware engineer: editor, EDA tools, waveforms, run status, review evidence, and Developer Agent share a single VS Code window.

Work interactively through conversation—edit code, run Booley Flows, debug a failure, open a scoped waveform view (`bwave gui`, see [Waveform-Based Debug](#waveform-based-debug))—or let Goal sessions work unattended in container terminals alongside. In either mode, Booley removes the manual transitions among editor, build commands, logs, waveform viewer, and review artifacts. Less time spent on that plumbing means faster iterations and more opportunities to verify, refine, and optimize the design.

Today that means stock VS Code chrome plus the Developer Agent chat and the Booley Flows it drives; native UI surfaces (Goal status, evidence status, Flow dashboards) are on the [roadmap](../internals/ROADMAP.md#native-ide-surface-in-vs-code), and the long-term direction is Booley as a **standalone agentic IDE**.

## Open Source and Local-First

Booley is free under Apache 2.0 and runs entirely on your machine.

## Interactive Mode

Interactive Mode is the workflow for small tasks, codebase exploration, and interactive debug and coding sessions: anything human-in-the-loop. `booley init` registers a Booley MCP server with the Developer Agent CLI, so the Developer Agent can call Booley Flows and Specialists (`sim`, `lint`, `reviewer`, `mutation_tester`, ...) directly from natural-language prompts,  each call self-contained. See [USAGE.md: Interactive Mode](USAGE.md#interactive-mode).

## Goal Mode

Use `/booley-goal` in a Developer Agent session for work with mandatory completion
conditions. Project-owned Goalsets become concrete Goals at entry; one linked
worktree and Goal Branch hold the work. Only Booley Flow and Specialist evidence
meets Goals. Human-approved changes are recorded in the Change Log, and Finish
returns a Review Package and Session Summary. The same session supports guided
or unattended work. See [Goal Mode](GOAL-MODE.md#goal-mode).

## Structured Booley Flow Contracts

Booley Flows are evidence contracts between real EDA behavior and Booley's decision loop. An MCP tool call does not just dump terminal output back into context: it resolves Targets and tests, runs the Flow, validates the result, stores predictable logs/traces/reports, and returns a normalized verdict the Developer Agent can safely use.

This matters most for high-consequence steps: simulation, lint, synthesis, debug, review, mutation testing. A passing simulation satisfies criteria only when the Booley Flow returns a valid `pass` verdict. Lint and synthesis likewise report structured findings or metrics instead of EDA-tool-specific log fragments. Failures, timeouts, inconclusive runs, and contract errors stay distinct, so the Developer Agent can choose the right next action instead of guessing from noisy EDA output.

The same contract layer is what keeps project-specific build steps contained: a per-test firmware build rides `[flows.sim].pre_run_commands` inside the Sandbox, while a host-provisioned tool remains subject to host authority and keeps the same public Flow names, MCP schemas, criteria, artifact layout, and report format.

## Named Targets and Tests

Booley does not let Developer Agents assemble simulator command lines from ad-hoc fragments. Every buildable variant of the design is a **named Target** in a FuseSoC `.core` file (CAPI2, FuseSoC's YAML design-description format), with filesets, parameters, defines, and top module in one place, and every test is a **named entry** in `.booley/tests.toml` keyed by that Target. Booley Flows take `target` and `test` arguments that resolve against them:

```yaml
# design.core — one target per build
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: my_top_tb
    parameters: [FEATURE_A]
```

```toml
# tests.toml — tests keyed by target name
[sim]
tests = ["test_basic", "test_edge_cases"]
```

This is the identity half of the Booley Flow contract, and it matters for two reasons. First, **reproducibility**: a request to run one test resolves to exactly that test under exactly that Target's parameters and top module, and the resulting report carries the resolved identity. There is no gap between "what the Developer Agent asked for" and "what actually ran" for it to hallucinate into. Second, **conditional-compilation coverage**: designs with `ifdef`-gated features declare one Target per build, and Booley can fan Flows (lint, simulation, synthesis) across all of them instead of silently validating only the default build.

Because Targets and tests are data, not code, the same names mean the same thing to the Developer Agent, every Specialist, the Booley Flows, and you, across every run.

Run-time arguments and seeds are declared the same way. A **Test Variant** in `tests.toml` gives one Test a named, fixed set of extra arguments and environment, and every **Test Run** of a seeded Target has an explicit seed that Booley passes to the simulator and records:

```toml
[sim.variants.test_edge_cases]
default = "short"
short = ["+iters=1000"]
soak  = ["+iters=100000"]
```

```bash
booley flow sim --target sim --test test_edge_cases+soak@42   # one Test Variant, one seed
booley flow sim --target sim --test test_edge_cases@x20       # 20 drawn seeds
```

There is no option for free-form simulator arguments, so a pass always belongs to a declared configuration, and a failure prints the command that reruns it with the same seed. A simulation Goal can require a Test Run, or N seeds of one (`test+variant@xN`): Booley draws the seeds once at entry and the Goal is met when all of them have passed. Cycle Count and coverage Goals name Test Runs the same way and are decided by their thresholds and floors. See [Test Runs, Test Variants and seeds](FLOW-SIM.md#test-runs-test-variants-and-seeds).

The same model covers **cocotb (Python) testbenches**: a Target whose flow options declare a `cocotb_module` lists its `@cocotb.test()` function names in `tests.toml`, and `sim` runs the selected set batched in one sim process, taking per-test verdicts from cocotb's `results.xml`; `--test` filters the set the same way, and `SIM:{target}` keeps its meaning.

## Evidence-backed Goals

Every Goal is mandatory. Booley decides whether it is met from valid Flow or
Specialist evidence at the current code, and stale evidence requires another
run. Thresholds can enforce area, timing, cycle count, coverage, and mutation
budgets. The Developer Agent can propose a change; only the human can approve it.
See [Working with evidence](GOAL-MODE.md#working-with-evidence).

## Docker Sandboxing

Developer Agents run with `--dangerously-skip-permissions` to operate autonomously. Docker sandboxing keeps this safe: every Developer Agent runs inside the per-folder Sandbox container with only the project workspace mounted, full access inside, no access to the host outside. There is no general internet access either: egress is restricted to the LLM API endpoints through a Booley proxy, so a prompt-injection payload picked up from a web page has nowhere to reach. The Developer Agent runs as a non-root user, and an idle reaper stops orphaned sessions. Booley MCP calls and supervised commands update the activity heartbeat. A running Docker exec used by a Sandbox Attachment or another client prevents idle reaping for as long as that exec remains running; the reaper's own heartbeat reads do not count. An orphaned Sandbox is reaped once no client exec is running and its effective activity timestamp exceeds the timeout. The sandbox image ships every built-in EDA tool (see [One Interface Over Every EDA Tool and Developer Agent](#one-interface-over-every-eda-tool-and-developer-agent)) and both Claude Code and Codex CLIs preinstalled.

The sandbox is **customizable** in two ways. Use `[sandbox].image` for a project image that extends `booley-sandbox` with EDA tools that must exist in every container. Run a Project setup script under `<booley_dir>/hooks/` when a new worktree needs generated inputs before Goal entry.

## Fresh Context per Specialist

Long single-context Developer Agent sessions degrade: the model loses the thread, gets distracted by stale logs, and quality falls off over a multi-hour task. Booley's answer is context isolation: each Specialist (code review, mutation testing) runs in a fresh LLM context, so none of them drags along stale logs or drafts from earlier iterations. A fresh context also means a fresh perspective: a Specialist that didn't write the code has no attachment to it and no memory of the reasoning that produced it, so it judges what's actually there rather than what was intended. This matters most for the reviewer: a Developer Agent reviewing its own work tends to confirm it, while an independent Specialist finds the real issues. Continuity lives in structured state carried by the Developer Agent (Goal Records, evidence, summaries), not in an ever-growing conversation. See [ARCHITECTURE.md](../internals/ARCHITECTURE.md#the-sandbox).

## Waveform-Based Debug

For debugging, Booley **observes actual simulation behavior** through a custom-built Rust waveform EDA tool (`bwave`) instead of just reasoning about the code. It can query signal values at specific time ranges, use one signal as a trigger for sampling another, and trace data through the design: systematic debugging instead of guesswork.

**FST trace store.** Raw VCD files from complex designs can reach 10 GB+, so Booley's successful trace artifact is FST: the open, transition-based waveform format, typically 10-50x smaller than the source VCD and readable by any off-the-shelf viewer (GTKWave, VaporView). `bwave gui` puts a scoped view straight into the user's VS Code window. Booley reads a Target's authored native FST directly. When the Target has no trace generation, Booley adds VCD tracing and streams the VCD through a named pipe (FIFO) to `bwave`, which converts it to FST in parallel. A Cocotb Target always traces through a VCD file that Booley converts to FST after the run; see [Tracing a Cocotb Target](TARGETS.md#tracing-a-cocotb-target). The reasoning is in [WHY.md: Why FST is the trace contract](../internals/WHY.md#why-fst-is-the-trace-contract).

## Goal recovery

After interruption or context compaction, `goal_status(rules=true)` recovers the
record and working rules in the same worktree. Saved proposal and Finish IDs
make retries recoverable. Quiet presence does not end a Goal; abandonment
requires explicit human instruction. See [Recovery](GOAL-MODE.md#recovery-unattended-work-and-parallel-sessions).

## Multi-Category Code Review

Code review is split into focus categories with severity-stratified issue tracking:

- **Correctness category** (functional, protocol, ifdef checking) runs once the RTL is ready and compiles, before any simulation: the equivalent of an RTL engineer's "quick look at the code I have just written," catching bugs before they cost time and tokens in simulation-fix loops.
- **Quality category** (optional: security, optimization, coding standards) runs after the RTL is bug-free and targets issues beyond functional correctness.
- Findings are classified as CRITICAL, MAJOR, or MINOR. A `_clean` review requires every current finding, including MINOR, to be verified fixed or explicitly waived with user-visible justification. A terminal `_done` review reports findings without triggering fixes; advisory findings remain visible at Finish.
- Testbench code gets its own separate review.

The shipped reviewer is read-only: it reports issues by severity, and the Developer Agent resolves findings for reviews requiring a clean verdict. See [CLI-REFERENCE.md: reviewer](CLI-REFERENCE.md#reviewer) for the supported review kinds.

## Mutation Testing

Booley includes a mutation testing framework to validate test quality, which matters more in agentic workflows than in human-driven ones. The read-only Mutation Tester Specialist proposes targeted, exact source replacements—subtle bugs a good testbench should detect. Booley validates the proposal bytes, runs an untouched baseline, then applies and compiles one isolated replacement at a time. It does not parse SystemVerilog or inject runtime selectors; the project's configured compiler decides whether each variant is valid. The deterministic harness counts which variants the testbench catches and measures the detection rate against a configurable threshold.

## Lint Triage

The biggest issue with lint EDA tools is signal-to-noise ratio: dangerous warnings get buried under tons of false positives, and engineers respond by ignoring lint altogether. Working through every message is exactly the tedium automation can afford and a human can't; delegating lint-output triage is what makes lint useful in practice. The `lint` Booley Flow runs the linter selected by each lint Target, normalizes its findings, and deduplicates them across Targets so the same issue isn't reported many times. Built-in support currently covers Verilator for structural and semantic checks and Verible for style and naming rules; the same lint contract is designed to accommodate other EDA tools, including licensed programs such as SpyGlass, in the future. Waivers remain native to the selected linter (`.vlt` waiver lines for Verilator, rule configuration and waiver files for Verible). During autonomous development, the Developer decides, per finding, whether to fix the RTL, add a justified waiver, or leave it for human attention. The result is a clean lint report instead of noise. See the [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md#built-in-flows) for the current engines.

To attach waivers to a lint Target, see the
[Verilator](TARGETS.md#verilator-lint-target) and
[Verible](TARGETS.md#verible-lint-target) recipes.

## LLM Backend Selection

Booley works with either Developer Agent client: **Codex CLI** (OpenAI) or **Claude Code** (Anthropic). Pick one per project via `booley.toml [agent] provider`; the Developer Agent and every Specialist then run on that single provider. Each platform brings its own agentic runtime, MCP tool integration, and execution model, which Booley drives through a unified interface.

Both backends support **subscription-based auth** alongside API billing: a Claude Pro/Max subscription (Claude Code) or a ChatGPT/Codex subscription (Codex CLI). That means you can run Booley on an existing subscription instead of paying per-token API costs, which matters for a token-hungry workflow like RTL development. See the Auth & billing note in [USAGE.md](USAGE.md#auth--billing) for details.

## Expert-Written RTL Guides

Developer Agents and Specialists need guidance for hardware development. Booley closes this gap with expert-written guides covering RTL coding style, testbench methodology, assertion strategy, debugging techniques, and more. Each guide has a **generic part** (shipped with Booley) and a **project-specific part** (filled in by you). For coding style, the project part goes in `.booley/rtl_style_guide.md` and `.booley/tb_style_guide.md`; whatever you write there is appended to the shipped guide during a quality review and takes precedence where the two conflict. Both files are optional: with neither present, reviews run against the shipped guides alone. Building up the project-specific knowledge over time directly improves the quality of every Specialist's output.

## Guided Project Setup

Making a project Booley-ready is a guided, mostly hands-off process. Booley ships a `booley-setup` skill, deployed into Claude Code or Codex by `booley init`, that drives the bulk of setup for you, plan-first: a Claude Code or Codex session on the host inspects the repository, gathers every decision into a setup plan for a single approval, then continues as the Developer Agent in the Sandbox to draft `booley.toml`, the FuseSoC `.core` targets, and `tests.toml`, running Booley Flows to validate its work as it goes. Your job is to support that session: answer its planning questions about your build system and design, approve the plan, and it handles the mechanical config authoring gate-free from there. See [SETUP.md](SETUP.md).

## Extensible Toolkit

Booley's MCP surface is designed for extension. The built-in Booley Flows and Specialists cover the core RTL workflow (simulation, synthesis, lint, review), but every project has unique needs: formal verification, logic equivalence checking, DFT insertion, custom lint rules, power analysis. Project-specific MCP tools can be added via the `.booley/mcp_tools/` directory, following the same base-class interface as built-in MCP tools. The Developer Agent discovers and invokes them just like built-in MCP tools. See [MCP-TOOLS.md](../internals/MCP-TOOLS.md) for the architecture and extension guide.

## Parallel sessions

Run each Goal session in a separate linked worktree and Goal Branch inside the
Sandbox. The Dashboard shows concurrent sessions, Goals, and Jobs. Per-Job-Class
admission caps in `[jobs]` limit resource use; excess work queues and running
Jobs are never preempted. See [Jobs](CONFIG.md#jobs--concurrency-jobs).

## Windows Support

Because every EDA tool and Developer Agent runs inside the Docker sandbox, Booley's
standard image-provisioned toolchain is functionally available from a native
Windows checkout: you don't need to build Verilator, Yosys, or the rest
natively, and you do not need to install a user-facing Ubuntu distribution.
The normal Docker Desktop workflow bind-mounts that checkout into a Linux
container. Filesystem-heavy EDA performance across that boundary is still
being qualified and should not yet be assumed equivalent to Linux-native
performance. A WSL-hosted checkout can be used as an optional advanced
workflow, but is not a prerequisite for Windows support.

Host-provisioned commercial EDA is Linux x86-64 only. Docker Desktop runs Linux
containers, and a native Windows EDA installation cannot execute merely because
its directory is mounted into one. See
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

## Firmware-in-the-Loop Debug

For designs with embedded processors, the debug loop naturally extends to firmware. The Developer Agent traces problems through the waveform regardless of whether the root cause is in RTL, firmware, or their interaction. There's no artificial boundary between "hardware debug" and "firmware debug." This requires telling Booley how to compile your firmware as part of the project configuration, so the Developer Agent can modify, rebuild, and re-simulate in a single debug iteration.

## Stealth Mode

Stealth Mode is Project policy for protecting a downstream design's private
identifiers. It never applies to a Booley source checkout, which is not a
Project and must not contain `.booley/` or receive Project Git hooks.

Setup asks whether to keep Booley out of your git history. Yes keeps
`.booley/` hidden and enables stealth mode, including the commit-message
scrub and hidden-core projection for authored cores; No uses open config and
native cores with stealth off. Unattended setup leaves stealth off unless
hidden authored cores require it. Existing hand-set values are preserved.
An omitted `enabled` key defaults to on at runtime.

When enabled, a commit-msg hook rejects attribution footers and sanitizes other
protected commit-message prose, and authored
FuseSoC cores remain self-contained under `.booley/`. Booley projects
ignored root-level core copies for FuseSoC, so pristine RTL needs no tracked
integration files or source symlinks. Customize history sanitation with
`[stealth] banned_words`; projects with unusable bundled cores may explicitly
set `ignore_native_cores = true` to resolve only through the stealth-authored
cores. Disable the feature with
`[stealth] enabled = false`. Everything stays on your machine.

## Native coverage collection and analysis

The Coverage Analyst explains one retained native Coverage Campaign, keeping
observed counts and stored verdicts separate from causal hypotheses. It can
suggest tests and Waiver Candidates, but cannot launch Simulation, read
waveforms, evaluate Criteria, or approve exclusions. In Goal Mode Booley
records screened candidates for a human decision through `goal_propose_change`;
approved waivers require fresh coverage evidence before Finish. Verified
Target sources are optional. Large Campaigns use bounded overview, point, and
source retrieval rather than copying every point into the model prompt; point views
preserve Approved Waiver provenance. Native-payload pruning leaves the normalized
Campaign analyzable. Explicit `sim --coverage` (alias `--cov`) collects native
Verilator evidence with or without a Coverage Criterion. Gated collection applies
exact-suite metric thresholds and the project-wide Approved Waiver Set; simulation
and coverage verdicts stay independent. See the [collection and retention
reference](COVERAGE.md) and [waivers at
review](COVERAGE.md#coverage-waivers-at-review).
