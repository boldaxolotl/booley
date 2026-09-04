# Features

Booley's core move is putting **one agent-native interface over the whole fragmented EDA toolchain**, every EDA tool and every coding agent behind the same typed surface, and wrapping the result in a single VS Code window. That makes agents more capable, but it also gives the engineer a faster, lower-friction RTL workflow. Every feature below builds on that foundation; the reasoning behind the load-bearing choices is in [WHY.md](../internals/WHY.md).

New to Booley's vocabulary (Developer Agent, Specialist, Session Runtime, Booley Flow, Target, Ticket Board)? The glossary in [CONTEXT.md](../CONTEXT.md) defines every term and the synonyms to avoid.

- [One Interface Over Every EDA Tool and Agent](#one-interface-over-every-eda-tool-and-agent)
- [The Agentic RTL IDE](#the-agentic-rtl-ide)
- [Open Source and Local-First](#open-source-and-local-first)
- [Interactive Mode](#interactive-mode)
- [Ticket-Driven Workflow](#ticket-driven-workflow)
- [Structured Booley Flow Contracts](#structured-booley-flow-contracts)
- [Named Targets and Tests](#named-targets-and-tests)
- [Machine-Checked Acceptance Criteria](#machine-checked-acceptance-criteria)
- [Docker Sandboxing](#docker-sandboxing)
- [Fresh Context per Specialist](#fresh-context-per-specialist)
- [Waveform-Based Debug](#waveform-based-debug)
- [Ticket Mode with Checkpoint & Resume](#ticket-mode-with-checkpoint--resume)
- [Multi-Category Code Review](#multi-category-code-review)
- [Mutation Testing](#mutation-testing)
- [Lint Triage](#lint-triage)
- [LLM Backend Selection](#llm-backend-selection)
- [Expert-Written RTL Guides](#expert-written-rtl-guides)
- [Agent-Driven Setup](#agent-driven-setup)
- [Extensible Toolkit](#extensible-toolkit)
- [Parallel Instances](#parallel-instances)
- [Windows Support](#windows-support)
- [Firmware-in-the-Loop Debug](#firmware-in-the-loop-debug)
- [Stealth Mode](#stealth-mode)
- [Push Notifications](#push-notifications)

## One Interface Over Every EDA Tool and Agent

An RTL flow is a pile of EDA tools that share nothing: Verilator, Icarus, Yosys, plus licensed heavyweight tools, each with its own CLI, flags, and output format. The agent driving them is a moving part too: today Claude Code, tomorrow Codex. Wire agents straight into that mess and you get N EDA tools × M agents of brittle glue. Booley collapses it to one interface:

- **One typed surface over every Booley Flow.** Each Flow drives its selected EDA tool through the same structured call and returns the same normalized verdict inside the Session Runtime. A tool can be supplied by the standard image or by an authorized read-only host installation under a built-in policy; the agent learns one interface, not one per EDA tool ([details](#structured-booley-flow-contracts)).
- **Agent-agnostic.** The same Booley Flows are exposed as MCP tools, the protocol-level functions Claude Code and Codex both invoke natively. Swap the model; the EDA stack stays identical ([details](#llm-backend-selection)).
- **Extension is linear, not multiplicative.** A new analysis is one custom MCP tool behind the same criteria contract ([details](#extensible-toolkit)); a new agent is one MCP client. Neither requires rewiring the other side.

EDA-tool support lives in the [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md). One expectation worth setting up front: the built-in `synth` Flow is a **fast PPA (power/performance/area) estimate to optimize RTL against**, not tape-out synthesis. [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md#built-in-flows) spells out exactly what it does and doesn't cover.

## The Agentic RTL IDE

Booley is not only infrastructure for an agent. It is workflow infrastructure for the hardware engineer: editor, EDA tools, waveforms, run status, review evidence, and agent share a single VS Code window.

Work interactively through conversation—edit code, run Booley Flows, debug a failure, open a scoped waveform view (`bwave gui`, see [Waveform-Based Debug](#waveform-based-debug))—or let autonomous tickets run in container terminals alongside. In either mode, Booley removes the manual transitions among editor, build commands, logs, waveform viewer, and review artifacts. Less time spent on that plumbing means faster iterations and more opportunities to verify, refine, and optimize the design.

Today that means stock VS Code chrome plus the agent chat and the Booley Flows it drives; native UI surfaces (ticket panel, criterion status, Flow dashboards) are on the [roadmap](../internals/ROADMAP.md#native-ide-surface-in-vs-code), and the long-term direction is Booley as a **standalone agentic IDE**.

## Open Source and Local-First

Booley is free under Apache 2.0 and runs entirely on your machine.

## Interactive Mode

Booley has two modes of operation; the next two sections ground the ticket-and-criteria vocabulary the mechanism sections below build on. Interactive Mode is the workflow for small tasks, codebase exploration, and interactive debug and coding sessions: anything human-in-the-loop. `booley init` registers a Booley MCP server with your agent CLI, so your interactive Claude Code or Codex session can call Booley Flows and Specialists (`sim`, `lint`, `reviewer`, `mutation_tester`, ...) directly from natural-language prompts, no ticket, no Developer Agent, each call self-contained. See [USAGE.md: Interactive Mode](USAGE.md#interactive-mode).

## Ticket-Driven Workflow

The workflow for long autonomous agentic work. You write tickets, agent executes them. Tickets are simple Markdown files with YAML frontmatter, stored on your local filesystem. No JIRA, no GitHub Issues, no external services. Four ticket types are supported: **Feature**, **Bug Fix**, **Refactor**, and **Verification**. See [USAGE.md: Ticket-Driven Workflow](USAGE.md#ticket-driven-workflow).

Each ticket declares a file **scope** — the files the work is expected to touch. An agent that needs to edit something outside it can, and Booley reports every such file to you at triage instead of discarding the change behind your back. What makes parallel ticket execution safe is the per-ticket worktree and branch, not the scope; the one thing a commit may never touch is Booley's own bookkeeping. See [USAGE.md: Scope](USAGE.md#scope).

## Structured Booley Flow Contracts

Booley Flows are evidence contracts between real EDA behavior and Booley's decision loop. An MCP tool call does not just dump terminal output back into context: it resolves Targets and tests, runs the Flow, validates the result, stores predictable logs/traces/reports, and returns a normalized verdict the Developer Agent can safely use.

This matters most for high-consequence steps: simulation, lint, synthesis, debug, review, mutation testing. A passing simulation satisfies criteria only when the Booley Flow returns a valid `pass` verdict. Lint and synthesis likewise report structured findings or metrics instead of EDA-tool-specific log fragments. Failures, timeouts, inconclusive runs, and contract errors stay distinct, so the agent can choose the right next action instead of guessing from noisy EDA output.

The same contract layer is what keeps project-specific build steps contained: a per-test firmware build rides `[flows.sim].pre_run_commands` inside the Session Runtime, while a host-provisioned tool remains subject to host authority and keeps the same public Flow names, MCP schemas, criteria, artifact layout, and report format.

## Named Targets and Tests

Booley does not let agents assemble simulator command lines from ad-hoc fragments. Every buildable variant of the design is a **named Target** in a FuseSoC `.core` file (CAPI2, FuseSoC's YAML design-description format), with filesets, parameters, defines, and top module in one place, and every test is a **named entry** in `.booley_project/tests.toml` keyed by that Target. Booley Flows take `target` and `test` arguments that resolve against them:

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

This is the identity half of the Booley Flow contract, and it matters for two reasons. First, **reproducibility**: a request to run one test resolves to exactly that test under exactly that Target's parameters and top module, and the resulting report carries the resolved identity. There is no gap between "what the agent asked for" and "what actually ran" for it to hallucinate into. Second, **conditional-compilation coverage**: designs with `ifdef`-gated features declare one Target per build, and Booley can fan Flows (lint, simulation, synthesis) across all of them instead of silently validating only the default build.

Because Targets and tests are data, not code, the same names mean the same thing to the Developer Agent, every Specialist, the Booley Flows, and you, across every run.

The same model covers **cocotb (Python) testbenches**: a Target whose flow options declare a `cocotb_module` lists its `@cocotb.test()` function names in `tests.toml`, and `sim` runs the selected set batched in one sim process, taking per-test verdicts from cocotb's `results.xml`; `--test` filters the set the same way, and `sim_pass_{target}` keeps its meaning.

## Machine-Checked Acceptance Criteria

A ticket declares **acceptance criteria**, and the harness, not the agent, decides when they are met. This is Booley's core defense against an agent declaring victory on work it never finished; criteria are satisfied only by a valid Booley Flow or Specialist verdict and re-verified whenever the underlying code changes. This is especially useful for configuration-heavy tickets with dozens of criteria, where relying on the agent to remember every check is fragile. See [USAGE.md: Acceptance Criteria](USAGE.md#acceptance-criteria).

## Docker Sandboxing

Agents run with `--dangerously-skip-permissions` to operate autonomously. Docker sandboxing keeps this safe: every agent runs inside the per-folder Session Runtime container with only the project workspace mounted, full access inside, no access to the host outside. There is no general internet access either: egress is restricted to the LLM API endpoints through a Booley proxy, so a prompt-injection payload picked up from a web page has nowhere to reach. The agent runs as a non-root user, and an idle reaper stops orphaned sessions. The sandbox image ships every built-in EDA tool (see [One Interface Over Every EDA Tool and Agent](#one-interface-over-every-eda-tool-and-agent)) and both agent CLIs preinstalled.

The sandbox is **customizable** in two ways. Use `[sandbox].image` for a project image that extends `booley-sandbox` with EDA tools that must exist in every container. Use a `post-setup` hook at `<project_dir>/hooks/post-setup.sh` for per-worktree setup after worktree creation.

## Fresh Context per Specialist

Long single-context agent sessions degrade: the model loses the thread, gets distracted by stale logs, and quality falls off over a multi-hour task. Booley's answer is context isolation: each Specialist (code review, mutation testing) runs in a fresh LLM context, so none of them drags along stale logs or drafts from earlier iterations. A fresh context also means a fresh perspective: a Specialist that didn't write the code has no attachment to it and no memory of the reasoning that produced it, so it judges what's actually there rather than what was intended. This matters most for the reviewer: an agent reviewing its own work tends to confirm it, while an unbiased one finds the real issues. Continuity lives in structured state carried by the Developer Agent (tickets, criteria, reports), not in an ever-growing conversation. See [ARCHITECTURE.md](../internals/ARCHITECTURE.md#the-sandbox).

## Waveform-Based Debug

For debugging, Booley **observes actual simulation behavior** through a custom-built Rust waveform EDA tool (`bwave`) instead of just reasoning about the code. It can query signal values at specific time ranges, use one signal as a trigger for sampling another, and trace data through the design: systematic debugging instead of guesswork.

**FST trace store.** Raw VCD files from complex designs can reach 10 GB+, so Booley's successful trace artifact is FST: the open, transition-based waveform format, typically 10-50x smaller than the source VCD and readable by any off-the-shelf viewer (GTKWave, VaporView). `bwave gui` puts a scoped view straight into the user's VS Code window. Booley reads a Target's authored native FST directly. When the Target has no trace generation, Booley adds VCD tracing and streams the VCD through a named pipe (FIFO) to `bwave`, which converts it to FST in parallel. The reasoning is in [WHY.md: Why FST is the trace contract](../internals/WHY.md#why-fst-is-the-trace-contract).

## Ticket Mode with Checkpoint & Resume

Booley is designed for unsupervised multi-hour execution, recovering from interruptions (reboot, crash, subscription limit) by resuming from the last completed Booley Flow or Specialist invocation, and blocking tickets for human triage when it gets stuck. See [USAGE.md: Running Unattended](USAGE.md#running-unattended).

## Multi-Category Code Review

Code review is split into focus categories with severity-stratified issue tracking:

- **Correctness category** (functional, protocol, ifdef checking) runs once the RTL is ready and compiles, before any simulation: the equivalent of an RTL engineer's "quick look at the code I have just written," catching bugs before they cost time and tokens in simulation-fix loops.
- **Quality category** (optional: security, optimization, coding standards) runs after the RTL is bug-free and targets issues beyond functional correctness.
- Issues are classified as CRITICAL, MAJOR, or MINOR; the ticket cannot pass with unresolved CRITICAL issues.
- Testbench code gets its own separate review.

The shipped reviewer is read-only: it reports issues by severity, and the Developer Agent resolves them. See [USAGE.md: RTL Code Review](USAGE.md#rtl-code-review) for the per-category criteria.

## Mutation Testing

Booley includes a mutation testing framework to validate test quality, which matters more in agentic workflows than in human-driven ones. A read-only creator agent proposes targeted, exact source replacements—subtle bugs a good testbench should detect. Booley validates the proposal bytes, runs an untouched baseline, then applies and compiles one isolated replacement at a time. It does not parse SystemVerilog or inject runtime selectors; the project's configured compiler decides whether each variant is valid. The deterministic harness counts which variants the testbench catches and measures the detection rate against a configurable threshold.

## Lint Triage

The biggest issue with lint EDA tools is signal-to-noise ratio: dangerous warnings get buried under tons of false positives, and engineers respond by ignoring lint altogether. Working through every message is exactly the tedium automation can afford and a human can't; delegating lint-output triage is what makes lint useful in practice. The `lint` Booley Flow runs the linter selected by each lint Target, normalizes its findings, and deduplicates them across Targets so the same issue isn't reported many times. Built-in support currently covers Verilator for structural and semantic checks and Verible for style and naming rules; the same lint contract is designed to accommodate other EDA tools, including licensed programs such as SpyGlass, in the future. Waivers remain native to the selected linter (`.vlt` waiver lines for Verilator, rule configuration and waiver files for Verible). During autonomous development, the Developer decides, per finding, whether to fix the RTL, add a justified waiver, or leave it for human attention. The result is a clean lint report instead of noise. See the [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md#built-in-flows) for the current engines.

## LLM Backend Selection

Booley works with either agent platform: **Codex CLI** (OpenAI) or **Claude Code** (Anthropic). Pick one per project via `booley.toml [agent] provider`; the Developer Agent and every specialist then run on that single provider. Each platform brings its own agentic runtime, MCP tool integration, and execution model, which Booley drives through a unified interface.

Both backends support **subscription-based auth** alongside API billing: a Claude Pro/Max subscription (Claude Code) or a ChatGPT/Codex subscription (Codex CLI). That means you can run Booley on an existing subscription instead of paying per-token API costs, which matters for a token-hungry workflow like RTL development. See the Auth & billing note in [USAGE.md](USAGE.md) for details.

## Expert-Written RTL Guides

LLM agents specialize in software. For hardware development, they need guidance. Booley closes this gap with expert-written guides covering RTL coding style, testbench methodology, assertion strategy, debugging techniques, and more. Each guide has a **generic part** (shipped with Booley) and a **project-specific part** (filled in by you). For coding style, the project part goes in `.booley_project/rtl_style_guide.md` and `.booley_project/tb_style_guide.md`; whatever you write there is appended to the shipped guide during a quality review and takes precedence where the two conflict. Both files are optional: with neither present, reviews run against the shipped guides alone. Building up the project-specific knowledge over time directly improves the quality of every Specialist's output.

## Agent-Driven Setup

Making a project Booley-ready is a guided, mostly hands-off process. Booley ships a `booley-setup` skill, deployed into your agent runtime by `booley init`, that drives the bulk of setup for you, plan-first: an agent inspects the repository, gathers every decision into a setup plan for a single approval, then drafts `booley.toml`, the FuseSoC `.core` targets, and `tests.toml`, running Booley Flows to validate its work as it goes. Your job is to support the agent: answer its planning questions about your build system and design, approve the plan, and it handles the mechanical config authoring gate-free from there. See [SETUP.md](SETUP.md).

## Extensible Toolkit

Booley's MCP surface is designed for extension. The built-in Booley Flows and Specialists cover the core RTL workflow (simulation, synthesis, lint, review), but every project has unique needs: formal verification, logic equivalence checking, DFT insertion, custom lint rules, power analysis. Project-specific MCP tools can be added via the `.booley_project/mcp_tools/` directory, following the same base-class interface as built-in MCP tools. The Developer Agent discovers and invokes them just like built-in MCP tools. See [MCP-TOOLS.md](../internals/MCP-TOOLS.md) for the architecture and extension guide.

## Parallel Instances

Multiple tickets can run concurrently inside one Session Runtime: start another `booley run` in another container terminal and it picks up the next ticket from the queue independently, alongside your interactive session.

Concurrency is safe by design, not by luck. Each running ticket operates in its own git worktree and its own artifact directory, so most isolation is **structural**: runs do not share the files they work on. Racing runs resolve ticket pickup through the Ticket Board (Booley's filesystem-backed ticket queue) and its atomic directory moves, and resource contention is governed by per-Job-Class admission caps (`[jobs]` in booley.toml, see [CONFIG.md](CONFIG.md#jobs--concurrency-jobs)) — a Job Class being the admission category a unit of work falls into (in-runtime EDA, model-API Specialist work, or a ticket's Developer Agent). Work beyond a cap waits in a priority queue (Interactive Mode ahead of Ticket Mode, running Jobs never preempted) instead of overcommitting the container.

## Windows Support

Because every EDA tool and agent runs inside the Docker sandbox, Booley's
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

For designs with embedded processors, the debug loop naturally extends to firmware. The agent traces problems through the waveform regardless of whether the root cause is in RTL, firmware, or their interaction. There's no artificial boundary between "hardware debug" and "firmware debug." This requires telling Booley how to compile your firmware as part of the project configuration, so the agent can modify, rebuild, and re-simulate in a single debug iteration.

## Stealth Mode

Stealth Mode is Project policy for protecting a downstream design's private
identifiers. It never applies to a Booley Source Checkout, which is not a
Project and must not contain `.booley_project/` or receive Project Git hooks.

Stealth mode is opt-in during setup: setup asks specifically whether you want to enable the commit-message scrub and writes `[stealth] enabled = false` unless you say yes. For compatibility with existing projects, an omitted `enabled` key still uses the older on-by-default runtime fallback.

When enabled, a commit-msg hook sanitizes AI-related history, and authored
FuseSoC cores remain self-contained under `.booley_project/`. Booley projects
ignored root-level core copies for FuseSoC, so pristine RTL needs no tracked
integration files or source symlinks. Customize history sanitation with
`[stealth] banned_words`; projects with unusable bundled cores may explicitly
set `ignore_native_cores = true` to resolve only through the stealth-authored
cores. Disable the feature with
`[stealth] enabled = false`. Everything stays on your machine.

## Push Notifications

Push notifications via [ntfy.sh](https://ntfy.sh) tell you when a ticket completes or blocks, so you don't have to watch the terminal. See [USAGE.md: Push Notifications](USAGE.md#push-notifications).
