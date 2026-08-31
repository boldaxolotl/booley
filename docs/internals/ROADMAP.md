# Roadmap

Directions Booley hasn't taken yet, ordered by impact: the highest-leverage
items come first. Commercial EDA backends are the best place for an outside
contribution; coverage measurement is the highest-impact item for the quality of
Booley's output. Each item opens with a status tag:

- **Planned** — not started.
- **Prototype** — code exists, but is not reliable enough for unattended use.
- **Partial** — works in some supported configurations but not all.
- **In tree, hidden** — built, but de-registered from the MCP tool registry until it matures.

## Verification Engineering

**Planned.** Booley will grow from an agentic RTL-development framework into a
tool that serves verification engineers as well as RTL design engineers. The
verification workflow should build on a Project's existing SystemVerilog or
cocotb environment and carry work from test execution through reproducible,
machine-checkable evidence rather than treating the testbench only as support
for an RTL ticket. This fits Booley's existing role as the common orchestrator
for configured Targets, Booley Flows, the Session Runtime, verification intent,
and their evidence: the same control plane can resolve, build, and run a full
regression matrix and report it consistently in local and CI environments
without a second collection of project-specific scripts.

The three highest-value capabilities are [coverage
measurement](#coverage-measurement), regression management, and failure
triage. Regression management should run and retain test/Target/seed matrices,
support parallel and nightly campaigns, and make every failure exactly
reproducible. Failure triage should cluster related failures, distinguish
design, testbench, and infrastructure problems, and rerun representative
failures with waveform capture for focused diagnosis.

## Commercial EDA Tools

**Partial.** Host-provisioned Vivado 2025.2 on Linux x86-64 is the first
supported commercial policy. The fixed-destination FlexNet relay remains
experimental because a real paid-seat checkout/accounting/return matrix was not
available. Windows supports the image-provisioned toolchain, not mounted native
Windows EDA installations. Future work includes
Cadence, Synopsys, and Siemens tools such as Xcelium, VCS, Design Compiler,
Questa, Genus, and HAL. A future policy must provide an equivalent built-in
installation contract, read-only runtime provisioning, licensing relay,
Doctor checks, security tests, and full-Flow proof; a wrapper script alone is
not support.

The blocker is licenses, not design: the maintainer can't validate a Flow for an EDA tool they can't run, which makes this the best place for an outside contribution. [CONTRIBUTING.md](CONTRIBUTING.md#the-1-priority-port-commercial-eda-tools) lists the specific EDA tools worth porting per vendor, which of them Edalize already invokes, and what a port actually takes. For what ships today, see [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md).

## Coverage Measurement

**In tree, hidden.** A coverage engine (the `coverage_analyst` Specialist) exists in the tree but is hidden from the MCP tool registry until it matures. It is built on bwave queries against the simulation trace: bwave measures toggle and value coverage mechanically, LLM Specialists derive branch/expression conditions and FSM states, and deterministic Python scores each goal (toggle, value, FSM state/transition, branch, expression) against configurable thresholds. No UVM covergroups, no simulator-specific coverage databases. Two things remain before it ships: maturing the Specialist itself, and making every metric fully deterministic, with no LLM in the measurement loop. This is one of the highest-impact items on this list: coverage analysis is the gate that decides whether a testbench is good enough, and testbench quality determines the correctness of Booley's output.

**The determinism half will use slang, not a Booley-written Verilog
front-end.** Toggle and value coverage are already mechanical: bwave reads them
straight off the trace. Branch and expression coverage are not: they need the
branches and sub-expressions *enumerated from the RTL* before anything can be
scored, which is why an LLM Specialist derives them today, as it does the FSM
state set. The slang integration will use its source-aware syntax model to
enumerate those constructs and its elaborated semantic model where resolved
types and names are needed, including FSM state discovery. The same integration
backs the [HDL Dependency Graph](#hdl-dependency-graph-intra-target-file-pruning),
so Booley does not build or maintain a second HDL parser. Toggle and value
coverage can ship deterministic ahead of the slang-backed metrics.

## Continuous Integration (`booley ci`)

**Planned.** `booley ci` runs a named set of Criteria against the current
checkout using Ticket Mode's catalog, syntax, Target bindings, threshold rules,
and evidence model. CI has no separate check system. Project policies for pull
requests and nightly runs choose Criteria, which choose Targets and Booley
Flows. The usual starting point is `sim_pass`: every registered test must pass
for each relevant Target. Other choices include lint, Cycle Count, area, Fmax,
FPGA resources, coverage, and Custom Flow Criteria.

Each required Criterion has a deterministic verdict. An unmet Criterion is a CI
failure with normalized evidence; an unavailable tool, invalid configuration,
or failed runner is a CI execution error. Both fail the run. Separate names stop
infrastructure faults from looking like design regressions. The report groups
results and evidence by Criterion.

Relative Criteria pin an immutable pull request merge base or configured target
branch commit, never a previous CI result. Ticket Mode's per-test Cycle Count
model also covers area, Fmax, FPGA resources, and coverage. Results such as
"+2.2% area, −6 MHz, +4.1% cycles" appear beside the diff. Crossing a threshold
fails CI.

Phase one saves the candidate diff, baseline and candidate identities, Criterion
parameters, normalized results, logs, waveforms, and detailed tool reports.
Area and timing reports include hierarchy as well as totals. This lets later
investigations work from evidence instead of scraped logs.

Phase two is opt-in. After a deterministic failure, CI starts an investigator
agent with the failed Criterion and saved evidence. The agent reports a probable
cause, confidence, and possible fixes or mitigations. It uses logs, waveforms,
and the diff for simulation failures; for area failures, it compares baseline
and candidate synthesis breakdowns. It may find the evidence insufficient. Its
advice cannot change the verdict, edit code, or push changes.

Ticket Mode and CI call the same Criterion evaluator but keep separate
orchestration. Ticket Mode passes a sealed Ticket and owns its lifecycle. CI
passes Criteria, reports the verdict, and may start the investigator without
depending on the Ticket Board or Harness lifecycle.

The sandbox image contains the EDA stack, so a hosted workflow needs no setup
and takes only a handful of lines. Slow Targets can run nightly. [Stealth
mode](../user/CONFIG.md#stealth-mode-stealth) keeps scheduling, structured results, and
history in `.booley_project/`, with no hosted checks or files in the RTL
repository. Execution and reporting differ; Criteria, baselines, and regression
limits stay the same.

## GitHub Issues in Ticket Mode

**Planned.** GitHub Issues should be first-class Ticket sources alongside the
local Markdown Ticket Board. Ticket creation, queue selection, execution, and
triage should work through either surface. Booley should discover eligible
issues from configured repositories and labels, claim each issue for exactly
one Runner, prepare and seal the normal executable Ticket contract, and report
blocked, review, and done transitions back through labels and comments. Issue
references should be usable for dependencies, while local Tickets retain the
complete offline workflow.

**The Harness contract does not change.** GitHub is the collaboration surface,
but each run snapshots the issue into a durable local record before execution.
Edits to an issue after it is claimed must not silently change its sealed Scope,
Criteria, or base revision; retries and recovery use the same snapshot and
evidence history. Credentials and network operations belong in a trusted
host-side integration rather than the Session Runtime or Developer Agent, and
remote updates must be idempotent and conflict-aware so a GitHub outage cannot
corrupt local work or acceptance evidence.

## Cocotb Support

**Partial.** [cocotb](https://www.cocotb.org/) testbenches run on the sandbox simulators today (see [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md)). What's left:

- Cocotb on future commercial policies. The hard part is proving the complete
  image/runtime and licensing contract, not a host Python environment.

## Full IP Design from Spec

**Prototype.** Provide an IP-level specification and Booley breaks it down into a dependency-ordered queue of tickets, then executes them all autonomously. Design an SPI controller, implement a complete AES core, etc. The breakdown tooling exists in prototype form but the end-to-end flow is not reliable enough for an unattended run yet.

## HDL Dependency Graph (intra-Target file pruning)

**Planned.** This item assumes the FuseSoC Target/fileset model: a **Target** is a named `.core` build target, and its *fileset* is the ordered list of source files that Target resolves to (see [CONTEXT.md](../CONTEXT.md) for the vocabulary and [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) for how the Flow uses it).

Prune a resolved Target's fileset down to the module a Booley Flow actually
needs. **The chosen front-end is slang; Booley will not implement its own
Verilog/SystemVerilog parser.** Booley passes slang the Target's ordered files,
defines, include directories, parameters, and top, then uses source information
for `include` and `import` edges and the elaborated hierarchy for instantiation
edges. A small Booley-owned adapter emits the dependency graph and hands a Flow
only the transitive closure of its top. The result is cached until its sources
or compilation inputs change. This cuts the time Booley Flows spend resolving
files they do not need and stops spurious syntax errors surfacing from unrelated
ones. The adapter is shared with the slang-backed coverage work rather than
creating a separate front-end for each feature.

The gap here is *inside* a Target, not across them. FuseSoC already answers "which files does Target X build": a `.core` Target resolves to a concrete ordered fileset and every Booley Flow selects one via `--target`. What it deliberately doesn't do is prune within that fileset: the fileset covers **that Target's top** (its top-level module), not the minimal closure for some other module you want to lint or elaborate in isolation. Filesets get shared across Targets and `depends` composition (a Target pulling in other cores it builds on) pulls in more of a dependency core than any one top instantiates, so in practice they run coarse.

**Fileset hygiene remains complementary.** Narrow, per-flow Targets, a pattern
the FuseSoC model already expects, reduce the work before slang runs and keep the
resolved fileset close to the required closure. The dependency graph provides
automatic pruning when shared filesets and `depends` composition are still too
coarse, without requiring project authors to maintain additional fine-grained
Targets.

## Additional Booley Flows

**Planned.** The current Booley Flows cover simulation, synthesis (ASIC and FPGA), and lint. (Note: `synth` is a **fast PPA estimate to guide RTL optimization**, not tape-out sign-off; see [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md#built-in-flows).) Production RTL workflows need more:

- Logic equivalence checking (LEC)
- Formal verification
- Clock domain crossing (CDC) and reset domain crossing (RDC) checks
- Design for test (DFT) insertion
- Static timing analysis (STA)
- Power analysis / UPF
- Coverage-driven verification: functional coverage collection and hole analysis to guide stimulus generation
- FPGA synthesis and PnR beyond AMD Vivado (Intel); Vivado already ships as the EDA tool driven by the built-in [`fpga`](FLOW_IMPLEMENTATION.md#fpga) Flow

## Native IDE Surface in VS Code

**Planned.** Booley turns VS Code into an *agentic RTL IDE*, but today that means the agent chat plus the Booley Flows it drives; the editor chrome itself is stock VS Code. The next step toward the framing is native UI that lives in the VS Code chrome rather than the terminal: a ticket/run panel, inline acceptance-criterion status, a Flow-run dashboard, and one-click waveform open. That turns "IDE" from an analogy into visible product.

The end state of this direction is Booley as a **standalone agentic IDE**: a VS Code fork where the agentic RTL workflow *is* the product rather than an extension bolted onto someone else's editor. That's a long way off; the extension-based surface above is the path there.
