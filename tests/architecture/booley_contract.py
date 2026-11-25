"""The executable form of Booley's approved source dependency contract.

The contract keeps low-level packages reusable and keeps composition in a few entry points
(the MCP server and the `booley` commands), so a change to an agent-facing surface cannot ripple
down into Sandbox, Config, or Flow code. The rules below list forbidden directions only. An
import that no rule covers is unclassified: neither endorsed nor forbidden, and its presence
never earns it a permission. When this file and any prose disagree, this file is right.

The analyzer (`import_graph.py`) counts every import in `src/booley`, including deferred imports
inside functions and `if TYPE_CHECKING:` blocks. It cannot see run-time imports (`importlib`,
generated hook bundles), so those need their own test.

Where shared code lives. Most rules exist because code both sides need was moved below them. When
a rule blocks an import, put the shared piece in the owner named here, not behind a permission.

- Runtime below Flows, Goals, and commands (D5, D6, D36, D37, D41): Runtime takes explicit inputs
  so Sandbox and Job mechanics work under any caller. Flow progress shape lives in
  `booley.core.progress_shape`. Job observation (`runtime.job_observation`) is read-only and reads
  the roots its caller passes; `harness.dashboard.jobs.JobReader` picks those roots and adds Goal
  and Target meaning. `runtime.project_repositories` owns repository discovery and bounded Git
  inspection. The MCP Job owners (`mcp.job_lifecycle`, `mcp.job_results`, `mcp.endpoint_process`,
  `mcp.report_encoding`) never reach `mcp.server`, so Job behavior does not depend on the MCP SDK.
- Config, EDA, and Runtime (D18-D20, D23-D25): Config parses declarative values and builds nothing.
  EDA owns installation, License Profile registration, Project Grants, and host validation.
  Runtime builds the agent backend from `AgentSettings` and owns Sandbox Issuance. Both use the
  neutral `core.private_store`, `core.file_lock`, `core.resources`, and `core.user_paths`, which
  D25 keeps free of caller policy. `eda.config`, `runtime.file_lock`, and
  `runtime.paths.package_data_dir` only re-export; new code imports the owner.
- Targets and FuseSoC (D12, D13, D27, D28): `core.build_paths.work_root_for` owns the Edalize
  build directory name and `core.scope_matching` owns pure Scope matching, so neither package
  needs Runtime or Flows. Runtime keeps glob expansion and Git staging. These two packages are the
  one approved cyclic group.
- Flows (D8-D11, D16, D38): `booley.evidence` holds the values that cross from an evidence
  producer into Criteria, so both depend downward. Source scanning and fingerprints stay in Flows
  (`flows.source_fingerprint`).
- Doctor (D26): each diagnostic owner takes a request and returns a complete report, so Doctor,
  MCP, and the Dashboard read the same observations. See the comment at the D26 rule for how to
  add an owner.
- B-Wave (D29): B-Wave owns FST inspection, VCD-to-FST conversion, store discovery, cache paths,
  and its half of FIFO streaming. The Simulation Flow owns attempt freshness, stall policy,
  manifests, publication, and the decision that a store is a Trace Artifact.
- Simulation Record storage (D31): callers use the public `booley.flows.sim.record` package
  (`authenticate_work_item()`, `inspect_retained_simulation_record()`), which returns immutable
  results and hides store layout, recovery, and storage errors. Only the coordinator and the
  inspection module touch the store; an AST check keeps `SimulationRecordStore`,
  `SimulationRecordRecovery`, `WorkItemRecovery`, and `SharedBuildRecovery` inside them.
- Commit policy and Feedback (D32-D34): `booley.commit_policy` is the one commit-policy
  implementation, Project Git hooks included; `booley.dev_support` keeps only command and hook
  adapters. Feedback renders reports from an `Environment` value the commands build, so it never
  imports them.
- Presentation (D40): terminal colors, tables, and the heartbeat sit below every owner.

Changing the contract. Change this file in the same change, and attach the output of
`python3 tests/architecture/compare_report.py --before-ref <base> --after-ref <head> --repo-root .`
to a PR that moves a boundary.

- Add a rule: add a `DirectionRule` with the next free number and a reason, and a seed edge in
  `test_every_direction_rule_selector_family_finds_a_forbidden_edge`. Fix existing imports the
  rule catches first.
- Allow one edge: prefer moving the shared piece below both packages. Only a composition root
  that really must join them gets a `CompositionPermission` naming source, target, and rule.
- Accept an existing violation for now: add a `LegacyWaiver` with the exact edge and the issue
  that removes it; remove it with the fix.
- Cycles: when a change splits the approved group, shrink `APPROVED_LEGACY_SCCS` in the same
  change. Never approve a new cycle across packages; fix the imports.
"""

from __future__ import annotations

from tests.architecture.contract import (
    ArchitectureContract,
    CompositionPermission,
    DirectionRule,
    ModuleSelector,
)

exact = ModuleSelector.exact
prefix = ModuleSelector.prefix
package_files = ModuleSelector.package_files


_D1_REASON = (
    "environment/configuration analysis and Target policy must not know Harness, MCP, "
    "or Specialist mechanisms"
)
_D3_REASON = (
    "a Specialist returns evidence without depending on its Harness or MCP composition mechanism"
)
_D4_REASON = "MCP infrastructure is independent of capabilities composed by its server"
_D5_REASON = "Sandbox mechanisms must remain usable without agent-facing mechanisms"
_D6_REASON = "shared Sandbox mechanisms must not acquire Harness knowledge"
_D7_REASON = "shared Target/Criteria policy is independent of presentation and agent exposure"
_D8_REASON = (
    "each built-in Booley Flow owns its tool-specific implementation and cannot couple "
    "to a sibling Flow"
)
_D42_REASON = (
    "the Mutation Flow composes the Simulation Flow's build and execution by design; the "
    "dependency runs one way, so no sibling Flow or Flow-neutral module imports it"
)
_D9_REASON = (
    "Flow-neutral policy and evidence modules cannot select a concrete Flow implementation"
)
_D10_REASON = "an EDA adapter satisfies its Flow's internal seam without knowing a sibling adapter"
_D11_REASON = "leaf synthesis adapters do not orchestrate their Flow or one another"
_D12_REASON = (
    "Target domain values and selector policy stay independent of FuseSoC, concrete "
    "Flows, catalog orchestration, and presentation; FuseSoC adapters do not depend "
    "back on catalog orchestration or presentation"
)
_D13_REASON = "FuseSoC mechanics remain reusable beneath concrete Flow implementations"
_D21_REASON = "Review renders artifacts from resolved evidence without Harness orchestration"

_FLOW_PREFIXES = tuple(prefix(f"booley.flows.{name}") for name in ("sim", "synth", "fpga", "lint"))
_D8_RULES = tuple(
    DirectionRule(
        "D8", (source,), tuple(target for target in _FLOW_PREFIXES if target != source), _D8_REASON
    )
    for source in _FLOW_PREFIXES
)

_COCOTB = (
    exact("booley.flows.sim.backends.cocotb"),
    exact("booley.flows.sim.backends.cocotb_results"),
)
_ICARUS = (exact("booley.flows.sim.backends.icarus"),)
_VERILATOR = (exact("booley.flows.sim.backends.verilator"),)
_D10_SIM_RULES = tuple(
    DirectionRule(
        "D10",
        source,
        tuple(
            selector
            for index, group in enumerate((_COCOTB, _ICARUS, _VERILATOR))
            if index != source_index
            for selector in group
        ),
        _D10_REASON,
    )
    for source_index, source in enumerate((_COCOTB, _ICARUS, _VERILATOR))
)

DIRECTION_RULES = (
    DirectionRule(
        "D40",
        (prefix("booley.presentation"),),
        (
            prefix("booley.harness"),
            prefix("booley.mcp"),
            prefix("booley.runtime"),
            prefix("booley.flows"),
        ),
        "shared presentation primitives stay beneath every composition and execution owner",
    ),
    DirectionRule(
        "D39",
        (prefix("booley.flows"),),
        (prefix("booley.harness"),),
        "Flows run under any composition; the Harness command layer supplies human CLI "
        "concerns through the invocation, never by Flow import",
    ),
    DirectionRule(
        "D37",
        (prefix("booley.runtime.job_observation"),),
        (prefix("booley.targets"),),
        "Job observation owns process and artifact facts without interpreting Target policy",
    ),
    DirectionRule(
        "D41",
        (prefix("booley.runtime"),),
        (prefix("booley.flows"),),
        "Runtime owns Sandbox and Job mechanisms; it reads Flow progress shape through "
        "booley.core and never imports a Flow",
    ),
    DirectionRule(
        "D34",
        (prefix("booley.commit_policy"),),
        (prefix("booley.dev_support"), prefix("booley.harness"), prefix("booley.runtime")),
        "shared commit policy remains independent of scripts and execution composition",
    ),
    DirectionRule(
        "D33",
        (prefix("booley.feedback"),),
        (prefix("booley.harness"),),
        "Feedback consumes resolved observations without command or Ticket Mode composition",
    ),
    DirectionRule(
        "D32",
        (prefix("booley.runtime"),),
        (prefix("booley.dev_support"),),
        "packaged execution consumes commit policy through its shared owner",
    ),
    DirectionRule(
        "D31",
        (prefix("booley"),),
        (exact("booley.flows.sim.record.store"), exact("booley.flows.sim.record.inspection")),
        "Simulation Record durable storage is private implementation behind the "
        "public Simulation Record interface",
    ),
    DirectionRule(
        "D29",
        (prefix("booley.bwave"),),
        (prefix("booley.flows"),),
        "B-Wave owns reusable waveform mechanics without Flow execution or evidence policy",
    ),
    DirectionRule(
        "D27",
        (prefix("booley.targets"),),
        (prefix("booley.flows"), prefix("booley.runtime")),
        "Target inspection uses shared build identity without Flow execution or Runtime",
    ),
    DirectionRule(
        "D28",
        (prefix("booley.fusesoc"),),
        (prefix("booley.runtime"),),
        "FuseSoC provenance consumes pure Scope matching without Runtime or Git execution",
    ),
    DirectionRule(
        "D23",
        (prefix("booley.config"),),
        (prefix("booley.eda"),),
        "Config owns declarative requests without depending on EDA provisioning policy",
    ),
    DirectionRule(
        "D24",
        (prefix("booley.eda"),),
        (prefix("booley.runtime"),),
        "EDA uses neutral host mechanisms without depending on Runtime execution or issuance",
    ),
    DirectionRule(
        "D25",
        tuple(
            exact(f"booley.core.{name}") for name in ("private_store", "file_lock", "resources")
        ),
        tuple(
            prefix(f"booley.{name}")
            for name in (
                "agent_workspace",
                "audit",
                "bwave",
                "commit_policy",
                "config",
                "criteria",
                "dev_support",
                "docker",
                "eda",
                "evidence",
                "feedback",
                "flows",
                "fusesoc",
                "harness",
                "mcp",
                "presentation",
                "projects",
                "review",
                "runtime",
                "specialists",
                "targets",
            )
        ),
        "Shared private storage, locking, and package resources do not own caller policy",
    ),
    # Adding a Doctor diagnostic owner (or moving a check out of `harness.doctor`):
    # - The owner takes a typed, frozen request and returns a `DiagnosticReport` built with
    #   `audit.diagnostic_results.Findings`; every WARN carries a `check_id` (plus `subject` and
    #   `dedupe` where the check had them). It accepts no Doctor reporting callbacks (a Sandbox
    #   command builder in the request is not one), takes raw inputs, and decides ownership and
    #   severity itself. It never repairs and never issues a Runtime.
    # - List its module below, and add a row for it (one forbidden target) to the table in
    #   `test_booley_contract.py::test_every_direction_rule_selector_family_finds_a_forbidden_edge`.
    #   `test_source_dependency_contract.py::test_every_d26_diagnostic_owner_names_an_importable_module`
    #   fails when a listed owner stops existing. Single-fact helpers (`audit.host_environment`,
    #   `runtime.host_docker`, `audit.output_excerpt`) and the value module
    #   `audit.diagnostic_results` stay outside D26.
    # - Doctor builds the request, calls the owner, and renders each report with
    #   `_Reporter.diagnostics` at the check's position. A moved check keeps its message, fix
    #   text, severity, check ID, waiver behavior, order, and exit code. An owner exposes one
    #   operation per position at which its checks render.
    # - Tests assert on the returned report and patch no private Doctor name.
    # - Rewriting a moved check's reporting calls into `Findings`, text unchanged, is a
    #   mechanical move under principle 18 of CODING_PRINCIPLES.md; the PR body states moved and
    #   new line counts. Sibling moves edit this same list, so land them one
    #   at a time, ordered by a `Blocked by` edge.
    DirectionRule(
        "D26",
        (
            exact("booley.harness.host_diagnostics"),
            exact("booley.harness.setup.readiness"),
            exact("booley.runtime.inspection"),
            exact("booley.flows.sim.backends.cocotb_target_checks"),
            exact("booley.runtime.vaporview"),
            exact("booley.flows.target_test_plan_inspection"),
            exact("booley.audit.core_tree_inspection"),
            exact("booley.runtime.core_repository_hygiene"),
            exact("booley.runtime.core_resolution"),
            exact("booley.runtime.project_image"),
            exact("booley.mcp.server_inspection"),
            exact("booley.flows.runtime_reality"),
            exact("booley.flows.fpga.vivado_startup"),
        ),
        (
            exact("booley.harness.doctor"),
            exact("booley.harness.init_cmd"),
            exact("booley.harness.booley"),
            exact("booley.presentation.colors"),
            exact("booley.harness.setup.common"),
        ),
        "diagnostic owners return observations without command orchestration or rendering",
    ),
    DirectionRule("D21", (prefix("booley.review"),), (prefix("booley.harness"),), _D21_REASON),
    DirectionRule(
        "D20",
        (prefix("booley.eda"),),
        (exact("booley.runtime.session_issuance"), exact("booley.runtime.issuance_invalidation")),
        "EDA supplies provisioning facts without knowing Runtime issuance or invalidation",
    ),
    DirectionRule(
        "D19",
        (prefix("booley.eda"),),
        (prefix("booley.flows"),),
        "EDA provisioning consumes declarative enablement without depending on Flow execution",
    ),
    DirectionRule(
        "D18",
        (prefix("booley.config"),),
        (prefix("booley.runtime"),),
        "configuration returns validated values and does not own Runtime construction or setup",
    ),
    DirectionRule(
        "D15",
        (prefix("booley.flows"),),
        (prefix("booley.mcp"),),
        "Deterministic Flow execution and its shared services do not depend on MCP exposure.",
    ),
    DirectionRule(
        "D1",
        tuple(
            prefix(name)
            for name in ("booley.audit", "booley.config", "booley.fusesoc", "booley.targets")
        ),
        tuple(prefix(name) for name in ("booley.harness", "booley.mcp", "booley.specialists")),
        _D1_REASON,
    ),
    DirectionRule(
        "D3",
        (prefix("booley.specialists"),),
        (prefix("booley.harness"), exact("booley.mcp.server")),
        _D3_REASON,
    ),
    DirectionRule(
        "D4",
        (prefix("booley.mcp"),),
        (prefix("booley.harness"), prefix("booley.specialists")),
        _D4_REASON,
    ),
    DirectionRule(
        "D5",
        (prefix("booley.runtime"),),
        (prefix("booley.mcp"), prefix("booley.specialists")),
        _D5_REASON,
    ),
    DirectionRule("D6", (prefix("booley.runtime"),), (prefix("booley.harness"),), _D6_REASON),
    DirectionRule(
        "D7",
        tuple(
            exact(name)
            for name in ("booley.flows.target_campaign", "booley.flows.target_test_suite")
        ),
        tuple(prefix(name) for name in ("booley.harness", "booley.mcp")),
        _D7_REASON,
    ),
    *_D8_RULES,
    DirectionRule("D9", (package_files("booley.flows"),), _FLOW_PREFIXES, _D9_REASON),
    DirectionRule(
        "D42",
        (*_FLOW_PREFIXES, package_files("booley.flows")),
        (prefix("booley.flows.mutation"),),
        _D42_REASON,
    ),
    *_D10_SIM_RULES,
    DirectionRule(
        "D10",
        (prefix("booley.flows.synth.backends.openroad"),),
        (prefix("booley.flows.synth.backends.yosys"),),
        _D10_REASON,
    ),
    DirectionRule(
        "D10",
        (prefix("booley.flows.synth.backends.yosys"),),
        (prefix("booley.flows.synth.backends.openroad"),),
        _D10_REASON,
    ),
    DirectionRule(
        "D11",
        (prefix("booley.flows.synth.backends.openroad"),),
        (exact("booley.flows.synth.flow"), prefix("booley.flows.synth.backends.yosys")),
        _D11_REASON,
    ),
    DirectionRule(
        "D11",
        (prefix("booley.flows.synth.backends.yosys"),),
        (exact("booley.flows.synth.flow"), prefix("booley.flows.synth.backends.openroad")),
        _D11_REASON,
    ),
    DirectionRule(
        "D12",
        (exact("booley.targets.domain"), exact("booley.targets.selection")),
        (
            prefix("booley.fusesoc"),
            *_FLOW_PREFIXES,
            exact("booley.targets.catalog"),
            exact("booley.targets.target_surface"),
        ),
        _D12_REASON,
    ),
    DirectionRule(
        "D12",
        (prefix("booley.fusesoc"),),
        tuple(exact(name) for name in ("booley.targets.catalog", "booley.targets.target_surface")),
        _D12_REASON,
    ),
    DirectionRule("D13", (prefix("booley.fusesoc"),), _FLOW_PREFIXES, _D13_REASON),
)

COMPOSITION_PERMISSIONS = (
    CompositionPermission(
        "C10",
        "D31",
        "booley.flows.sim.record.coordinator",
        "booley.flows.sim.record.store",
        "Simulation Record execution composes durable transaction, recovery, and publication.",
    ),
    CompositionPermission(
        "C12",
        "D31",
        "booley.flows.sim.record.inspection",
        "booley.flows.sim.record.store",
        "Public read capabilities inspect storage through the private Simulation Record store.",
    ),
    CompositionPermission(
        "C13",
        "D31",
        "booley.flows.sim.record",
        "booley.flows.sim.record.inspection",
        "The Simulation Record package facade publishes the storage-backed read capabilities.",
    ),
    CompositionPermission(
        "C2",
        "D4",
        "booley.mcp.server",
        "booley.specialists.specialist",
        "The MCP server classifies and composes Specialist endpoints.",
    ),
)

LEGACY_WAIVERS = ()

# Measured after #3061 removed the Runtime edges into Flows and Harness, which
# dissolved the last large group once Flows no longer imported Harness. The
# production SCC metadata test requires tightening on every split.
APPROVED_LEGACY_SCCS = (frozenset(("booley.fusesoc", "booley.targets")),)

BOOLEY_SOURCE_DEPENDENCY_CONTRACT = ArchitectureContract(
    rules=DIRECTION_RULES,
    permissions=COMPOSITION_PERMISSIONS,
    waivers=LEGACY_WAIVERS,
    approved_sccs=APPROVED_LEGACY_SCCS,
)
