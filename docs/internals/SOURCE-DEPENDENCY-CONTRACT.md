# Source Dependency Contract

This contract defines Booley's intended Python source-dependency directions. Its
stable rules do not make the current package graph a universal allowlist. The
test-only analyzer describes source knowledge without adding a production
abstraction layer.

## Source map

The package layout serves the canonical concepts in [CONTEXT.md](../CONTEXT.md):

| Canonical concept | Principal source owners | Responsibility |
| --- | --- | --- |
| Host Bootstrap | `booley.harness.bootstrap`, `booley.harness.bootstrap_cli`, `booley.harness.host_sidecars` | Reconcile Project-independent host prerequisites and shared infrastructure. |
| Project Initialization | `booley.harness.init_cmd`, `booley.harness.setup`, `booley.agent_workspace` | Validate and reconcile one Project before issuing its Session Runtime. |
| Session Runtime | `booley.runtime`, `booley.harness.session_runtime`, `booley.harness.runtime_attachment` | Own shared execution records, processes, paths, and runtime lifecycle. |
| Booley Flow | `booley.flows` | Turn a structured request into an EDA invocation and machine-checkable evidence. |
| Target | `booley.targets`, `booley.fusesoc` | Resolve the design and named operation selected for a Flow. |
| Criteria | `booley.criteria`, Criteria modules within `booley.ticket_board` | Define and evaluate acceptance policy independently of its producing endpoint. |
| Specialist | `booley.specialists` | Run a scoped LLM sub-agent and return structured evidence. |
| Harness | `booley.harness.developer`, `booley.harness.developer_guardrails` | Drive the Developer Agent toward accepted Criteria. |
| Ticket Board | `booley.ticket_board` | Persist tickets, transitions, Criteria state, and execution records. |
| MCP | `booley.mcp` | Expose Flows and Specialists to calling agents. |
| B-Wave | `booley.bwave` | Answer structured waveform questions and control human viewing. |

Supporting mechanism packages keep their names. `booley.audit` owns typed
environment and configuration analysis; `booley.config` owns configuration;
`booley.eda` owns trusted EDA registrations and Grants; `booley.review` owns review
evidence; `booley.projects` owns Project inventory commands; `booley.core` owns
dependency-light primitives; and `booley.dev_support`, `booley.docker`, `booley.data`,
and `booley.feedback` own their named mechanisms. These descriptions create no new
domain concepts.

## Graph semantics

The analyzer uses `ast` to parse every `*.py` file below `src/booley`. It records
facts of the form `importing Python module -> imported in-repository Python module`
for every `Import` and `ImportFrom`, including imports inside functions,
conditions, and `TYPE_CHECKING` blocks.

It resolves relative imports from the importing package, including package
`__init__.py` files. For `from package import name`, it prefers an importable
in-tree submodule over treating `name` as a symbol; otherwise the package or module
base is the dependency. Aliases do not change identity. The graph retains only
modules discoverable below the selected `booley` source root. A source read or
syntax failure aborts analysis.

Rules and permissions operate at module granularity. A prefix selector matches a
named module and its descendants; an exact selector matches only the named module.
Fan-out counts the unique target modules imported by one source module.

The cycle diagnostic projects each edge to its immediate `booley.<package>` owner
and discards same-package edges. An approved legacy SCC specifies an exact member
set at that projection; it does not approve every edge within the set.

## Direction rules

Each rule states independent policy. The production-tree gate in
`tests/architecture/test_source_dependency_contract.py` enforces the full table,
as tracked by [#281](https://github.com/boldaxolotl/booley/issues/281).

| Rule | Source selector | Target selector | Decision | Design reason |
| --- | --- | --- | --- | --- |
| D1 | Prefixes `booley.audit`, `booley.config`, `booley.fusesoc`, `booley.targets` | Prefixes `booley.harness`, `booley.mcp`, `booley.specialists` | Forbid | Environment/configuration analysis and Target policy must not know Harness, MCP, or Specialist mechanisms. |
| D2 | Prefix `booley.criteria` | Prefixes `booley.harness`, `booley.mcp`, `booley.specialists` | Forbid, subject only to W1-W2 | Criteria is acceptance policy; endpoint discovery is an agent-facing mechanism. |
| D3 | Prefix `booley.specialists` | Prefix `booley.harness` and exact module `booley.mcp.server` | Forbid | A Specialist returns evidence without depending on its Harness or MCP composition mechanism. |
| D4 | Prefix `booley.mcp` | Prefixes `booley.harness`, `booley.specialists` | Forbid, subject only to C1-C2 | MCP infrastructure is independent of the capabilities composed by its server. |
| D5 | Prefix `booley.runtime` | Prefixes `booley.mcp`, `booley.specialists` | Forbid | Session Runtime mechanisms must remain usable without agent-facing mechanisms. |
| D6 | Prefix `booley.runtime` | Prefix `booley.harness` | Forbid, subject only to C3-C7 | Shared Session Runtime mechanisms must not acquire Harness knowledge; exact entry-point composition remains explicit. |
| D7 | Exact modules `booley.flows.target_campaign`, `booley.flows.target_criteria`, `booley.flows.target_test_suite` | Prefixes `booley.harness`, `booley.mcp`, `booley.ticket_board` | Forbid | Shared Target/Criteria policy is independent of presentation, agent exposure, and Ticket Board persistence. |
| D8 | Each prefix in `booley.flows.{sim,synth,fpga,lint}` | The other three prefixes in that set | Forbid | Each built-in Booley Flow owns its tool-specific implementation and cannot couple to a sibling Flow. |
| D9 | Root module and direct file-module children of `booley.flows` (not child package initializers) | Prefixes `booley.flows.{sim,synth,fpga,lint}` | Forbid | Flow-neutral policy and evidence modules cannot select a concrete Flow implementation. |
| D10 | One exact adapter selector set S1-S5 below | The other selector sets for the same Flow (S1-S3 or S4-S5) | Forbid | An EDA adapter satisfies its Flow's internal seam without knowing a sibling adapter. |
| D11 | Prefixes `booley.flows.synth.backends.yosys`, `booley.flows.synth.backends.openroad` | Exact module `booley.flows.synth.flow` and the sibling backend prefix | Forbid | Leaf synthesis adapters do not orchestrate their Flow or one another. |

D9 resolves PR 1's ambiguous phrase "direct module children" according to its
Flow-neutral design reason. It includes the root package module and direct file
modules such as `booley.flows.target_campaign`. It excludes child package
initializers such as `booley.flows.sim`, which belong to the selected concrete
Flow. This explicit selector grants no exception from other direction rules.

These D10 adapter selector sets are exhaustive:

- S1, Cocotb: exact modules `booley.flows.sim.backends.cocotb` and
  `booley.flows.sim.backends.cocotb_results`.
- S2, Icarus: exact module `booley.flows.sim.backends.icarus`.
- S3, Verilator: exact module `booley.flows.sim.backends.verilator`.
- S4, OpenROAD: prefix `booley.flows.synth.backends.openroad`.
- S5, Yosys: prefix `booley.flows.synth.backends.yosys`.

For each S1-S3 source, D10 forbids targets in the other S1-S3 sets. Each S4-S5
source cannot target the other S4-S5 set. Shared backend policy and the
experimental simulator readers remain unclassified.

All other source edges remain unclassified pending design work. Their presence
does not endorse them, and the checker must not generate permissions from them.

## Exact composition-root permissions

These are the only rule exceptions classified as enforced design. Each permission
belongs to one named rule and gives no source module a blanket exemption.

| Permission | Rule | Exact source -> exact target | Reason |
| --- | --- | --- | --- |
| C1 | D4 | `booley.mcp.server -> booley.harness.auto_doctor` | The MCP server composes the Doctor endpoint at the agent-facing entry point. |
| C2 | D4 | `booley.mcp.server -> booley.specialists.specialist` | The MCP server classifies and composes Specialist endpoints. |
| C3 | D6 | `booley.runtime.heartbeat -> booley.harness.colors` | The heartbeat command composes terminal presentation at its executable entry point. |
| C4 | D6 | `booley.runtime.heartbeat -> booley.harness.terminal` | The heartbeat command composes terminal lifecycle at its executable entry point. |
| C5 | D6 | `booley.runtime.incontainer_register -> booley.harness.auto_doctor` | In-container registration composes its Doctor command entry point. |
| C6 | D6 | `booley.runtime.incontainer_register -> booley.harness.upgrade_cli` | In-container registration composes upgrade commands. |
| C7 | D6 | `booley.runtime.incontainer_register -> booley.harness.upgrade_review` | In-container registration composes upgrade-review commands. |

## Exact legacy waivers

Waivers are exact, live edges. They permit no package prefix and cannot transfer
to a replacement edge. Both current waivers retire through
[#284](https://github.com/boldaxolotl/booley/issues/284).

| Waiver | Rule | Exact source -> exact target | Design explanation | Retirement work |
| --- | --- | --- | --- | --- |
| W1 | D2 | `booley.criteria.actions -> booley.mcp.registry` | Invocation rendering currently discovers the endpoint-to-Criterion relationship from MCP registration. This is legacy mechanism knowledge, not desired policy direction. | #284 |
| W2 | D2 | `booley.criteria.reference -> booley.mcp.registry` | Generated Criteria reference text currently discovers producing endpoints through the MCP registry. | #284 |

## Dynamic-import inventory

The general graph excludes dynamic resolution. This inventory names each current
production use and its existing proof so the limits of static analysis remain
explicit:

| Owner | Mechanism and scope | Existing named proof |
| --- | --- | --- |
| `booley.dev_support.validate_commit_msg` | Imports packaged `core.run_command` or a flat vendored `run_command`; the packaged case is the dynamic equivalent of `booley.dev_support.validate_commit_msg -> booley.core.run_command`. | `tests/dev_support/test_validate_commit_msg.py` proves packaged, vendored, and stale-hook resolution. |
| `booley.mcp.server` | Imports discovered built-in `booley.mcp.*` endpoint modules and Project-local MCP files. | MCP server and registry discovery tests prove built-in and custom endpoint loading. |
| `booley.harness.booley` | Imports a registry-selected built-in `booley.*` MCP tool class or a Project-local MCP file for diagnostic commands. | `tests/harness/test_booley.py` proves built-in and Project-local loading. |

Uses of `importlib.metadata`, `importlib.resources`, and `importlib.util.find_spec`
that inspect distributions, resources, or module availability create no hidden
in-repository source edges. PR 2 should keep the three named mechanisms explicit
and must not speculate about arbitrary Python expressions.

## Reproducible baseline

The baseline combines production source at `094d1c5d` (current `main` when PR 1
began) with the analyzer introduced by PR 1 at `4725cd09`. Reproduce this historical
two-tree combination from any checkout containing both commits:

```console
baseline_dir="$(mktemp -d)"
git archive 4725cd09 tests/architecture | tar -x -C "${baseline_dir}"
git archive 094d1c5d src/booley | tar -x -C "${baseline_dir}"
python3 "${baseline_dir}/tests/architecture/report.py" \
  --source-root "${baseline_dir}/src/booley" --top 30
```

The analyzer parses 370 Python modules and emits 1,761 located dependency facts
representing 1,378 unique module-to-module edges. At the 02 SEP 2026 baseline,
seven edges are enforced composition permissions, two Criteria edges are exact
legacy waivers, and the other 1,369 remain unclassified by design.

The one approved legacy multi-package SCC has this exact member set:

```text
booley.agent_workspace, booley.audit, booley.bwave, booley.config,
booley.criteria, booley.dev_support, booley.eda, booley.feedback, booley.flows,
booley.fusesoc, booley.harness, booley.mcp, booley.projects, booley.review,
booley.runtime, booley.specialists, booley.targets, booley.ticket_board
```

The SCC excludes the separate `booley.core`, `booley.data`, and `booley.docker`
package groups. PR 2's subset ratchet may let the approved SCC split, but it may
not admit another group or merge groups that were separate.

Current direct mutual top-level package pairs are:

```text
booley.bwave <-> booley.flows
booley.config <-> booley.runtime
booley.criteria <-> booley.flows
booley.criteria <-> booley.mcp
booley.dev_support <-> booley.runtime
booley.eda <-> booley.flows
booley.eda <-> booley.harness
booley.feedback <-> booley.harness
booley.flows <-> booley.fusesoc
booley.flows <-> booley.mcp
booley.flows <-> booley.targets
booley.flows <-> booley.ticket_board
booley.fusesoc <-> booley.runtime
booley.fusesoc <-> booley.targets
booley.harness <-> booley.mcp
booley.harness <-> booley.review
booley.harness <-> booley.runtime
booley.harness <-> booley.ticket_board
booley.mcp <-> booley.specialists
booley.mcp <-> booley.ticket_board
booley.review <-> booley.ticket_board
booley.runtime <-> booley.ticket_board
```

Named composition hotspots use file fan-out only as diagnostic evidence:

| Canonical role | Exact module | Unique target modules |
| --- | --- | ---: |
| Host/Project diagnostic composition | `booley.harness.doctor` | 62 |
| Command composition | `booley.harness.booley` | 52 |
| Project Initialization | `booley.harness.init_cmd` | 42 |
| Harness | `booley.harness.developer` | 40 |
| Simulation Flow | `booley.flows.sim.flow` | 35 |
| Synthesis Flow | `booley.flows.synth.flow` | 30 |
| MCP composition | `booley.mcp.server` | 29 |
| FPGA Flow | `booley.flows.fpga.flow` | 26 |
| Mutation Specialist | `booley.specialists.mutation_tester` | 24 |
| Coverage Specialist | `booley.specialists.coverage_analyst` | 22 |

High fan-out is diagnostic, not a violation. A change to one of these modules
records before and after output in
[#279](https://github.com/boldaxolotl/booley/issues/279), allowing the later fan-out
decision to distinguish legitimate composition from unjustified knowledge growth.

## Required gate

The pytest gate checks every normalized production dependency against the direction
rules, exact composition permissions, and exact legacy waivers. It rejects missing
waiver metadata and stale waivers whose exact edge has gone. Its SCC ratchet rejects
each current multi-package SCC that is not a subset of an approved legacy member
set. Approved groups may split; new acyclic singletons need no baseline entry.

Direction failures name the source location, normalized edge, rule, and any exact
permission or waiver for that source under the rule. The deterministic report also
prints every named hotspot fan-out value under a diagnostic-only heading. Fan-out
values do not gate changes.

Run the complete architecture check and its report from the repository root:

```console
pytest -q tests/architecture/
python3 tests/architecture/report.py --source-root src/booley --top 30
```
