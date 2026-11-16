# Architecture

This page is for contributors and agents changing Booley. It explains how
Booley's parts fit together and why the main ones are built the way they are;
read it before changing the Sandbox, the MCP server, a Booley Flow,
or Job admission. It is not a usage guide: how to use Booley is in
[USAGE.md](../user/USAGE.md) and [CLI-REFERENCE.md](../user/CLI-REFERENCE.md).
Terms such as Booley Flow, Target, Specialist, and Sandbox are defined in the
[glossary](../GLOSSARY.md), the history of each decision is in the ADRs under
`docs/adr/`, and `tests/architecture/booley_contract.py` enforces which
packages may import which.

## Overview

Booley is an agentic development environment (ADE) for RTL design and
verification. It makes EDA workflows drivable by LLM agents without trusting
the agents' claims: it wraps EDA toolchains as **Booley Flows** with
structured inputs and machine-checkable results, and exposes those Flows and
LLM-backed **Specialists** to the Developer Agent through MCP.

[![Booley architecture: on the host, Booley issues and supervises one Sandbox per Project folder, and VS Code attaches to it. In the Sandbox, Developer Agent sessions edit the Project's Git worktrees (the main checkout plus one per concurrent session) and call the one MCP server. The MCP server submits each Booley Flow or Specialist run as a Job, admitted against the Memory Budget. A Flow resolves the Target with FuseSoC, runs the EDA tools, and parses their logs into metrics and report.json. B-Wave queries FST traces outside any Job and opens them in VaporView. Vivado is mounted read-only from the host, and egress goes through the host's egress proxy by default.](../user/assets/architecture.svg#gh-light-mode-only)](../user/assets/architecture.svg)
[![Booley architecture: on the host, Booley issues and supervises one Sandbox per Project folder, and VS Code attaches to it. In the Sandbox, Developer Agent sessions edit the Project's Git worktrees (the main checkout plus one per concurrent session) and call the one MCP server. The MCP server submits each Booley Flow or Specialist run as a Job, admitted against the Memory Budget. A Flow resolves the Target with FuseSoC, runs the EDA tools, and parses their logs into metrics and report.json. B-Wave queries FST traces outside any Job and opens them in VaporView. Vivado is mounted read-only from the host, and egress goes through the host's egress proxy by default.](../user/assets/architecture-dark.svg#gh-dark-mode-only)](../user/assets/architecture-dark.svg)

The diagram reads from the outside in, and so does this page:

1. [The host and the Sandbox](#the-host-and-the-sandbox): Booley on the host
   prepares and supervises one container per Project folder, and all Booley
   work runs inside it.
2. [Agent sessions](#agent-sessions): VS Code attaches to the Sandbox, and
   each Developer Agent session runs in one of its terminals; concurrent
   sessions get their own worktrees.
3. [The MCP server](#the-mcp-server): the one surface through which every
   session reaches Booley.
4. [Jobs](#jobs): how every run the MCP server starts shares the container's
   memory.
5. [Booley Flows](#booley-flows), [Measurement](#measurement),
   [Specialists](#specialists), and [B-Wave](#b-wave): what those runs do.
6. [State](#state): where Booley keeps what it records.
7. [Security & Trust Model](#security--trust-model): what the boundary
   protects and where it leaks.

### Why an ADE, not an IDE

An IDE is built around a person typing code. In Booley the Developer Agent
writes the RTL and testbenches and drives the EDA tools; the engineer decides
what to build, answers the agent's questions, and judges the evidence.
[Warp](https://www.warp.dev/articles/what-is-an-agentic-development-environment)
coined the term agentic development environment for a terminal where agents
are first-class.
[T3 Code](https://github.com/pingdotgg/t3code), an open-source control plane
for coding agents, is built the same way: it runs Claude Code and Codex
threads side by side and manages their git worktrees.

Hardware changes what an ADE has to supply. A software ADE can trust the
compiler and test runner it finds in the repository. An RTL agent needs a
pinned, sandboxed EDA stack, typed Booley Flows instead of hand-run Tcl,
waveforms it can query, and acceptance it can't fake: area, timing, cycle
counts, and coverage checked by Booley against fresh results.

The framing also decides where product work goes: into supervising agents
(several sessions at once, Job progress, fast review), not into editor
features. VS Code already is a good editor
([ADR 0083](../adr/0083-position-booley-as-an-agentic-development-environment.md)).

## The host and the Sandbox

Booley installed on the host manages Host Bootstrap, the Sandbox lifecycle,
EDA installation registrations and Project Grants, the egress proxy, and the
idle reaper. Developer Agent commands run only inside the Sandbox, with one
known exception through shared Git metadata (see
[Security & Trust Model](#security--trust-model)).

Preparation runs in one direction: [Host
Bootstrap](../user/INSTALL.md#host-bootstrap), then Project Initialization
([SETUP.md](../user/SETUP.md)), then the Sandbox. A later step checks the
earlier one but never repairs it: `booley init --force` rebuilds only the
Project's own images, and when the base image is missing or stale it stops and
asks for `booley bootstrap --update`. This keeps host-wide changes in one
command the user runs deliberately; see
[ADR 0056](../adr/0056-separate-host-bootstrap-from-project-initialization.md).

The Sandbox is the shared execution and containment boundary. The
`booley-sandbox` image supplies the open-source simulation, lint, synthesis,
timing, and waveform stack, so most Projects need nothing installed; the
packaged EDA tools are listed in
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md). The container runs
as the non-root `agent` user with the Project Root and Booley Directory
mounted in. It stays up when the editor window closes and stops only on
`booley session down` or when the idle reaper finds it unattached for
`[sandbox] idle_timeout_seconds` in the host `config.toml` (default 2 hours).
Setup and image customization are in [SETUP.md](../user/SETUP.md) and
[SANDBOX.md](../user/SANDBOX.md#custom-sandbox-image).

`runtime/image_lifecycle.py` is the one module that reconciles a Sandbox Image
and the managed images it is built from, for init, refresh, and Doctor alike.
Callers get an immutable image identity and typed diagnostics, so none of them
reimplements Docker freshness rules
([ADR 0053](../adr/0053-authoritative-session-image-lifecycle.md)). Replacing
a running Sandbox is a separate roll-forward step (`runtime/session_refresh.py`):
it keeps no journal and never restores the previous container, so a failure
names its step and rerunning refresh converges.

### Why Docker

Booley runs LLM agents that execute real EDA flows against the user's RTL,
and those agents should not run loose on the user's machine. Docker gives the
Sandbox boundary: the agent has no host command channel and no unmediated
network. A misbehaving or hijacked agent can still damage the working copy it
was handed, so review stays essential.

The container is also a shared development environment. The whole open-source
stack (Verilator, Icarus, Verible, Yosys, OpenROAD, sv2v, `bwave`, Python) ships
in the image, so the user installs no EDA tools and everyone on a Project runs
the same pinned versions.

The container costs no measurable EDA runtime. On a 16-core Intel Core
i7-14650HX workstation, a controlled Ibex comparison ran all four Ibex
configurations through clean, warm, direct-runtime, and traced Verilator
simulation, plus Yosys/OpenROAD synthesis, as CPU-pinned container/host pairs
(190 measured legs). Pooled median differences ranged from -0.13% to +0.20%,
within ordinary run-to-run variation.

### Why one container

Cloud coding agents (Codex cloud, Devin) and research harnesses (SWE-agent,
OpenHands) start a fresh sandbox per task. Booley uses one container per
opened project folder instead. The Dev Containers extension attaches one
container per VS Code window; a container per chat tab would need
session-to-container plumbing the extension lacks. Every session shares one
MCP server, one EDA stack, and one configuration, so another parallel session
is just another tab.

The downside is that every session draws on one container's memory and CPU,
which makes [Job admission](#jobs) mandatory, and every worktree in the
container shares one trust domain.

## Agent sessions

`booley init` issues the Project's devcontainer specification; reopening the
folder in the container connects VS Code to the Sandbox. `booley chat` (or
bare `booley`) starts a Developer Agent session in a Sandbox terminal. The
first session works in the main checkout, and Booley adds a worktree for each
further concurrent session; see
[ADR 0082](../adr/0082-give-each-concurrent-interactive-session-its-own-worktree.md).
See [SETUP.md](../user/SETUP.md) for the lifecycle and
[USAGE.md](../user/USAGE.md#interactive-mode) for daily use.

The MCP surface is the same for every Agent CLI. A Project lists the Agent CLIs
it may run (Claude Code, Codex, or both) and names a default for agent
sessions and Booley's own calls. Configuration is under
[`[agent]`](../user/CONFIG.md#agent-clis-agent) and
[`[models]`](../user/CONFIG.md#model-selection-models); sign-in and billing
are in [SETUP.md](../user/SETUP.md#sign-in-and-billing).

`booley dashboard` is the Sandbox view beside the editor. It shows the
Sandbox's sessions and Jobs and can start Booley Flows as Standalone Jobs
([ADR 0068](../adr/0068-booley-dashboard.md),
[ADR 0086](../adr/0086-launch-flows-and-specialists-from-the-dashboard.md)).

### Why VS Code

Claude Code and Codex both run in VS Code terminals, so both Agent CLIs share
one editor and the Sandbox. The Dev Containers extension makes "open this
repository inside the Booley container" a built-in operation, so Booley didn't
have to build it. And the editor itself matters: hardware work is not pure
delegation, and the user reads and edits RTL between agent turns in the same
workspace.

## The MCP server

On every container start, `postStartCommand` starts a loopback MCP server
inside the container, so a reopened session reconnects on its own. It is the
only way the Developer Agent reaches Booley Flows, Measurement, Specialists,
B-Wave, and runtime status; the agent never spawns EDA tools itself. The server
submits each Flow, Measurement, or Specialist run as a [Job](#jobs) and
returns its run id at once; `booley_poll` reads the result. B-Wave queries run
directly, outside any Job. `BOOLEY_MCP_TOOLS`
and the nested-agent allowlists control which MCP tools are listed. The MCP
extension model is in [MCP-TOOLS.md](MCP-TOOLS.md).

### Why an MCP server

Claude Code and Codex both call MCP tools natively, so a Flow offered as an MCP
tool is a typed function call the model was trained to make, not prose it must
turn into a shell command. Both
[Anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
and [OpenAI](https://openai.com/index/function-calling-and-other-api-updates/)
train their models for native tool calls, and
[MCPVerse](https://arxiv.org/abs/2508.16260) measured a large accuracy drop when
the same tool definitions moved from the native tool list into the prompt. A
well-documented CLI can match the success rate, but in
[one Claude Code comparison](https://mariozechner.at/posts/2025-08-15-mcp-vs-cli/)
it cost about 40 times the tokens. The MCP surface also gives Booley:

- **Schemas with no drift.** `mcp/flow_adapter.py` derives each MCP tool's
  input schema from the endpoint's argparse parser (custom tools may supply an
  `mcp_schema()` hook), so the agent never needs a `--help` round-trip and no
  prompt doc can fall out of date. `bwave` is the exception: its parser uses
  `REMAINDER`, so its schema is written by hand in `mcp/catalog.py`.
- **Curated results.** `mcp/report_encoding.py` returns the exit code, the
  tail of stdout (12,000 characters by default) and stderr, and the main
  `report.json` fields; the full report travels as structured content. Through
  a shell the agent gets raw output and has to remember to read the report.
- **Hidden tools stay hidden.** `BOOLEY_MCP_TOOLS`, the nested-agent
  allowlists, and the mode rules in `mcp/catalog.py` remove MCP tools from the
  list the agent sees. With a CLI, the same restriction is only a request in
  the prompt.

## Jobs

Every Flow, Measurement, or Specialist run is a Job, whether the MCP server
submits it or a human starts it as a Standalone Job. The Job is the whole run,
not one EDA command. A Job holds a reservation estimated from the Resource
History and runs while the reservations fit the Memory Budget;
excess work queues, and a running Job is never preempted. The rules are in
`src/booley/runtime/job_slots.py` and
[ADR 0087](../adr/0087-admit-jobs-against-a-measured-memory-budget.md).

Claims are files under `<runtime>/jobs/slots/` (see
[Why plain files](#why-plain-files)). The rule the recovery code protects: a
claim becomes free only when the processes that used it are provably gone.
Otherwise a Job whose root process died could free its claim while its EDA
child processes still run, and the next Job would start on top of them. This
spans `runtime/job_slots.py`, `runtime/execution_records.py`,
`runtime/execution_recovery.py`, and the supervisor.

A Simulation Record (code: `flows/sim/record/`) runs its first test on the
Simulation Job's own claim. Each further parallel item takes its own child
memory claim, sized from the item's history, while the reservations fit the
Memory Budget; the number of parallel items never exceeds the cores. Items
whose resolved run directory is the same still run one at a time. Losing a
child claim's lease stops only that test; losing the Job's own claim cancels
the run.

## Booley Flows

A **Booley Flow** deterministically turns a structured request into EDA tool
runs and their logs into metrics and a `report.json`. Once its Job is
admitted, the Flow resolves the Target with FuseSoC; Edalize builds the EDA
tool command for lint, sim, and fpga, and synth renders its own Yosys and
OpenROAD scripts. Mutation testing is a Flow (`booley flow mutation`),
not a Specialist: it needs no model. The catalog is in
[CLI-REFERENCE.md](../user/CLI-REFERENCE.md#booley-flows--specialists) and the
code in `src/booley/flows/`.

A built-in Flow that drives interchangeable EDA tools keeps one adapter per
tool under `src/booley/flows/<flow>/backends/`: simulation has Cocotb, Icarus,
and Verilator; synthesis has Yosys and OpenROAD; FPGA implementation has
Vivado. "backends" is only the package name; docs and configuration call the
external program an **EDA tool**. `src/booley/eda/provisioning/` owns host
installation and licensing policy.

### Why FuseSoC

Booley owns the build: the layer that turns a design into the exact command
for each EDA tool. That ownership makes trusted results possible: Booley can
only trust a result it produced from an invocation it controlled. Booley, not
the agent's prompt, decides how a Flow runs.

Booley doesn't write that build system itself. FuseSoC is the most mature
open-source EDA build system and sits directly on Edalize, which generates the
per-tool command for most Flows. File-list ordering, typed parameters,
per-flow targets, and the long tail of EDA tool integrations are code FuseSoC
maintains.

The cost is that every Project needs a FuseSoC `.core` with a Target before
Booley can build it. The setup skills write it, and for a Project with
existing file lists it is a mechanical restatement of them. Booley offers no
per-call escape hatch for ad-hoc files or defines: that would bring back a
second source-discovery path and builds Booley didn't control. The one thing a
`.core` can't express, a non-RTL build step per test, is covered by
`[flows.sim].pre_run_commands` rather than a parallel build path.

## Measurement

Measurement is how a commit is accepted under
[Metric-Driven Development](../GLOSSARY.md#metric-driven-development-and-reviews):
`booley measure` runs the Flows of every Target that declares Target Metrics
in `metrics.toml` and checks each result against its bound. A run that meets
every enforced metric makes a Measured Commit; locally that verdict is
advisory, and a run in CI is the trusted one
([ADR 0092](../adr/0092-measure-commits-against-target-metrics.md)). Through
MCP, `booley_measure` runs it as one Job.

## Specialists

**Specialists** are optional capabilities the Developer Agent invokes for one
task, with fresh context: the Reviewer and the Coverage Analyst. A Specialist
didn't write the code it checks, so it judges what is there rather than what
was intended. `runtime.agent_execution` runs the Agent CLI for
Specialists and Doctor, and each Specialist runs on the Agent CLI its
configured model implies; see
[ADR 0084](../adr/0084-run-each-specialist-on-the-agent-cli-its-model-implies.md).

## B-Wave

**[B-Wave](../../crates/bwave/GLOSSARY.md)** is Booley's waveform query layer.
A trace is too large for an LLM to read, so B-Wave answers structured
questions about signals, events, values, and time ranges in an FST store;
`bwave register --build` converts a VCD into one.

B-Wave doesn't render waveforms. When the user needs to see one, `bwave gui`
opens the relevant signals and time window in VaporView inside VS Code,
through a Booley extension bridge
([ADR 0078](../adr/0078-drive-vaporview-through-a-booley-extension-bridge.md)).
Query and viewing commands are in
[USAGE.md](../user/USAGE.md#viewing-waveforms).

### Why FST is the trace contract

Every successful traced run ends with a fresh FST store that B-Wave has proven
queryable. A VCD is an unindexed text log: finding a signal's value at time T
means parsing from the top, which for a multi-gigabyte trace is a batch job,
not a query. FST is indexed and compact, which makes `bwave`'s time-range
reads, trigger sampling, and data tracing fast enough for an agent to iterate
on. GTKWave, Surfer, and VaporView all read it, so `bwave gui` needs no export
step.

- **A Verilator Target that declares `--trace-fst` stays native.** It already
  has a matching C++ harness and trace options; Booley only points the output
  at an `.fst` path. Rewriting half of that setup could make the Verilator
  flags disagree with the harness, and converting through VCD would add work
  without adding evidence.
- **Otherwise Booley adds VCD tracing**, which Verilator and Icarus both
  support. For a Verilator Target, when the native `bwave` binary is present,
  the simulator writes VCD into a named pipe while `bwave` converts it in
  parallel, so the large VCD never reaches disk and conversion overlaps the
  run. Otherwise, and for Cocotb and Icarus Targets, Booley converts after the
  run. A Target that already asks for VCD takes the same path without its
  options being rewritten.

## State

A Project's Booley Directory splits into two kinds of state. Common Booley
State, which every checkout shares (Session and Job coordination, Project-wide
records and evidence, shared caches), lives only in the main checkout's Booley
Directory, and a command run in any worktree reaches it there. Checkout Booley
State belongs to one checkout: configuration versioned with its code
(`booley.toml`, `cores/`, hooks) and the build trees and Flow reports of its
sources. `booley.core.booley_dir_parts` classifies each path; the path-by-path
split is in
[ADR 0082](../adr/0082-give-each-concurrent-interactive-session-its-own-worktree.md).

`mcp.unbound_recorder` records evidence for MCP calls.

### Why plain files

Booley keeps all state in files: evidence state as JSON, Job claims, and Job
records. There is no database or state daemon, so nothing needs installing or
keeping alive, and any state can be read with `ls` and `cat`. The cost is
that atomic renames and lock files are the concurrency primitives, and they
take care to get right. That trade fits Booley's scale: one human per Project.

## Security & Trust Model

Booley treats Developer Agent behavior as untrusted, whether it comes from
error or adversarial input. Developer Agents, MCP tools, Flows, and EDA
processes run inside the Sandbox with no host command channel. The container
gets the Project Root, its Booley Directory, linked worktrees, and explicitly
authorized resources; it has no host home directory, SSH keys, Docker socket,
or host EDA configuration. A real `.ssh` directory holding only a regular
`authorized_keys` that is not mounted from the host grants inbound access to
the Sandbox, not host authority; a mounted or symlinked one is host authority,
and Doctor FAILs on it. The container runs as a non-root user with all
capabilities dropped, `no-new-privileges`, and a PID limit. A memory limit
applies only when the Project sets `[sandbox].memory`.

Network egress belongs to the whole Sandbox; there is no per-Flow network
boundary. By default every process in the Sandbox, EDA tools included, reaches
only the egress proxy's allowlist: the endpoints the Agent CLIs need, plus any
hosts in the host `config.toml` `egress_allowlist`. A host may opt into
[Unproxied Sandboxes](../user/SANDBOX.md#host-configuration-configtoml), whose
egress Booley does not mediate. Vivado license traffic goes through its own
fixed-destination relay, not the egress proxy.

Host-installed EDA tools follow the same boundary. The host registers an
installation and grants it to a Project; Booley mounts it read-only,
revalidates the grant before a Sandbox resumes, and limits license traffic to a
fixed-destination relay. Invalid, stale, or revoked authority fails before the
Sandbox starts instead of falling back to running anything on the host. This
keeps dispatch, configuration, execution, and result interpretation on the
container side of the trust boundary.

Protected Input checks guard the configuration that produces evidence. Git
keeps committed changes for review and recovery, but the writable Git metadata
stays subject to the escape hatch below.

**Known escape hatch:** shared Git metadata is writable from the Sandbox. An
agent can plant Git configuration or hooks that a later Git command on the
host runs, including Booley's own host-side Git calls. That code runs with the
host user's authority, without exploiting Docker or the kernel. Doctor does
not detect or prevent it. The fix is tracked in
[#1529](https://github.com/boldaxolotl/booley/issues/1529); see the
[current precautions](../user/SANDBOX.md#known-sandbox-escape-through-shared-git-metadata).

This removes most of the agent's host authority, but it is not complete
isolation. The remaining risks are the shared-Git escape hatch, container or
kernel escape, wrong host provisioning, vendor EDA tool behavior inside the
Sandbox, and license-server traffic the host approved.
