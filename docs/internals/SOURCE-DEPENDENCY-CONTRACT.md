# Source Dependency Contract

This contract says which Booley Python packages may import which, why each
boundary exists, and how to change one. Read it when you move code between
packages, add a package or a module that crosses a boundary, or a test under
`tests/architecture/` fails. It is for contributors and agents. The big
picture is in [ARCHITECTURE.md](ARCHITECTURE.md); Doctor's diagnostic owners
and the shared health interface are in [DOCTOR-HEALTH.md](DOCTOR-HEALTH.md);
terms are defined in the [glossary](../GLOSSARY.md).

The rules are executable. `tests/architecture/booley_contract.py` holds them,
and the architecture tests check them against every import in `src/booley`.
This doc explains them; when the two disagree, the code is what runs, so fix
this doc.

The contract keeps low-level packages reusable and keeps composition in a few
entry points (the MCP server and the `booley` commands). A change to an
agent-facing surface then cannot ripple down into Sandbox, Config, or Flow
code. The rules list forbidden directions only. An import that no rule covers
is unclassified: neither endorsed nor forbidden, and its presence never earns
it a permission.

## Run the check

From the repository root:

```console
pytest -q tests/architecture/
python3 tests/architecture/report.py --source-root src/booley --top 30
python3 tests/architecture/compare_report.py \
  --before-ref <base-commit> --after-ref <head-commit> --repo-root .
```

- `pytest` is the gate. A direction failure names the source location, the
  import edge, the rule and its reason, and any permission that rule grants
  that source.
- `report.py` prints module and edge counts, cyclic package groups, mutual
  package pairs, and import fan-out (the number of distinct modules one module
  imports) for the largest importers and for a fixed list of composition
  modules (`NAMED_HOTSPOTS` in `tests/architecture/import_graph.py`). Fan-out
  never fails the gate; a large one is a hint that a module knows too much.
- `compare_report.py` diffs two source trees: added and removed edges, mutual
  pairs, cyclic-group membership per package, and changed hotspot fan-out. It
  is informational and exits 0 whenever the comparison runs, even with
  changes; bad refs, roots, or source fail nonzero. Give it two commits (each
  is archived before analysis) or two unpacked package roots with
  `--before-root`/`--after-root` plus the required
  `--before-label`/`--after-label`. Put its output in the PR that changes a
  boundary, so reviewers see what moved.

## What the analyzer sees

The analyzer (`tests/architecture/import_graph.py`) parses every `*.py` file
under `src/booley` with `ast` and records each `import` and `from ... import`
as an edge from the importing module to the imported in-repository module.
Imports inside functions, conditions, and `if TYPE_CHECKING:` blocks count
too: a deferred or type-only import still couples the two modules, so it
cannot hide from a rule.

- Relative imports resolve from the importing package. For
  `from package import name`, a submodule called `name` wins over a symbol.
  Aliases don't change the target.
- A file it cannot read or parse fails the analysis instead of being skipped.
- Selectors in the rules come in three kinds: an exact module, a prefix (the
  module and everything below it), and package-files (a package module plus
  the file modules directly inside it, not its child packages; only D9 uses
  it).
- Imports built at run time (`importlib.import_module`, loading a file by
  path) are invisible to it. Each such use is listed in
  [Dynamic imports](#dynamic-imports).

## What the gate checks

1. **Directions.** Every edge is checked against the [direction
   rules](#direction-rules). An edge a rule forbids fails unless an exact
   [composition permission](#composition-permissions) or legacy waiver
   (`LegacyWaiver`) names that exact source and target.
2. **Metadata.** Every rule needs selectors and a reason. Every permission or
   waiver names a known rule and an exact source and target module. A waiver
   also names the GitHub issue that retires it, and a waiver whose edge no
   longer exists fails as stale. There are no waivers today
   (`LEGACY_WAIVERS = ()`).
3. **Cyclic package groups.** Each edge is projected onto its top-level
   package (`booley.<package>`), and same-package edges are dropped. Every
   cycle among packages must fit inside an approved group in
   `APPROVED_LEGACY_SCCS`, and
   `test_approved_cyclic_groups_are_tightened_to_actual_groups` requires that
   list to equal the measured groups exactly. So when a change breaks a cycle,
   the same change shrinks the approved list, and the cycle can't come back.
   The only approved group is `booley.fusesoc` and `booley.targets`.

A pass proves these rules and nothing more. A new cycle inside the approved
group passes, and a package cycle does not mean any single module imports
itself back.

Other tests in `tests/architecture/` guard narrower ownership rules:

| Test file | Guards |
| --- | --- |
| `test_source_dependency_contract.py` | Simulation Record storage types stay in their owners; low-level Target mechanics stay behind `TargetCatalog` and the FuseSoC adapter; MCP Job owners never reach `mcp.server`; shared health never reaches command, MCP, or Specialist code. |
| `test_goal_mode_boundaries.py` | Only the modules listed in its `_ALLOWED` table import `booley.goals` or `booley.criteria`. |
| `test_config_eda_runtime_separation.py` | The approved cyclic groups match the measured ones; separated packages cannot rejoin a cycle with `booley.flows`; no `booley.flows` module imports `booley.specialists`. |
| `test_color_helper_contract.py` | Raw terminal color codes appear only in `booley.presentation.colors`. |
| `test_shared_endpoint_dispatch.py` | Shared endpoint code calls declared hooks instead of checking for the Simulation Flow by name. |

## Source map

Each package owns one concept from the [glossary map](../../GLOSSARY-MAP.md):

| Concept | Main source owners | Responsibility |
| --- | --- | --- |
| Host Bootstrap | `booley.harness.bootstrap`, `booley.harness.bootstrap_cli`, `booley.harness.host_sidecars`, `booley.harness.host_diagnostics` | Check and set up host prerequisites and shared infrastructure that no single Project owns. |
| Project Initialization | `booley.harness.init_cmd`, `booley.harness.setup` (with `readiness`), `booley.agent_workspace` | Validate and set up one Project before issuing its Sandbox. |
| Sandbox | `booley.runtime` | Execution records, processes, paths, worktree identity, Sandbox Images, and Sandbox lifecycle. |
| Booley Flow | `booley.flows` | Turn a structured request into an EDA tool run and checkable evidence. |
| Target | `booley.targets`, `booley.fusesoc` | Resolve the design and named operation a Flow runs. |
| Criteria | `booley.criteria` | Define and evaluate acceptance policy, independent of the endpoint that produced the evidence. |
| Criterion evidence values | `booley.evidence` | Persisted evidence field names, recipe identity, reviewer receipts, and per-clock timing values that Criteria and Flows share. |
| Specialist | `booley.specialists` | Run a scoped LLM sub-agent and return structured evidence. |
| Goal Mode | `booley.goals` | Translate Goals into Criteria; own Goal Records, their locks and revisions, and the Change Log. |
| MCP | `booley.mcp` | Expose Flows and Specialists to the Developer Agent. |
| B-Wave | `booley.bwave` | Inspect, convert, find, and query waveform stores; drive human viewing. |
| Metric-Driven Development | `booley.measurement` | What each Flow measures and how it is read. |
| Result Cache | `booley.result_cache` | Result Key part helpers (unfinished; removed with Goal Mode, ADR 0092). |

`booley.harness` holds the CLI, Doctor, and setup code; the old "Harness"
orchestration term is retired. Supporting packages own their named
mechanism: `booley.audit` (environment and configuration analysis, typed
diagnostic reports), `booley.config` (configuration), `booley.eda` (EDA
registrations and Project Grants), `booley.review` (review artifacts from
resolved evidence), `booley.projects` (Project Inventory commands),
`booley.commit_policy` (Stealth Mode commit policy and message validation),
`booley.presentation` (terminal colors, tables, heartbeat, and report and log rendering), `booley.core`
(dependency-light primitives), and `booley.dev_support`, `booley.docker`,
`booley.data`, and `booley.feedback`.

## Direction rules

Every rule forbids its source-to-target imports, including deferred and
type-only ones. Its only exceptions are the [composition
permissions](#composition-permissions) that name it. Rule numbers are not
reused, so a gap is a rule that was removed; give a new rule the next number,
D42.

| Rule | Source | Forbidden target | Reason |
| --- | --- | --- | --- |
| D1 | Prefixes `booley.audit`, `booley.config`, `booley.fusesoc`, `booley.targets` | Prefixes `booley.harness`, `booley.mcp`, `booley.specialists` | Environment and configuration analysis and Target policy don't know command, MCP, or Specialist code. |
| D2 | Prefix `booley.criteria` | Prefixes `booley.harness`, `booley.mcp`, `booley.specialists` | Criteria is acceptance policy; endpoint discovery is an agent-facing mechanism. |
| D3 | Prefix `booley.specialists` | Prefix `booley.harness`; exact `booley.mcp.server` | A Specialist returns evidence without depending on the code that composes it. |
| D4 | Prefix `booley.mcp` | Prefixes `booley.harness`, `booley.specialists` | MCP infrastructure is independent of the capabilities its server composes. Exception: C2. |
| D5 | Prefix `booley.runtime` | Prefixes `booley.mcp`, `booley.specialists` | Sandbox code stays usable without agent-facing code. |
| D6 | Prefix `booley.runtime` | Prefix `booley.harness` | Shared Sandbox code doesn't learn command code. |
| D7 | Exact `booley.flows.target_campaign`, `booley.flows.target_criteria`, `booley.flows.target_test_suite` | Prefixes `booley.harness`, `booley.mcp` | Shared Target and Criteria policy is independent of presentation and agent exposure. |
| D8 | Each of the prefixes `booley.flows.{sim,synth,fpga,lint}` | The other three | Each built-in Flow owns its EDA-tool-specific code and doesn't couple to a sibling. The Mutation Flow composes `sim` by design, sits outside this set, and is governed by D42. |
| D9 | Package-files `booley.flows` | Prefixes `booley.flows.{sim,synth,fpga,lint}` | Flow-neutral policy and evidence modules never select a concrete Flow. |
| D10 | Each adapter set S1-S5 (below) | The other sets of the same Flow | An EDA tool adapter fits its Flow's seam without knowing a sibling adapter. |
| D11 | Prefixes `booley.flows.synth.backends.yosys`, `booley.flows.synth.backends.openroad` | Exact `booley.flows.synth.flow`; the sibling backend prefix | Leaf synthesis adapters don't drive their Flow or each other. |
| D12 | Exact `booley.targets.domain`, `booley.targets.selection` | Prefix `booley.fusesoc`; prefixes `booley.flows.{sim,synth,fpga,lint}`; exact `booley.targets.catalog`, `booley.targets.target_surface` | Target values and selector policy stay independent of FuseSoC, concrete Flows, catalog orchestration, and presentation. |
| D12 | Prefix `booley.fusesoc` | Exact `booley.targets.catalog`, `booley.targets.target_surface` | FuseSoC adapters don't depend back on catalog orchestration or presentation. |
| D13 | Prefix `booley.fusesoc` | Prefixes `booley.flows.{sim,synth,fpga,lint}` | FuseSoC code stays reusable beneath the concrete Flows. |
| D15 | Prefix `booley.flows` | Prefix `booley.mcp` | Deterministic Flow execution doesn't depend on MCP exposure; schemas and adaptation belong to MCP. |
| D16 | Prefix `booley.criteria` | Prefix `booley.flows` | Criteria evaluates shared evidence without depending on how Flows produce it. |
| D18 | Prefix `booley.config` | Prefix `booley.runtime` | Configuration returns validated values; Runtime and Project Initialization build backends and own setup. |
| D19 | Prefix `booley.eda` | Prefix `booley.flows` | EDA reads declarative Flow enablement without depending on Flow execution. |
| D20 | Prefix `booley.eda` | Exact `booley.runtime.session_issuance`, `booley.runtime.issuance_invalidation` | EDA supplies provisioning facts without knowing Sandbox Issuance or its invalidation. |
| D21 | Prefix `booley.review` | Prefix `booley.harness` | Review renders artifacts from resolved evidence without command code. |
| D23 | Prefix `booley.config` | Prefix `booley.eda` | Config owns declarative requests without depending on EDA provisioning policy. |
| D24 | Prefix `booley.eda` | Prefix `booley.runtime` | EDA uses neutral host mechanisms, not Sandbox execution or issuance. |
| D25 | Exact `booley.core.private_store`, `booley.core.file_lock`, `booley.core.resources` | Every top-level `booley.*` package except `booley.core`, `booley.data`, `booley.measurement`, `booley.result_cache` | Shared private storage, locking, and package resources don't own caller policy. |
| D26 | Exact `booley.harness.host_diagnostics`, `booley.harness.setup.readiness`, `booley.runtime.inspection`, `booley.runtime.vaporview`, `booley.flows.target_test_plan_inspection`, `booley.flows.sim.backends.cocotb_target_checks`, `booley.audit.core_tree_inspection`, `booley.runtime.core_repository_hygiene`, `booley.runtime.core_resolution`, `booley.runtime.project_image` | Exact `booley.harness.doctor`, `booley.harness.init_cmd`, `booley.harness.booley`, `booley.presentation.colors`, `booley.harness.setup.common` | Diagnostic owners return complete observations without depending on command orchestration or rendering. |
| D27 | Prefix `booley.targets` | Prefixes `booley.flows`, `booley.runtime` | Target inspection uses shared build identity, not Flow execution or Runtime. |
| D28 | Prefix `booley.fusesoc` | Prefix `booley.runtime` | FuseSoC provenance checks use pure Scope matching, not Runtime or Git execution. |
| D29 | Prefix `booley.bwave` | Prefix `booley.flows` | B-Wave owns reusable waveform code without Flow execution or simulation evidence policy. |
| D31 | Prefix `booley` | Exact `booley.flows.sim.campaign.store`, `booley.flows.sim.campaign.inspection` | Simulation Record storage is private behind the public `booley.flows.sim.campaign` package. Exceptions: C10, C12, C13. |
| D32 | Prefix `booley.runtime` | Prefix `booley.dev_support` | Packaged execution uses commit policy through its shared owner; development commands don't own Runtime behavior. |
| D33 | Prefix `booley.feedback` | Prefix `booley.harness` | Feedback renders reports from resolved environment observations without command code. |
| D34 | Prefix `booley.commit_policy` | Prefixes `booley.runtime`, `booley.harness`, `booley.dev_support` | Shared commit policy stays independent of execution and of script adapters. |
| D35 | Prefix `booley.goals` | Prefixes `booley.harness`, `booley.mcp`, `booley.specialists`, `booley.flows.{sim,synth,fpga,lint}` | MCP and the commands compose Goal Mode; Goal policy doesn't depend on them, on Specialists, or on concrete Flows. |
| D36 | Prefix `booley.runtime` | Prefix `booley.goals` | Runtime takes explicit execution inputs; Goal history and binding belong to higher-level composition. |
| D37 | Prefix `booley.runtime.job_observation` | Prefix `booley.targets` | Job observation reports process and artifact facts without interpreting Targets. |
| D38 | Prefixes `booley.flows`, `booley.specialists` | Prefix `booley.goals` | Evidence producers get Goal presentation and candidate paths through neutral execution interfaces. |
| D39 | Prefix `booley.flows` | Prefix `booley.harness` | Flows run under any composition; the command layer passes human CLI concerns in through the invocation. |
| D40 | Prefix `booley.presentation` | Prefixes `booley.harness`, `booley.mcp`, `booley.runtime`, `booley.goals`, `booley.flows` | Shared presentation primitives sit beneath every composition and execution owner. |
| D41 | Prefix `booley.runtime` | Prefix `booley.flows` | Runtime owns Sandbox and Job mechanisms; it reads Flow progress shape through `booley.core` and never imports a Flow. |
| D42 | Prefixes `booley.flows.{sim,synth,fpga,lint}`, package-files `booley.flows` | Prefix `booley.flows.mutation` | The Mutation Flow composes the Simulation Flow's build and execution by design; the dependency runs one way, so no sibling Flow or Flow-neutral module imports it. |

The D10 adapter sets are exhaustive. Each simulation set (S1-S3) may not
import the other two; each synthesis set (S4-S5) may not import the other.
Code shared by a Flow's backends is unclassified.

- S1, cocotb: exact `booley.flows.sim.backends.cocotb`,
  `booley.flows.sim.backends.cocotb_results`.
- S2, Icarus: exact `booley.flows.sim.backends.icarus`.
- S3, Verilator: exact `booley.flows.sim.backends.verilator`.
- S4, OpenROAD: prefix `booley.flows.synth.backends.openroad`.
- S5, Yosys: prefix `booley.flows.synth.backends.yosys`.

## Where shared code lives

Most rules exist because code both sides need was moved below them. When a
rule blocks an import, put the shared piece in the owner named here.

- **Runtime below Flows, Goals, and commands (D5, D6, D36, D37, D41).**
  Runtime takes explicit inputs, so Sandbox and Job mechanics work under any
  caller. Flow progress shape lives in `booley.core.progress_shape`. Job
  observation (`booley.runtime.job_observation`) is read-only and reads the
  roots its caller passes; the Dashboard's `harness.dashboard.jobs.JobReader`
  picks those roots from Goal Records and adds Goal and Target meaning.
  `runtime.project_repositories` owns repository discovery and bounded Git
  inspection for Goal worktrees and submodules. The MCP Job owners
  (`mcp.job_lifecycle`, `mcp.job_results`, `mcp.endpoint_process`,
  `mcp.report_encoding`) never reach `mcp.server`, so Job behavior doesn't
  depend on the MCP SDK or transport.
- **Config, EDA, and Runtime (D18-D20, D23-D25).** Config parses declarative
  values (`config.eda`, `config.flow_enablement`) and builds nothing. EDA owns
  installation and License Profile registration, Project Grants, and host
  validation (`eda.provisioning.configuration`). Runtime builds the agent
  backend from `AgentSettings` and owns Sandbox Issuance. Both use the neutral
  `core.private_store`, `core.file_lock`, `core.resources`, and
  `core.user_paths`; D25 keeps those free of caller policy. `eda.config`,
  `runtime.file_lock`, and `runtime.paths.package_data_dir` only re-export
  the owners; new code imports the owner.
- **Targets and FuseSoC (D12, D13, D27, D28).** `core.build_paths.work_root_for`
  owns the per-checkout Edalize build directory name, and
  `core.scope_matching` owns pure Scope matching, so neither package needs
  Runtime or Flows. Runtime keeps glob expansion and Git staging. These two
  packages are the one approved cyclic group.
- **Flows (D8-D11, D16, D38).** `booley.evidence` holds the values that cross
  from an evidence producer into Criteria (`fields`, `recipe`, `timing`,
  `acceptance`), so Criteria and Flows both depend downward. Source scanning
  and fingerprints stay in Flows (`flows.source_fingerprint`).
- **Goal Mode (D35, D38).** `flows.execution_persistence` defines the
  recording interface (`AcceptanceRecorder`). `goals.flow_execution` supplies
  the Goal adapter, and `mcp.unbound_recorder` the one for calls outside a
  Goal. MCP (`mcp.call_context`, `mcp.goal_flow_runner`) binds a call to its
  Goal Record and picks the concrete Flow, so Flows never import Goals and
  Goals never import a concrete Flow.
- **Doctor (D26).** Each diagnostic owner takes a request and returns a
  complete report, so Doctor, MCP, and the Dashboard read the same
  observations. The owners, their permitted side effects, and how to add one
  are in [DOCTOR-HEALTH.md](DOCTOR-HEALTH.md#adding-a-diagnostic-owner).
- **B-Wave (D29).** B-Wave owns FST inspection, VCD-to-FST conversion, store
  discovery, cache paths, and its half of FIFO streaming. The Simulation Flow
  owns attempt freshness, stall policy, manifests, publication, and the
  decision that a store is a Trace Artifact.
- **Simulation Record storage (D31).** Callers use the public
  `booley.flows.sim.campaign` package: `authenticate_work_item()` and
  `inspect_retained_campaign()` return immutable results and hide store
  layout, recovery, and storage errors. Only the coordinator and the
  inspection module touch the store. A separate AST check keeps
  `CampaignStore`, `CampaignRecovery`, `WorkItemRecovery`, and
  `SharedBuildRecovery` inside the store, coordinator, and inspection modules,
  and every D31 permission must match a live import.
- **Commit policy and Feedback (D32-D34).** `booley.commit_policy` is the one
  commit-policy implementation every caller uses, the Project Git hooks
  included; `booley.dev_support` keeps only the command and hook adapters. Feedback
  renders reports from an `Environment` value that the commands build, so it
  never imports them.
- **Presentation (D40).** Terminal colors, tables, and the heartbeat sit below
  every owner, so Flows and the Dashboard print the same way without importing
  command code.

## Composition permissions

A permission allows one exact edge under one rule, for a composition root
that has to join two sides. It gives the source no wider exemption.

| Permission | Rule | Exact edge | Reason |
| --- | --- | --- | --- |
| C2 | D4 | `booley.mcp.server -> booley.specialists.specialist` | The MCP server classifies and composes Specialist endpoints. |
| C10 | D31 | `booley.flows.sim.campaign.coordinator -> booley.flows.sim.campaign.store` | Record execution composes durable transaction, recovery, and publication. |
| C12 | D31 | `booley.flows.sim.campaign.inspection -> booley.flows.sim.campaign.store` | The public read capabilities inspect storage through the private store. |
| C13 | D31 | `booley.flows.sim.campaign -> booley.flows.sim.campaign.inspection` | The package facade publishes the storage-backed read capabilities. |

## Dynamic imports

The analyzer can't see these run-time imports, so each one is listed here
with the test that covers it:

| Owner | What it imports | Covered by |
| --- | --- | --- |
| `booley.dev_support.validate_commit_msg` | The packaged `booley.core.run_command`, or a flat copy inside the Project Git-hook bundle. | `tests/dev_support/test_validate_commit_msg.py` |
| `booley.harness.setup.project_git_hook_bundle` | Builds `.booley/.managed/project-git-hooks.pyz` from nine source modules plus a launcher that imports the hook by name. The Project's Git hooks run it with `python -I -S`, so they never import the installed Booley package. | `tests/harness/setup/test_project_git_hook_bundle.py` |
| `booley.mcp.catalog`, `booley.mcp.catalog_worker` | Built-in `booley.mcp.*` endpoint modules, and Project-local MCP files in a separate worker process. | `tests/mcp_tools/test_catalog.py`, `tests/mcp_tools/test_live_catalog.py` |
| `booley.harness.booley` | A built-in MCP endpoint class or a Project-local MCP file, for the diagnostic commands. | `tests/harness/test_booley.py` |
| `booley.harness.dashboard.launch` | The module of a built-in Flow the user starts from the Dashboard. | `tests/harness/test_dashboard_run.py` |
| `booley.mcp.project_flow_loader` | A named Project-local `BooleyFlow` from an explicit file. | `tests/flows/test_transport_contract.py` |

`importlib.metadata`, `importlib.resources`, and `importlib.util.find_spec`
only inspect distributions, resources, or module availability, so they add no
hidden edges. Add a row here for any new run-time import of Booley code.

## Change the contract

Change `tests/architecture/booley_contract.py` and this doc in the same
change, and attach the `compare_report.py` output.

- **Add a rule.** Add a `DirectionRule` with the next number and a reason
  that says why the direction matters, add its row to
  [Direction rules](#direction-rules), and add a seed edge for it to
  `test_every_direction_rule_selector_family_finds_a_forbidden_edge` in
  `tests/architecture/test_booley_contract.py`. Fix any existing import the
  rule catches before landing it.
- **Allow one edge.** Prefer moving the shared piece below both packages. If
  a composition root really must join them, add a `CompositionPermission`
  naming the exact source, target, and rule, and add its row to
  [Composition permissions](#composition-permissions).
- **Accept an existing violation for now.** Add a `LegacyWaiver` with the
  exact edge and the issue that will remove it. The gate fails once the edge
  is gone, so remove the waiver with the fix.
- **Break or add a package cycle.** When a change splits the approved group,
  shrink `APPROVED_LEGACY_SCCS` to the measured groups in the same change. A
  new cycle across packages is a design change: fix the imports instead of
  approving it.
- **Import `booley.goals` or `booley.criteria` from a new module.** Add the
  module and its reason to `_ALLOWED` in
  `tests/architecture/test_goal_mode_boundaries.py`.
