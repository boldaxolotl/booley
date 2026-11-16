# Architecture

This page is for contributors and agents changing Booley. It explains how
Booley's parts fit together and why the load-bearing ones are built the way
they are; read it before changing the Sandbox, the MCP server, or a
Booley Flow. Terms such as Booley Flow, Target, Specialist, and Sandbox are
defined in the [glossary](../GLOSSARY.md). How to use Booley is in
[USAGE.md](../user/USAGE.md) and [CLI-REFERENCE.md](../user/CLI-REFERENCE.md).
Which packages may import which is enforced by
`tests/architecture/booley_contract.py`. The history of each decision is in the
ADRs under `docs/adr/`.

## Overview

Booley is an agentic development environment (ADE) for RTL design and
verification. It makes EDA workflows drivable by LLM agents without trusting the
agents' claims: it wraps EDA toolchains as **Booley Flows** with structured
inputs and machine-checkable results, and exposes those Flows and LLM-backed
**Specialists** to the Developer Agent through MCP.

All Booley work runs in one container, the **Sandbox**, per opened project
folder. A human steers the Developer Agent in a VS Code devcontainer
(Interactive Mode), using the `.booley/` configuration.

Booley installed on the host manages Host Bootstrap, the Sandbox lifecycle,
EDA installation registrations and Project Grants, the egress proxy, and the
idle reaper.
Developer Agent commands run only inside the Sandbox, with one known exception
through shared Git metadata (see [Security & Trust Model](#security--trust-model)).

Preparation runs in one direction: [Host
Bootstrap](../user/INSTALL.md#host-bootstrap), then Project Initialization
([SETUP.md](../user/SETUP.md)), then the Sandbox. A later step checks the
earlier one but never repairs it. `booley init
--force` rebuilds only the Project's own images; when the base image is
missing or stale it stops and asks for `booley bootstrap --update`. This keeps
host-wide changes in one command the user runs deliberately
([ADR 0056](../adr/0056-separate-host-bootstrap-from-project-initialization.md)).

![Booley architecture: VS Code opens an agent session (Claude Code or Codex, in Interactive Mode) inside the Booley devcontainer Sandbox. The agent edits the Project Git worktree, and calls the MCP Server (Booley Flows), which drives the EDA tools (linter, simulator, synthesis tool) through Edalize, and calls Specialist Agents. An Egress Proxy Container lets the Sandbox reach the LLM provider only, and a heavy tool installed on the host (Vivado) is mounted read-only.](../user/assets/architecture.svg)

### Why an ADE, not an IDE

An IDE is built around a person typing code. In Booley the Developer Agent
writes the RTL and testbenches and drives the EDA tools; the engineer decides
what to build, answers the agent's questions, and judges the evidence. Tools
built that way call themselves agentic development environments:
[Warp](https://www.warp.dev/articles/what-is-an-agentic-development-environment)
coined the term for a terminal where agents are first-class, and
[Orca](https://www.onorca.dev/) runs a fleet of coding agents, each in its own
git worktree.

Hardware changes what an ADE has to supply. A software ADE can trust the
compiler and test runner it finds in the repository. An RTL agent needs a
pinned, sandboxed EDA stack, typed Booley Flows instead of hand-run Tcl,
waveforms it can query, and acceptance it can't fake: area, timing, cycle
counts, and coverage checked by Booley against fresh results. The rest of this
page describes those pieces.

The framing also decides where product work goes: into supervising agents
(several sessions at once, Job progress, fast review), not into
editor features. VS Code already is a good editor
([ADR 0083](../adr/0083-position-booley-as-an-agentic-development-environment.md)).

## The Sandbox

The Sandbox is the shared execution and containment boundary. The
`booley-sandbox` image supplies the open-source simulation, lint, synthesis,
timing, and waveform stack, so most Projects need nothing installed; the
packaged EDA tools are listed in
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md). The container runs as
the non-root `agent` user with the Project Root and Booley Directory mounted
in. It stays up when the editor window closes and stops only on `booley session
down` or when the idle reaper finds it unattached for `[sandbox]
idle_timeout_seconds` in the host `config.toml` (default 2 hours). Setup and
image customization are in [SETUP.md](../user/SETUP.md) and
[SANDBOX.md](../user/SANDBOX.md#custom-sandbox-image).

`runtime/image_lifecycle.py` is the one module that reconciles a Sandbox Image
and the managed images it is built from, for init, refresh, and Doctor alike.
Callers get an immutable image identity and typed diagnostics, so none of them
reimplements Docker freshness rules
([ADR 0053](../adr/0053-authoritative-session-image-lifecycle.md)). Replacing a
running Sandbox is a separate roll-forward step (`runtime/session_refresh.py`):
it keeps no journal and never restores the previous container, so a failure
names its step and rerunning refresh converges.

### Why Docker

Booley runs LLM agents that execute real EDA flows against the user's RTL,
which is exactly what nobody wants loose on their machine. Docker gives the
Sandbox boundary: the agent has no host command channel and no unmediated
network. A misbehaving or hijacked agent can still damage the working copy it
was handed, so review stays essential.

The container pays for itself a second time as a shared development
environment. The whole open-source stack (Verilator, Icarus, Verible, Yosys,
OpenROAD, sv2v, `bwave`, Python) ships in the image, so the user installs no
EDA tools and everyone on a Project runs the same pinned versions.

The container costs no measurable EDA runtime. On a 16-core Intel Core
i7-14650HX workstation, a controlled Ibex comparison ran all four Ibex
configurations through clean, warm, direct-runtime, and traced Verilator
simulation, plus Yosys/OpenROAD synthesis, as CPU-pinned container/host pairs
(190 measured legs). Pooled median differences ranged from -0.13% to +0.20%,
within ordinary run-to-run variation.

### Why one container

Cloud coding agents (Codex cloud, Devin) and research harnesses (SWE-agent,
OpenHands) start a fresh sandbox per task. Booley uses one container per opened
project folder instead. The Dev Containers extension attaches one container per
VS Code window; a container per chat tab would need session-to-container
plumbing the extension lacks. Every session shares one MCP server, one EDA
stack, and one configuration,
so another parallel session is just another tab.

The cost: every session draws on one container's memory and CPU, which makes
admission control mandatory (next section), and every worktree in the
container shares one trust domain.

### Job admission

Every Flow or Specialist run is a Job. A Job holds a reservation estimated from
the Resource History and runs while the reservations fit the Memory Budget;
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

## Booley Flows and Specialists

The Developer Agent reaches two kinds of capability through one MCP surface,
never by spawning EDA tools itself. **Booley Flows** deterministically turn a
structured request into an EDA tool invocation and its result into evidence.
**Specialists** are scoped LLM sub-agents for work such as review. Mutation testing is a Flow
(`booley flow mutation`), not a Specialist: it needs no model. The
catalog is in
[CLI-REFERENCE.md](../user/CLI-REFERENCE.md#booley-flows--specialists), the
Flow code in `src/booley/flows/`, and the MCP extension model in
[MCP-TOOLS.md](MCP-TOOLS.md).

A built-in Flow that drives interchangeable EDA tools keeps one adapter per
tool under `src/booley/flows/<flow>/backends/`: simulation has Cocotb, Icarus,
and Verilator; synthesis has Yosys and OpenROAD; FPGA implementation has
Vivado. "backends" is only the package name; docs and configuration call the
external program an **EDA tool**. `src/booley/eda/provisioning/` owns host
installation and licensing policy.

### Why FuseSoC

Booley owns the build: the layer that turns a design into the exact command
for each EDA tool. That ownership makes trusted results possible: Booley can
only trust a result it produced
from an invocation it controlled. Booley, not the agent's prompt, decides how a
Flow runs.

Booley doesn't write that build system itself. FuseSoC is the most mature
open-source EDA build system and sits directly on Edalize, which generates the
per-tool command. File-list ordering, typed parameters, per-flow targets, and
the long tail of EDA tool integrations are code FuseSoC maintains.

The cost is that every Project needs a FuseSoC `.core` with a Target before
Booley can build it. The setup skills write it, and for a Project with
existing file lists it is a mechanical restatement of them. Booley offers no
per-call escape hatch for ad-hoc files or defines: that would bring back a
second source-discovery path and builds Booley didn't control. The one thing a
`.core` can't express, a non-RTL build step per test, is covered by
`[flows.sim].pre_run_commands` rather than a parallel build path.

### Why an MCP server

Claude Code and Codex both call MCP tools natively, so a Flow offered as an MCP
tool is a typed function call the model was trained to make, not prose it must
turn into a shell command. Both [Anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
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
  tail of stdout (12,000 characters by default) and stderr, and the key
  `report.json` fields; the full report travels as structured content. Through
  a shell the agent gets raw output and has to remember to read the report.
- **Visibility that really hides.** `BOOLEY_MCP_TOOLS`, the nested-agent
  allowlists, and the mode rules in `mcp/catalog.py` remove MCP tools from the
  list the agent sees. With a CLI, the same restriction is only a request in
  the prompt.

## Interactive Mode

Interactive Mode is how a human works with Booley and the way into the
Sandbox.
`booley init` issues the Project's devcontainer specification; reopening the
folder in the container connects VS Code to the Sandbox. On every container
start, `postStartCommand` starts a loopback MCP server inside the container,
so a reopened session reconnects on its own. Through it the Developer Agent
reaches Flows, read-only Specialists, B-Wave, and runtime status.
`BOOLEY_MCP_TOOLS` and nested allowlists control which MCP tools are listed. See
[SETUP.md](../user/SETUP.md) for the lifecycle and
[USAGE.md](../user/USAGE.md#interactive-mode) for daily use.

### Why VS Code

VS Code covers three needs in one program:

- **One editor for both Agent CLIs.** `booley chat` (or bare `booley`) starts
  Claude Code or Codex in a Sandbox terminal; both share the editor and the
  Sandbox.
- **Sandbox sessions for free.** The Dev Containers extension makes "open this
  repository inside the Booley container" a built-in operation, so Booley
  didn't have to build it.
- **A good editor.** Hardware work is not pure delegation: the user reads and
  edits RTL between agent turns in the same workspace.

## Dashboard

`booley dashboard` is the Sandbox view and can also start Booley Flows
([ADR 0086](../adr/0086-launch-flows-and-specialists-from-the-dashboard.md));
its design is in [ADR 0068](../adr/0068-booley-dashboard.md).

### Shared execution and evidence

- `runtime.agent_execution` runs the Agent CLI for Specialists and Doctor.
- `mcp.unbound_recorder` records evidence for MCP calls.

Evaluation inside this code uses the internal
[Criterion](../GLOSSARY.md#execution-and-evidence) vocabulary.

## B-Wave

**[B-Wave](../../crates/bwave/GLOSSARY.md)** is Booley's waveform query layer.
A trace is too large for an LLM to read, so B-Wave answers
structured questions about signals, events, values, and time ranges in an FST
store; `bwave register --build` converts a VCD into one.

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

## Agent CLIs

The MCP surface is the same for every Agent CLI. A Project lists the Agent CLIs
it may run (Claude Code, Codex, or both) and names a default for agent
sessions and Booley's own calls. Each Specialist runs on the Agent CLI its
configured model implies
([ADR 0084](../adr/0084-run-each-specialist-on-the-agent-cli-its-model-implies.md)).
Configuration is under [`[agent]`](../user/CONFIG.md#agent-clis-agent) and
[`[models]`](../user/CONFIG.md#model-selection-models); sign-in and billing are
in [SETUP.md](../user/SETUP.md#sign-in-and-billing).

## Why plain files

Booley keeps all state in files: evidence state as JSON, Job claims, and Job
records. There is no database or state daemon, so nothing needs installing
or keeping alive, and any state can be read with `ls` and `cat`. The cost is
that atomic renames and lock files are the concurrency primitives, and they
take care to get right. That trade fits Booley's scale: one human per Project.

## Common and Checkout Booley State

A Project's Booley Directory splits into two kinds of state. Common Booley
State, which every checkout shares (Session and Job coordination, Project-wide
records and evidence, shared caches), lives only in the
main checkout's Booley Directory, and a command run in any worktree reaches it
there. Checkout Booley State belongs to one checkout: configuration versioned
with its code (`booley.toml`, `cores/`, hooks) and the build trees
and Flow reports of its sources. `booley.core.booley_dir_parts` classifies each
path; the path-by-path split is in
[ADR 0082](../adr/0082-give-each-concurrent-interactive-session-its-own-worktree.md).

## Security & Trust Model

Booley treats Developer Agent behavior as untrusted, whether it comes from error
or adversarial input. Developer Agents, MCP tools, Flows, and EDA processes run
inside the Sandbox with no host command channel. The container gets the Project
Root, its Booley Directory, linked worktrees, and explicitly authorized
resources; it has no host home directory, SSH keys, Docker socket, or host EDA
configuration. A real `.ssh` directory holding only a regular `authorized_keys`
that is not mounted from the host grants inbound access to the Sandbox, not host
authority; a mounted or symlinked one is host authority, and Doctor FAILs on it.
It runs as a non-root user with all capabilities dropped, `no-new-privileges`,
and a PID limit. A memory limit applies only when the Project sets
`[sandbox].memory`.

Network egress belongs to the whole Sandbox; there is no per-Flow network
boundary. By default every process in the Sandbox, EDA tools included, reaches
only the egress proxy's allowlist: the endpoints the Agent CLIs need, plus any
hosts in the host `config.toml` `egress_allowlist`. A host may opt into
[Unproxied Sandboxes](../user/SANDBOX.md#host-configuration-configtoml), whose
egress Booley does not mediate.

Host-installed EDA tools follow the same boundary. The host registers an
installation and grants it to a Project; Booley mounts it read-only,
revalidates the grant before a Sandbox resumes, and limits license traffic to a
fixed-destination relay. Invalid, stale, or revoked authority fails before the
Sandbox starts instead of falling back to running anything on the host. This
keeps dispatch, configuration, execution, and result interpretation on the
container side of the trust boundary.

**Known escape hatch:** shared Git metadata is writable from the Sandbox. An
agent can plant Git configuration or hooks that a later Git command on the host
runs, including Booley's own host-side Git calls. That code runs with the host
user's authority, without exploiting Docker or the kernel. Doctor does not
detect or prevent it. The fix is tracked in
[#1529](https://github.com/boldaxolotl/booley/issues/1529); see the
[current precautions](../user/SANDBOX.md#known-sandbox-escape-through-shared-git-metadata).

Protected Input checks guard the configuration that produces evidence. Git keeps
committed changes for review and
recovery, but the writable Git metadata stays subject to the escape hatch
above.

This reduces the agent's authority a lot but is not complete isolation. The
remaining risks are the shared-Git escape hatch, container or kernel escape,
wrong host provisioning, vendor EDA tool behavior inside the Sandbox, and
license-server traffic the host approved.
