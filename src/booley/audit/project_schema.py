"""Typed audits for project-owned ``booley.toml`` tables."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from booley.audit.config_common import (
    ConfigFinding,
    ConfigFindingSeverity,
    ConfigTableAudit,
    fail_finding,
    failure,
)
from booley.core.boundary import BoundaryError, as_dict, as_str, is_str_list, require_bool

KNOWN_BOOLEY_TOML_TABLES = frozenset(
    {
        "project",
        "flows",
        "mcp_tools",
        "sandbox",
        "agent",
        "models",
        "jobs",
        "interactive",
        "notifications",
        "feedback",
        "sources",
        "developer",
        "eda",
        "stealth",
        "submodules",
    }
)

RETIRED_BOOLEY_TOML_TABLES = {
    "tools": (
        "retired — move deterministic settings to [flows.*] and Specialist or "
        "other non-Flow endpoint settings to [mcp_tools.*]"
    ),
    "fusesoc": "removed in ADR 0030 — Target scoping now lives in .core files",
}


def audit_known_tables(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Warn about top-level tables that no live Booley consumer recognizes."""
    findings: list[ConfigFinding] = []
    for key in data:
        if key in KNOWN_BOOLEY_TOML_TABLES:
            continue
        hint = RETIRED_BOOLEY_TOML_TABLES.get(key)
        if hint:
            message = f"booley.toml [{key}] is no longer used — {hint}"
        else:
            message = (
                f"booley.toml has an unrecognized top-level table/key [{key}] — "
                "likely a typo or stale config; its settings are ignored"
            )
        findings.append(
            ConfigFinding(
                ConfigFindingSeverity.WARN,
                message,
                check_id="config.unknown-table",
                subject=key,
            )
        )
    return ConfigTableAudit(tuple(findings))


def audit_project_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit optional project metadata without making it authoritative."""
    project = as_dict(data.get("project"))
    if project is None:
        return ConfigTableAudit((_project_warning("booley.toml missing [project] table"),))
    findings: list[ConfigFinding] = []
    name = as_str(project.get("name"))
    if name is not None and name.strip():
        findings.append(
            ConfigFinding(ConfigFindingSeverity.PASS, "booley.toml [project].name set")
        )
    else:
        findings.append(_project_warning("booley.toml [project].name missing or empty"))
    if "description" in project:
        findings.append(
            _project_warning(
                "booley.toml [project].description is unused; keep the description "
                "in the .core and delete this duplicate"
            )
        )
    return ConfigTableAudit(tuple(findings))


def _project_warning(message: str) -> ConfigFinding:
    return ConfigFinding(
        ConfigFindingSeverity.WARN,
        message,
        check_id="config.project-metadata",
    )


def audit_feedback_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit the live feedback disclosure and redaction settings."""
    raw_feedback = data.get("feedback")
    if raw_feedback is None:
        return ConfigTableAudit()
    feedback = as_dict(raw_feedback)
    if feedback is None:
        return failure(
            "booley.toml [feedback] must be a table",
            "rewrite [feedback] as a TOML table",
        )
    findings = _feedback_field_findings(feedback)
    if findings:
        return ConfigTableAudit(tuple(findings))
    mode = as_str(feedback.get("mode"), "ask")
    return ConfigTableAudit(
        (
            ConfigFinding(
                ConfigFindingSeverity.PASS,
                f"booley.toml [feedback] settings valid (mode={mode})",
            ),
        )
    )


def _feedback_field_findings(feedback: Mapping[str, Any]) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    raw_mode = feedback.get("mode", "ask")
    mode = as_str(raw_mode)
    if mode not in {"ask", "email", "file-only", "off"}:
        findings.append(
            fail_finding(
                f"booley.toml [feedback].mode is invalid: {raw_mode!r}",
                "use one of: ask, email, file-only, off",
            )
        )
    extra = feedback.get("redact_extra")
    if extra is not None and not is_str_list(extra):
        findings.append(
            fail_finding(
                "booley.toml [feedback].redact_extra must be a list of strings",
                'use e.g. redact_extra = ["codename"]',
            )
        )
    if "redact_identifiers" in feedback and not _has_valid_bool(feedback, "redact_identifiers"):
        findings.append(
            fail_finding(
                "booley.toml [feedback].redact_identifiers must be a boolean",
                "use true or false",
            )
        )
    return findings


def audit_stealth_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit native-core isolation's explicit stealth-only contract."""
    raw_stealth = data.get("stealth")
    if raw_stealth is None:
        return ConfigTableAudit()
    stealth = as_dict(raw_stealth)
    if stealth is None:
        return failure(
            "booley.toml [stealth] must be a table",
            "rewrite [stealth] as a TOML table",
        )
    if "ignore_native_cores" not in stealth:
        return ConfigTableAudit()
    try:
        ignore_native = require_bool(stealth, "ignore_native_cores")
    except BoundaryError:
        return failure(
            "booley.toml [stealth].ignore_native_cores must be a boolean",
            "use true or false",
        )
    if ignore_native and not _has_enabled_stealth(stealth):
        return failure(
            "booley.toml cannot ignore native .core files while stealth mode is disabled",
            "set [stealth] enabled = true or ignore_native_cores = false",
        )
    return ConfigTableAudit()


def _has_enabled_stealth(stealth: Mapping[str, Any]) -> bool:
    try:
        return require_bool(stealth, "enabled")
    except BoundaryError:
        return False


def _has_valid_bool(section: Mapping[str, Any], key: str) -> bool:
    try:
        require_bool(section, key)
    except BoundaryError:
        return False
    return True


def audit_sandbox_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit the surviving Session Runtime sandbox table."""
    raw_sandbox = data.get("sandbox")
    if raw_sandbox is None:
        return ConfigTableAudit()
    sandbox = as_dict(raw_sandbox)
    if sandbox is None:
        return failure(
            "booley.toml [sandbox] must be a table",
            "rewrite [sandbox] as a TOML table",
        )
    if "mode" in sandbox:
        return failure(
            "booley.toml [sandbox].mode is retired; the Session Runtime is always Docker",
            "delete [sandbox].mode",
        )
    return ConfigTableAudit()


def audit_interactive_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Reject policy that moved from Project config to host config."""
    raw = data.get("interactive")
    if raw is None:
        return ConfigTableAudit()
    interactive = as_dict(raw)
    if interactive is None:
        return failure(
            "booley.toml [interactive] must be a table",
            "delete it or move host policy to ~/.config/booley/config.toml",
        )
    from booley.config.host_config import retired_project_policy_message

    migration = retired_project_policy_message(data)
    if migration:
        return failure(migration, migration)
    if "app" not in interactive:
        return ConfigTableAudit()
    return failure(
        "booley.toml [interactive].app is retired",
        "delete it and select the runtime with [agent].provider",
    )


def audit_developer_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Keep auto-retry's disable control single-valued."""
    developer = as_dict(data.get("developer"))
    if developer is None:
        return ConfigTableAudit()
    auto_retry = as_dict(developer.get("auto_retry"))
    if auto_retry is None or "enabled" not in auto_retry:
        return ConfigTableAudit()
    return failure(
        "booley.toml [developer.auto_retry].enabled is retired",
        "delete enabled and use max_attempts = 0 to disable",
    )


def audit_eda_config(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit project EDA configuration using the authoritative parser."""
    from booley.eda.config import (
        EdaConfigError,
        parse_eda_config,
        retired_config_error,
        validate_host_provisioning_platform,
    )

    migration = retired_config_error(data)
    if migration:
        return failure(
            f"booley.toml {migration}",
            "remove retired commercial-EDA authority keys",
        )
    try:
        eda_configs = parse_eda_config(data.get("eda"))
        validate_host_provisioning_platform(eda_configs)
    except EdaConfigError as exc:
        return failure(
            f"booley.toml {exc}",
            "fix [eda] provisioning configuration",
        )
    return ConfigTableAudit()
