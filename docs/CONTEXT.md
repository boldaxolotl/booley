# Booley

## Read this first

New to Booley? Read [README.md](../README.md) first for the high-level picture — what Booley is, the two ways to drive it, and the Flow/EDA stack — and this document then pins the exact vocabulary. It assumes you can read RTL and know a hardware flow (simulate, lint, synthesis, waveforms). One piece of outside background it leans on throughout: Booley builds every design through FuseSoC/Edalize, and a `.core` file is FuseSoC's build description — see [WHY.md](WHY.md#why-fusesoc) for why. The entries below are grouped topically and cross-reference each other, so a **bolded term** you meet before its own definition has an entry elsewhere in the doc; follow the pointer or read on.

Booley is an **agentic RTL development framework**. It orchestrates LLM-driven hardware design work inside isolated **Session Runtimes**, through either **Ticket Mode** (a ticket-based workflow with automated acceptance criteria and structured escalation) or **Interactive Mode**, a human-driven session over the same Booley Flows and Specialists. Neither mode is the definition; the Session Runtime is the invariant, and all Booley work executes there. Positioning is fixed: Booley is a *framework*, not a system, a library, an IDE, or a platform. The one sanctioned reframe is that **Booley turns VS Code into an agentic RTL development IDE** (VS Code is the IDE; Booley is the framework behind it). _Avoid_ (for the product itself): system, library, IDE, platform, toolkit, package.

This document is Booley's controlled vocabulary: the canonical glossary of its domain terms and, for each, the words to _avoid_. It exists because Booley's concepts collide with overloaded industry words ("tool", "agent", "target", "stage", "harness"); pinning exactly one term to each concept keeps everyone reasoning about the same thing. The audience is both humans onboarding to the project and the LLM agents that must use these terms precisely in prompts, tickets, and code. An agent that says "task" for a **Ticket** or "Coder" for RTL work has already drifted from the model. Read each entry as a definition followed by an _Avoid_ line listing rejected synonyms; treat the _Avoid_ terms as forbidden, not as loose alternatives. Retired and overloaded terms are collected under **Flagged ambiguities** at the end.

## Language

### Execution

How and where Booley work runs. The **Session Runtime** is the execution environment; **Ticket Mode** and **Interactive Mode** are the two ways to drive it; the remaining entries are the machinery inside.

**Session Runtime**:
The isolated execution environment for one opened project folder, and the place where all Booley work executes. It owns filesystem access, shell execution, git operations, EDA subprocesses, MCP servers, logs, and secrets; the host may provision immutable EDA installation files and narrowly scoped license connectivity, but never execution authority. Tickets receive their own git worktrees and branches inside the runtime; the branch and its commits are the durable artifact of a run, while the worktree itself is runtime-scoped scratch. Docker is the default implementation, not the domain concept.
_Avoid_: Session Container, Docker Session, MCP sandbox, per-ticket sandbox

**Ticket Mode**:
The ticket-driven execution mode: a `booley run` invocation, issued from inside a Session Runtime, launches a Developer Agent per selected Ticket and drives each Ticket through its lifecycle to completion or escalation. Multiple Tickets may execute concurrently within one Session Runtime, alongside an Interactive Mode session; each Ticket works in its own git worktree and branch. Ticket Mode no longer creates a Session Runtime of its own.
_Avoid_: batch mode, automated mode, host mode

**Interactive Mode**:
Execution mode in which a human uses the Claude Code or Codex VS Code extension in a VS Code window attached to a Session Runtime; the standalone Claude Code and Codex apps are not supported clients. The extension's filesystem access, shell execution, git operations, MCP servers, Booley Flows, and Specialists execute inside that runtime; there is no Ticket, Scope, Developer Agent, Harness-managed state file, or Criteria tracking.
_Avoid_: MCP Mode, Standalone Mode, Tab Mode, Booley Interactive

**Runtime Attachment**:
The connection method by which a human-facing app or autonomous driver uses a Session Runtime. VS Code Dev Containers ("Open Folder in Container" / "Reopen in Container") is the first Interactive Mode attachment; direct subprocess execution is the Ticket Mode attachment.
_Avoid_: remote, tunnel, app bridge

**Runner**:
The CLI entry point (`booley run`) that drives Ticket execution inside the Session Runtime it is invoked from, launching a Developer Agent within the Harness for each selected Ticket. It works only inside a Session Runtime. Specific to Ticket Mode.
_Avoid_: launcher, executor

**Preflight**:
The fast-fail validation Booley runs before Ticket intake. It checks the execution environment, Ticket Board and Git state, Custom Flow metadata, Criteria structure, and configured agent backend. Blocking failures stop the run before Ticket work begins; non-blocking findings are warnings. `booley doctor` provides related diagnostics without starting a Ticket run, but it does not reproduce every preflight result.
_Avoid_: Flow validation, doctor, startup test

**Harness**:
The Ticket Mode runtime infrastructure that the Developer Agent operates within, managing ticket lifecycle, Criteria tracking, logging, and cleanup. Interactive Mode may reuse lower-level Session Runtime infrastructure, but does not run inside the Harness.
_Avoid_: engine, core, framework, harness

**Developer Agent**:
The LLM agent that drives Booley Flow and Specialist selection during ticket execution, making decisions about what to invoke next based on criteria state. The Developer Agent also authors RTL and testbench code itself; there is no separate coder Specialist (the TB Coder Specialist is retained but hidden until it matures; see [ROADMAP.md](ROADMAP.md)); its edits are allowed when Scope permits, invalidate dependent Criteria, and require passing Verification Checks like any other edit.
_Avoid_: bare "Developer", loop, controller, scheduler, harness

**Execution Rationale**:
A concise final-summary explanation of the Booley Flows and Specialists the Developer Agent used and the code edits it made, and why. It accounts for actions taken rather than requiring justification for every unused capability.
_Avoid_: skipped-Flow audit, mandatory route log

**Workflow Region**:
An advisory cluster of Developer Agent activity, useful Specialists, Booley Flows, and intended outcomes. The three Workflow Regions are `pre_sim`, `core_loop`, and `post_sim`; each Criterion declares its region via the `workflow_region` key in criteria.toml, which drives advisory ordering only. Workflow Regions guide ticket execution without imposing mandatory order, mandatory Flow use, or hidden completion gates.
_Avoid_: stage, phase, pipeline step

**Sandbox**:
The isolation policy applied to a Session Runtime: mounted paths, network access, credentials, memory, process limits, and Linux capabilities. It is a property of the runtime, not of individual Booley Flows or Specialists: both execute inside the same runtime and share its policy. Network egress is default-deny and admitted only through purpose-specific runtime gateways such as the model-service egress proxy and an authorized **FlexNet License Relay**; there is no per-Flow network boundary.
_Avoid_: container, jail, runtime; per-Flow network policy

**Host-Provisioned Sandbox EDA Tool**:
An EDA tool whose immutable installation files are supplied by the host while every process executes inside the **Session Runtime** under its **Sandbox**. Host provisioning conveys file availability, not host execution authority.
_Avoid_: host execution, host EDA flow, container-installed tool, trusted tool, bare tool

**Image-Provisioned Sandbox EDA Tool**:
An EDA tool whose installation is part of the selected Session Runtime image and whose processes execute inside that runtime. Image provisioning is the default when a Project does not request a host registration for that EDA kind.
_Avoid_: built-in tool, bare tool

**EDA Provisioning**:
The policy selecting whether one EDA kind is image-provisioned or host-provisioned for a **Session Runtime**. Provisioning selects where installation files originate, never where EDA processes execute.
_Avoid_: execution location, EDA backend

**FlexNet License Relay**:
A fixed-destination raw-TCP runtime egress gateway through which an authorized **Session Runtime** reaches one registered FlexNet server and its fixed license-manager ports. The relay provides no general network route and is available runtime-wide rather than being a per-Flow boundary.
_Avoid_: license proxy, HTTP proxy, license sidecar (except when discussing deployment topology)

**FlexNet SERVER Host Identifier**:
The exact FlexNet server identifier advertised by the license manager and mapped to a **FlexNet License Relay**, distinct from the server's literal upstream IP address or an ordinary DNS lookup name.
_Avoid_: server hostname, DNS name, upstream address

**Job**:
A background run of a Booley Flow or Specialist, tracked by a `run_id` through the submit → poll contract from submission to a terminal result. A Job that starts immediately and finishes quickly completes inline, reading like a synchronous call; one that waits for a slot surfaces a QUEUED state first and can be withdrawn by `run_id` while it waits.
_Avoid_: task, process, async call

**Job Class**:
The admission category of a Job or Developer Agent, determined by which scarce resource it consumes: in-runtime EDA work (`heavy`), model-API-bound Specialist work (`light`), or a Developer Agent itself (`ticket`). Each class carries a configurable concurrency cap; work beyond the cap queues in priority order (Interactive Mode ahead of Ticket Mode) rather than being refused, and running work is never preempted. The one refusal is a full queue: past the configured `queue_max`, admission raises rather than waits.
_Avoid_: tier, weight, pool, semaphore

### Configuration

The design-description primitives Booley references but does not own. **Target** is the load-bearing one: almost every other entry binds to a Target by name.

**Project**:
A codebase initialized with `booley init`, containing a `.booley_project/` directory with tickets, configuration, and logs. Booley discovers the active project by walking up the directory tree.
_Avoid_: repo, workspace

**EDA Installation Registration**:
A host-owned record of one approved **Host-Provisioned Sandbox EDA Tool** installation and its built-in compatibility policy. Registration identifies available immutable files but grants no Project access by itself.
_Avoid_: tool enrollment, mount registration, host tool

**License Profile**:
A host-owned record of one approved commercial-license topology, including its fixed server identity, literal upstream address, and license-manager ports. A **Project Grant** authorizes it for one Project root and EDA kind independently of how the EDA installation is provisioned; Project configuration never selects one directly.
_Avoid_: project license config, forwarded license environment, license server setting

**Project Grant**:
Host-owned authorization for one exact canonical **Project** root and EDA kind to use an **EDA Installation Registration**, a **License Profile**, or both. Moving, copying, or separately opening a Project creates a different root that requires its own grant.
_Avoid_: workspace allowlist, project enrollment, inherited repository access

**Target**:
A named FuseSoC `.core` build target, the single source of truth for one design-description: filesets, typed parameters, defines, and top module. Booley does not redefine these; its verification-intent — tests (`tests.toml`) and Criteria (`criteria.toml`) — references a Target by name (e.g., Criterion `sim_pass_config1` binds the Target `config1`). The name the agent passes to a Booley Flow, the FuseSoC target name, and the `<target>` suffix in `sim_pass_<target>` are all the same name.

- *Naming.* Booley-authored Targets are named `<axis>_<subject>`: a leading axis token naming the Booley Flow family (`sim`, `lint`, `synth`, `fpga`), then a subject that distinguishes the Target from others, coarse to fine (`sim_smoke`, `synth_timing`). The axis leads because the name is the only place `synth` and `fpga` are distinguishable at all — CAPI2 (FuseSoC's Core API v2, the `.core` file format) has no synthesis flow, so both resolve as `generic` — and because a leading axis makes the sorted `booley targets` listing group itself by Flow. Vendored upstream cores keep whatever names upstream gave them.
- *Parameter ownership.* The Target owns the parameters outright: names, types, defaults, and *values* alike. There is no per-call override surface: every define and parameter lives in the Target as a declared value, and a run that needs different values needs a different Target.
- *Qualification (`vlnv#name`).* A Booley config is modeled as a *target within one project core*, not its own core, so a bare name suffices while it stays unambiguous; when the same target name appears in more than one core, it is qualified as `vlnv#name` (VLNV = FuseSoC's Vendor:Library:Name:Version core identifier). Every Booley Flow call names its Target explicitly; Doctor selection lives on the Target itself.

_Avoid_: Design Configuration, build config, profile, named config

**Cocotb Target**:
A sim **Target** whose testbench is a cocotb Python module, declared in the Target's flow options rather than authored as HDL. Its `toplevel` is whatever the Python testbench attaches to: the DUT itself for a simple design, with no HDL testbench wrapper; or a thin HDL wrapper when the DUT's ports are SystemVerilog interfaces, since cocotb's bus interfaces bind to interface *instances*, which something must instantiate. Its tests are named cocotb test functions registered in `tests.toml`, executed batched in a single simulation, with per-test verdicts taken from cocotb's result file (`results.xml`) rather than from a **Simulation Sentinel** (defined below under Waveform analysis).
_Avoid_: python testbench config, cocotb core, cocotb suite

**Pre-Run Commands**:
Project-declared shell commands that Booley executes inside the **Session Runtime** immediately before each simulation run, with the run's test selection and authoritative run directory in the environment. For an HDL-testbench Target the hook fires once per test; for a **Cocotb Target** it fires once before the batched run. This is the sanctioned seam for non-RTL per-test build steps (per-case firmware compiles, vector staging) that FuseSoC cannot express.
_Avoid_: pre-test hook, prebuild adapter, test fixture script

### Flows, EDA tools, and MCP tools

| Term | Meaning | Examples |
|---|---|---|
| **Booley Flow** | Deterministic end-to-end orchestration | Simulation, Elaboration, Lint, ASIC Synthesis, FPGA Implementation |
| **EDA tool** | Concrete external program driven by a Flow | Verilator, Icarus, Verible, Yosys, Vivado |
| **MCP tool** | Protocol-level mechanism used to invoke a Flow or Specialist | Implementation detail rather than product taxonomy |

**Booley Flow**:
Deterministic end-to-end orchestration: `lint`, `sim` (Simulation), `elab` (Elaboration), `synth` (ASIC Synthesis), or `fpga` (FPGA Implementation). In Ticket Mode it is invoked by the Developer Agent and updates Criteria; in Interactive Mode it is invoked by the outer runtime through an MCP tool with no Criteria side effects. The EDA tool a Booley Flow drives is chosen by the resolved **Target**'s EDA-selection field. Every Booley Flow builds its command through Booley's FuseSoC/Edalize path, executes inside the **Session Runtime**, and interprets the result into evidence.
_Avoid_: B-Tool, mechanical tool, utility, command

**EDA tool**:
Concrete external program driven by a Flow, such as Verilator, Icarus, Verible, Yosys, or Vivado. A Target selects the EDA tool; the Booley Flow owns orchestration, evidence normalization, artifacts, and Criteria rather than delegating those responsibilities to the EDA tool.
_Avoid_: bare tool, Booley Flow, backend

**MCP tool**:
Protocol-level mechanism used to invoke a Flow or Specialist. MCP tools are implementation details rather than Booley's product taxonomy: describe the invoked capability as a **Booley Flow** or **Specialist** unless the protocol boundary itself is the subject.
_Avoid_: bare tool, Booley Flow (when referring specifically to the protocol endpoint)

**Elaboration Check**:
A fast Booley Flow run that verifies RTL/testbench structural readiness without running full simulation. It is useful as Developer Agent diagnostic feedback; `elab_*` Criteria are supported for unusual tickets, but normal RTL/testbench completion is expressed with Simulation Criteria because simulation already includes elaboration.
_Avoid_: simulation substitute, default criterion

**Verification Check**:
A passing criterion-family-specific Booley Flow run required after an RTL or testbench edit. Simulation Criteria are checked by simulation, synthesis-related Criteria by synthesis, and lint Criteria by lint; RTL work requires a testbench for simulation, whether pre-existing or created during ticket execution.
_Avoid_: review gate, planner approval

**Specialist**:
An optional LLM-powered sub-agent invoked with fresh context for a single delegated task. Does not carry history from previous invocations. The active Specialists are Reviewer and Mutation Tester (the canonical list lives in [USAGE.md](USAGE.md#booley-flows--specialists)); Coverage Analyst and TB Coder also exist but are hidden until they mature; the Developer Agent authors testbenches itself. Specialists are capabilities the Developer Agent may use, not mandatory stages in a fixed pipeline.
_Avoid_: agentic MCP tool, agent, worker

**Specialist Source Isolation**:
When a **Specialist** reviews or mutates one side of the design, the other side's source is hidden from it. This is a non-negotiable Specialist context boundary that preserves independent readings of the functional spec: a Specialist judging one side of the RTL/testbench divide runs with the opposite side's sources hidden. Reviewers see only their own category's sources; the **Mutation Tester** designs RTL mutations without reading the testbench, so surviving mutants (injected bugs the testbench fails to catch) measure real testbench quality rather than mutations tailored to dodge it. Diagnostic and integration Specialists may read both when their task requires cross-checking RTL/TB agreement.
_Avoid_: optional blindness, reviewer independence

**Custom Flow**:
A project-authored Booley Flow that does not ship with Booley. Its MCP tool implementation lives under `.booley_project/mcp_tools/`; it implements the same deterministic orchestration and evidence contract as built-in Flows, is discovered and invoked through the same MCP tool infrastructure, and may update project Criteria. It adds a new Flow alongside the built-ins (for example, a DRC check); it is not a side door for replacing the EDA tool driven by an existing Flow.
_Avoid_: Custom Tool, plugin, user tool, project tool

### Work management

**Ticket**:
A self-contained unit of hardware development work that carries its own acceptance criteria and lifecycle state, expressed as a Markdown file with YAML frontmatter.
_Avoid_: task, issue, story

**Criterion**:
A named boolean condition that must be satisfied for ticket completion, bound to a **Target** by name, automatically invalidated when its dependency category (RTL, TB) changes. Tracks whether it was ever met across resets; any Flow requirement not enforced by the Harness itself must be expressed as an explicit Criterion.
_Avoid_: check, gate, acceptance test

**Simulation Criterion**:
A Criterion satisfied by a passing simulation Booley Flow run. Any Ticket that authorizes RTL or testbench edits must include at least one Simulation Criterion; otherwise the Ticket shape is invalid before development. The required testbench may already exist or be created during ticket execution when Scope permits it.
_Avoid_: optional sim, smoke test

**Cycle Count**:
A non-negative integer emitted by one named test for one execution of its declared workload on a Target. It is a performance measurement whose desired direction is supplied by a Criterion; lower is not inherently better.
_Avoid_: cycle time, runtime, performance score

**Cycle Count Criterion**:
A specialized Simulation Criterion for one Target and named test, satisfied only when the test passes and its Cycle Count meets every declared threshold. A mandatory Cycle Count Criterion fulfills the simulation requirement for that test without requiring a duplicate Simulation Criterion.
_Avoid_: cycle budget, synthesis criterion, benchmark score

**Ticket Board**:
The filesystem-backed state machine that tracks one Ticket from draft through execution and review. Its normal route is draft → queued → running → review → done, with waiting and blocked as pre-review pauses; review can instead archive the Ticket or explicitly reset it to a clean queued state, but never sends retained work back for partial rework. Directories live under `board/`; the status strings draft, queued, and running map to `drafts/`, `queue/`, and `active/`, while waiting, blocked, review, done, and archived match their directory names.
_Avoid_: bare "Board", kanban, tracker, backlog

**Scope**:
The set of files a ticket is authorized to commit. The Developer Agent may edit anything in its worktree, but the Harness commits only Scope-matching paths and preserves other edits uncommitted for Ticket triage. A per-run deviation report (`.runtime/scope_deviations.json`) records any outside paths that nevertheless reached branch history. The per-worktree pre-commit hook hard-rejects out-of-Scope files and Harness bookkeeping (development state, Criteria, ticket files, `booley.toml`). The `["*"]` sentinel grants no ownership: a ticket that names no files authorizes no automatic commits.
_Avoid_: allowlist

**Escalation**:
A signal that a decision exceeds the current authority level, flowing Specialist to Developer Agent to Human. When the Developer Agent escalates, the ticket moves to blocked on the Ticket Board.
_Avoid_: spec gap, blocker, impediment

### Waveform analysis

**B-Wave**:
An agent-facing MCP tool for waveform queries over FST trace stores. Converts VCD simulation output into FST, then supports signal queries, virtual signal evaluation, and text-mode waveform display. CLI entry point: `bwave`. (The custom `.bwave` store is retired in favor of FST.)
_Avoid_: waveform viewer, VCD parser, `.bwave` format

**Trace Artifact**:
Waveform evidence produced by a traced simulation. The preferred Trace Artifact is a valid FST store; a VCD file is also valid evidence when FST conversion is unavailable or delayed.
_Avoid_: sim output, log, no-sim

**Simulation Sentinel**:
A configured output string that Booley scans to determine a simulation verdict. Fail sentinels take priority over pass sentinels; when no sentinel is found after a clean run, the result is inconclusive. Applies to HDL-testbench Targets only: a **Cocotb Target**'s verdict comes from cocotb's result file, with assertion-output scanning retained; a missing or truncated result file is inconclusive, never a pass.
_Avoid_: regex, marker, exit-code-only verdict

**Virtual Signal**:
A named 1-bit boolean predicate defined over existing waveform signals using Verilog-subset expressions. Evaluated per-timepoint against cached waveform data. Supports composition (virtuals referencing other virtuals).
_Avoid_: computed signal, derived signal, expression

**Waveform Viewer**:
The human-facing GUI for visually exploring a Trace Artifact, opened via `bwave gui`. Booley launches an off-the-shelf viewer and never implements waveform rendering itself; B-Wave stays the agent-facing query surface. The VaporView VS Code extension is the default implementation, not the domain concept.
_Avoid_: waveform renderer, waveform GUI, wave window, B-Wave display

### Presentation

**Console**:
The full-screen TUI (Textual) that shows live execution state: one active Booley Flow or Specialist at a time, persistent Criteria panel, and dynamic counters. It is the default display for `booley run`; disable it with `--no-console` (`-L`) to fall back to plain scrolling log output.
_Avoid_: flashy mode, monitor, dashboard

### Feedback

**Finding**:
One logged observation about a Booley run, held in the **Findings Log** and rendered into the user report and outbound view. Every entry is one of four kinds — a *finding* proper (something malfunctioned; needs a reproduction, an observed and an expected), a **Friction Report**, an **Impression**, or a *win* (a check that passed first try, recorded so the finding count has a denominator). Each carries a bucket saying whose problem it is: `project` (the user's repo/config/environment), `booley`, `docs`, or `unknown` until triaged. Only `booley`/`docs` entries with enough evidence, not already filed, are eligible to go upstream. The `/booley-feedback` skill classifies and records ad-hoc feedback.
_Avoid_: issue, ticket (that is Booley's unit of *work*), defect report

**Friction Report**:
A Finding that records confusion rather than malfunction — nothing broke, but Booley was hard to follow. Held to its own evidence bar: where it happened (component or the check that surfaced it) and what the reporter expected instead, never a reproduction.
_Avoid_: minor bug, nitpick, UX bug

**Impression**:
A Finding carrying what a user *thinks* of Booley — praise, a gripe, a feature wish, whether it earned its keep on a real project — with a `sentiment` of `praise`, `gripe`, `wish` or `mixed`. The one kind with no evidence bar at all: a single sentence is a complete report, because there is nothing to reproduce. Reported and redacted through the same path as everything else, but framed apart from defects in both the user report and outbound view so an opinion is never counted as a bug.
_Avoid_: feature request, review, rating, testimonial

**Findings Log**:
The append-only `findings.jsonl` in the project state directory, one JSON entry per line, written concurrently by setup steps, sub-agents and ad-hoc reports. Outlives the run that started it: a project set up in March and hit by a bug in July appends to the same file, which is why entries carry an origin and a `filed` stamp. Rendered into one persistent, local, unredacted **user report** (`SETUP-REPORT.md`, or `FEEDBACK-REPORT.md` on a project that never ran setup). The maintainer-facing view is filtered and redacted transiently for preview/submission; outbound commands require explicit Finding IDs (or an intentional `--all`) so one conversation cannot pull in the unfiled backlog. The `/booley-feedback` skill persists the selected view as `BOOLEY-FEEDBACK.md` only when the user explicitly requests an export.
_Avoid_: bug database, feedback queue, telemetry (nothing here is automatic or silent)

## Flagged ambiguities

You will not need these unless you are reading older tickets, code, or docs; they are terms that were renamed or removed. Skim now, refer back when you hit one.

- **"tool"**: Overloaded across Booley, agent clients, MCP, and EDA. Never use the bare word in Booley prose or identifiers: say **Booley Flow** for deterministic orchestration, **EDA tool** for the external program a Flow drives, and **MCP tool** only for the protocol-level invocation mechanism.
- **"agent"**: Overloaded across Booley (Specialist), Claude Code (the outer agent), and the LLM industry generally. Use **Specialist** for Booley's LLM-powered sub-agents, **Developer Agent** for the agent that executes Tickets.
- **"stage"**, and lowercase **"harness"** used as a synonym for Booley's architecture as a whole: legacy framing: the system is a Developer Agent choosing capabilities, not a fixed pipeline. Do not use them that way. (The capitalized **Harness** *is* canonical: the Ticket Mode runtime infrastructure the Developer Agent runs within; see its entry above. What to avoid is "harness" as a loose synonym for the overall system.)
- **"engine" / "core"**: Legacy synonyms for Harness. Do not use.
- **"abandoned" / "failed"**: Removed ticket states. Use **archived** for tickets that won't be completed.
- **"effort"**: Deprecated ticket/resource hint. Do not use it to decide Workflow Regions, Specialist requirements, or Developer Agent routing.
- **"Design Configuration"**: Retired. The Booley-side bundle of EDA params no longer exists; design-description lives in a FuseSoC **Target**, and Booley only references it by name. Use **Target**.
- **"Session ID"**: Never implemented. Branch names and worktree paths derive from the ticket slug, and container names from the workspace folder name; there is no stable per-runtime identity to refer to.
- **"parameter override"** / **`-d`** / **`--define`**: Retired. There is no per-call build-time injection into a **Target**; declare the value in the Target, or use a different Target.
- **"colon-free target names"**: Retired absolute. VLNV grammar (the FuseSoC Vendor:Library:Name:Version identifier) is permitted on Booley's surface: bare names when unambiguous, `vlnv#name` on collision.
- **"target"**: Overloaded: a FuseSoC `.core` build **Target** vs. an EDA "target device/part" (the FPGA/ASIC the design maps to). The part is one field *inside* a Target, not a synonym for it. Always mean the FuseSoC build **Target** unqualified; say "target device" or "part" for the silicon.

## Example dialogue

> **Dev:** I need to add a FIFO module to our design. How do I set this up in Booley?
>
> **Expert:** Create a **Ticket**: define the **Scope** to cover the RTL files you'll add, and set **Criteria** like `sim_pass_default` and `lint_clean_default` for each **Target** you need.
>
> **Dev:** Then what happens?
>
> **Expert:** Put it in the queue on the **Ticket Board**. The **Runner** picks it up inside the **Session Runtime**, sets up an isolated worktree and branch, then launches the **Developer Agent** within the **Harness**. The **Developer Agent** decides which **Booley Flows** and **Specialists** to invoke. It authors both the RTL and any testbench itself, then the lint **Booley Flow** checks it.
>
> **Dev:** What if it can't figure out the reset behavior from the spec?
>
> **Expert:** The **Developer Agent** authors the RTL itself, so it hits that spec gap directly. If it can't resolve it, it raises an **Escalation**; still unresolved, the **Ticket** moves to blocked on the **Ticket Board**, and you resolve it.
>
> **Dev:** Does all of this run in the Sandbox?
>
> **Expert:** Yes: both **Booley Flows** and **Specialists** execute inside the **Session Runtime**, under one **Sandbox** policy rather than a per-Flow one. The host may provide an authorized commercial EDA installation and a narrow **FlexNet License Relay**, but it never executes agent-controlled commands.
