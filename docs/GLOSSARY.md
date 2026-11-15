# Booley glossary

This is the canonical vocabulary for concepts shared across Booley, written for
contributors and agents who write Booley's code, prompts, and docs. Look a term
up here when it is unfamiliar and use it exactly as defined; this is not an
onboarding sequence. Each entry says what a term means, not how it is built:
behavior and configuration belong in the user and internal docs. The
[context map](../GLOSSARY-MAP.md) points to the vocabularies that one context
owns, such as Goal Mode or B-Wave.

Booley is an **agentic development environment (ADE)** for RTL design and verification: the working environment where an engineer directs and supervises Developer Agents doing RTL work, whether hands-on or unattended. **Goal Mode** and **Interactive Mode** share the same isolated **Sandbox**, Booley Flows, and Specialists; neither mode alone defines the product.
_Avoid_ (for the product itself): IDE, agentic RTL IDE, framework, system, library, platform, toolkit, package, harness

The glossary also records words to _avoid_. Booley's concepts collide with
overloaded industry words such as “tool,” “agent,” “target,” and “harness”; one
term per concept keeps prompts, Goals, documentation, and code aligned. Treat
each entry's _Avoid_ terms as rejected synonyms rather than loose alternatives.

## Using Booley

### Lifecycle

How a host and codebase become ready for Booley work. **Host Bootstrap** prepares
reusable host capabilities, **Project Initialization** reconciles one **Project**'s files,
and **Project Setup** adapts that Project to its design.

**Host Bootstrap**:
Idempotent, Project-independent preparation of the host environment and the current user's Booley integrations for the installed Booley version.
_Avoid_: Project bootstrap, machine setup, installation

**Project Initialization**:
Idempotent, location-neutral reconciliation of one codebase's Project files
(Booley Directory, Git integration, guidance links, core projections, Dashboard
task), making it a **Project** without interpreting its design and without
Docker, host credentials or **Sandbox Issuance** ([ADR
0085](adr/0085-run-project-initialization-anywhere.md)).
_Avoid_: Project bootstrap, Project enrollment, Project Setup

**Project Setup**:
Design-aware adaptation and validation of an initialized **Project**, including its design description, verification intent, and Developer Agent guidance.
_Avoid_: Project Initialization, onboarding, porting

### Execution

How and where Booley work runs. The **Sandbox** is the execution environment; **Goal Mode** and **Interactive Mode** are the two execution modes; the remaining entries are the machinery inside.

**Developer Agent**:
The agent of an agent session (Claude Code or Codex) in a **Sandbox**. It authors code and chooses which **Booley Flows** and **Specialists** to use. It exists in **Interactive Mode** and in **Goal Mode**, whether a human is steering or the work is unattended.
_Avoid_: coding agent, host agent, main agent, outer agent, the agent session in Goal Mode

**Agent CLI**:
The coding-agent program an agent runs on: Claude Code (`claude`) or Codex
(`codex`). A Project lists the Agent CLIs it may use and names a default; each
**Specialist** runs on the Agent CLI its model implies.
_Avoid_: agent app, provider (in user-facing text)

**Sandbox**:
The isolated environment, one per opened project folder, where all Booley work
executes: file access, shell, git, EDA subprocesses, MCP servers, logs, and
secrets. Its **Sandbox Policy** is the shared isolation rule set for mounts,
credentials, memory, process limits, capabilities, and egress. Docker by
default.
_Avoid_: Session Runtime, Session Container, Docker Session, MCP sandbox; per-Flow network policy (for Sandbox Policy)

**Sandbox Image**:
The reusable immutable filesystem and installed-program artifact from which
**Sandboxes** are created: a Booley-owned, Project-derived, or external image. A
mutable tag is only a locator, not the image's identity.
_Avoid_: Runtime Image, Session Image, sandbox tag, container, Dockerfile

**Sandbox Issuance**:
The host-owned act of sealing and vouching for one exact Sandbox specification, including its immutable image, trusted mounts, network policy, and granted provisioning inputs. It is distinct from **EDA Provisioning**, which decides where EDA installation files originate.
_Avoid_: Session Runtime Issuance, EDA runtime spec, provisioning issuance

**Interactive Mode**:
Execution mode in which a human steers the Developer Agent inside a Sandbox,
using the Developer Agent's CLI in a Sandbox terminal. Work outside Goal Mode
has no persistent Goal tracking.
_Avoid_: MCP Mode, Standalone Mode, Tab Mode, Booley Interactive

**Goal Mode**:
The execution mode the Developer Agent enters within its session in a linked worktree, on its own Goal Branch, in which its work is judged against its Goals until it finishes with all Goals met or is abandoned. It serves both human-steered and unattended work; the worktree owns its Goal Record. [ADR 0092](adr/0092-measure-commits-against-target-metrics.md) removes Goal Mode in 0.5.0 in favor of [Metric-Driven Development](#metric-driven-development-and-reviews).
_Avoid_: ticket mode, ticket, goal session, objective

**Goal Branch**:
The branch created from a clean worktree's HEAD when a session enters Goal Mode; that HEAD is the base against which Goals are judged. One Goal Mode owns exactly one Goal Branch.
_Avoid_: ticket branch, work branch

**Goal**:
A named, mandatory boolean condition held by a session in Goal Mode, met only by Booley Flow or Specialist evidence at the session's current code. The Developer Agent may ask to relax a Goal; only a human may approve it. Its **Goal Name** is its single human-visible name: its uppercase capability and Target, or its review kind, with requirements beside it. Shared evaluation code still says [Criterion](#execution-and-evidence).
_Avoid_: Criterion, acceptance criterion, optional criterion, check, gate; Goal label, encoded key (for a Goal Name)

**Goalset**:
A named, Project-owned bundle of Goals, written as free-form prose that the Developer Agent translates into Goals when entering Goal Mode.
_Avoid_: Ticket Creation Guidance, criteria defaults, goal profile

**Session Summary**:
The mandatory prose report the Developer Agent writes when its session finishes Goal Mode: what changed and why, which Booley Flows and Specialists it used, and remaining uncertainties.
_Avoid_: Developer Report, run report, Goal Report

**Goal Record**:
The local, uncommitted state of one Goal Mode in the **Main Booley Directory**: state, worktree, Goal Branch, base commit, Goals, protected-input digests, change log, evidence, and the Session Summary. Outside Stealth its committed counterpart is the summary file on the Goal Branch when that path is Git-trackable and owned by the RTL repository; Paired Checkout summaries remain local.
_Avoid_: ticket record, board entry, goal file

**Goal Change**:
One approved change to a Goal of an active Goal Mode: add, relax, retarget, or waive coverage points through a Waiver Candidate. It starts as a **Goal Change Proposal**, the Developer Agent's request, pending until a human approves or rejects it by elicitation form or quoted words in chat. The **Change Log**, the Goal Record's append-only list of Goal Changes, keeps each one's before and after, kind, reason, approval method and session.
_Avoid_: amendment, Ticket Amendment, waiver request, criteria edit; audit log (for the Change Log)

**Protected Input**:
A file that decides how evidence is produced and is therefore digested at Goal Mode entry; editing one blocks Finish until reverted. The set is in `src/booley/goals/protected_inputs.py`.
_Avoid_: frozen file, locked config, Scope

**Review Package**:
The structured result Finish produces for a Goal Mode: diff summary against the base, each Goal with its final evidence, the Change Log, open review findings, Target changes, constraint-file edits, and the Session Summary. Finish replies with a short summary and the path of the full text.
_Avoid_: triage package, run report, acceptance report

**Session**:
In the Session Registry, one agent client thread or process that calls Booley
from a worktree, falling back to the worktree itself when the client cannot be
told apart. It feeds the Dashboard and the shared-worktree warning, and never
selects a Goal Record.
_Avoid_: connection, tab, MCP session, Sandbox session

**Sandbox Attachment**:
The connection method by which a human-facing app or autonomous driver uses a
Sandbox, such as VS Code Dev Containers or `booley session` terminal attachment.
_Avoid_: Runtime Attachment, remote, tunnel, app bridge

**Doctor**:
The diagnostic command for Booley's build and execution machinery in a
**Project**'s environment, including integration with Project build
configuration. Correctly executed and interpreted design failures are compatible
with healthy machinery; see the [Doctor reference](user/DOCTOR.md).
_Avoid_: design verification, Ticket Preflight, acceptance gate

**Unproxied Sandbox**:
A **Sandbox** that explicitly opts out of Booley's egress proxy, so agents and
tools reach the network the platform provides, such as a Codespace. It is never
inferred; Doctor keeps it visible as the waivable WARN `sandbox.unproxied` ([ADR
0077](adr/0077-declare-unproxied-sandboxes-explicitly.md)).
_Avoid_: no proxy, open egress, direct egress

**EDA Provisioning**:
The policy selecting whether one EDA kind is image-provisioned or
host-provisioned for a **Sandbox**: where installation files originate, never
where EDA processes execute. An **Image-Provisioned Sandbox EDA Tool**, the
default, is installed in the Sandbox Image; a **Host-Provisioned Sandbox EDA
Tool** has files supplied by the host.
_Avoid_: execution location, EDA backend; host execution, host EDA flow, trusted tool, bare tool (for a provisioned tool)

**Job**:
A run of a Booley Flow or Specialist, tracked by a Job record and its `run_id`
from submission to a terminal result. A Developer Agent's Job follows a submit →
poll contract; one waiting for admission against the Memory Budget shows QUEUED
and can be cancelled by `run_id`.
_Avoid_: task, process, async call

**Standalone Job**:
A Job with no owning session, started by a human from a Sandbox shell or the
Booley Dashboard. It is admitted against the Memory Budget like any other Job
and never records, satisfies, or invalidates Goal evidence.
_Avoid_: human run, dashboard job, manual job

**Job admission**:
How a Job starts: it reserves its estimated peak memory and runs while the
reservations fit the Memory Budget, the Sandbox's memory limit less a reserve
for the agents ([ADR
0087](adr/0087-admit-jobs-against-a-measured-memory-budget.md)).
_Avoid_: Job class, Job type, Job kind, queue

### Configuration

The design-description primitives Booley references but does not own. **Target** is the load-bearing one: almost every other entry binds to a Target by name.

**Project**:
A codebase on which Project Initialization has run, with a **Booley Directory** for its configuration and state.
_Avoid_: repo, workspace

**Booley Directory**:
Where Booley keeps everything for a **Project**: configuration, Goalsets, Goal Records, evidence, logs, and worktrees; by default `.booley/` at the **Project Root**. **Common Booley State**, what all checkouts share, lives only in the **Main Booley Directory**; **Checkout Booley State** belongs to one checkout (its versioned configuration, build trees and reports). A linked worktree's **Worktree Booley Directory** holds only the latter.
_Avoid_: Project directory, Project dir, Project data, state dir; global state, worktree state (for Common or Checkout Booley State)

**Project Root**:
The top directory of a **Project**'s codebase checkout, and the default home of its **Booley Directory**.
_Avoid_: Project directory, Project checkout, repo root

**Main Booley Directory**:
The **Booley Directory** of the main checkout. It holds the Common Booley State and the main checkout's Checkout Booley State, and so owns the **Project**'s Goal Records, Goalsets, and lifecycle files.
_Avoid_: control Project, control Project directory, session Project

**Paired Checkout**:
A linked worktree's **Booley Directory** when the Booley Directory is a **Versioned Booley Directory**: a Git checkout on the worktree's own branch.
_Avoid_: paired Project checkout, paired Project repository, paired copy

**Snapshot**:
A linked worktree's **Booley Directory** when the Booley Directory is not versioned: a copy of its configuration taken when the worktree is created. Results, logs, and Common Booley State stay in the Main Booley Directory.
_Avoid_: clean snapshot, project copy

**Versioned Booley Directory**:
A **Booley Directory** that is its own Git repository, separate from the RTL repository.
_Avoid_: Project repository, versioned Project

**Stealth**:
The Project mode, Stealth Mode in user docs, set by `[stealth] enabled = true`,
that keeps Booley out of the RTL repository and its history. It is off unless
enabled.

**Project Inventory**:
The host-owned catalog of canonical Project paths and their **Project Grants**, including Projects whose **Booley Directory** is missing or uninitialized.
_Avoid_: Project registry, workspace list, filesystem scan

**License Profile**:
A host-owned record of one approved commercial-license topology: its fixed
server identity, literal upstream address, and license-manager ports. A
**Project Grant** authorizes it for one Project Root and EDA kind; Project
configuration never selects one. A Sandbox reaches it via a **FlexNet License
Relay**, a fixed-destination raw-TCP gateway, by its **FlexNet SERVER Host
Identifier**.
_Avoid_: project license config, license server setting; license proxy, server hostname (for the relay)

**Project Grant**:
Host-owned authorization for one exact canonical Project path and EDA kind to
use an **EDA Installation Registration**, the host's record of one approved
host-provisioned installation; a **License Profile**; or both. A moved or copied
Project is a different path and needs its own grant.
_Avoid_: workspace allowlist, project enrollment, inherited repository access; tool enrollment, mount registration (for a registration)

**Target**:
A named FuseSoC `.core` build target, the single source of truth for one
design-description: filesets, typed parameters with their values, defines, and
top module. Booley's tests (`tests.toml`) and Criteria bind to a Target, and a
Booley Flow selects it by name ([naming](user/TARGETS.md#target-authoring),
[selection](user/FLOW_REFERENCE.md#choosing-targets)).
_Avoid_: Design Configuration, build config, profile, named config

**Cocotb Target**:
A sim **Target** whose testbench is a cocotb Python module, declared in the
Target's flow options rather than authored as HDL. Its **Tests** are cocotb test
functions registered in `tests.toml`, and its verdicts come from cocotb's
`results.xml`, not from a **Simulation Sentinel**.
_Avoid_: python testbench config, cocotb core, cocotb suite

**Pre-Sim Commands**:
Project-declared shell commands that Booley executes inside the **Sandbox**
immediately before each simulator process, for non-RTL per-test build steps that
FuseSoC cannot express. They run once per Test Run, or once per batch for a
**Cocotb Target**.
_Avoid_: Pre-Run Commands, pre-test hook, prebuild adapter, test fixture script

### Flows, EDA tools, and MCP tools

**Booley Flow**:
Deterministic end-to-end orchestration: `lint`, `sim` (Simulation), `synth`
(ASIC Synthesis), or `fpga` (FPGA Implementation), invoked through MCP or the
CLI inside the **Sandbox**. Every Booley Flow builds its command for a resolved
**Target** through Booley's FuseSoC/Edalize path, executes it inside the
Sandbox, and interprets the result into evidence.
_Avoid_: B-Tool, mechanical tool, utility, command

**EDA tool**:
Concrete external program driven by a Flow, such as Verilator, Icarus, Verible,
Yosys, or Vivado. A Target's EDA-selection field normally selects the program;
the FPGA Flow always drives Vivado. The Booley Flow, not the EDA tool, owns
orchestration, evidence normalization, and artifacts.
_Avoid_: bare tool, Booley Flow, backend

**MCP tool**:
Protocol-level mechanism used to invoke a Flow or Specialist. MCP tools are implementation details rather than Booley's product taxonomy: describe the invoked capability as a **Booley Flow** or **Specialist** unless the protocol boundary itself is the subject.
_Avoid_: bare tool, Booley Flow (when referring specifically to the protocol endpoint)

**Elaboration Check**:
A fast Simulation Flow mode that compiles, elaborates, and links a simulation
Target without running its tests. It can satisfy an `elab` Goal, not a `sim`
Goal, which requires test execution.
_Avoid_: syntax check, compile-only, Elaboration Flow, simulation substitute

**Specialist**:
An optional capability the **Developer Agent** invokes for one task, with fresh
context and no history: the Reviewer and the [Coverage
Analyst](../src/booley/flows/sim/GLOSSARY.md)
([list](user/CLI-REFERENCE.md#booley-flows--specialists)). Under **Specialist
Source Isolation**, one that judges one side of the RTL/testbench divide cannot
see the other side's sources.
_Avoid_: agentic MCP tool, agent, worker; optional blindness, reviewer independence (for Specialist Source Isolation)

**Custom Flow**:
A project-authored Booley Flow that does not ship with Booley, implemented under
`.booley/mcp_tools/` and discovered and invoked like the built-in Flows. It adds
a new Flow alongside the built-ins (for example, a DRC check); it does not
replace the EDA tool an existing Flow drives.
_Avoid_: Custom Tool, plugin, user tool, project tool

**Mutation Flow**:
The Booley Flow `booley flow mutation` that measures how well a Target's tests
catch injected RTL bugs: it injects **Mutants** made by fixed Mutation
Operators, runs the tests against each, and reports how many are detected. It
runs no model, so equal sources and settings give equal Mutants ([ADR
0088](adr/0088-generate-mutants-deterministically-in-a-mutation-flow.md)).
_Avoid_: mutation agent, LLM mutation, fault injection

**Mutant**:
One seeded source change with a stable ID, made by a **Mutation Operator**, one
rule of Booley's fixed table (such as swapping a relational operator) that
Projects cannot change, and judged detected or not detected by the Target's
tests. Its ID comes from the change and its enclosing design unit, so it
survives edits elsewhere.
_Avoid_: mutation (for one change), variant, fault; mutator, mutation rule (for an operator)

### Simulation evidence

**Test**:
A named stimulus scenario that a sim **Target**'s testbench implements and that `tests.toml` registers for that Target. Booley tells the testbench which Test to run; for a **Cocotb Target**, a Test is one cocotb test function.
_Avoid_: test case, testcase, bench test, sequence

**Test Variant**:
A named, fixed set of run-time arguments and environment for one **Test**,
declared in `tests.toml` and written `test+variant`. A Test with Variants has
exactly one default Variant, which is what the bare Test name means.
_Avoid_: test config, preset, profile, flavor, plusarg override, build variant

**Test Run**:
One **Test Variant** simulated with one seed, written `test+variant@seed`. With
no seed named it uses the **Default Seed**, a Booley-wide constant a Target may
override; only a Test Run that exactly matches a Goal's resolved form counts
toward that Goal.
_Avoid_: run (bare), iteration, seed run, QA Run; random seed (for the Default Seed)

**Simulation Record**:
The durable, resumable record of the exact simulation workload one Simulation
Flow invocation runs on one **Target**, with the strict aggregate outcome of all
selected work. It may contain a [**Coverage
Record**](../src/booley/flows/sim/GLOSSARY.md); Goal-side evidence names still
say "campaign" ([ADR
0090](adr/0090-rename-simulation-and-coverage-campaigns-to-records.md)).
_Avoid_: Simulation Campaign, campaign, regression run, test batch

**Simulator Bundle**:
An authenticated simulator executable and its supporting build outputs, built for a declared build variant of one **Simulation Record**. Its scope is either shared by compatible work items in that Simulation Record or private to one simulation attempt; it is not a cross-record cache.
_Avoid_: binary cache, global build cache, simulator image

**Trace Artifact**:
Fresh waveform evidence produced by a traced simulation and proven queryable by
[B-Wave](../crates/bwave/GLOSSARY.md). It is an FST store; VCD is an input or
intermediate, not successful trace evidence.
_Avoid_: sim output, log, no-sim

**Simulation Sentinel**:
A configured output string that Booley scans to determine the verdict of an
HDL-testbench simulation. Fail sentinels take priority over pass sentinels; when
none is found after a clean run, the result is inconclusive.
_Avoid_: regex, marker, exit-code-only verdict

### Presentation

**Booley Dashboard**:
The terminal view, one per Sandbox, of agent sessions, Goals, Booley Flow Jobs,
Targets, and health signals, from which a human can also launch Booley Flows as
Standalone Jobs. Its **Session activity** is what a session's agent client last
reported, never inferred from Booley calls or Jobs.
_Avoid_: control room, console, monitor; session status, session state (for Session activity)

## Execution and evidence

**Criterion**:
A named boolean condition bound to the evidence and subject it evaluates.
_Avoid_: check, gate, acceptance test

### Metric-Driven Development and reviews

How a commit is accepted once Goal Mode is removed: [ADR
0092](adr/0092-measure-commits-against-target-metrics.md) measures commits
against metrics their Targets declare, and [ADR
0091](adr/0091-review-commits-outside-measurement.md) reviews commits outside
that measurement. `booley measure` checks `metrics.toml` and measures and stores
runs; Review Runs are not built yet.

**Metric-Driven Development**:
Accepting work by measuring a commit against the **Target Metrics** its Project declares, in place of approving a list of criteria for each task. Adoption is optional: a Project that declares no metrics is unaffected.
_Avoid_: Sign-off, sign-off flow, acceptance mode

**Target Metrics**:
The metrics a Target declares for its Booley Flows in `metrics.toml`. Each is **enforced** (it has an absolute bound that must be met) or **tracked** (reported; its value never blocks).
_Avoid_: Criteria, Goals, thresholds, KPIs

**Measured Commit**:
A commit whose tree's latest full **Measurement Run**, one stored `booley
measure` run over every Target that declares metrics, met every enforced Target
Metric with no error. Locally it is advisory; a run in CI is the trusted one.
Its **Measurement Report** gives each metric's value, bound and verdict.
_Avoid_: signed-off commit, passing commit, green commit; Sign-off run, Sign-off Report (for its run and report)

**Base**:
The commit a measured or reviewed commit is compared with: by default its merge-base with the default branch. A stored Measurement Run of the Base is optional; it adds deltas to the Measurement Report and never decides a verdict.
_Avoid_: baseline (a Booley Flow's `--baseline` compares one Target with another revision), reference commit

**Declared Review**:
A Reviewer focus that a Target declares in `reviews.toml` for one source role (RTL or testbench), optionally narrowed to matching files, with the spec it reviews against and the severity at which findings must be resolved.
_Avoid_: review kind, review type, Review Goal

**Reviewed Commit**:
A commit on which a verify **Review Run**, one stored run of its Declared Reviews (the discovery pass over changes since the **Base**, or the verify pass re-checking open findings), left no finding open at or above the Declared Reviews' threshold. It is not reproducible, never gates CI, and is independent of being a **Measured Commit**.
_Avoid_: approved commit, signed-off commit, clean review; review session, review receipt (for a Review Run)

## Retired and ambiguous terminology

You will not need these unless you are reading older work records, code, or docs; they are terms that were renamed or removed. Skim now, refer back when you hit one.

- **Harness** → **none**: Retired name for Ticket lifecycle orchestration. Name the Goal Record, evidence machinery, or Sandbox capability concerned. The `booley.harness` package still contains active CLI, Doctor, and setup code.
- **Result Cache** and **`booley ci`** → **Measurement Run**: the Result Cache was never finished, and [ADR 0092](adr/0092-measure-commits-against-target-metrics.md) measures commits instead. Its key-part helpers under `booley.result_cache` go with Goal Mode.
- **TB Coder** → **none**: the Developer Agent writes testbenches itself.

- **"tool"**: Overloaded across Booley, agent clients, MCP, and EDA. Never use the bare word in Booley prose or identifiers: say **Booley Flow** for deterministic orchestration, **EDA tool** for the external program a Flow drives, and **MCP tool** only for the protocol-level invocation mechanism.
- **"agent"**: Name the **Developer Agent** in full at its first mention in a document or section, and in every sentence that discusses it alongside a **Specialist**. After that first mention, "the agent" is an allowed short form within the same section when no Specialist is a possible referent. Never use "agent" for a Specialist.
- **"engine" / "core"**: Legacy names for retired orchestration infrastructure. No replacement umbrella term; name the capability or source module.
- **"parameter override"** / **`-d`** / **`--define`**: Retired. There is no per-call build-time injection into a **Target**; declare the value in the Target, or use a different Target.
- **"target"**: Overloaded: a FuseSoC `.core` build **Target** vs. an EDA "target device/part" (the FPGA/ASIC the design maps to). The part is one field *inside* a Target, not a synonym for it. Always mean the FuseSoC build **Target** unqualified; say "target device" or "part" for the silicon.
