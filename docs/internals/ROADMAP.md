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

**Planned.** A single command, `booley ci`, that runs the CI-enabled Targets
through the Booley Flows that can drive them and reports the result as a matrix:
what lints, what elaborates, what simulates, what synthesizes. CI membership
belongs with each Target in its `.core` design description: Booley-owned
per-Target metadata should explicitly include or exclude that Target rather
than making users maintain a second Target allowlist in a workflow file. Push
it to CI and the answer to "does this repo still build" stops being folklore.

Per-test Cycle Count Criteria already compare Ticket work with its immutable
baseline and disclose known workload drift. A future `booley ci` will bring
that evidence together with area, Fmax, and coverage in a hosted matrix measured
against the same design before the change rather than against a stale number from last week. Reviewers
see "+2.2% area, −6 MHz, +4.1% cycles" next to the diff, so users can track both
area and performance in CI. A project can set limits so a regression fails the
run instead of being noticed a month later.

Because the sandbox image already carries the EDA stack, there is nothing to
install on a hosted runner: the workflow is a handful of lines and no simulator
or synthesis setup at all. Slow Targets can be kept off every push and run
nightly instead.

The same engine should also support [stealth
mode](../user/CONFIG.md#stealth-mode-stealth) without committing a Booley-shaped hosted
workflow. In that deployment the runner and scheduler are local, and structured
run results and history live in `.booley_project/`'s own repository rather than
appearing as hosted checks or files in the RTL repository. The Target selection,
matrix semantics, regression limits, and baseline comparison stay the same;
only the execution and reporting plane changes.

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
