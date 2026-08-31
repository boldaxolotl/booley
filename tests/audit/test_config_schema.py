"""Focused regression tests for project configuration schema audits."""

import ast
from pathlib import Path

import pytest

from booley.audit import agent_schema, config_common, configs_schema, flow_schema, project_schema

_ROOT = Path(__file__).resolve().parents[2]


def test_valid_configs_are_normalized_and_returned() -> None:
    audit = configs_schema.audit_configs_toml(
        {
            "test_lists": {"shared": ["smoke", "full"]},
            "fast": {
                "defines": [],
                "top_module": "dut",
                "test_list": "shared",
                "parameters": {
                    "WIDTH": 32,
                    "SECURE": True,
                    "MODE": {"expr": "pkg::Fast"},
                    "INIT": {"string": "rom.mem"},
                },
            },
        }
    )

    assert audit.is_valid
    assert audit.issues == ()
    assert audit.configs is not None
    assert audit.configs["fast"]["tests"] == ["smoke", "full"]


def test_normalization_error_is_an_actionable_issue() -> None:
    audit = configs_schema.audit_configs_toml(
        {
            "test_lists": {"shared": ["smoke"]},
            "fast": {"defines": [], "tests": ["smoke"], "test_list": "shared"},
        }
    )

    assert not audit.is_valid
    assert audit.configs is None
    assert audit.issues == (
        configs_schema.ConfigIssue(
            "configs.toml [fast] must not set both tests and test_list",
            "fix configs.toml",
        ),
    )


def test_section_audit_collects_all_independent_field_issues() -> None:
    audit = configs_schema.audit_configs_toml(
        {
            "bad": {
                "top_module": 3,
                "tb_top": False,
                "tests": "smoke",
                "parameters": "WIDTH=8",
            }
        }
    )

    messages = [issue.message for issue in audit.issues]
    assert not audit.is_valid
    assert messages == [
        "configs.toml [bad].defines must be present as list[str]",
        "configs.toml [bad].top_module must be a string",
        "configs.toml [bad].tb_top must be a string",
        "configs.toml [bad].tests must be list[str]",
        "configs.toml [bad].parameters must be a table",
    ]


def test_parameter_audit_rejects_ambiguous_and_malformed_values() -> None:
    audit = configs_schema.audit_configs_toml(
        {
            "bad": {
                "defines": [],
                "parameters": {
                    "PLAIN": "pkg::Mode",
                    "EMPTY_EXPR": {"expr": ""},
                    "BAD_STRING": {"string": 4},
                    "AMBIGUOUS": {"expr": "A", "string": "B"},
                    "FLOAT": 1.5,
                },
            }
        }
    )

    messages = [issue.message for issue in audit.issues]
    assert any("PLAIN plain strings are not allowed" in message for message in messages)
    assert any("EMPTY_EXPR.expr must be a non-empty string" in message for message in messages)
    assert any("BAD_STRING.string must be a string" in message for message in messages)
    assert any("AMBIGUOUS table must contain exactly one key" in message for message in messages)
    assert any("FLOAT must be bool, int" in message for message in messages)


def test_empty_configs_file_is_invalid() -> None:
    audit = configs_schema.audit_configs_toml({})

    assert not audit.is_valid
    assert audit.issues[0].message == "configs.toml has no config sections"


def test_unknown_tables_have_stable_warning_identity_and_migration_hint() -> None:
    audit = project_schema.audit_known_tables({"fusesoc": {}, "toolz": {}})

    assert audit.is_valid
    assert [finding.check_id for finding in audit.findings] == [
        "config.unknown-table",
        "config.unknown-table",
    ]
    assert [finding.subject for finding in audit.findings] == ["fusesoc", "toolz"]
    assert "ADR 0030" in audit.findings[0].message
    assert "Target scoping now lives in .core files" in audit.findings[0].message
    assert "default_target" not in audit.findings[0].message
    assert "settings are ignored" in audit.findings[1].message


def test_project_metadata_is_advisory_and_preserves_warning_identity() -> None:
    audit = project_schema.audit_project_table(
        {"project": {"name": "  ", "description": "duplicate"}}
    )

    assert audit.is_valid
    assert [finding.severity for finding in audit.findings] == [
        config_common.ConfigFindingSeverity.WARN,
        config_common.ConfigFindingSeverity.WARN,
    ]
    assert {finding.check_id for finding in audit.findings} == {"config.project-metadata"}


def test_valid_project_and_feedback_settings_produce_pass_findings() -> None:
    project = project_schema.audit_project_table({"project": {"name": "demo"}})
    feedback = project_schema.audit_feedback_table(
        {
            "feedback": {
                "mode": "file-only",
                "redact_extra": ["codename"],
                "redact_identifiers": True,
            }
        }
    )

    assert project.findings == (
        config_common.ConfigFinding(
            config_common.ConfigFindingSeverity.PASS,
            "booley.toml [project].name set",
        ),
    )
    assert feedback.is_valid
    assert feedback.findings[0].message.endswith("(mode=file-only)")


def test_feedback_audit_collects_independent_field_failures() -> None:
    audit = project_schema.audit_feedback_table(
        {
            "feedback": {
                "mode": "sometimes",
                "redact_extra": "codename",
                "redact_identifiers": "false",
            }
        }
    )

    assert not audit.is_valid
    assert len(audit.findings) == 3
    assert all(
        finding.severity is config_common.ConfigFindingSeverity.FAIL for finding in audit.findings
    )


def test_stealth_audit_enforces_native_core_isolation_contract() -> None:
    audit = project_schema.audit_stealth_table(
        {"stealth": {"enabled": False, "ignore_native_cores": True}}
    )

    assert not audit.is_valid
    assert audit.findings[0].message == (
        "booley.toml cannot ignore native .core files while stealth mode is disabled"
    )
    assert audit.findings[0].fix == ("set [stealth] enabled = true or ignore_native_cores = false")


def test_retired_structural_controls_have_actionable_failures() -> None:
    audits = (
        project_schema.audit_sandbox_table({"sandbox": {"mode": "docker"}}),
        project_schema.audit_interactive_table({"interactive": {"app": "claude"}}),
        project_schema.audit_developer_table({"developer": {"auto_retry": {"enabled": False}}}),
    )

    assert all(not audit.is_valid for audit in audits)
    assert [audit.findings[0].fix for audit in audits] == [
        "delete [sandbox].mode",
        "delete it and select the runtime with [agent].provider",
        "delete enabled and use max_attempts = 0 to disable",
    ]


def test_retired_project_interactive_policy_names_host_replacement() -> None:
    audit = project_schema.audit_interactive_table(
        {"interactive": {"idle_timeout_seconds": 600, "max_sessions": 2}}
    )
    assert not audit.is_valid
    finding = audit.findings[0]
    assert "~/.config" not in finding.message  # the diagnostic uses the actionable absolute path
    assert ".config/booley/config.toml" in finding.message
    assert "[interactive]\nidle_timeout_seconds = 600\nmax_sessions = 2" in finding.message


def test_agent_audit_uses_authoritative_provider_and_auth_parsers() -> None:
    valid = agent_schema.audit_agent_table(
        {"agent": {"provider": "codex", "auth": "subscription"}}
    )
    retired = agent_schema.audit_agent_table({"agent": {"primary": "claude"}})

    assert valid.is_valid
    assert valid.findings[0].message.endswith("codex (auth: subscription)")
    assert not retired.is_valid
    assert "retired key(s): primary" in retired.findings[0].message


def test_models_audit_preserves_warning_identity_and_pass_details() -> None:
    audit = agent_schema.audit_models_table(
        {
            "models": {
                "heavyy": "typo",
                "heavy": "custom-heavy",
                "roles": {"reviewer": "custom-reviewer"},
            }
        }
    )

    assert audit.is_valid
    assert audit.findings[0].check_id == "config.models-unknown-key"
    assert audit.findings[0].subject == "heavyy"
    assert any("tier overrides: heavy" in item.message for item in audit.findings)
    assert any("reviewer=custom-reviewer" in item.message for item in audit.findings)


def test_flow_audit_collects_shape_failures_and_ignored_knob_warning() -> None:
    audit = flow_schema.audit_flow_table(
        "lint",
        {
            "enabled": "yes",
            "default_target": ["lint_fast"],
            "pre_run_commands": ["make", 4],
            "timeout_ms": 20,
        },
    )

    assert not audit.is_valid
    assert (
        sum(item.severity is config_common.ConfigFindingSeverity.FAIL for item in audit.findings)
        == 3
    )
    warnings = [item for item in audit.findings if item.check_id == "config.flow-knob-ignored"]
    assert {item.subject for item in warnings} == {
        "lint.timeout_ms",
        "lint.pre_run_commands",
    }


def test_flow_collection_reports_required_sections_and_retired_aliases() -> None:
    audit = flow_schema.audit_flow_tables(
        {"flows": {"simulate": {"default_target": "sim_fast"}}},
        ("sim", "lint", "synth"),
    )

    assert not audit.is_valid
    assert any("[flows.simulate] is retired" in item.message for item in audit.findings)
    missing = [item for item in audit.findings if item.check_id == "config.flow-section-missing"]
    assert {item.subject for item in missing} == {"lint", "synth"}


@pytest.mark.parametrize("retired", ["elab", "elaborate"])
def test_flow_collection_reports_retired_elaboration_tables(retired: str) -> None:
    audit = flow_schema.audit_flow_tables(
        {"flows": {retired: {"standalone_frontend": "iverilog"}, "sim": {}}},
        ("sim",),
    )

    assert not audit.is_valid
    finding = next(item for item in audit.findings if f"[flows.{retired}]" in item.message)
    assert "sim --elab-only" in finding.fix
    assert "[flows.sim].standalone_frontend" in finding.fix


def test_sim_accepts_standalone_frontend_and_rejects_removed_cache_knob() -> None:
    valid = flow_schema.audit_flow_table("sim", {"standalone_frontend": "verilator"})
    invalid_frontend = flow_schema.audit_flow_table("sim", {"standalone_frontend": "vcs"})
    removed_cache_knob = flow_schema.audit_flow_table("sim", {"keep_build_dir": True})

    assert valid.is_valid
    assert not invalid_frontend.is_valid
    assert not removed_cache_knob.is_valid


def test_empty_eda_configuration_is_valid() -> None:
    assert project_schema.audit_eda_config({}).is_valid


def test_doctor_does_not_reimplement_config_schema_mechanisms() -> None:
    doctor_path = _ROOT / "src" / "booley" / "harness" / "doctor.py"
    tree = ast.parse(doctor_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    extracted = {
        "_parameter_table_error",
        "_parameter_value_error",
        "_validate_parameters_table",
        "_validate_known_tables",
        "_validate_project_table",
        "_validate_feedback_table",
        "_validate_stealth_table",
        "_validate_sandbox_table",
        "_validate_interactive_table",
        "_validate_developer_table",
    }
    assert not function_names & extracted
    source = doctor_path.read_text(encoding="utf-8")
    assert "agent_schema.audit_agent_table" in source
    assert "_parse_role_models" not in source
    assert "_SELECTIVE_FLOW_KNOBS" not in source


def test_config_schema_domains_do_not_depend_on_presentation_layers() -> None:
    forbidden = ("booley.harness", "booley.mcp", "booley.specialists")
    for name in (
        "agent_schema.py",
        "config_common.py",
        "configs_schema.py",
        "flow_schema.py",
        "project_schema.py",
    ):
        module_path = _ROOT / "src" / "booley" / "audit" / name
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        )
        assert not {
            module for module in imports if any(module.startswith(prefix) for prefix in forbidden)
        }
