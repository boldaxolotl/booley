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
Idempotent, location-neutral reconciliation of one codebase's Project files (the Booley Directory, Git integration, guidance links, core projections and the Dashboard task), making it a **Project** without interpreting its design. It needs no Docker, host credentials or **Sandbox Issuance**. Host `booley init` adds Sandbox Image preparation and Sandbox Issuance, which then owns the Dashboard task; in a hand-written Sandbox it runs alone; a Booley-issued Sandbox refuses it.
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
The coding-agent program an agent runs on: Claude Code (`claude`) or Codex (`codex`). A Project lists the Agent CLIs it may use and names a default, which runs the **Developer Agent** and Booley's own calls; each **Specialist** runs on the Agent CLI its model implies (`claude-*` on Claude Code, `gpt-*` on Codex). Each Agent CLI has its own sign-in and billing policy.
_Avoid_: agent app, provider (in user-facing text)

**Sandbox**:
The isolated environment, one per opened project folder, where all Booley work executes. It owns file access, shell, git, EDA subprocesses, MCP servers, logs, and secrets; the host may add read-only EDA files and scoped license connectivity, never execution authority. Its **Sandbox Policy**, the shared isolation rules, covers mounts, credentials, memory, process limits, capabilities, and egress, default-deny outside an Unproxied Sandbox. Docker by default.
_Avoid_: Session Runtime, Session Container, Docker Session, MCP sandbox; per-Flow network policy (for Sandbox Policy)

**Sandbox Image**:
The reusable immutable filesystem and installed-program artifact from which **Sandboxes** are created. A Project selects either a Booley-owned image, an automatically named Project-derived image, or an explicitly external image; a mutable tag is only a locator and is not the Sandbox Image's identity.
_Avoid_: Runtime Image, Session Image, sandbox tag, container, Dockerfile

**Sandbox Issuance**:
The host-owned act of sealing and vouching for one exact Sandbox specification, including its immutable image, trusted mounts, network policy, and granted provisioning inputs. It is distinct from **EDA Provisioning**, which decides where EDA installation files originate.
_Avoid_: Session Runtime Issuance, EDA runtime spec, provisioning issuance

**Interactive Mode**:
Execution mode in which a human steers the Developer Agent inside a Sandbox, using the Developer Agent's CLI in a Sandbox terminal. The Developer Agent's filesystem access, shell execution, git operations, MCP servers, Booley Flows, and Specialists execute inside it; work outside Goal Mode has no persistent Goal tracking. The same session may enter Goal Mode when the human is ready to state machine-checked Goals.
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
A file that decides how evidence is produced and is therefore digested at Goal Mode entry: Project configuration, `booley.toml`, hooks, generators, Project MCP tool code under `mcp_tools`, and files the Project lists in `[project].protected_inputs` ([full list](internals/FLOW_IMPLEMENTATION.md#goal-baselines-and-editable-design-inputs)). Editing one warns at once and blocks Finish until reverted.
_Avoid_: frozen file, locked config, Scope

**Review Package**:
The structured result Finish produces for a Goal Mode: diff summary against the base, each Goal with its final evidence, the Change Log, open review findings, Target changes, constraint-file edits, and the Session Summary. Finish replies with a short summary and the path of the full text.
_Avoid_: triage package, run report, acceptance report

**Session**:
In the Session Registry, one agent client thread or process that calls Booley from a worktree, falling back to the worktree itself when the client cannot be told apart. It feeds the Dashboard and the shared-worktree warning. When the client's process runs at the root of a linked worktree of the server's repository, that worktree is the default checkout for its calls that omit `work_dir`; a Codex client qualifies only when `booley chat` started it. It never selects a Goal Record.
_Avoid_: connection, tab, MCP session, Sandbox session

**Sandbox Attachment**:
The connection method by which a human-facing app or autonomous driver uses a Sandbox. VS Code Dev Containers ("Open Folder in Container" / "Reopen in Container") attaches the editor and its agent sessions for Interactive Mode and Goal Mode; `booley session` provides terminal attachment for automation.
_Avoid_: Runtime Attachment, remote, tunnel, app bridge

**Doctor**:
The diagnostic command for Booley's build and execution machinery in a **Project**'s environment, including integration with Project build configuration. Its intended scope treats correctly executed and interpreted design failures as compatible with healthy machinery and leaves design correctness to normal **Booley Flows**; current classification limits and deep validation policy are described in the [Doctor reference](user/DOCTOR.md).
_Avoid_: design verification, Ticket Preflight, acceptance gate

**Unproxied Sandbox**:
A **Sandbox** whose spec declares `BOOLEY_EGRESS=unproxied`, hand-written or issued under host `[sandbox] egress = "unproxied"`. Booley neither sets nor forces the egress proxy there, so agents and tools reach the network the platform provides, such as a Codespace. It is an explicit opt-in, never inferred, and Doctor keeps it visible as the waivable WARN `sandbox.unproxied`. Host `egress`, not the declaration, decides an issued Sandbox's network.
_Avoid_: no proxy, open egress, direct egress

**EDA Provisioning**:
The policy selecting whether one EDA kind is image-provisioned or host-provisioned for a **Sandbox**: where installation files originate, never where EDA processes execute. An **Image-Provisioned Sandbox EDA Tool**, the default, is installed in the Sandbox Image; a **Host-Provisioned Sandbox EDA Tool** has immutable files supplied by the host while every process runs in the Sandbox.
_Avoid_: execution location, EDA backend; host execution, host EDA flow, trusted tool, bare tool (for a provisioned tool)

**Job**:
A run of a Booley Flow or Specialist, tracked by a Job record and its `run_id` from submission to a terminal result. A Developer Agent's Job goes through the submit → poll contract: one that starts immediately and finishes quickly completes inline, reading like a synchronous call; one that waits for admission (its estimated memory to fit the Memory Budget, or a light slot) surfaces a QUEUED state first and can be cancelled by `run_id` while it waits or runs. A human's Job runs in the foreground of a Sandbox shell or detached from the Booley Dashboard.
_Avoid_: task, process, async call

**Standalone Job**:
A Job with no owning session, started by a human from a Sandbox shell or the Booley Dashboard. It is admitted against the Memory Budget like any other Job, writes `job.log`, and never records, satisfies, or invalidates Goal evidence.
_Avoid_: human run, dashboard job, manual job

**Job Class**:
How a Job is admitted. The **memory** class holds EDA Jobs (simulation, synthesis, FPGA implementation, lint, Custom Flows): each reserves its estimated peak memory and runs while the reservations fit the Memory Budget, which is `[jobs].memory_budget`, else the container's memory limit, else the installed memory, minus a reserve for the agents. The **light** class holds Specialists and the Mutation Flow: a count (`max_light`) caps them, because the AI provider, not memory, limits them.
_Avoid_: Job type, Job kind, queue

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
The Project mode, Stealth Mode in user docs, set by `[stealth] enabled = true`, that keeps Booley out of the RTL repository and its history: the Booley Directory and any cores Booley authors stay untracked, with ignored copies where FuseSoC finds the cores, and Git hooks reject commit messages that carry protected words. It is off unless enabled.

**Project Inventory**:
The host-owned catalog of canonical Project paths and their **Project Grants**, including Projects whose **Booley Directory** is missing or uninitialized.
_Avoid_: Project registry, workspace list, filesystem scan

**License Profile**:
A host-owned record of one approved commercial-license topology: its fixed server identity, literal upstream address, and license-manager ports. A **Project Grant** authorizes it for one Project Root and EDA kind; Project configuration never selects one. A Sandbox reaches the server via a **FlexNet License Relay**, a fixed-destination raw-TCP gateway, by its **FlexNet SERVER Host Identifier**, the name the license manager advertises, not an IP address or DNS lookup name.
_Avoid_: project license config, license server setting; license proxy, server hostname (for the relay)

**Project Grant**:
Host-owned authorization for one exact canonical Project path and EDA kind to use an **EDA Installation Registration**, the host's record of one approved host-provisioned installation and its built-in compatibility policy, which grants no access by itself; a **License Profile**; or both. Moving, copying, or separately opening a Project creates a different path that needs its own grant.
_Avoid_: workspace allowlist, project enrollment, inherited repository access; tool enrollment, mount registration (for a registration)

**Target**:
A named FuseSoC `.core` build target, the single source of truth for one design-description: filesets, typed parameters with their values, defines, and top module. Booley does not redefine these; its tests (`tests.toml`) and Criteria bind to a Target, and a Booley Flow selects it by name, or by a VLNV suffix and `#name` when the name is ambiguous. Booley-authored Targets are named `<axis>_<subject>` ([naming](user/TARGETS.md#target-authoring), [selection](user/FLOW_REFERENCE.md#choosing-targets)).
_Avoid_: Design Configuration, build config, profile, named config

**Cocotb Target**:
A sim **Target** whose testbench is a cocotb Python module, declared in the Target's flow options rather than authored as HDL. Its `toplevel` is the DUT, or a thin HDL wrapper when the DUT's ports are SystemVerilog interfaces. Its **Tests** are cocotb test functions registered in `tests.toml`. Booley runs its **Test Runs** in batches and takes per-test verdicts from cocotb's `results.xml`, not from a **Simulation Sentinel**.
_Avoid_: python testbench config, cocotb core, cocotb suite

**Pre-Sim Commands**:
Project-declared shell commands that Booley executes inside the **Sandbox** immediately before each simulator process, with the **Test Runs** it prepares and the authoritative run directory in the environment. For an HDL-testbench Target the hook fires once per Test Run; for a **Cocotb Target** it fires once before each batch. This is the sanctioned seam for non-RTL per-test build steps (per-case firmware compiles, vector staging) that FuseSoC cannot express.
_Avoid_: Pre-Run Commands, pre-test hook, prebuild adapter, test fixture script

### Flows, EDA tools, and MCP tools

**Booley Flow**:
Deterministic end-to-end orchestration: `lint`, `sim` (Simulation), `synth` (ASIC Synthesis), or `fpga` (FPGA Implementation), invoked through MCP or the CLI inside the **Sandbox**. Every Booley Flow builds its command for a resolved **Target** through Booley's FuseSoC/Edalize path, executes it inside the Sandbox, and interprets the result into evidence. In Goal Mode its MCP-bound evidence updates Goals; a CLI call returns a diagnostic verdict.
_Avoid_: B-Tool, mechanical tool, utility, command

**EDA tool**:
Concrete external program driven by a Flow, such as Verilator, Icarus, Verible, Yosys, or Vivado. A Target's EDA-selection field participates in FuseSoC resolution and normally selects the program; the FPGA Flow is the fixed-backend exception and always drives Vivado. The Booley Flow owns orchestration, evidence normalization, artifacts, and evidence rather than delegating those responsibilities to the EDA tool.
_Avoid_: bare tool, Booley Flow, backend

**MCP tool**:
Protocol-level mechanism used to invoke a Flow or Specialist. MCP tools are implementation details rather than Booley's product taxonomy: describe the invoked capability as a **Booley Flow** or **Specialist** unless the protocol boundary itself is the subject.
_Avoid_: bare tool, Booley Flow (when referring specifically to the protocol endpoint)

**Elaboration Check**:
A fast Simulation Flow mode that compiles, elaborates, and links a simulation Target without running its tests. It can satisfy an `elab` Goal through `elab_pass` evidence; it does not satisfy a `sim` Goal, which requires test execution.
_Avoid_: syntax check, compile-only, Elaboration Flow, simulation substitute

**Specialist**:
An optional capability the **Developer Agent** invokes for one task, with fresh context and no history: the Reviewer and the [Coverage Analyst](../src/booley/flows/sim/GLOSSARY.md) ([list](user/CLI-REFERENCE.md#booley-flows--specialists)). Under **Specialist Source Isolation**, one that judges one side of the RTL/testbench divide cannot see the other side's sources, so it reads the spec independently of the code it judges.
_Avoid_: agentic MCP tool, agent, worker; optional blindness, reviewer independence (for Specialist Source Isolation)

**Custom Flow**:
A project-authored Booley Flow that does not ship with Booley. Its MCP tool implementation lives under `.booley/mcp_tools/`; it implements the same deterministic orchestration and evidence contract as built-in Flows, is discovered and invoked through the same MCP tool infrastructure, and may produce evidence for declared Goals. It adds a new Flow alongside the built-ins (for example, a DRC check); it is not a side door for replacing the EDA tool driven by an existing Flow.
_Avoid_: Custom Tool, plugin, user tool, project tool

**Mutation Flow**:
The Booley Flow `booley flow mutation` that measures how well a Target's tests catch injected RTL bugs. It injects **Mutants** made by the fixed Mutation Operators into the Target's own RTL, or into a caller-chosen scope, runs the Target's tests against each, and reports how many are detected. It runs no model, so the same sources and settings always produce the same Mutants and comparable scores ([ADR 0088](adr/0088-generate-mutants-deterministically-in-a-mutation-flow.md)). It replaced the Mutation Tester Specialist.
_Avoid_: mutation agent, LLM mutation, fault injection

**Mutant**:
One seeded source change with a stable ID, made by a **Mutation Operator**, one rule of Booley's fixed table (such as swapping a relational operator) that Projects cannot change, and judged detected or not detected by the Target's tests. Its ID comes from the change and its place in the enclosing design unit, so it survives edits to other files and other design units. A Mutant that changes no observable behavior (an equivalent Mutant) can never be detected.
_Avoid_: mutation (for one change), variant, fault; mutator, mutation rule (for an operator)

### Simulation evidence

**Test**:
A named stimulus scenario that a sim **Target**'s testbench implements and that `tests.toml` registers for that Target. Booley tells the testbench which Test to run; for a **Cocotb Target**, a Test is one cocotb test function.
_Avoid_: test case, testcase, bench test, sequence

**Test Variant**:
A named, fixed set of run-time arguments and environment for one **Test**, declared in `tests.toml` and written `test+variant`. A Test with Variants has exactly one default Variant, which is what the bare Test name means. A Test Variant adds no build inputs.
_Avoid_: test config, preset, profile, flavor, plusarg override, build variant

**Test Run**:
One **Test Variant** simulated with one seed, written `test+variant@seed`: `test@seed` for a **Test** without Variants, and without `@seed` on a Target that cannot apply seeds. With no seed named it uses the **Default Seed**, a Booley-wide constant a Target may override, never the simulator's own. It is the unit that simulation evidence records and Goals name; only a Test Run that exactly matches a Goal's resolved form counts toward that Goal.
_Avoid_: run (bare), iteration, seed run, QA Run; random seed (for the Default Seed)

**Simulation Record**:
The durable, resumable record of the exact simulation workload one Simulation Flow invocation runs on one **Target**, with the strict aggregate outcome of all selected work; an explicit resume continues it later. It may contain a [**Coverage Record**](../src/booley/flows/sim/GLOSSARY.md). Code, messages, formats and docs say "campaign" until [#2711](https://github.com/boldaxolotl/booley/issues/2711); Goal-side names keep it ([ADR 0090](adr/0090-rename-simulation-and-coverage-campaigns-to-records.md)).
_Avoid_: Simulation Campaign, campaign, regression run, test batch

**Simulator Bundle**:
An authenticated simulator executable and its supporting build outputs, built for a declared build variant of one **Simulation Record**. Its scope is either shared by compatible work items in that Simulation Record or private to one simulation attempt; it is not a cross-record cache.
_Avoid_: binary cache, global build cache, simulator image

**Trace Artifact**:
Fresh waveform evidence produced by a traced simulation and proven queryable by
[B-Wave](../crates/bwave/GLOSSARY.md). A Trace Artifact is an FST store; VCD is
an input or intermediate, not successful trace evidence. B-Wave proves store
queryability; Simulation alone proves that the store is fresh evidence from the
current Simulation Attempt and publishes it as a Trace Artifact.
_Avoid_: sim output, log, no-sim

**Simulation Sentinel**:
A configured output string that Booley scans to determine a simulation verdict. Fail sentinels take priority over pass sentinels; when no sentinel is found after a clean run, the result is inconclusive. Applies to HDL-testbench Targets only: a **Cocotb Target**'s verdict comes from cocotb's result file, with assertion-output scanning retained; a missing or truncated result file is inconclusive, never a pass.
_Avoid_: regex, marker, exit-code-only verdict

### Presentation

**Booley Dashboard**:
The terminal view, one per Sandbox, of agent sessions, Goals, Booley Flow Jobs, Targets, and health signals in that Sandbox, from which a human can also launch Booley Flows as Standalone Jobs. Its **Session activity** is what a session's agent client last reported through lifecycle hooks (working, waiting for tool call, or waiting for user input), or unknown when nothing fresh was reported; it is never inferred from Booley calls or Jobs.
_Avoid_: control room, console, monitor; session status, session state (for Session activity)

## Execution and evidence

**Criterion**:
A named boolean condition bound to the evidence and subject it evaluates.
_Avoid_: check, gate, acceptance test

### Metric-Driven Development and reviews

How a commit is accepted once Goal Mode is removed: [ADR 0092](adr/0092-measure-commits-against-target-metrics.md) measures commits against metrics their Targets declare, and [ADR 0091](adr/0091-review-commits-outside-measurement.md) reviews commits outside that measurement. Booley loads and checks `metrics.toml` (`booley measure --validate`); measuring a commit, Measurement Runs, and Review Runs are not built yet.

**Metric-Driven Development**:
Accepting work by measuring a commit against the **Target Metrics** its Project declares, in place of approving a list of criteria for each task. Adoption is optional: a Project that declares no metrics is unaffected.
_Avoid_: Sign-off, sign-off flow, acceptance mode

**Target Metrics**:
The metrics a Target declares for its Booley Flows in `metrics.toml`. Each is **enforced** (it has an absolute bound that must be met) or **tracked** (reported; its value never blocks).
_Avoid_: Criteria, Goals, thresholds, KPIs

**Measured Commit**:
A commit whose tree's latest full **Measurement Run**, one stored `booley measure` run over every Target that declares metrics, kept per Git tree so an identical rewrite reuses it, met every enforced Target Metric with no error. Locally it is advisory; a run in CI is the trusted one. Its **Measurement Report** gives each metric's value, bound and verdict, and what changed since the **Base**, rules included.
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

- **heavy Job**, **Job slot (for EDA Jobs)**, `max_heavy`, `heavy_memory` → **Job Class** (memory) and the Memory Budget: Retired. EDA Jobs are admitted by their memory reservation, and `booley.toml` refuses the two keys.
- **Ticket Mode** → **Goal Mode**: Retired ticket-driven workflow. `booley run` and `booley board` now print a migration pointer and exit 2.
- **Harness** → **none**: Retired name for Ticket lifecycle orchestration. Name the Goal Record, evidence machinery, or Sandbox capability concerned. The `booley.harness` package still contains active CLI, Doctor, and setup code.
- **Console** → **Booley Dashboard** for the live view; **none** for the old single-Ticket display: The Dashboard shows sessions, Goals, Jobs, and health across the Sandbox.
- **Simulation Campaign** → **Simulation Record** ([ADR 0090](adr/0090-rename-simulation-and-coverage-campaigns-to-records.md)); that entry says where "campaign" remains.
- **Coverage Campaign** → [**Coverage Record**](../src/booley/flows/sim/GLOSSARY.md), on the same terms.
- **Result Cache** and **`booley ci`** → **Measurement Run**: the Result Cache was never finished, and [ADR 0092](adr/0092-measure-commits-against-target-metrics.md) measures commits instead. Its key-part helpers under `booley.result_cache` go with Goal Mode.
- **Escalation** → **none**: the Developer Agent asks the human in chat ([ADR 0067](adr/0067-replace-ticket-mode-with-goal-mode.md)).
- **TB Coder** → **none**: the Developer Agent writes testbenches itself.

- **"tool"**: Overloaded across Booley, agent clients, MCP, and EDA. Never use the bare word in Booley prose or identifiers: say **Booley Flow** for deterministic orchestration, **EDA tool** for the external program a Flow drives, and **MCP tool** only for the protocol-level invocation mechanism.
- **"agent"**: Name the **Developer Agent** in full at its first mention in a document or section, and in every sentence that discusses it alongside a **Specialist**. After that first mention, "the agent" is an allowed short form within the same section when no Specialist is a possible referent. Never use "agent" for a Specialist.
- **"engine" / "core"**: Legacy names for retired orchestration infrastructure. No replacement umbrella term; name the capability or source module.
- **"parameter override"** / **`-d`** / **`--define`**: Retired. There is no per-call build-time injection into a **Target**; declare the value in the Target, or use a different Target.
- **"target"**: Overloaded: a FuseSoC `.core` build **Target** vs. an EDA "target device/part" (the FPGA/ASIC the design maps to). The part is one field *inside* a Target, not a synonym for it. Always mean the FuseSoC build **Target** unqualified; say "target device" or "part" for the silicon.
