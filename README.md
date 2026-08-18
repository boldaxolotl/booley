# Booley

**Agentic RTL development framework for faster, evidence-driven hardware iteration**

[![Tests](https://github.com/boldaxolotl/booley/actions/workflows/test.yml/badge.svg)](https://github.com/boldaxolotl/booley/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/booley-rtl)](https://pypi.org/project/booley-rtl/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

![Booley in VS Code with RTL, an interactive agent session, ticket progress, and waveform inspection](docs/booley-screenshot.png)

> **Want to skip ahead?** Jump to the [Quick Start](#quick-start) to watch the videos or try the demo.

It's 2026, and LLMs are finally good enough to do real work. The software world already accepts this: agents write more and more of the code while humans move up to architecture, specification, review, and integration. Hardware moves slower, but it moves: every major EDA vendor and a wave of startups are shipping AI systems for chip design. Those systems are closed, expensive, and out of reach if you don't work at a big company. Raw agents like Claude Code and Codex are useful, but they lack tight integration with EDA tools and still fail in predictable ways on RTL work. So I built the framework I was missing: free and open source.

The idea:

1. Take the most capable coding agents available (Claude Code, Codex).
2. Put them in a sandbox with the repo under development and no general host access, so they can run autonomously without repeated permission prompts.
3. Give them quick, one-command access to an open-source sim/lint/synth/waveform stack inside the sandbox, so they can check their own work and iterate fast.
4. Wrap it all in one IDE: VS Code.

## What Booley Solves

### Hallucination and unreliability

Agents are capable, but they still make predictable mistakes: misreading logs, running the wrong configuration, guessing at behavior instead of inspecting a waveform, trusting a weak testbench, or declaring work complete too early. Booley addresses these failure modes through several independent checks:

- **Waveform-aware debugging (`bwave`)**: a custom Rust tool lets the agent query real traces—find events, inspect values, sample one signal on another, and trace data through the design—instead of guessing from RTL. A prebuilt Linux x86-64 binary for the Session Runtime is attached to every [GitHub Release](https://github.com/boldaxolotl/Booley/releases), alongside its SHA-256 checksum ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#waveform-based-debug)).
- **Machine-checked completion**: Booley Flows return normalized verdicts (`pass`, `fail`, `inconclusive`, or `timeout`), and the harness—not the agent—decides whether every ticket criterion is satisfied ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/USAGE.md#acceptance-criteria)).
- **Named targets and tests**: simulation runs resolve registered FuseSoC `.core` Targets and `tests.toml` tests instead of relying on agent-assembled source lists and commands. The chosen source set, build configuration, and test are explicit and recorded in the report, so the agent—and the reviewer—can verify that the intended simulation actually ran ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#named-targets-and-tests)).
- **Independent Specialist review**: a fresh-context Specialist, separate from the agent that wrote the code, checks RTL for bugs, protocol and spec compliance, style, optimization, and security, then reports findings by severity ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#multi-category-code-review)).
- **Mutation testing**: Booley injects targeted RTL bugs, deterministically runs each one, and measures how many the testbench catches against a threshold. Tests must not only pass; they must prove they can fail ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#mutation-testing)).

### A fragmented toolchain, one interface

EDA tools differ in how they are configured, invoked, and interpreted. Booley gives agents one consistent interface for using them:

- **One typed Booley Flow surface**: the same structured call and normalized verdict across tools serving the same Flow—for example, Icarus Verilog and Verilator for simulation—so agents do not need to adapt to each tool's CLI and output. The same contract is designed to extend to Cadence Xcelium and Synopsys VCS as support is added ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#structured-booley-flow-contracts)).
- **Agent-agnostic**: Claude Code and Codex drive the same Booley Flows through MCP, so changing models does not change the toolchain ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#llm-backend-selection)).
- **One IDE**: the editor, agent, and EDA tools share a VS Code window, keeping RTL work, tool runs, and results in one place.

### You can't let an agent loose on your machine

Autonomous work only pays off when the agent can run without constant permission prompts. Booley confines that autonomy to a recoverable workspace:

- **Sandboxed by default**: the agent and every command it launches run inside the project's Docker sandbox, not on the host. It cannot invoke host commands or read files outside explicit mounts, runs as a non-root user, and has network access only to required service endpoints. Restricted egress reduces exposure to web-based prompt injection, while the sandbox limits the authority and blast radius of malicious instructions. Container isolation reduces risk rather than eliminating it ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md#docker-sandboxing), [security model](https://github.com/boldaxolotl/Booley/blob/main/docs/ARCHITECTURE.md#security--trust-model)).
- **Git isolation**: each ticket uses an isolated worktree with no access to remotes or real branches. Off-script edits and accidental damage remain behind a human review-and-merge gate.
- **Controlled EDA access**: the open-source stack runs in the project's Docker container; approved host installations can be supplied read-only under built-in tool policies while every EDA process remains in that container ([details](https://github.com/boldaxolotl/Booley/blob/main/docs/SUPPORTED-EDA-TOOLS.md)).

### A faster workflow for the engineer

Booley is not only infrastructure that helps agents work reliably. It removes friction from the engineer's whole RTL workflow: turn an idea into a well-defined task, invoke EDA flows without tool-specific command archaeology, move from a failure to the relevant waveform, monitor long-running work, and receive review-ready results.

Interactive or delegated, the editor, agent, EDA tools, waveforms, and evidence stay in one workflow. When you need visual inspection, `bwave gui` opens the relevant signals and time window directly in VaporView. Less time spent on plumbing means faster iteration—and more opportunities to verify, refine, and optimize the design.

See [FEATURES.md](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md) for the full list of capabilities.

## Modes of Operation

Two modes, same project configuration, same EDA stack, same per-folder Docker container, just two ways to drive it.

### Interactive Mode

The hands-on path. Tell the agent what to inspect or edit, which simulation or synthesis to run, what failure to debug, what code to review, or which waveform to open. You guide the work closely and make decisions as they come up.

The agent already knows the project's Targets, tests, Booley Flows, and Specialists, so each chat starts ready to work. With a voice-transcription app such as [Handy](https://handy.computer) or [Wispr Flow](https://wisprflow.ai), you can describe a change or debugging step aloud instead of typing it.

### Ticket Mode

The autonomous path. You write a ticket (the ticket-creation skill helps) specifying what needs doing, which files are in scope, which tests must pass, and any other completion criteria. Start it with `booley run` from a terminal inside the devcontainer; run additional tickets in additional terminals. Booley creates an isolated worktree, drives the Developer Agent through the required Booley Flows and Specialist reviews, tracks the acceptance criteria, and hands you a review-ready result.

When the ticket reaches review, you inspect the result, adjust if needed, then approve and merge into your working branch.

## Supported EDA Tools

Current integrations:

- **Simulate / elaborate** — Verilator, Icarus Verilog; cocotb testbenches supported
- **Lint** — Verilator, Verible
- **ASIC synthesis** (PPA estimate, not tape-out) — Yosys + OpenROAD/OpenSTA
- **Waveform debug** — `bwave` (+ VaporView GUI in VS Code)
- **FPGA implementation** — AMD Vivado

For exact versions, provisioning, trace support, and platform constraints, see
**[SUPPORTED-EDA-TOOLS.md](https://github.com/boldaxolotl/Booley/blob/main/docs/SUPPORTED-EDA-TOOLS.md)**.
Support for additional commercial EDA tools is coming soon; see the
[roadmap](https://github.com/boldaxolotl/Booley/blob/main/docs/ROADMAP.md#commercial-eda-tools).

## Limitations

- **Booley will not design hardware for you.** You design the architecture and write the specs; Booley handles the grunt work. Force multiplier, not replacement.
- **You need prior digital design experience.** Even the most advanced LLM is useless without electronic engineering fundamentals; Booley assumes you can read RTL, judge a waveform, and know what a sane result looks like.
- **Source languages are SystemVerilog and Verilog only.** VHDL is not supported.
- **Testbenches are simple and direct.** Direct SystemVerilog and cocotb testbenches are supported; UVM is not.
- **Only tested at the IP level.** Complex IPs, like a RISC-V core or crypto accelerators, but never chip- or SoC-level integration. See [Ports](https://github.com/boldaxolotl/Booley/blob/main/docs/SETUP.md#ports) for what has actually been through it.
- **Setup can take effort.** The setup skills make integration as smooth as I could get it, but every build system is different; complex flows or heavy licensed EDA tools may still need project-specific work. It's a price you pay once, though. After that, every ticket and every session builds on it, and development speeds up significantly.
- **The code quality is "hardware engineer writing software."** The architecture is sound, but the Python could use polish. Contributions from actual software developers are very welcome.
- **Work in progress.** Expect occasional bugs and rough edges in the UI. I'm actively on it, and things keep getting better.

## Quick Start

Three ways in, ordered by how much you want to invest:

1. **[Level 1: Watch](#level-1-watch).** See an engineer drive Booley on a demo project, start to finish. Zero setup.
2. **[Level 2: Try the demo yourself](#level-2-try-the-demo-yourself).** Clone a pre-configured demo repo and run its tickets out of the box.
3. **[Level 3: Use it on your own project](#level-3-use-it-on-your-own-project).** Full integration on your own RTL.

### Level 1: Watch

Four videos show me driving Booley on a demo project end to end, so you can see the workflow before touching anything:

1. **[Design Optimization](https://youtu.be/zHuvU4QJbvE)** (12:43)
2. **[Finding and Fixing Bugs](https://youtu.be/hsYHHZcx82w)** (9:40)
3. **[Feature Ticket Creation](https://youtu.be/sy1KMCHYnEw)** (10:36)
4. **[Ticket Results Review](https://youtu.be/nHOgd5Jz6Eo)** (11:21)

I recorded all four videos myself, then replaced my narration with text-to-speech to preserve my anonymity for now.

### Level 2: Try the demo yourself

**The demo IP** is [picorv32](https://github.com/YosysHQ/picorv32), Claire Wolf's open-source RISC-V CPU core. It is a small, area-optimized design with a straightforward multi-cycle architecture. That makes the RTL easy to understand and keeps lint, simulation, and synthesis runs fast, while still exercising Booley on a real project rather than a toy example.

You clone the **official upstream repo, untouched**, and drop the pre-configured Booley project ([booley-prj-picorv32](https://github.com/boldaxolotl/booley-prj-picorv32)) inside it as `.booley_project/`. Everything Booley-related (design description, tests, config, a queued ticket) ships in that one directory, and no `/booley-setup` is needed.

Follow the [demo repository's README](https://github.com/boldaxolotl/booley-prj-picorv32#readme) to try it yourself.

For platform, software, and agent-account requirements, see [Installation prerequisites](https://github.com/boldaxolotl/Booley/blob/main/docs/INSTALL.md#prerequisites).

### Level 3: Use it on your own project

Follow [SETUP.md](https://github.com/boldaxolotl/Booley/blob/main/docs/SETUP.md) to integrate Booley with your own RTL project.

## Documentation

### Essential documentation

- [Features](https://github.com/boldaxolotl/Booley/blob/main/docs/FEATURES.md): expanded descriptions of all capabilities
- [Architecture](https://github.com/boldaxolotl/Booley/blob/main/docs/ARCHITECTURE.md): the big-picture structure of Booley and how its parts fit together
- [Installation](https://github.com/boldaxolotl/Booley/blob/main/docs/INSTALL.md): supported hosts, prerequisites, and CLI installation
- [Setup](https://github.com/boldaxolotl/Booley/blob/main/docs/SETUP.md): integrating Booley with an RTL project, from initial bootstrap through configuration and validation
- [Usage](https://github.com/boldaxolotl/Booley/blob/main/docs/USAGE.md): for learning the day-to-day Booley workflow, both interactively and through tickets
- [Supported EDA tools](https://github.com/boldaxolotl/Booley/blob/main/docs/SUPPORTED-EDA-TOOLS.md)

### In-depth documentation

- [Context](https://github.com/boldaxolotl/Booley/blob/main/docs/CONTEXT.md): the controlled vocabulary: what every Booley term means, and the synonyms to avoid
- [Configuration](https://github.com/boldaxolotl/Booley/blob/main/docs/CONFIG.md): project configuration, targets, tests, providers, and advanced setup
- [Booley Flows](https://github.com/boldaxolotl/Booley/blob/main/docs/BOOLEY-FLOWS.md): build and evidence contracts for simulation, lint, synthesis, and FPGA flows
- [Troubleshooting](https://github.com/boldaxolotl/Booley/blob/main/docs/TROUBLESHOOTING.md): symptoms and their fixes when something misbehaves
- [MCP tools](https://github.com/boldaxolotl/Booley/blob/main/docs/MCP-TOOLS.md): discovery, shared execution contracts, Criteria wiring, execution in the Docker container, and custom extensions
- [Why](https://github.com/boldaxolotl/Booley/blob/main/docs/WHY.md): the rationale behind the load-bearing decisions (Docker, VS Code, two modes, FuseSoC, MCP, the waveform store)
- [Roadmap](https://github.com/boldaxolotl/Booley/blob/main/docs/ROADMAP.md): planned features and future directions
- [Contributing](https://github.com/boldaxolotl/Booley/blob/main/docs/CONTRIBUTING.md): development setup and contribution guidelines

## Contributing

Booley is still early in its development, so the most valuable contribution
right now is simply using it and reporting back: what works, what doesn't, and
any bugs you hit along the way. Tell the **`/booley-feedback`** skill in your
agent chat; it chooses the right reporting path, gathers any evidence needed,
and handles redaction for you.

Praise, gripes, feature wishes, "this saved me three days on our NoC", "this was not worth the setup" — all of it is wanted, and opinions need no reproduction. Nothing leaves your machine until you have read the exact text and said yes. See [USAGE.md: When Booley itself misbehaves](https://github.com/boldaxolotl/Booley/blob/main/docs/USAGE.md#when-booley-itself-misbehaves) and [CONFIG.md: Feedback](https://github.com/boldaxolotl/Booley/blob/main/docs/CONFIG.md#feedback-feedback).

**A note on scope:** Booley starts from the premise that AI is a useful tool for hardware design. Feedback on how Booley uses AI is welcome. Broader debates about AI's effects on society or employment are outside the scope of this project. Please keep criticism of Booley technical and specific.

Contributions welcome. See [CONTRIBUTING.md](https://github.com/boldaxolotl/Booley/blob/main/docs/CONTRIBUTING.md) for guidelines.

For suspected vulnerabilities, do not open a public issue. Follow the private
reporting process in [SECURITY.md](https://github.com/boldaxolotl/Booley/blob/main/SECURITY.md).

## Acknowledgments

Booley stands on a lot of other people's work. Thank you to:

- **The authors of [Edalize](https://github.com/olofk/edalize) and [FuseSoC](https://github.com/olofk/fusesoc)**, and especially their lead maintainer, Olof Kindgren, for the framework that makes Booley's whole idea of a simple, unified CLI-over-EDA interface possible.
- **The author of [vcdvcd](https://github.com/cirosantilli/vcdvcd), Ciro Santilli**, for the VCD-parsing work that seeded the `bwave` idea.
- **The author of [wavepeek](https://github.com/kleverhq/wavepeek)**, another neat waveform-to-CLI EDA tool, for the clean top-level CLI interface that inspired `bwave`'s top-level CLI (the internals started well before wavepeek and are quite different).
- **The author of [VaporView](https://github.com/Lramseyer/vaporview), Lloyd Ramseyer**, for the excellent VS Code waveform viewer that `bwave gui` drives for scoped waveform inspection right in the IDE.
- **The authors of [Yosys](https://github.com/YosysHQ/yosys), [Verilator](https://github.com/verilator/verilator), [Icarus Verilog](https://github.com/steveicarus/iverilog), [Verible](https://github.com/chipsalliance/verible), and [sv2v](https://github.com/zachjs/sv2v)**, for the excellent open-source EDA tools that make Booley possible at all.
- **[Matt Pocock](https://www.aihero.dev/)**, for his great agentic software engineering techniques, which shaped how Booley's agents are built and driven.

## License

Apache 2.0. See [LICENSE](https://github.com/boldaxolotl/Booley/blob/main/LICENSE) for details.
