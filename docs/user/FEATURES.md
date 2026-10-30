# Features

Booley's core move is giving Developer Agents **one consistent interface to the EDA tools** through Booley Flows, the same for Claude Code and Codex and the same across the EDA tools each Flow supports, and wrapping the result in a single VS Code window. That makes Developer Agents more capable, but it also gives the engineer a faster, lower-friction RTL workflow. Every feature below builds on that foundation; the reasoning behind the load-bearing choices is in [WHY.md](../internals/WHY.md).

New to Booley's vocabulary (Developer Agent, Specialist, Sandbox, Booley Flow, Target, Goal Mode)? The [context map](../../GLOSSARY-MAP.md) points to each canonical glossary and its rejected synonyms.

- [Same Flows Across Developer Agents and EDA Tools](#same-flows-across-developer-agents-and-eda-tools)
- [The Agentic RTL IDE](#the-agentic-rtl-ide)
- [Open Source](#open-source)
- [Goal Mode](#goal-mode)
- [Named Targets and Tests](#named-targets-and-tests)
- [Docker Sandboxing](#docker-sandboxing)
- [Independent Specialists](#independent-specialists)
- [Waveform-Based Debug](#waveform-based-debug)
- [Multi-Category Code Review](#multi-category-code-review)
- [Mutation Testing](#mutation-testing)
- [Lint Triage](#lint-triage)
- [LLM Backend Selection](#llm-backend-selection)
- [Guided Project Setup](#guided-project-setup)
- [Extensible Toolkit](#extensible-toolkit)
- [Parallel Sessions](#parallel-sessions)
- [Windows Support](#windows-support)
- [Stealth Mode](#stealth-mode)
- [Code Coverage](#code-coverage)

## Same Flows Across Developer Agents and EDA Tools

A Booley Flow (`sim`, `lint`, `synth`, `fpga`) looks the same no matter which Developer Agent calls it or which EDA tool runs underneath.

- **Claude Code or Codex.** Booley exposes its Flows as MCP tools, which both Claude Code and Codex call natively. Switch Developer Agents and the Flows, Targets, and results stay the same ([details](#llm-backend-selection)).
- **Same call, same verdict, whichever EDA tool.** `sim` takes the same call and returns the same normalized verdict whether the Target uses Verilator or Icarus; `lint` does the same for Verilator and Verible. The tool can come from the Sandbox Image or from a licensed install on the host ([SANDBOX.md](SANDBOX.md#commercial-eda-provisioning)); the Developer Agent drives it the same way.
- **Clear results instead of raw logs.** Every Flow returns a definite verdict, keeping failures, timeouts, inconclusive runs, and setup errors distinct, along with structured lint findings or synthesis metrics. Logs, waveforms, and reports land in predictable places. The Developer Agent acts on a clear result instead of guessing from noisy EDA output, and Goal Mode can trust that result as evidence.

The [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md) lists which EDA tools each Flow supports.

## The Agentic RTL IDE

Booley is not only infrastructure for the Developer Agent. It is workflow infrastructure for the hardware engineer: editor, EDA tools, waveforms, run status, review evidence, and Developer Agent share a single VS Code window.

Work interactively through conversation—edit code, run Booley Flows, debug a failure, open a scoped waveform view (`bwave gui`, see [Waveform-Based Debug](#waveform-based-debug))—or let Goal sessions work unattended in container terminals alongside. In either mode, Booley removes the manual transitions among editor, build commands, logs, waveform viewer, and review artifacts. Less time spent on that plumbing means faster iterations and more opportunities to verify, refine, and optimize the design.

Today that means stock VS Code chrome plus the Developer Agent chat and the Booley Flows it drives; native UI surfaces (Goal status, evidence status, Flow dashboards) are on the [roadmap](../internals/ROADMAP.md#native-ide-surface-in-vs-code), and the long-term direction is Booley as a **standalone agentic IDE**.

## Open Source

Booley is free and open source under Apache 2.0.

## Goal Mode

Goal Mode is for changes where "done" has to be proven, not claimed. Before the Developer Agent starts, you agree on what finished means, for example: lint is clean, the FIFO tests pass, a code review finds no bugs, and area grows by no more than 10%. Each of these conditions is a Goal. The Developer Agent keeps working until Booley's own simulation, lint, synthesis, and review runs confirm every Goal on the final code. It can't declare itself done, and it can't quietly drop or weaken a Goal: any change to the Goals needs your approval.

Use it for features, bug fixes, refactors, and optimizations you would otherwise re-check by hand, and for work you want to leave running unattended. A Goal survives crashes and context compaction: the Developer Agent picks it up where it left off. For quick questions and exploration, just talk to the Developer Agent.

To start, type `/booley-goal` and describe the task. The Developer Agent proposes Goals from your project's reusable checklists (for features, bug fixes, refactors, and verification) plus anything specific to this task, then works on its own branch in a separate worktree. When every Goal is met, you get a Review Package: the diff, the evidence behind each Goal, any Goal changes you approved, and the Developer Agent's summary. Merging stays your call. See [Goal Mode](GOAL-MODE.md#goal-mode).

## Named Targets and Tests

Every build configuration of your design is a named Target, and every test is a named entry. The idea and the implementation of Targets come from [FuseSoC](https://github.com/olofk/fusesoc): Targets live in FuseSoC `.core` files, and FuseSoC resolves them. The Developer Agent picks a Target and a test by name and can't make up its own simulator arguments. So every pass or fail traces back to a declared configuration and seed, and every failure comes with the exact command to rerun it. Target and test names mean the same thing to you, the Developer Agent, every Specialist and every Booley Flow, run after run. cocotb testbenches work the same way. See [TARGETS.md](TARGETS.md) and [TESTS.md](TESTS.md).

## Docker Sandboxing

Developer Agents run with permission prompts turned off so they can work without stopping to ask. The Sandbox keeps that safe: each Developer Agent runs as a non-root user inside a Docker container that sees only the project workspace, not the rest of your machine. Internet access is limited to the LLM API through a Booley proxy, so a prompt-injection payload has nowhere else to reach. The Sandbox image comes with the open-source EDA tools and both Claude Code and Codex preinstalled, and you can extend it with your own.

## Independent Specialists

Specialists (the Reviewer, Mutation Tester, and Coverage Analyst) are separate LLM agents that Booley runs to check the Developer Agent's work. A Specialist didn't write the code and has no memory of the reasoning behind it, so it judges what is actually there rather than what was intended. A Developer Agent reviewing its own work tends to confirm it. Each Specialist also starts from a fresh context, free of the stale logs and drafts a long session accumulates.

A Developer Agent could spawn its own subagent, but then it writes the brief, decides what the subagent sees, and reads back a free-form reply. Specialists close those gaps:

- **Booley writes the brief** from expert-written guides. In Goal Mode the Developer Agent can't add its own framing, so it can't steer the verdict.
- **Each side is judged against the spec, not against the other side.** The RTL and the testbench should be independent readings of the spec. A testbench Reviewer that could read the RTL would check the tests against what the RTL does, not what the spec requires, and miss bugs where both sides share the same misreading. So the testbench Reviewer sees only the testbench and the spec, and the RTL Reviewer doesn't see the testbench.
- **Results count.** Findings come back in a structured form Booley validates, and in Goal Mode they are evidence that can meet a review Goal.

## Waveform-Based Debug

For debugging, Booley **observes actual simulation behavior** through a custom-built Rust waveform EDA tool (`bwave`) instead of just reasoning about the code. It can query signal values at specific time ranges, use one signal as a trigger for sampling another, and trace data through the design: systematic debugging instead of guesswork.

**FST trace store.** Raw VCD files from complex designs can reach 10 GB+, so Booley's successful trace artifact is FST: the open, transition-based waveform format, typically 10-50x smaller than the source VCD and readable by any off-the-shelf viewer (GTKWave, VaporView). `bwave gui` puts a scoped view straight into the user's VS Code window. Booley reads a Target's authored native FST directly. Otherwise Booley converts VCD to FST as the simulation runs; Cocotb Targets convert after the run ([details](TARGETS.md#tracing-a-cocotb-target)). The reasoning is in [WHY.md: Why FST is the trace contract](../internals/WHY.md#why-fst-is-the-trace-contract).

## Multi-Category Code Review

Each review covers one focus, so the Reviewer looks hard at one kind of problem instead of skimming for everything at once.

| When | Focus | Checks |
|---|---|---|
| Before simulation | `rtl_spec` | RTL matches the spec: no more, no less |
| | `rtl_bugs` | Bug patterns, synthesis hazards, `ifdef`/config consistency |
| | `rtl_protocol` | Bus/protocol compliance and clock-domain crossings |
| After simulation passes | `rtl_security` | Resistance to fault injection, power/timing side channels, secret exposure |
| | `rtl_optimization` | Dead code and missed power/performance/area wins |
| | `rtl_code_style` | Comments, naming, readability, assertion coverage |
| Testbench | `tb_quality` | False passes, coverage gaps, testbench code quality |

The early reviews are the equivalent of an engineer's quick look at freshly written code: they catch bugs before they cost time and tokens in simulate-and-fix loops. Findings are graded CRITICAL, MAJOR, or MINOR. A Goal can require a `clean` review, where every finding is fixed (and confirmed by a recheck) or waived with a visible reason, or a `done` review, which only reports. Reviews follow expert-written RTL and testbench guides shipped with Booley; for the `rtl_code_style` and `tb_quality` reviews, add your project's own style rules in `.booley/rtl_style_guide.md` and `.booley/tb_style_guide.md`, and they take precedence. See [CLI-REFERENCE.md: reviewer](CLI-REFERENCE.md#reviewer).

## Mutation Testing

Booley includes a mutation testing framework to validate test quality, which matters more in agentic workflows than in human-driven ones. The read-only Mutation Tester Specialist proposes targeted, exact source replacements—subtle bugs a good testbench should detect. It never reads the testbench, so its bugs aren't shaped around what the tests already check. Booley validates the proposal bytes, runs an untouched baseline, then applies and compiles one isolated replacement at a time. It does not parse SystemVerilog or inject runtime selectors; the project's configured compiler decides whether each variant is valid. The deterministic harness counts which variants the testbench catches and measures the detection rate against a configurable threshold. The LLM-proposed mutations are temporary: mutation testing will become fully deterministic, with no LLM involved (see the [roadmap](../internals/ROADMAP.md#deterministic-mutation-testing)).

## Lint Triage

The biggest issue with lint EDA tools is signal-to-noise ratio: dangerous warnings get buried under tons of false positives, and engineers respond by ignoring lint altogether. Working through every message is exactly the tedium automation can afford and a human can't; delegating lint-output triage is what makes lint useful in practice. The `lint` Booley Flow runs the linter selected by each lint Target, normalizes its findings, and deduplicates them across Targets so the same issue isn't reported many times. Built-in support currently covers Verilator for structural and semantic checks and Verible for style and naming rules; the same lint contract is designed to accommodate other EDA tools, including licensed programs such as SpyGlass, in the future. Waivers remain native to the selected linter (`.vlt` waiver lines for Verilator, rule configuration and waiver files for Verible). During autonomous development, the Developer decides, per finding, whether to fix the RTL, add a justified waiver, or leave it for human attention. The result is a clean lint report instead of noise. See the [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md#built-in-flows) for the current engines.

To attach waivers to a lint Target, see the
[Verilator](TARGETS.md#verilator-lint-target) and
[Verible](TARGETS.md#verible-lint-target) recipes.

## LLM Backend Selection

Booley works with either Developer Agent client: **Codex CLI** (OpenAI) or **Claude Code** (Anthropic). Pick one per project via `booley.toml [agent] provider`; the Developer Agent and every Specialist then run on that single provider. Each platform brings its own agentic runtime, MCP tool integration, and execution model, which Booley drives through a unified interface.

Both backends support **subscription-based auth** alongside API billing: a Claude Pro/Max subscription (Claude Code) or a ChatGPT/Codex subscription (Codex CLI). That means you can run Booley on an existing subscription instead of paying per-token API costs, which matters for a token-hungry workflow like RTL development. See the Auth & billing note in [USAGE.md](USAGE.md#auth--billing) for details.

## Guided Project Setup

Making a project Booley-ready is a guided, mostly hands-off process. Booley ships a `booley-setup` skill, installed into Claude Code or Codex by Host Bootstrap, that drives the bulk of setup for you, plan-first: a Claude Code or Codex session on the host inspects the repository, gathers every decision into a setup plan for a single approval, then continues as the Developer Agent in the Sandbox to draft `booley.toml`, the FuseSoC `.core` targets, and `tests.toml`, running Booley Flows to validate its work as it goes. Your job is to support that session: answer its planning questions about your build system and design, approve the plan, and it handles the mechanical config authoring gate-free from there. See [SETUP.md](SETUP.md).

## Extensible Toolkit

Booley's MCP surface is designed for extension. The built-in Booley Flows and Specialists cover the core RTL workflow (simulation, synthesis, lint, review), but every project has unique needs: formal verification, logic equivalence checking, DFT insertion, custom lint rules, power analysis. Project-specific MCP tools can be added via the `.booley/mcp_tools/` directory, following the same base-class interface as built-in MCP tools. The Developer Agent discovers and invokes them just like built-in MCP tools. See [MCP-TOOLS.md](../internals/MCP-TOOLS.md) for the architecture and extension guide.

## Parallel Sessions

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
container, so filesystem-heavy EDA builds may be slower than on Linux-native
storage. A WSL-hosted checkout can be used as an optional advanced
workflow, but is not a prerequisite for Windows support.

Host-provisioned commercial EDA is Linux x86-64 only. Docker Desktop runs Linux
containers, and a native Windows EDA installation cannot execute merely because
its directory is mounted into one. See
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

## Stealth Mode

Stealth Mode keeps your AI-assisted workflow out of your repository and its history. Booley's configuration, and any FuseSoC cores it writes for your design, live in a `.booley/` folder that Git ignores, so your RTL repo gains no Booley files. A commit-message hook keeps AI attribution lines and words you ban out of commit messages, such as tool or internal project names, and can enforce your commit conventions. Setup asks whether you want it. See [STEALTH.md](STEALTH.md).

## Code Coverage

Add `--coverage` to a `sim` run and Booley collects Verilator code coverage (line, branch, expression, toggle, and cover properties) across all the tests of a Target, merged into one result. Only RTL counts toward the percentages; testbench code is reported but not scored. A Goal can require coverage floors, for example 90% line and 80% branch coverage over a named set of tests.

When coverage falls short, the Coverage Analyst Specialist explains where the gaps are, suggests tests to close them, and proposes waivers for points that can't or shouldn't be covered, such as unreachable code. Only you can approve a waiver, and an approved waiver counts only after a fresh coverage run. Coverage needs Verilator; Icarus Targets aren't supported. See [COVERAGE.md](COVERAGE.md).
