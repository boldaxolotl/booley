"""Diagnostic owner for the ``tests.toml`` and Test Run plan audit (ADR 0072).

Takes a typed request and returns a :class:`DiagnosticReport`; the caller
(Doctor) only renders it. The owner knows nothing of Doctor, CLI orchestration
or rendering.

Permitted effects: reads ``<booley_dir>/tests.toml`` and the Project's
``.core`` files through :class:`TargetCatalog`; no subprocess, writes nothing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.config.project_config import (
    TESTS_SECTION_KEYS,
    lookup_target_section,
    normalize_tests_toml,
)
from booley.config.tests_toml_rules import TEST_NAME_RULES
from booley.flows.simulator_family import normalize_eda_tool
from booley.flows.target_test_plan import TargetTestPlan, build_target_test_plan
from booley.flows.target_test_plan_loading import plusarg_parameter_names
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetInspection
from booley.targets.selection import (
    TargetSectionConflictError,
    section_key_names,
    target_section_key,
)

__all__ = ["TargetTestPlanInspectionRequest", "inspect_target_test_plans"]


@dataclass(frozen=True, slots=True)
class TargetTestPlanInspectionRequest:
    """Inputs of the ``tests.toml`` and Test Run plan audit of one checkout."""

    project_root: Path  # where the .core Targets are enumerated
    booley_dir: Path  # holds tests.toml


def inspect_target_test_plans(request: TargetTestPlanInspectionRequest) -> DiagnosticReport:
    """Validate ``tests.toml`` and check each sim Target can apply what it declares."""
    findings = Findings()
    tests_path = request.booley_dir / "tests.toml"
    if not tests_path.exists():
        findings.skip("tests.toml not present (each sim Target runs its testbench's default test)")
        return findings.report()
    try:
        with tests_path.open("rb") as f:
            raw = tomllib.load(f)
        sections = normalize_tests_toml(raw)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        findings.fail(
            f"tests.toml invalid: {exc}",
            "fix the tests.toml entry named above (a Target section holds only "
            f"{', '.join(TESTS_SECTION_KEYS)}; {TEST_NAME_RULES})",
        )
        return findings.report()
    findings.pass_(
        "tests.toml valid (section keys, Test names, Test Variants, select and seed "
        "templates well-formed)"
    )
    _audit_section_keys(request.project_root, sections, findings)
    _audit_test_run_plans(request.project_root, sections, findings)
    return findings.report()


def _audit_section_keys(project_root: Path, sections: dict[str, Any], findings: Findings) -> None:
    """Check every tests.toml section key names one Target, and no Target has two.

    A key naming no Target is dead config that looks live: the lookup misses,
    no selector reaches the binary, and the testbench runs its *default* test
    to a green PASS. Two keys naming one Target make the choice of section
    ambiguous. Both use the lookup rule the Simulation Flow uses.
    """
    try:
        handles = TargetCatalog.build(project_root).list()
    except FuseSocError:
        return  # enumeration failure is already reported by the structural audit
    if not handles:
        return
    for key in sections:
        if not any(
            section_key_names(key, handle.identity) or key == handle.name for handle in handles
        ):
            findings.fail(
                f"tests.toml section [{key}] names no declared .core Target",
                "fix the section key (a dead key runs the testbench's default "
                "test and reports a false PASS)",
            )
    for handle in handles:
        try:
            target_section_key(sections, handle.identity)
        except TargetSectionConflictError as exc:
            findings.fail(str(exc), "keep one tests.toml section per Target")


def _audit_test_run_plans(
    project_root: Path, sections: dict[str, Any], findings: Findings
) -> None:
    """Check each sim Target can apply what tests.toml declares for it (ADR 0072).

    These are the checks the loader cannot make without the resolved ``.core``
    Target: a Test Variant that overrides a Target plusarg, arguments or a
    ``seed`` template on a Cocotb Target, ``default_seed`` on a Target that
    cannot be seeded. A Target that passes gets one line naming how its Test
    Runs are seeded; "unseeded" is a fact about the Target, not a problem.
    """
    try:
        catalog = TargetCatalog.build(project_root)
        handles = catalog.list(for_flow="sim")
    except FuseSocError:
        return  # enumeration failure is already reported by the structural audit
    for handle in handles:
        try:
            inspection = catalog.inspect(handle)
        except FuseSocError:
            continue  # the resolve check reports a Target that does not inspect
        try:
            section = lookup_target_section(sections, handle.identity)
        except TargetSectionConflictError:
            continue  # the section-key audit reports the duplicate keys once
        try:
            plan = build_target_test_plan(
                handle.selector,
                section,
                adapter=_simulation_adapter(inspection),
                target_plusargs=plusarg_parameter_names(inspection.parameters),
            )
        except ValueError as exc:
            findings.fail(
                f"tests.toml cannot apply to Target '{handle.selector}': {exc}",
                "fix the tests.toml entry named above",
            )
            continue
        findings.pass_(_test_run_plan_line(handle.selector, plan))


def _simulation_adapter(inspection: TargetInspection) -> str:
    """The adapter family of a Target: ``cocotb``, its simulator family, or its raw tool name.

    Deliberately not ``simulation_adapter_kind``: that requires a ``str``
    ``cocotb_module``, while this audit has always treated any truthy value as
    Cocotb. Aligning the two would change Doctor's output.
    """
    if inspection.flow_options.get("cocotb_module"):
        return "cocotb"
    try:
        return normalize_eda_tool(inspection.eda_tool)
    except ValueError:
        return str(inspection.eda_tool or "")


def _test_run_plan_line(selector: str, plan: TargetTestPlan) -> str:
    """One PASS line: how *selector*'s Test Runs are seeded."""
    if not plan.mechanism.seeded:
        return f"Target '{selector}' Test Runs are unseeded ({plan.unseeded_cause()})"
    return (
        f"Target '{selector}' Test Runs are seeded through {plan.mechanism.describe()} "
        f"(Default Seed {plan.default_seed})"
    )
