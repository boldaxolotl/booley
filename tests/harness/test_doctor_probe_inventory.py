"""Orchestrator probe-inventory ratchet for booley doctor.

``doctor.py`` wires ~70 probe functions into a handful of orchestrators. A
probe silently dropped from an orchestrator's call list keeps its own unit
tests green while never running for a real user — the highest-leverage rot
mode this suite had no answer to. These tests parse ``doctor.py`` with ``ast``
(no doctor code runs) and pin:

1. per-orchestrator inventories: the exact set of ``_check_*`` / ``_run_*`` /
   ``_audit_*`` names each orchestrator references, frozen below. Adding or
   removing a probe from an orchestrator is a deliberate act — update the
   frozen set here in the same change.
2. no orphaned probes: every module-level ``_check_*`` function must stay
   reachable from ``run_doctor`` through the module-level call graph. A probe
   nothing calls is dead weight pretending to be coverage.

These are ratchets, not behavior tests: they fail loudly on wiring drift and
say exactly which name moved.
"""

from __future__ import annotations

import ast
import importlib
import re
from functools import lru_cache
from pathlib import Path

import pytest

from booley.harness import doctor

_DOCTOR_SRC = Path(doctor.__file__)

# Probe naming convention: everything an orchestrator dispatches to is named
# _check_* (single probe), _run_* (sub-orchestrator / grouped probe), or
# _audit_* (config-section audit), or a named public owner entry point.
_PROBE_NAME_RE = re.compile(
    r"^(_check_|_run_|_audit_|inspect_runtime$|inspect_retained_resources$|inspect_host$|load_project$|check_guidance$|check_stealth_cores$|inspect_worktree_portability$|inspect_line_endings$|inspect_interactive_git$|inspect_cocotb_targets$|project_has_cocotb_target$|inspect_target_test_plans$|inspect_core_tree$|inspect_core_security$|inspect_core_repository_hygiene$|inspect_viewer_bridge$|inspect_native_dependencies$|inspect_core_resolution$|inspect_mcp_server$|inspect_flow_runtime_reality$|inspect_vivado_startup$)"
)

# ---------------------------------------------------------------------------
# Frozen inventories — the committed expectation this ratchet checks against.
# Keys are the orchestrators whose call lists this ratchet pins: run_doctor is
# the root; the _run_* entries are the phase groupings it delegates to.
#
# Derived from doctor.py as of 2026-07-26 by the same extractor that the test
# runs (so a green run means "unchanged", not "re-derived"). When you wire a
# probe in or out of an orchestrator ON PURPOSE, update the matching set here.
# ---------------------------------------------------------------------------
EXPECTED_INVENTORY: dict[str, frozenset[str]] = {
    "_run_container_checks": frozenset(
        {
            "_check_container_uid",
            "_check_custom_image_freshness",
            "_check_current_runtime_activity_hooks",
            "_check_current_runtime_dashboard_extension",
            "_check_current_runtime_egress",
            "_check_current_runtime_git_identity",
            "_check_current_runtime_web_isolation",
            "_check_derived_image_freshness",
            "_check_image_bakes_current_booley",
            "_check_image_freshness",
            "_check_riscv_toolchain",
            "_run_container_tool_checks",
        }
    ),
    "_run_container_tool_checks": frozenset(
        {"_check_container_runtime_payload", "project_has_cocotb_target"}
    ),
    "_check_target_metadata": frozenset(
        {
            "_check_doctor_target_compatibility",
            "_check_legacy_core_targets",
            "_check_naming_conventions",
            "_check_synth_recipe_migrations",
            "_check_target_flow_option_defines",
            "_check_yosys_targets_have_arch",
            "_check_sim_traceable",
            "inspect_cocotb_targets",
        }
    ),
    "_run_mcp_checks": frozenset(
        {
            "inspect_runtime",
            "inspect_retained_resources",
            "inspect_interactive_git",
            "inspect_viewer_bridge",
            "_run_agent_credential_checks",
            "inspect_mcp_server",
        }
    ),
    "_run_project_checks": frozenset(
        {
            "_check_goal_history_ignored",
            "_check_agent_backend_health",
            "_check_custom_endpoints_and_criteria",
            "_check_disabled_endpoints",
            "_check_git_state",
            "_check_handle_map_size",
            "_check_project_gitignore",
            "_check_project_trackability",
            "_check_ticket_leftovers",
            "_check_repo_footprint",
        }
    ),
    "_run_deep_checks": frozenset(
        {
            "_run_fpga_impl_deep_notice",
            "_run_selftest_checks",
            "_run_flow_check",
        }
    ),
    "_run_deep_phase": frozenset(
        {
            "_run_core_resolution",
            "_run_deep_checks",
        }
    ),
    "_run_core_resolution": frozenset({"inspect_core_resolution"}),
    "_run_flow_audit": frozenset(
        {
            "_check_design_size",
            "_check_doctor_targets",
            "_check_sim_run_cwd",
            "_run_flow_check",
            "_run_flow_runtime_inspection",
        }
    ),
    "_run_flow_runtime_inspection": frozenset(
        {"inspect_flow_runtime_reality", "inspect_vivado_startup"}
    ),
    "_run_project_phase": frozenset(
        {
            "inspect_host",
            "load_project",
            "check_guidance",
            "check_stealth_cores",
            "inspect_line_endings",
            "_check_project_data_destination_branch",
            "_check_upgrade_review",
            "_check_legacy_source_stashes",
            "_check_goal_presence",
            "_check_worktree_booley_pairing",
            "_check_common_state_in_worktrees",
            "_check_worktree_core_shadow_guard",
            "_check_worktree_prune_guard",
        }
    ),
    "_run_runtime_phase": frozenset(
        {
            "_check_memory_invariant",
            "_check_seat_caps",
            "_check_runtime_location",
            "inspect_worktree_portability",
            "_run_container_checks",
            "_run_mcp_checks",
            "_run_project_checks",
        }
    ),
    "_run_core_audit": frozenset(
        {
            "_check_target_metadata",
            "_check_target_sources",
            "inspect_core_repository_hygiene",
            "inspect_core_security",
            "inspect_core_tree",
            "inspect_native_dependencies",
            "inspect_target_test_plans",
        }
    ),
    "_run_flow_and_core_phase": frozenset(
        {
            "_run_core_audit",
            "_run_flow_audit",
        }
    ),
    "run_doctor_result": frozenset(
        {
            "_run_deep_phase",
            "_run_project_phase",
            "_run_runtime_phase",
            "_run_flow_and_core_phase",
        }
    ),
    "run_doctor": frozenset(),
}

# Module-level _check_* functions that are legitimately NOT probes wired into
# an orchestrator. Currently every _check_* in doctor.py is reachable from
# run_doctor, so this is empty — add a name here ONLY after verifying it is a
# helper by design, not a probe that lost its wiring.
ORPHAN_ALLOWLIST: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _module_functions() -> dict[str, ast.AST]:
    """Parse doctor.py once; return {name: node} for module-level functions."""
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _probe_refs(func_name: str) -> frozenset[str]:
    """Names matching the probe convention referenced anywhere in *func_name*.

    Walks the whole body, so both direct calls and probes passed as callbacks
    (bare ``ast.Name`` in an argument position) are collected. ``Attribute``
    references (``mod._check_x``) count by attribute name. Self-references are
    excluded so a recursive orchestrator does not inventory itself.
    """
    node = _module_functions().get(func_name)
    assert node is not None, (
        f"orchestrator {func_name!r} no longer exists as a module-level "
        f"function in {_DOCTOR_SRC} — update EXPECTED_INVENTORY "
        "after verifying the restructure is deliberate"
    )
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and _PROBE_NAME_RE.match(sub.id) and sub.id != func_name:
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute) and _PROBE_NAME_RE.match(sub.attr):
            names.add(sub.attr)
    return frozenset(names)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("orchestrator", list(EXPECTED_INVENTORY))
def test_orchestrator_probe_inventory(orchestrator: str) -> None:
    actual = _probe_refs(orchestrator)
    expected = EXPECTED_INVENTORY[orchestrator]
    dropped = sorted(expected - actual)
    added = sorted(actual - expected)
    problems = []
    for name in dropped:
        problems.append(f"probe {name} dropped from {orchestrator}")
    for name in added:
        problems.append(f"probe {name} added to {orchestrator}")
    assert actual == expected, (
        "; ".join(problems) + " — update the EXPECTED_INVENTORY in "
        "tests/harness/test_doctor_probe_inventory.py after verifying the "
        "change is deliberate (a dropped probe silently stops running for "
        "every user while its own unit tests stay green)"
    )


def test_every_check_function_is_reachable_from_run_doctor() -> None:
    """Catch fully-orphaned probes.

    Builds the module-level call graph (bare ``Name`` references between
    module functions — how doctor passes and calls its probes) and asserts
    every ``_check_*`` function is reachable from ``run_doctor``. A probe that
    falls out of this set still imports, still passes its unit tests, and
    never runs for anyone.
    """
    funcs = _module_functions()
    graph: dict[str, set[str]] = {}
    for name, node in funcs.items():
        refs = {
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and sub.id in funcs and sub.id != name
        }
        graph[name] = refs

    reachable: set[str] = set()
    stack = ["run_doctor"]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(graph.get(current, ()))

    all_checks = {name for name in funcs if name.startswith("_check_")}
    orphans = sorted(all_checks - reachable - ORPHAN_ALLOWLIST)
    assert not orphans, (
        f"probe(s) {orphans} are module-level _check_* functions no longer "
        "reachable from run_doctor — they will never run for any user. "
        "Re-wire them into an orchestrator, delete them, or (only if they are "
        "helpers by design) add them to ORPHAN_ALLOWLIST after verifying."
    )
    # Ratchet the allowlist itself: entries that became reachable (or were
    # deleted) must be removed so the list never accumulates stale names.
    stale_allowlisted = sorted(
        name for name in ORPHAN_ALLOWLIST if name not in all_checks or name in reachable
    )
    assert not stale_allowlisted, (
        f"ORPHAN_ALLOWLIST entries {stale_allowlisted} are reachable or "
        "deleted — drop them from the allowlist"
    )


def test_doctor_holds_no_cocotb_rules() -> None:
    """The cocotb Target rules and AST name scan live in Flows, not in Doctor."""
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "ast" not in imported_modules, "doctor.py parses Python source again"
    cocotb_functions = sorted(name for name in _module_functions() if "cocotb" in name)
    assert not cocotb_functions, f"cocotb rules moved back into Doctor: {cocotb_functions}"


def test_core_audit_inventory_names_the_core_tree_owner_calls() -> None:
    """The core audit calls the owner; no Doctor check stands in for its operations."""
    refs = _probe_refs("_run_core_audit")
    assert {"inspect_core_tree", "inspect_core_security"} <= refs
    assert not refs & {
        "_check_core_schema",
        "_check_core_setup_hazards",
        "_run_core_security_audit",
    }


def test_doctor_holds_no_core_tree_rules() -> None:
    """The .core schema, setup-hazard, security, ownership and rejection rules live in the owner."""
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    moved = {
        "_owned_core_files",
        "_fusesoc_rejected_cores",
        "_rejected_target_core",
        "_check_core_schema",
        "_core_schema_findings",
        "_fusesoc_core_rejection",
        "_check_core_setup_hazards",
        "_report_recursive_symlink_hazards",
        "_report_recursive_symlink_group",
        "_run_core_security_audit",
        "_CORE_SCHEMA_FIX",
        "_RECURSIVE_SYMLINK_FIX",
        "_SCOPE_DEPENDENT_VIOLATIONS",
    }
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert not moved & defined, (
        f"core-tree rules moved back into Doctor: {sorted(moved & defined)}"
    )


def test_core_audit_inventory_names_the_core_repository_hygiene_owner_call() -> None:
    """The core audit calls the owner; no Doctor check stands in for its rules."""
    refs = _probe_refs("_run_core_audit")
    assert "inspect_core_repository_hygiene" in refs
    assert not refs & {
        "_check_core_repository_hygiene",
        "_check_core_files_tracked",
        "_check_readmemh_targets_tracked",
        "_check_committed_build_artifacts",
    }


def test_doctor_holds_no_core_repository_hygiene_rules() -> None:
    """The three Git-hygiene rules for authored ``.core`` inputs live in the owner."""
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    moved = {
        "_check_core_repository_hygiene",
        "_check_core_files_tracked",
        "_check_readmemh_targets_tracked",
        "_check_committed_build_artifacts",
        "_looks_built_from_sibling_source",
        "_readmem_literal_targets",
        "_BUILD_ARTIFACT_SUFFIXES",
        "_BUILD_SOURCE_SUFFIXES",
        "_BUILD_SOURCE_NAMES",
        "_READMEM_RE",
    }
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert not moved & defined, f"hygiene rules moved back into Doctor: {sorted(moved & defined)}"


def test_deep_phase_inventory_names_the_core_resolution_owner_call() -> None:
    """The deep phase calls the core resolution wrapper, which calls the owner."""
    assert _probe_refs("_run_core_resolution") == {"inspect_core_resolution"}
    refs = _probe_refs("_run_deep_phase")
    assert "_run_core_resolution" in refs
    assert "_run_core_resolve_checks" not in refs


def test_doctor_holds_no_core_resolution_rules() -> None:
    """Core resolution, its Sandbox snippet and the image-run/host fallbacks are gone."""
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    moved = {
        "_run_core_resolve_checks",
        "_resolve_deep_core_targets",
        "_execute_core_resolution",
        "_grade_core_resolution",
        "_report_core_resolve",
        "_core_resolve_payload",
        "_CORE_RESOLVE_SNIPPET",
        "_run_core_resolve_in_docker",
        "_CORE_RESOLVE_TIMEOUT_S",
        "_docker_wrap",
        "_sandbox_guard_failed",
        "_SANDBOX_GUARD_MARKER",
        "_SANDBOX_GUARD_EXIT",
        "_SANDBOX_GUARD_SCRIPT",
        "_OUTPUT_ERROR_MARKERS",
        "_output_excerpt_lines",
    }
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert not moved & defined, (
        f"core resolution rules moved back into Doctor: {sorted(moved & defined)}"
    )
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "resolve_target_handle" not in attributes, "the host-FuseSoC fallback is gone"


def test_doctor_holds_no_mcp_probe_rules() -> None:
    """The MCP server probe, its grading rules and constants live in the owner."""
    moved = {
        "_run_mcp_probe",
        "_sandbox_booley_dir",
        "_mcp_probe_command",
        "_check_mcp_probe_result",
        "_parse_mcp_probe_stdout",
        "_check_mcp_tool_payload",
        "_required_mcp_tools",
        "_advisory_mcp_tools",
        "_BASE_REQUIRED_MCP_TOOLS",
        "_ADVISORY_SPECIALIST_TOOLS",
        "_ADVISORY_FLOW_TOOLS",
        "_MCP_PROBE_TIMEOUT_S",
    }
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert not moved & defined, (
        f"MCP probe rules moved back into Doctor: {sorted(moved & defined)}"
    )
    owner = importlib.import_module("booley.mcp.server_inspection")
    assert callable(owner.inspect_mcp_server)


def test_mcp_checks_inventory_names_the_mcp_server_owner_call() -> None:
    """The MCP checks call the owner; no Doctor probe stands in for it."""
    refs = _probe_refs("_run_mcp_checks")
    assert "inspect_mcp_server" in refs
    assert "_run_mcp_probe" not in refs


def test_flow_audit_inventory_names_the_flow_runtime_owner_calls() -> None:
    """The Flow audit calls the runtime wrapper, which calls both owners; no Doctor probe stands in."""
    assert "_run_flow_runtime_inspection" in _probe_refs("_run_flow_audit")
    assert "_check_flow_runtime_reality" not in _probe_refs("_run_flow_audit")
    assert _probe_refs("_run_flow_runtime_inspection") == {
        "inspect_flow_runtime_reality",
        "inspect_vivado_startup",
    }


def test_doctor_holds_no_flow_runtime_probe_rules() -> None:
    """The Flow runtime-reality and Vivado startup rules live in their owners, not Doctor."""
    moved = {
        "_check_flow_runtime_reality",
        "_runtime_probe_binaries",
        "_check_session_binary",
        "_vivado_startup_command",
        "_check_vivado_startup",
        "_EDA_TOOL_BINARIES",
    }
    tree = ast.parse(_DOCTOR_SRC.read_text(encoding="utf-8"))
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert not moved & defined, (
        f"Flow runtime probe rules moved back into Doctor: {sorted(moved & defined)}"
    )
    reality = importlib.import_module("booley.flows.runtime_reality")
    startup = importlib.import_module("booley.flows.fpga.vivado_startup")
    assert callable(reality.inspect_flow_runtime_reality)
    assert callable(startup.inspect_vivado_startup)
