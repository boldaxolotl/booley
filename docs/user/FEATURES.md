# Features

Booley's core move is giving Developer Agents **one consistent interface to the
EDA tools** through Booley Flows, the same for Claude Code and Codex and the
same across the EDA tools each Flow supports, and wrapping the result in an
agentic development environment (ADE): one VS Code window where you direct and
supervise those agents. That makes Developer Agents more capable, but it also
gives the engineer a faster, lower-friction RTL workflow. Every feature below
builds on that foundation; the reasoning behind the load-bearing choices is in
[ARCHITECTURE.md](../internals/ARCHITECTURE.md).

New to Booley's vocabulary (Developer Agent, Specialist, Sandbox, Booley Flow,
Target)? The [context map](../../GLOSSARY-MAP.md) points to each canonical
glossary and its rejected synonyms.

- [Foundation](#foundation)
    - [Same Flows Across Developer Agents and EDA Tools](#same-flows-across-developer-agents-and-eda-tools)
    - [An Agentic Development Environment](#an-agentic-development-environment)
    - [LLM Backend Selection](#llm-backend-selection)
    - [Open Source](#open-source)
    - [Windows Support](#windows-support)
- [Running Simulations at Scale](#running-simulations-at-scale)
    - [Named Targets and Tests](#named-targets-and-tests)
    - [Regression Runs](#regression-runs)
    - [Job Admission](#job-admission)
    - [Parallel Sessions](#parallel-sessions)
    - [Code Coverage](#code-coverage)
- [Checking the Agent's Work](#checking-the-agents-work)
    - [Independent Specialists](#independent-specialists)
    - [Multi-Category Code Review](#multi-category-code-review)
    - [Mutation Testing](#mutation-testing)
    - [Lint Triage](#lint-triage)
    - [Waveform-Based Debug](#waveform-based-debug)
- [Safety and Privacy](#safety-and-privacy)
    - [Docker Sandboxing](#docker-sandboxing)
    - [Stealth Mode](#stealth-mode)
- [Setup and Extension](#setup-and-extension)
    - [Guided Project Setup](#guided-project-setup)
    - [Extensible Toolkit](#extensible-toolkit)

## Foundation

### Same Flows Across Developer Agents and EDA Tools

Every EDA tool has its own command line, options, and log format, and a
Developer Agent driving each one directly spends its effort on that instead of
on the design. A Booley Flow (`sim`, `lint`, `synth`, `fpga`, `mutation`) looks
the same no matter which Developer Agent calls it or which EDA tool runs
underneath.

- **Claude Code or Codex.** Booley exposes its Flows as MCP tools, which both
  Claude Code and Codex call natively. Switch Developer Agents and the Flows,
  Targets, and results stay the same ([details](#llm-backend-selection)).
- **Same call and result format, whichever EDA tool.** `sim` takes the same
  call and reports its verdict in the same normalized format whether the
  Target uses Verilator or Icarus; `lint` does the same for Verilator and
  Verible. The verdict itself comes from the tool the Target names, so two
  tools can still disagree on the same design. Open-source tools ship in the
  Sandbox Image, and Vivado comes from a licensed install on a Linux x86-64
  host ([SANDBOX.md](SANDBOX.md#commercial-eda-provisioning)); the Developer
  Agent drives it the same way.
- **Clear results instead of raw logs.** Every Flow returns a definite verdict,
  keeping failures, timeouts, inconclusive runs, and setup errors distinct,
  along with structured lint findings or synthesis metrics. Logs, waveforms,
  and reports land in predictable places. The Developer Agent acts on a clear
  result instead of guessing from noisy EDA output.

The [supported EDA tools matrix](SUPPORTED-EDA-TOOLS.md) lists which EDA tools
each Flow supports.

### An Agentic Development Environment

An IDE is built around a person editing code; directing agents with separate
terminals, logs, and a waveform viewer leaves you switching among them. An
ADE is built around a person directing agents that edit code. Booley is the
second kind, for hardware: the Developer Agent writes RTL and testbenches and
runs the EDA tools, and you set the direction, answer its questions, and judge
the evidence.

Booley is not only infrastructure for the Developer Agent. It is workflow
infrastructure for the hardware engineer: Developer Agent sessions, editor, EDA
tools, waveforms, run status, and review evidence share a single VS Code
window.

- **Hands-on.** Work through conversation: ask for an edit, run Booley Flows,
  debug a failure, open a scoped waveform view (`bwave gui`, see
  [Waveform-Based Debug](#waveform-based-debug)). You can still read and edit
  RTL yourself between agent turns.
- **Parallel.** Leave sessions running in container terminals alongside, each
  on its own branch and worktree ([Parallel Sessions](#parallel-sessions)).
  The Booley Dashboard shows every session and Job; you step in when an agent
  needs a decision.

Either way, Booley removes the manual transitions among editor, build commands,
logs, waveform viewer, and review artifacts. Less time spent on that plumbing
means faster iterations and more opportunities to verify, refine, and optimize
the design.

Today the shell is stock VS Code: agent sessions run in its terminals and the
Booley Dashboard in its panel. Native agent-supervision UI (evidence status,
Flow dashboards, one-click review) is on the
[roadmap](../internals/ROADMAP.md#native-ade-surface), and the long-term
direction is Booley as a **standalone ADE**.

### LLM Backend Selection

Teams settle on different Agent CLIs, and per-token API billing adds up in
a token-hungry workflow like RTL development. Booley works with either
Agent CLI: **Codex CLI** (OpenAI) or **Claude Code** (Anthropic).
`booley.toml [agent] default` picks the one the Developer Agent runs on, and
`[agent] clis` can list both so a Specialist runs on the other CLI when its
model needs it ([details](CONFIG.md#agent-clis-agent)). Each platform brings
its own agentic runtime, MCP tool integration, and execution model, which
Booley drives through a unified interface.

Both backends support **subscription-based auth** alongside API billing: a
Claude Pro/Max subscription (Claude Code) or a ChatGPT/Codex subscription
(Codex CLI). That means you can run Booley on an existing subscription instead
of paying per-token API costs. See [Sign-in and
billing](SETUP.md#sign-in-and-billing) for details.

### Open Source

A closed tool can't be read or changed by the people who rely on it. Booley is
free and open source under Apache 2.0: read its code, change it, and run it
without a license fee.

### Windows Support

Most open-source EDA tools are built for Linux, and building Verilator or Yosys
on Windows is a project of its own. Booley runs from a native Windows checkout
with Docker Desktop: every EDA tool runs inside the Sandbox, so you don't build
them on Windows or install Ubuntu. A WSL-hosted checkout is an optional
advanced workflow, not a requirement.

EDA runs can be slower than on Linux, because the container reads your
checkout through a Windows bind mount, and local image builds take longer.
The `fpga` Flow doesn't run on Windows: it needs a Vivado installed on a Linux
host; see
[Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).

## Running Simulations at Scale

### Named Targets and Tests

A Developer Agent that writes its own simulator command lines leaves results
nobody can reproduce: which defines, which files, which seed? Every build
configuration of your design is a named Target, and every test is a named
entry. The idea and the implementation of Targets come from
[FuseSoC](https://github.com/olofk/fusesoc): Targets live in FuseSoC `.core`
files, and FuseSoC resolves them. The Developer Agent picks a Target and a test
by name and can't make up its own simulator arguments. So every pass or fail
traces back to a declared configuration and seed, and every failure comes with
the exact command to rerun it. Target and test names mean the same thing to
you, the Developer Agent, every Specialist and every Booley Flow, run after
run. cocotb testbenches work the same way. See [TARGETS.md](TARGETS.md) and
[TESTS.md](TESTS.md).

### Regression Runs

Running a regression by hand means looping over tests and seeds, then searching
logs for the seed that failed. One `sim` call runs a whole regression, not just
one test. With no tests named it runs every Test of the Target; name tests to
run a subset, or list them in a file. Run a test N times with different random
seeds (`stress@x20`), or pin one seed to reproduce a failure. Test Variants
give one test several named sets of run-time arguments, such as a `soak`
variant with a longer run. Tests run in parallel, as many at once as [Job
Admission](#job-admission) allows; add `--coverage` to merge coverage from all
of them into one result.

The report gives one verdict for the whole run and names the seed of every
failure, with the command that reruns it with that seed. A multi-hour run that
gets interrupted resumes where it stopped, with the same seeds, without
rerunning the tests that already finished. See [FLOW-SIM.md](FLOW-SIM.md) and
[TESTS.md](TESTS.md).

Each run stands alone today: named and scheduled regressions, results compared
across runs, and grouping of related failures are on the
[roadmap](../internals/ROADMAP.md#verification-engineering).

### Job Admission

Start several Developer Agents and they soon run many EDA tools at once.
Together those tools can need more memory than the Sandbox has, and the kernel
then kills one of them for running out of memory (an OOM kill), losing the run.

Booley guards against those kills by admitting work against a Memory Budget:
the Sandbox's memory limit (`[sandbox].memory`) less a reserve for the agents.
Every Booley Flow and Specialist run is a Job, and a Job starts when its
estimated memory fits what the budget has left.

- **Measured, not guessed.** Booley reserves the peak memory the same kind
  of run used before, so nobody guesses what each EDA tool needs.
  `booley resources` shows those peaks.
- **Waits instead of failing.** A Job that doesn't fit waits until memory frees
  up. A running Job is never interrupted to make room.
- **One retry, alone.** When Booley sees the kernel kill an EDA tool run
  because the Sandbox reached its memory limit (`[sandbox].memory` plus its
  swap), it runs that work again once, alone. A kill caused by the host running
  out of memory is not retried.

<details markdown>
<summary>With host-installed licensed tools</summary>

`[jobs.seats]` caps how many licensed seats of a commercial EDA tool, such as
Vivado, Booley uses at once. See [Commercial
EDA](SANDBOX.md#commercial-eda-provisioning).

</details>

Settings are in [Jobs](CONFIG.md#jobs--concurrency-jobs); the reasoning is in
[ADR 0087](../adr/0087-admit-jobs-against-a-measured-memory-budget.md).

### Parallel Sessions

Several Developer Agents working in one checkout overwrite each other's edits
and builds. Run each session in its own linked worktree and branch inside the
Sandbox instead; the Dashboard shows the sessions and their Jobs,
and [Job Admission](#job-admission) decides how many of their Jobs run at once.

### Code Coverage

A passing regression doesn't say which parts of the RTL it never exercised.
Add `--coverage` to a `sim` run and Booley collects code coverage (line,
branch, expression, toggle, and cover properties) across all the tests of a
Target, merged into one result. Only RTL counts toward the percentages;
testbench code is reported but not scored.

When coverage falls short, the Coverage Analyst Specialist explains where the
gaps are, suggests tests to close them, and proposes waivers for points that
can't or shouldn't be covered, such as unreachable code. Only you can approve a
waiver, and an approved waiver counts only after a fresh coverage run.
Coverage needs Verilator; Icarus Targets aren't supported. See
[COVERAGE.md](COVERAGE.md).

## Checking the Agent's Work

### Independent Specialists

A Developer Agent reviewing its own work tends to confirm it. Specialists (the
Reviewer and Coverage Analyst) are separate runs that Booley starts to
check the Developer Agent's work. A Specialist didn't write the code and has no
memory of the reasoning behind it, so it judges what is actually there rather
than what was intended. Each Specialist also starts from a fresh context, free
of the stale logs and drafts a long session accumulates.

A Developer Agent could spawn its own subagent, but then it writes the brief,
decides what the subagent sees, and reads back a free-form reply. Specialists
close those gaps:

- **Booley writes the brief** from expert-written guides.
- **Each side is judged against the spec, not against the other side.** The
  RTL and the testbench should be independent readings of the spec. A
  testbench Reviewer that could read the RTL would check the tests against
  what the RTL does, not what the spec requires, and miss bugs where both
  sides share the same misreading. So the testbench Reviewer never sees the
  RTL, and the RTL Reviewer never sees the testbench.

### Multi-Category Code Review

Asked to check everything at once, a reviewer skims and misses whole kinds of
problems. So each review covers one focus, and the Reviewer looks hard at one
kind of problem.

| Focus | Checks |
|---|---|
| `rtl_spec` | RTL matches the spec: no more, no less |
| `rtl_bugs` | Bug patterns, synthesis hazards, `ifdef`/config consistency |
| `rtl_protocol` | Bus/protocol compliance and clock-domain crossings |
| `rtl_security` | Resistance to fault injection, power/timing side channels, secret exposure |
| `rtl_optimization` | Dead code and missed power/performance/area wins |
| `rtl_code_style` | Comments, naming, readability, assertion coverage |
| `tb_quality` | False passes, coverage gaps, testbench code quality |

Findings are graded CRITICAL, MAJOR, or MINOR. Reviews follow expert-written
RTL and testbench guides shipped with Booley, and you can add your project's
own style rules. See [Reviewer](REVIEWER.md).

### Mutation Testing

A passing testbench can still miss real bugs, and that is likelier when the
same Developer Agent writes both the RTL and the tests. Booley's mutation
testing measures how many planted bugs the tests catch.

The `mutation` Flow (`booley flow mutation`, [details](FLOW-MUTATION.md))
generates Mutants deterministically from the RTL with no LLM involved: the same
RTL, testbench HDL, Target parameters, count, and seed produce the same Mutants
(on a Verilator Target, with the same Verilator version), so detection rates
are reproducible and comparable across runs. Mutants are planted in the RTL
only, never in the testbench, so its bugs aren't shaped around what the tests
already check; the testbench's HDL is read only for the design's top module
and the parameters it gives the design.

Booley runs an untouched baseline, then applies and compiles one isolated
Mutant at a time; a Mutant that doesn't build is replaced by the next
candidate. The harness counts which Mutants the testbench detects and compares
the detection rate against a threshold (80 % by default, configurable). On a
Verilator Target, a Mutant that builds the same simulator as the original is
replaced by the next candidate instead of being scored. Some survivors may
still be equivalent Mutants, which the default threshold allows for.

### Lint Triage

The biggest issue with lint EDA tools is signal-to-noise ratio: dangerous
warnings get buried under tons of false positives, and engineers respond by
ignoring lint altogether. Working through every message is exactly the tedium
automation can afford and a human can't; delegating lint-output triage is what
makes lint useful in practice. The `lint` Booley Flow runs the linter each lint
Target names, normalizes its findings into one format whichever linter produced
them, and deduplicates them across Targets so the same issue isn't reported many
times. Waivers stay in the linter's own format. When it runs lint, the Developer
Agent decides, per finding, whether to fix the RTL, add a justified waiver, or
leave it for human attention. The result is a clean lint report instead of
noise. See the [supported EDA tools
matrix](SUPPORTED-EDA-TOOLS.md#built-in-flows) for the current engines.

To attach waivers to a lint Target, see the
[Verilator](FLOW-LINT.md#verilator-lint-target) and
[Verible](FLOW-LINT.md#verible-lint-target) recipes.

### Waveform-Based Debug

A Developer Agent that debugs by reading code alone guesses at what the
simulation did. Booley lets it observe actual simulation behavior through
`bwave`, a custom-built Rust waveform tool. It can query signal values at
specific time ranges, use one signal as a trigger for sampling another, and
trace data through the design: systematic debugging instead of guesswork.

**FST trace store.** Raw VCD files from complex designs can reach 10 GB+, so
Booley's successful trace artifact is FST: the open, transition-based waveform
format, typically 10-50x smaller than the source VCD and readable by any
off-the-shelf viewer (GTKWave, VaporView). `bwave gui` puts a scoped view
straight into the user's VS Code window. Booley reads a Target's authored
native FST directly. Otherwise Booley converts VCD to FST, during or after the
run depending on the simulator (see [FLOW-SIM
waveforms](FLOW-SIM.md#waveforms)). The reasoning is in
[ARCHITECTURE.md: Why FST is the trace contract](../internals/ARCHITECTURE.md#why-fst-is-the-trace-contract).

<details markdown>
<summary>With a cocotb testbench</summary>

[Cocotb Targets](FLOW-SIM.md#cocotb-targets-python-testbenches) convert after
the run ([details](FLOW-SIM.md#tracing-a-cocotb-target)).

</details>

## Safety and Privacy

### Docker Sandboxing

A Developer Agent that stops to ask permission for every command can't work
unattended, and one that doesn't ask can reach your whole machine. Booley runs
Developer Agents with permission prompts off inside the Sandbox, which keeps
that safe: each Developer Agent runs as a non-root user inside a Docker
container that sees only the project workspace, not the rest of your machine.
Internet access is limited to the LLM API through a Booley proxy, so a
prompt-injection payload has nowhere else to reach. The Sandbox Image comes
with the open-source EDA tools and both Claude Code and Codex preinstalled, and
you can extend it with your own.

### Stealth Mode

Some teams can't add AI tooling files or AI attribution to their RTL
repository. Stealth Mode keeps your AI-assisted workflow out of your repository
and its history. Booley's configuration, and any FuseSoC cores it writes for
your design, live in a `.booley/` folder that Git ignores, so your RTL repo
gains no Booley files. A commit-message hook keeps AI attribution lines and
words you ban out of commit messages, such as tool or internal project names.
Setup asks whether you want it. See [STEALTH.md](STEALTH.md).

## Setup and Extension

### Guided Project Setup

Making an existing RTL repository work with Booley means writing
`booley.toml`, FuseSoC `.core` Targets, and `tests.toml` for a design Booley
has never seen. Booley ships a `booley-setup` skill, installed into Claude Code
or Codex by Host Bootstrap, that does the bulk of that for you, plan-first: a
Claude Code or Codex session on the host inspects the repository, gathers every
decision into a setup plan for a single approval, then continues as the
Developer Agent in the Sandbox to draft `booley.toml`, the FuseSoC `.core`
targets, and `tests.toml`, running Booley Flows to validate its work as it
goes. Your job is to support that session: answer its planning questions about
your build system and design, approve the plan, and it handles the mechanical
config authoring gate-free from there. See [SETUP.md](SETUP.md).

### Extensible Toolkit

Many projects need steps no built-in Booley Flow covers: formal verification,
logic equivalence checking, DFT insertion, custom lint rules, power analysis.
Add them as project MCP tools in `.booley/mcp_tools/`; the Developer Agent
discovers and calls them like the built-in ones. See [Custom MCP
tools](CONFIG.md#custom-mcp-tools).
