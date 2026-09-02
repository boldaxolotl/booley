"""The executable form of Booley's approved source dependency contract."""

from __future__ import annotations

from tests.architecture.contract import (
    ArchitectureContract,
    CompositionPermission,
    DirectionRule,
    LegacyWaiver,
    ModuleSelector,
)

exact = ModuleSelector.exact
prefix = ModuleSelector.prefix
package_files = ModuleSelector.package_files


_D1_REASON = (
    "environment/configuration analysis and Target policy must not know Harness, MCP, "
    "or Specialist mechanisms"
)
_D2_REASON = "Criteria is acceptance policy; endpoint discovery is an agent-facing mechanism"
_D3_REASON = (
    "a Specialist returns evidence without depending on its Harness or MCP composition mechanism"
)
_D4_REASON = "MCP infrastructure is independent of capabilities composed by its server"
_D5_REASON = "Session Runtime mechanisms must remain usable without agent-facing mechanisms"
_D6_REASON = "shared Session Runtime mechanisms must not acquire Harness knowledge"
_D7_REASON = (
    "shared Target/Criteria policy is independent of presentation, agent exposure, and "
    "Ticket Board persistence"
)
_D8_REASON = (
    "each built-in Booley Flow owns its tool-specific implementation and cannot couple "
    "to a sibling Flow"
)
_D9_REASON = (
    "Flow-neutral policy and evidence modules cannot select a concrete Flow implementation"
)
_D10_REASON = "an EDA adapter satisfies its Flow's internal seam without knowing a sibling adapter"
_D11_REASON = "leaf synthesis adapters do not orchestrate their Flow or one another"

_FLOW_PREFIXES = tuple(prefix(f"booley.flows.{name}") for name in ("sim", "synth", "fpga", "lint"))
_D8_RULES = tuple(
    DirectionRule(
        "D8",
        (source,),
        tuple(target for target in _FLOW_PREFIXES if target != source),
        _D8_REASON,
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
        "D1",
        tuple(
            prefix(name)
            for name in ("booley.audit", "booley.config", "booley.fusesoc", "booley.targets")
        ),
        tuple(prefix(name) for name in ("booley.harness", "booley.mcp", "booley.specialists")),
        _D1_REASON,
    ),
    DirectionRule(
        "D2",
        (prefix("booley.criteria"),),
        tuple(prefix(name) for name in ("booley.harness", "booley.mcp", "booley.specialists")),
        _D2_REASON,
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
    DirectionRule(
        "D6",
        (prefix("booley.runtime"),),
        (prefix("booley.harness"),),
        _D6_REASON,
    ),
    DirectionRule(
        "D7",
        tuple(
            exact(name)
            for name in (
                "booley.flows.target_campaign",
                "booley.flows.target_criteria",
                "booley.flows.target_test_suite",
            )
        ),
        tuple(prefix(name) for name in ("booley.harness", "booley.mcp", "booley.ticket_board")),
        _D7_REASON,
    ),
    *_D8_RULES,
    DirectionRule("D9", (package_files("booley.flows"),), _FLOW_PREFIXES, _D9_REASON),
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
        (
            exact("booley.flows.synth.flow"),
            prefix("booley.flows.synth.backends.yosys"),
        ),
        _D11_REASON,
    ),
    DirectionRule(
        "D11",
        (prefix("booley.flows.synth.backends.yosys"),),
        (
            exact("booley.flows.synth.flow"),
            prefix("booley.flows.synth.backends.openroad"),
        ),
        _D11_REASON,
    ),
)

COMPOSITION_PERMISSIONS = (
    CompositionPermission(
        "C1",
        "D4",
        "booley.mcp.server",
        "booley.harness.auto_doctor",
        "The MCP server composes the Doctor endpoint at the agent-facing entry point.",
    ),
    CompositionPermission(
        "C2",
        "D4",
        "booley.mcp.server",
        "booley.specialists.specialist",
        "The MCP server classifies and composes Specialist endpoints.",
    ),
    CompositionPermission(
        "C3",
        "D6",
        "booley.runtime.heartbeat",
        "booley.harness.colors",
        "The heartbeat command composes terminal presentation at its executable entry point.",
    ),
    CompositionPermission(
        "C4",
        "D6",
        "booley.runtime.heartbeat",
        "booley.harness.terminal",
        "The heartbeat command composes terminal lifecycle at its executable entry point.",
    ),
    CompositionPermission(
        "C5",
        "D6",
        "booley.runtime.incontainer_register",
        "booley.harness.auto_doctor",
        "In-container registration composes its Doctor command entry point.",
    ),
    CompositionPermission(
        "C6",
        "D6",
        "booley.runtime.incontainer_register",
        "booley.harness.upgrade_cli",
        "In-container registration composes upgrade commands.",
    ),
    CompositionPermission(
        "C7",
        "D6",
        "booley.runtime.incontainer_register",
        "booley.harness.upgrade_review",
        "In-container registration composes upgrade-review commands.",
    ),
)

LEGACY_WAIVERS = (
    LegacyWaiver(
        "W1",
        "D2",
        "booley.criteria.actions",
        "booley.mcp.registry",
        "Invocation rendering currently discovers the endpoint-to-Criterion relationship "
        "from MCP registration; this is legacy mechanism knowledge, not desired policy direction.",
        "#284",
    ),
    LegacyWaiver(
        "W2",
        "D2",
        "booley.criteria.reference",
        "booley.mcp.registry",
        "Generated Criteria reference text currently discovers producing endpoints through "
        "the MCP registry.",
        "#284",
    ),
)

APPROVED_LEGACY_SCCS = (
    frozenset(
        f"booley.{name}"
        for name in (
            "agent_workspace",
            "audit",
            "bwave",
            "config",
            "criteria",
            "dev_support",
            "eda",
            "feedback",
            "flows",
            "fusesoc",
            "harness",
            "mcp",
            "projects",
            "review",
            "runtime",
            "specialists",
            "targets",
            "ticket_board",
        )
    ),
)

BOOLEY_SOURCE_DEPENDENCY_CONTRACT = ArchitectureContract(
    rules=DIRECTION_RULES,
    permissions=COMPOSITION_PERMISSIONS,
    waivers=LEGACY_WAIVERS,
    approved_sccs=APPROVED_LEGACY_SCCS,
)
