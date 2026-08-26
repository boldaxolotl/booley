"""Typed audits for Booley Flow configuration tables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from booley.audit.config_common import (
    ConfigFinding,
    ConfigTableAudit,
    fail_finding,
    failure,
    warn_finding,
)
from booley.core.boundary import BoundaryError, as_dict, is_str_list, require_bool
from booley.targets.flow_names import (
    DEFAULT_TARGET_KEY,
    LEGACY_TO_CANONICAL,
    RETIRED_TARGET_KEY,
    canonical,
    config_section,
)

SELECTIVE_FLOW_KNOBS = {
    "timeout_ms": frozenset({"sim", "synth", "fpga"}),
    "pre_run_commands": frozenset({"sim"}),
    "sim_time_grace_s": frozenset({"sim"}),
    "keep_build_dir": frozenset({"elab"}),
    "fail_on_timing_violation": frozenset({"synth"}),
    "warnings_as_errors": frozenset({"lint"}),
    "run_cwd": frozenset({"sim"}),
    "trace_args": frozenset({"sim"}),
    "trace_files": frozenset({"sim"}),
    "output_dir": frozenset({"sim", "synth"}),
    "expected_latches": frozenset({"synth"}),
}

_MOVED_TARGET_RECIPE_KEYS = frozenset(
    {
        "base_defines",
        "advanced_settings_openroad",
        "advanced_settings_yosys",
        "flatten",
        "frontend",
        "openroad",
        "out_of_context",
        "part",
        "ppa_profile",
        "sdc",
        "slang_options",
        "strategy",
        "synth_mode",
        "timing",
        "timing_engine",
        "yosys",
    }
)


def audit_flow_tables(
    data: Mapping[str, Any],
    required_flows: Sequence[str],
) -> ConfigTableAudit:
    """Audit all configured Flow tables and retired Flow configuration."""
    if data.get("tools") is not None:
        return failure(
            "booley.toml [tools] is retired",
            "move deterministic settings to [flows.*], move Specialist or other "
            "non-Flow endpoint settings to [mcp_tools.*], and use enabled = false "
            "for opt-outs",
        )
    flows = as_dict(data.get("flows", {}))
    if flows is None:
        return failure(
            "booley.toml [flows] must be a table",
            "rewrite [flows] as a TOML table",
        )
    findings = _retired_flow_findings(flows)
    findings.extend(_required_flow_findings(flows, required_flows))
    findings.extend(_additional_flow_findings(flows, required_flows))
    return ConfigTableAudit(tuple(findings))


def _retired_flow_findings(flows: Mapping[str, Any]) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    for retired in ("builtin", "custom"):
        if retired in flows:
            findings.append(
                fail_finding(
                    f"booley.toml [flows].{retired} is retired",
                    "delete the allowlist and use [flows.<name>].enabled = false for opt-outs",
                )
            )
    for old, new in LEGACY_TO_CANONICAL.items():
        if old in flows:
            findings.append(
                fail_finding(
                    f"booley.toml [flows.{old}] is retired",
                    f"rename it to [flows.{new}]",
                )
            )
    return findings


def _required_flow_findings(
    flows: Mapping[str, Any],
    required_flows: Sequence[str],
) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    for flow_name in required_flows:
        section = config_section(flows, flow_name)
        section_present = flow_name in flows or any(
            old in flows and new == flow_name for old, new in LEGACY_TO_CANONICAL.items()
        )
        if not section_present:
            findings.append(
                warn_finding(
                    f"booley.toml [flows.{flow_name}] missing; using built-in defaults",
                    "config.flow-section-missing",
                    subject=flow_name,
                )
            )
        else:
            findings.extend(audit_flow_table(flow_name, section).findings)
    return findings


def _additional_flow_findings(
    flows: Mapping[str, Any],
    required_flows: Sequence[str],
) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    validated = set(required_flows)
    for raw_name, section in flows.items():
        if raw_name in {"builtin", "custom"} or raw_name in LEGACY_TO_CANONICAL:
            continue
        flow_name = canonical(raw_name)
        if flow_name in validated:
            continue
        findings.extend(audit_flow_table(flow_name, section).findings)
        validated.add(flow_name)
    return findings


def audit_flow_table(flow_name: str, section: Any) -> ConfigTableAudit:
    """Audit one canonical Flow table without rendering its findings."""
    section_table = as_dict(section)
    if section_table is None:
        return failure(
            f"booley.toml [flows.{flow_name}] must be a table",
            f"fix [flows.{flow_name}]",
        )
    findings = _retired_flow_key_findings(flow_name, section_table)
    findings.extend(_target_recipe_findings(flow_name, section_table))
    findings.extend(_flow_shape_findings(flow_name, section_table))
    findings.extend(_ignored_flow_knob_findings(flow_name, section_table))
    return ConfigTableAudit(tuple(findings))


def _retired_flow_key_findings(
    flow_name: str,
    section: Mapping[str, Any],
) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    if "selftest" in section:
        findings.append(
            fail_finding(
                f"booley.toml [flows.{flow_name}.selftest] is retired",
                "delete the table; Doctor now discovers simulation's bad-overlay "
                "and lint's lint_selftest_bad Target by convention",
            )
        )
    if "sandbox" in section:
        findings.append(
            fail_finding(
                f"booley.toml [flows.{flow_name}].sandbox is retired",
                "delete sandbox; all Flows run in the Session Runtime",
            )
        )
    for retired in (RETIRED_TARGET_KEY, DEFAULT_TARGET_KEY, "calibration_target"):
        if retired not in section:
            continue
        findings.append(
            fail_finding(
                f"booley.toml [flows.{flow_name}].{retired} is retired",
                "delete it; Flow calls require an explicit target, and Doctor selection "
                "lives in each .core Target's flow_options.booley.doctor list",
            )
        )
    return findings


def _target_recipe_findings(
    flow_name: str,
    section: Mapping[str, Any],
) -> list[ConfigFinding]:
    if flow_name not in {"synth", "fpga"}:
        return []
    moved = sorted(set(section) & _MOVED_TARGET_RECIPE_KEYS)
    if not moved:
        return []
    return [
        fail_finding(
            f"booley.toml [flows.{flow_name}] contains Target build input(s): {', '.join(moved)}",
            "move them to the selected .core Target's flow_options "
            "(and express defines as vlogdefine parameters)",
        )
    ]


def _flow_shape_findings(
    flow_name: str,
    section: Mapping[str, Any],
) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    if "enabled" in section:
        try:
            require_bool(section, "enabled")
        except BoundaryError:
            findings.append(
                fail_finding(
                    f"booley.toml [flows.{flow_name}].enabled must be a bool "
                    "(true/false, no quotes)",
                    f"fix [flows.{flow_name}].enabled",
                )
            )
    pre_run = section.get("pre_run_commands")
    if pre_run is not None and not is_str_list(pre_run):
        findings.append(
            fail_finding(
                f"booley.toml [flows.{flow_name}].pre_run_commands must be a "
                "list of strings (shell lines run before each sim run)",
                f"fix [flows.{flow_name}].pre_run_commands",
            )
        )
    return findings


def _ignored_flow_knob_findings(
    flow_name: str,
    section: Mapping[str, Any],
) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    for knob, readers in SELECTIVE_FLOW_KNOBS.items():
        if knob not in section or flow_name in readers:
            continue
        findings.append(
            warn_finding(
                f"booley.toml [flows.{flow_name}].{knob} is set but {flow_name} "
                f"ignores it (only {', '.join(sorted(readers))} read {knob}); it has no effect",
                "config.flow-knob-ignored",
                subject=f"{flow_name}.{knob}",
            )
        )
    return findings
