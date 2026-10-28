# Booley glossary

This is the canonical vocabulary for concepts shared across Booley. Consult it
when a term is unfamiliar; it is not an onboarding sequence. The
[context map](../GLOSSARY-MAP.md) points to the separately owned vocabularies.

Booley is the **agentic RTL IDE**: the integrated working environment for human-guided and autonomous RTL development. **Goal Mode** and **Interactive Mode** share the same isolated **Sandbox**, Booley Flows, and Specialists; neither mode alone defines the product.
_Avoid_ (for the product itself): framework, system, library, platform, toolkit, package, harness

The glossary also records words to _avoid_. Booley's concepts collide with
overloaded industry words such as “tool,” “agent,” “target,” and “harness”; one
term per concept keeps prompts, Goals, documentation, and code aligned. Treat
each entry's _Avoid_ terms as rejected synonyms rather than loose alternatives.

## Using Booley

### Lifecycle

How a host and codebase become ready for Booley work. **Host Bootstrap** prepares
reusable host capabilities, **Project Initialization** creates one **Project**,
and **Project Setup** adapts that Project to its design.

**Host Bootstrap**:
Idempotent, Project-independent preparation of the host environment and the current user's Booley integrations for the installed Booley version.
_Avoid_: Project bootstrap, machine setup, installation

**Project Initialization**:
Idempotent mechanical creation and reconciliation of Booley state and Sandbox infrastructure for one codebase, making it a **Project** without interpreting its design.
_Avoid_: Project bootstrap, Project enrollment, Project Setup

**Project Setup**:
Design-aware adaptation and validation of an initialized **Project**, including its design description, verification intent, and Developer Agent guidance.
_Avoid_: Project Initialization, onboarding, porting

### Execution

How and where Booley work runs. The **Sandbox** is the execution environment; **Goal Mode** and **Interactive Mode** are the two execution modes; the remaining entries are the machinery inside.

**Developer Agent**:
The agent of an agent session (Claude Code or Codex) in a **Sandbox**. It authors code and chooses which **Booley Flows** and **Specialists** to use. It exists in **Interactive Mode** and in **Goal Mode**, whether a human is steering or the work is unattended.
_Avoid_: coding agent, host agent, main agent, outer agent, the agent session in Goal Mode

**Sandbox**:
The isolated execution environment for one opened project folder, and the place where all Booley work executes. It owns filesystem access, shell execution, git operations, EDA subprocesses, MCP servers, logs, and secrets; the host may provision immutable EDA installation files and narrowly scoped license connectivity, but never execution authority. Goal Mode runs in a linked git worktree on its own Goal Branch inside the Sandbox; the branch and its commits are durable artifacts, while the worktree is Sandbox-scoped scratch. Docker is the default implementation, not the domain concept.
_Avoid_: Session Runtime, Session Container, Docker Session, MCP sandbox, per-ticket sandbox

**Sandbox Image**:
The reusable immutable filesystem and installed-program artifact from which **Sandboxes** are created. A Project selects either a Booley-owned image, an automatically named Project-derived image, or an explicitly external image; a mutable tag is only a locator and is not the Sandbox Image's identity.
_Avoid_: Runtime Image, Session Image, sandbox tag, container, Dockerfile

**Sandbox Issuance**:
The host-owned act of sealing and vouching for one exact Sandbox specification, including its immutable image, trusted mounts, network policy, and granted provisioning inputs. It is distinct from **EDA Provisioning**, which decides where EDA installation files originate.
_Avoid_: Session Runtime Issuance, EDA runtime spec, provisioning issuance

**Interactive Mode**:
Execution mode in which a human steers the Developer Agent inside a Sandbox, using the agent CLI in a Sandbox terminal. The Developer Agent's filesystem access, shell execution, git operations, MCP servers, Booley Flows, and Specialists execute inside it; work outside Goal Mode has no persistent Goal tracking. The same session may enter Goal Mode when the human is ready to state machine-checked Goals.
_Avoid_: MCP Mode, Standalone Mode, Tab Mode, Booley Interactive

**Goal Mode**:
The execution mode the Developer Agent enters within its session in a linked worktree, on its own Goal Branch, in which its work is judged against its Goals until it finishes with all Goals met or is abandoned. It serves both human-steered and unattended work; the worktree owns its Goal Record.
_Avoid_: ticket mode, ticket, goal session, objective

**Goal Branch**:
The branch created from a clean worktree's HEAD when a session enters Goal Mode; that HEAD is the base against which Goals are judged. One Goal Mode owns exactly one Goal Branch.
_Avoid_: ticket branch, work branch

**Goal**:
A named, mandatory boolean condition held by a session in Goal Mode, met only by Booley Flow or Specialist evidence at the session's current code. The Developer Agent may ask to relax a Goal; only a human may approve it.
The internal [Criterion](#execution-and-evidence) vocabulary remains in shared evaluation code; use Goal for the public success condition.
_Avoid_: Criterion, acceptance criterion, optional criterion, check, gate

**Goal Name**:
The single human-visible name of a **Goal**: its uppercase capability and Target,
or its review kind for a review Goal. Requirements appear beside the name.
_Avoid_: Criterion key, family key, encoded key, Goal label

**Goalset**:
A named, Project-owned bundle of Goals, written as free-form prose that the Developer Agent translates into Goals when entering Goal Mode.
_Avoid_: Ticket Creation Guidance, criteria defaults, goal profile

**Session Summary**:
The mandatory prose report the Developer Agent writes when its session finishes Goal Mode: what changed and why, which Booley Flows and Specialists it used, and remaining uncertainties.
_Avoid_: Developer Report, run report, Goal Report

**Goal Record**:
The local, uncommitted state of one Goal Mode in the **Main Booley Directory**: state, worktree, Goal Branch, base commit, Goals, protected-input digests, change log, evidence, and the Session Summary. Outside Stealth its committed counterpart is the summary file on the Goal Branch when that path is Git-trackable and owned by the RTL repository; Paired Checkout summaries remain local.
_Avoid_: ticket record, board entry, goal file

**Goal Change Proposal**:
A Developer Agent's request, made through the proposal MCP tool, to change one Goal of an active Goal Mode: add it, relax it, retarget it to another Target, or waive coverage points through a Coverage Waiver Candidate. It is pending until a human approves or rejects it, either through the client's elicitation form or in chat with the Developer Agent recording the quoted words.
_Avoid_: amendment, Ticket Amendment, waiver request

**Change Log**:
The append-only record inside a Goal Record of every approved Goal Change Proposal as it is applied (a [Goal Change](../src/booley/goals/GLOSSARY.md)): the Goal before and after, the kind of change (add, relax, retarget, or waiver), the human's reason, whether the approval was elicited by the client or agent-recorded with the human's quoted words, and the acting session.
_Avoid_: audit log, history, journal

**Protected Input**:
A file that decides how evidence is produced and is therefore digested at Goal Mode entry: Project configuration and checkout-root `booley.toml`, their legacy `pipeline.toml` siblings, `FUSESOC_IGNORE`, the `hooks`, `.managed`, `generators`, and `mcp_tools` directories at their consumer roots, and files the Project declares in `[project].protected_inputs`. The Protected Input digests cover the **Main Booley Directory**, **Worktree Booley Directory**, and Project Root files that consumers can read (see [Goal baselines](internals/FLOW_IMPLEMENTATION.md#goal-baselines-and-editable-design-inputs)). Editing one warns at once and blocks Finish until reverted.
_Avoid_: frozen file, locked config, Scope

**Review Package**:
The structured result Finish presents for a Goal Mode: diff summary against the base, each Goal with its final evidence, the Change Log, open review findings, Target changes, constraint-file edits, and the Session Summary.
_Avoid_: triage package, run report, acceptance report

**Session**:
In the Session Registry, one agent client thread or process that calls Booley from a worktree, falling back to the worktree itself when the client cannot be told apart. Presentational only: it feeds the Dashboard and the shared-worktree warning and never selects a Goal Record.
_Avoid_: connection, tab, MCP session, Sandbox session

**Sandbox Attachment**:
The connection method by which a human-facing app or autonomous driver uses a Sandbox. VS Code Dev Containers ("Open Folder in Container" / "Reopen in Container") attaches the editor and its agent sessions for Interactive Mode and Goal Mode; `booley session` provides terminal attachment for automation.
_Avoid_: Runtime Attachment, remote, tunnel, app bridge

**Doctor**:
The diagnostic command for Booley's build and execution machinery in a **Project**'s environment, including integration with Project build configuration. Its intended scope treats correctly executed and interpreted design failures as compatible with healthy machinery and leaves design correctness to normal **Booley Flows**; current classification limits and deep validation policy are described in the [Doctor reference](user/DOCTOR.md).
_Avoid_: design verification, Ticket Preflight, acceptance gate

**Workflow Region**:
An advisory cluster of Developer Agent activity, useful Specialists, Booley Flows, and intended outcomes. The three Workflow Regions are `pre_sim`, `core_loop`, and `post_sim`; each internal [Criterion](#execution-and-evidence) declares its region via the `workflow_region` key in criteria.toml, which drives advisory ordering only. Workflow Regions organize capability guidance without imposing mandatory order, mandatory Flow use, or hidden completion gates in Goal Mode.
_Avoid_: stage, phase, pipeline step

**Sandbox Policy**:
The isolation rules applied to a Sandbox: mounted paths, network access, credentials, memory, process limits, and Linux capabilities. Booley Flows and Specialists share this policy within the same Sandbox. Network egress is default-deny and admitted only through purpose-specific gateways such as the model-service egress proxy and an authorized **FlexNet License Relay**, except in an **Unproxied Sandbox**, whose egress Booley does not mediate; there is no per-Flow network boundary. Host authority (Docker sockets, SSH credentials, host EDA configuration) is never present in a Sandbox; an SSH directory holding only an `authorized_keys` file grants inbound access to the Sandbox, not host authority.
_Avoid_: per-Flow network policy

**Unproxied Sandbox**:
A **Sandbox** whose spec declares `BOOLEY_EGRESS=unproxied`, either hand-written or issued by Booley under host `[sandbox] egress = "unproxied"`. Booley neither sets nor forces the egress proxy environment there, so agents and tools reach the network the platform provides, such as a Codespace. It is an explicit opt-in, never inferred from platform signals, and Doctor keeps it visible as the waivable WARN `sandbox.unproxied`. The declaration is not the isolation boundary: a Sandbox Booley issued under host `egress = "proxy"` keeps its default-deny network whatever the variable says; under host `egress = "unproxied"` Booley issues it on a routed network.
_Avoid_: no proxy, open egress, direct egress

**Host-Provisioned Sandbox EDA Tool**:
An EDA tool whose immutable installation files are supplied by the host while every process executes inside the **Sandbox** under its **Sandbox Policy**. Host provisioning conveys file availability, not host execution authority.
_Avoid_: host execution, host EDA flow, container-installed tool, trusted tool, bare tool

**Image-Provisioned Sandbox EDA Tool**:
An EDA tool whose installation is part of the selected Sandbox Image and whose processes execute inside that Sandbox. Image provisioning is the default when a Project does not request a host registration for that EDA kind.
_Avoid_: built-in tool, bare tool

**EDA Provisioning**:
The policy selecting whether one EDA kind is image-provisioned or host-provisioned for a **Sandbox**. Provisioning selects where installation files originate, never where EDA processes execute.
_Avoid_: execution location, EDA backend

**FlexNet License Relay**:
A fixed-destination raw-TCP egress gateway through which an authorized **Sandbox** reaches one registered FlexNet server and its fixed license-manager ports. The relay provides no general network route and is available Sandbox-wide rather than being a per-Flow boundary.
_Avoid_: license proxy, HTTP proxy, license sidecar (except when discussing deployment topology)

**FlexNet SERVER Host Identifier**:
The exact FlexNet server identifier advertised by the license manager and mapped to a **FlexNet License Relay**, distinct from the server's literal upstream IP address or an ordinary DNS lookup name.
_Avoid_: server hostname, DNS name, upstream address

**Job**:
A background run of a Booley Flow or Specialist, tracked by a `run_id` through the submit → poll contract from submission to a terminal result. A Job that starts immediately and finishes quickly completes inline, reading like a synchronous call; one that waits for a slot surfaces a QUEUED state first and can be withdrawn by `run_id` while it waits.
_Avoid_: task, process, async call

**Job Class**:
The admission category of a Job, determined by which scarce resource it consumes: EDA work inside the Sandbox (`heavy`) or model-API-bound Specialist work (`light`). Each class has a configurable concurrency cap; excess work queues without preempting running work, and a full queue refuses admission. Queue priority comes from the requester role, independently of class: `interactive` sorts ahead of other requester roles; unknown persisted roles retain a fallback priority.
_Avoid_: tier, weight, pool, semaphore

### Configuration

The design-description primitives Booley references but does not own. **Target** is the load-bearing one: almost every other entry binds to a Target by name.

**Project**:
A codebase initialized with `booley init`, with a **Booley Directory** for its configuration and state.
_Avoid_: repo, workspace

**Booley Directory**:
The directory that holds everything Booley keeps for a **Project**: configuration, Goalsets, Goal Records, evidence, logs, and worktrees. Its default location is `.booley/` at the **Project Root**; a Project may select another location.
_Avoid_: Project directory, Project dir, Project data, Project data directory, state dir

**Project Root**:
The top directory of a **Project**'s codebase checkout, and the default home of its **Booley Directory**.
_Avoid_: Project directory, Project checkout, repo root

**Main Booley Directory**:
The **Booley Directory** that owns a **Project**'s Goal Records, Goalsets, and lifecycle files. It is the main checkout's Booley Directory.
_Avoid_: control Project, control Project directory, session Project

**Worktree Booley Directory**:
A linked worktree's own **Booley Directory**. It is either a **Paired Checkout** or a **Snapshot**.
_Avoid_: session Project, paired copy

**Paired Checkout**:
The **Worktree Booley Directory** of a **Versioned Booley Directory**: a Git checkout on the worktree's own branch.
_Avoid_: paired Project checkout, paired Project repository

**Snapshot**:
The **Worktree Booley Directory** of a non-versioned **Booley Directory**: a plain copy taken when the worktree is created.
_Avoid_: clean snapshot, project copy

**Versioned Booley Directory**:
A **Booley Directory** that is its own Git repository, separate from the RTL repository.
_Avoid_: Project repository, versioned Project

**Project Inventory**:
The host-owned catalog of canonical Project paths and their **Project Grants**, including Projects whose **Booley Directory** is missing or uninitialized.
_Avoid_: Project registry, workspace list, filesystem scan

**EDA Installation Registration**:
A host-owned record of one approved **Host-Provisioned Sandbox EDA Tool** installation and its built-in compatibility policy. Registration identifies available immutable files but grants no Project access by itself.
_Avoid_: tool enrollment, mount registration, host tool

**License Profile**:
A host-owned record of one approved commercial-license topology, including its fixed server identity, literal upstream address, and license-manager ports. A **Project Grant** authorizes it for one Project Root and EDA kind independently of how the EDA installation is provisioned; Project configuration never selects one directly.
_Avoid_: project license config, forwarded license environment, license server setting

**Project Grant**:
Host-owned authorization for one exact canonical Project path and EDA kind to use an **EDA Installation Registration**, a **License Profile**, or both. Moving, copying, or separately opening a Project creates a different path that requires its own grant.
_Avoid_: workspace allowlist, project enrollment, inherited repository access

**Target**:
A named FuseSoC `.core` build target, the single source of truth for one design-description: filesets, typed parameters, defines, and top module. Booley does not redefine these; its verification-intent — tests (`tests.toml`) and Criteria (`criteria.toml`) — binds to a Target, while a Booley Flow invocation selects that same Target with an unambiguous selector.

- *Naming.* Booley-authored Targets are named `<axis>_<subject>`: a leading axis token naming the Booley Flow family (`sim`, `lint`, `synth`, `fpga`), then a subject that distinguishes the Target from others, coarse to fine (`sim_smoke`, `synth_timing`). The axis leads because the name is the only place `synth` and `fpga` are distinguishable at all — CAPI2 (FuseSoC's Core API v2, the `.core` file format) has no synthesis flow, so both resolve as `generic` — and because a leading axis makes the sorted `booley targets` listing group itself by Flow. Vendored upstream cores keep whatever names upstream gave them.
- *Parameter ownership.* The Target owns the parameters outright: names, types, defaults, and *values* alike. There is no per-call override surface: every define and parameter lives in the Target as a declared value, and a run that needs different values needs a different Target.
- *Identity and selection.* A Target's durable identity is its declaring core's VLNV (FuseSoC Vendor:Library:Name:Version) plus its Target name. A Booley Flow receives the shortest selector that is unambiguous in the Project: the bare Target name when unique, otherwise a sufficient VLNV suffix followed by `#name`. Identity and selector name the same Target but are not interchangeable representations; Goals and evidence compare identity while commands render selectors. Built-in Booley Flows may select an omitted Target [automatically](user/FLOW_REFERENCE.md#choosing-targets); custom Booley Flows require explicit selection by default. Doctor selection lives on the Target itself.

_Avoid_: Design Configuration, build config, profile, named config

**Tech Cell Replacement**:
A Project's documented mapping and design inputs for realizing technology-dependent RTL intent with cells from its selected physical-library family, including preserved direct instantiations and substitutions through Project hooks or adapters.

**Memory Timing Surrogate**:
A standard-cell stand-in for a memory, selected by a synth-only define, that drops storage capacity but keeps the memory's latency, reset behavior and data dependence, so the surrounding logic synthesizes as in the real design.
_Avoid_: RAM stub, fake RAM

**Cocotb Target**:
A sim **Target** whose testbench is a cocotb Python module, declared in the Target's flow options rather than authored as HDL. Its `toplevel` is whatever the Python testbench attaches to: the DUT itself for a simple design, with no HDL testbench wrapper; or a thin HDL wrapper when the DUT's ports are SystemVerilog interfaces, since cocotb's bus interfaces bind to interface *instances*, which something must instantiate. Its **Tests** are named cocotb test functions registered in `tests.toml`. Booley executes its **Test Runs** in batches, one simulator process for Test Runs of different Tests that share an environment and a seed, with per-test verdicts taken from cocotb's result file (`results.xml`) rather than from a **Simulation Sentinel** (defined below under Simulation evidence).
_Avoid_: python testbench config, cocotb core, cocotb suite

**Pre-Sim Commands**:
Project-declared shell commands that Booley executes inside the **Sandbox** immediately before each simulator process, with the **Test Runs** it prepares and the authoritative run directory in the environment. For an HDL-testbench Target the hook fires once per Test Run; for a **Cocotb Target** it fires once before each batch. This is the sanctioned seam for non-RTL per-test build steps (per-case firmware compiles, vector staging) that FuseSoC cannot express.
_Avoid_: Pre-Run Commands, pre-test hook, prebuild adapter, test fixture script

### Flows, EDA tools, and MCP tools

| Term | Meaning | Examples |
|---|---|---|
| **Booley Flow** | Deterministic end-to-end orchestration | Simulation, Lint, ASIC Synthesis, FPGA Implementation |
| **EDA tool** | Concrete external program driven by a Flow | Verilator, Icarus, Verible, Yosys, Vivado |
| **MCP tool** | Protocol-level mechanism used to invoke a Flow or Specialist | Implementation detail rather than product taxonomy |

**Booley Flow**:
Deterministic end-to-end orchestration: `lint`, `sim` (Simulation), `synth` (ASIC Synthesis), or `fpga` (FPGA Implementation). In Goal Mode its MCP-bound evidence updates Goals; an ordinary CLI call returns a diagnostic verdict without persistent Goal state. It can be invoked through MCP or the CLI inside the Sandbox. A resolved **Target** supplies the EDA-selection field used during FuseSoC resolution. Simulation and lint drive that selected tool directly; the FPGA Flow rebuilds the resolved design inputs into its fixed Vivado EDAM, so the Target's `fpga` naming axis declares drivability while its EDA-selection field remains a resolution input. Every Booley Flow builds its command through Booley's FuseSoC/Edalize path, executes inside the **Sandbox**, and interprets the result into evidence.
_Avoid_: B-Tool, mechanical tool, utility, command

**EDA tool**:
Concrete external program driven by a Flow, such as Verilator, Icarus, Verible, Yosys, or Vivado. A Target's EDA-selection field participates in FuseSoC resolution and normally selects the program; the FPGA Flow is the fixed-backend exception and always drives Vivado. The Booley Flow owns orchestration, evidence normalization, artifacts, and evidence rather than delegating those responsibilities to the EDA tool.
_Avoid_: bare tool, Booley Flow, backend

**Generated Build Artifact**:
A file a Target consumes that Git ignores in the RTL repository and that is not HDL, a `.core` declaration, or a declared program.

**MCP tool**:
Protocol-level mechanism used to invoke a Flow or Specialist. MCP tools are implementation details rather than Booley's product taxonomy: describe the invoked capability as a **Booley Flow** or **Specialist** unless the protocol boundary itself is the subject.
_Avoid_: bare tool, Booley Flow (when referring specifically to the protocol endpoint)

**Elaboration Check**:
A fast Simulation Flow mode that compiles, elaborates, and links a simulation Target without running its tests. It can satisfy an `elab` Goal through `elab_pass` evidence; it does not satisfy a `sim` Goal, which requires test execution.
_Avoid_: syntax check, compile-only, Elaboration Flow, simulation substitute

**Specialist**:
An optional LLM-powered capability the **Developer Agent** invokes with fresh context for a single delegated task. Does not carry history from previous invocations. The active Specialists are Reviewer, Mutation Tester, and [Coverage Analyst](../src/booley/flows/sim/GLOSSARY.md) (the canonical list lives in [CLI-REFERENCE.md](user/CLI-REFERENCE.md#booley-flows--specialists)); TB Coder also exists but is hidden until it matures; the Developer Agent authors testbenches itself. Specialists are capabilities the Developer Agent may use, not mandatory stages in a fixed pipeline.
_Avoid_: agentic MCP tool, agent, worker

**Specialist Source Isolation**:
When a **Specialist** reviews or mutates one side of the design, the other side's source is hidden from it. This is a non-negotiable Specialist context boundary that preserves independent readings of the functional spec: a Specialist judging one side of the RTL/testbench divide runs with the opposite side's sources hidden. Reviewers see only their own category's sources; the **Mutation Tester** designs RTL mutations without reading the testbench, so surviving mutants (injected bugs the testbench fails to catch) measure real testbench quality rather than mutations tailored to dodge it. Diagnostic and integration Specialists may read both when their task requires cross-checking RTL/TB agreement.
_Avoid_: optional blindness, reviewer independence

**Custom Flow**:
A project-authored Booley Flow that does not ship with Booley. Its MCP tool implementation lives under `.booley/mcp_tools/`; it implements the same deterministic orchestration and evidence contract as built-in Flows, is discovered and invoked through the same MCP tool infrastructure, and may produce evidence for declared Goals. It adds a new Flow alongside the built-ins (for example, a DRC check); it is not a side door for replacing the EDA tool driven by an existing Flow.
_Avoid_: Custom Tool, plugin, user tool, project tool

### Simulation evidence

**Test**:
A named stimulus scenario that a sim **Target**'s testbench implements and that `tests.toml` registers for that Target. Booley tells the testbench which Test to run; for a **Cocotb Target**, a Test is one cocotb test function.
_Avoid_: test case, testcase, bench test, sequence

**Test Variant**:
A named, fixed set of run-time arguments and environment for one **Test**, declared in `tests.toml` and written `test+variant`. A Test with Variants has exactly one default Variant, which is what the bare Test name means. A Test Variant adds no build inputs.
_Avoid_: test config, preset, profile, flavor, plusarg override, build variant

**Default Seed**:
The seed that a **Test Run** uses when no seed is named: a Booley-wide constant that a **Target** may override. Booley never relies on a simulator's own default seed; a Target that cannot apply a seed has unseeded Test Runs.
_Avoid_: simulator default seed, random seed

**Test Run**:
One **Test Variant** simulated with one seed, written `test+variant@seed`: `test@seed` for a **Test** without Variants, and without `@seed` on a Target that cannot apply seeds. It is the unit that simulation evidence records and that Goals name; a multi-seed Goal (`test+variant@xN`) names N Test Runs whose seeds are drawn at random once and frozen with the Goal. Only a Test Run that exactly matches a Goal's resolved form counts toward that Goal; every other Test Run is diagnostic.
_Avoid_: run (bare), iteration, seed run, QA Run

**Simulation Campaign**:
The durable execution record for one immutable, exact simulation workload on one **Target**. A Simulation Campaign may span multiple Simulation Flow invocations through explicit resume and records the strict aggregate outcome of all selected work. It is distinct from a [**Coverage Campaign**](../src/booley/flows/sim/GLOSSARY.md), which records native RTL coverage for one Target and one Simulation Flow invocation; a coverage-collecting Simulation Campaign may contain a separate Coverage Campaign as evidence for an attempt.
_Avoid_: Campaign, regression run, test batch, Coverage Campaign

**Simulator Bundle**:
An authenticated simulator executable and its supporting build outputs, built for a declared build variant of one **Simulation Campaign**. Its scope is either shared by compatible work items in that Simulation Campaign or private to one simulation attempt; it is not a cross-campaign cache.
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
The read-only terminal view, one per Sandbox, of agent sessions, Goals, Booley Flow Jobs, and health signals in that Sandbox.
_Avoid_: control room, console, monitor

**Session activity**:
What a session's agent client last reported through its lifecycle hooks: working, waiting for tool call, or waiting for user input. Unknown when nothing fresh was reported; never inferred from Booley calls or running Jobs. Presentational only.
_Avoid_: session status, session state

## Execution and evidence

**Criterion**:
A named boolean condition bound to the evidence and subject it evaluates.
_Avoid_: check, gate, acceptance test

**Cycle Count Criterion**:
A Criterion requiring one **Test Run** to pass on one Target and its reported cycle count to meet one declared threshold.
_Avoid_: cycle budget, synthesis criterion, benchmark score

**Result Cache**:
The per-Project store, in the **Main Booley Directory**, of graded `sim`, `synth`, and `fpga` passes and metrics from runs at a **clean commit** that stayed clean until the run ended. A stored result is served in place of a fresh run only when its Result Key matches. The key covers: the tracked bytes of the RTL repository and of the Booley Directory's repository, `booley.toml` minus its verdict-neutral sections, the resolved Target Test Plan, non-Booley ignored files, the Target and its resolved EDAM, the keyed environment, Booley's build, the Sandbox Image, any mounted EDA tool's manifest, and a Booley-generated identity of the same host. A hit is labelled as cached with its producing run. Sampled re-runs check hits, and a mismatch disables the cache for the Project. It stores results, never builds; that is what distinguishes it from a **Simulator Bundle** and from the compiler cache.
_Avoid_: build cache, global build cache, binary cache, memoization

**Escalation**:
A signal that a decision exceeds the current authority level and must move from Specialist to Developer Agent to Human.
_Avoid_: spec gap, blocker, impediment

## Retired and ambiguous terminology

You will not need these unless you are reading older work records, code, or docs; they are terms that were renamed or removed. Skim now, refer back when you hit one.

- **Ticket Mode** → **Goal Mode**: Retired ticket-driven workflow. `booley run` and `booley board` now print a migration pointer and exit 2.
- **Ticket Preflight** → **Goal entry refusals** and **Doctor** "Project checks": The old Ticket-intake gate is retired; entry enforces Goal worktree and input requirements, while Doctor diagnoses shared Project machinery. They are separate checks, not a one-for-one reproduction of Ticket Preflight.
- **Harness** → **none**: Retired name for Ticket lifecycle orchestration. Name the Goal Record, evidence machinery, or Sandbox capability concerned. The `booley.harness` package still contains active CLI, Doctor, and setup code.
- **Console** → **Booley Dashboard** for the live view; **none** for the old single-Ticket display: The Dashboard shows sessions, Goals, Jobs, and health across the Sandbox.

- **"tool"**: Overloaded across Booley, agent clients, MCP, and EDA. Never use the bare word in Booley prose or identifiers: say **Booley Flow** for deterministic orchestration, **EDA tool** for the external program a Flow drives, and **MCP tool** only for the protocol-level invocation mechanism.
- **"agent"**: Name the **Developer Agent** in full at its first mention in a document or section, and in every sentence that discusses it alongside a **Specialist**. After that first mention, "the agent" is an allowed short form within the same section when no Specialist is a possible referent. Never use "agent" for a Specialist.
- **"stage"**: Legacy framing for a mandatory pipeline. The Developer Agent chooses Booley Flows and Specialists as needed; use **Workflow Region** only for advisory capability groupings.
- **"engine" / "core"**: Legacy names for retired orchestration infrastructure. No replacement umbrella term; name the capability or source module.
- **"Design Configuration"**: Retired. The Booley-side bundle of EDA params no longer exists; design-description lives in a FuseSoC **Target**, and Booley only references it by name. Use **Target**.
- **"Session ID"** as a Sandbox identity: Never implemented. Use **Session** for the presentational agent-client identity, **Worktree Identity** for the checkout, or **Goal Record** for the work being tracked; none is a per-Sandbox Session ID.
- **"parameter override"** / **`-d`** / **`--define`**: Retired. There is no per-call build-time injection into a **Target**; declare the value in the Target, or use a different Target.
- **"plusarg override"** / **`--plusarg`**: Never offered. A run-time argument that a run needs is declared as a **Test Variant**, so every Test Run is a declared configuration.
- **"colon-free target names"**: Retired absolute. VLNV grammar (the FuseSoC Vendor:Library:Name:Version identifier) is permitted on Booley's surface: bare names when unambiguous, `vlnv#name` on collision.
- **"target"**: Overloaded: a FuseSoC `.core` build **Target** vs. an EDA "target device/part" (the FPGA/ASIC the design maps to). The part is one field *inside* a Target, not a synonym for it. Always mean the FuseSoC build **Target** unqualified; say "target device" or "part" for the silicon.
