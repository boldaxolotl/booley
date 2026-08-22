"""Typed audits for agent selection and model configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from booley.audit.config_common import (
    ConfigFinding,
    ConfigTableAudit,
    fail_finding,
    failure,
    pass_finding,
    warn_finding,
)
from booley.config.agent import (
    KNOWN_ROLES,
    MODEL_TIERS,
    BackendConfigError,
    parse_auth,
    parse_provider,
    parse_role_models,
)
from booley.core.boundary import as_dict, as_str


def audit_agent_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit the runtime provider and authentication selection."""
    raw_agent = data.get("agent")
    if raw_agent is None:
        return ConfigTableAudit()
    agent = as_dict(raw_agent)
    if agent is None:
        return failure(
            "booley.toml [agent] must be a table",
            "rewrite [agent] as a TOML table",
        )
    retired = sorted(set(agent) & {"primary", "primary_auth", "secondary", "secondary_auth"})
    if retired:
        return failure(
            f"booley.toml [agent] uses retired key(s): {', '.join(retired)}",
            "use only [agent].provider/auth",
        )
    return _audit_agent_selection(agent)


def _audit_agent_selection(agent: Mapping[str, Any]) -> ConfigTableAudit:
    try:
        provider = parse_provider(agent)
    except BackendConfigError as exc:
        return failure(str(exc), 'set [agent] provider = "claude" or "codex"')
    try:
        auth = parse_auth(agent)
    except BackendConfigError as exc:
        return failure(
            str(exc),
            'set [agent] auth = "auto", "subscription", or "api_key"',
        )
    if not provider:
        return ConfigTableAudit()
    detail = f" (auth: {auth})" if auth else ""
    return ConfigTableAudit((pass_finding(f"booley.toml [agent] provider: {provider}{detail}"),))


def audit_models_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit model-tier overrides and per-role model pins."""
    raw_models = data.get("models")
    if raw_models is None:
        return ConfigTableAudit()
    models = as_dict(raw_models)
    if models is None:
        return failure(
            "booley.toml [models] must be a table",
            "rewrite [models] as a TOML table",
        )
    findings = _unknown_model_key_findings(models, MODEL_TIERS)
    try:
        role_models = parse_role_models(models)
    except BackendConfigError as exc:
        findings.append(fail_finding(str(exc), f"use one of: {', '.join(sorted(KNOWN_ROLES))}"))
        return ConfigTableAudit(tuple(findings))
    findings.extend(_model_pass_findings(models, MODEL_TIERS, role_models))
    return ConfigTableAudit(tuple(findings))


def _unknown_model_key_findings(
    models: Mapping[str, Any],
    tiers: Sequence[str],
) -> list[ConfigFinding]:
    return [
        warn_finding(
            f"booley.toml [models] has an unrecognized key {key!r} — expected a tier "
            f"({'/'.join(tiers)}) or the [models.roles] table; it is ignored",
            "config.models-unknown-key",
            subject=key,
        )
        for key in models
        if key not in tiers and key != "roles"
    ]


def _model_pass_findings(
    models: Mapping[str, Any],
    tiers: Sequence[str],
    role_models: Mapping[str, str],
) -> list[ConfigFinding]:
    findings: list[ConfigFinding] = []
    configured_tiers = [tier for tier in tiers if as_str(models.get(tier)) is not None]
    if configured_tiers:
        findings.append(
            pass_finding(f"booley.toml [models] tier overrides: {', '.join(configured_tiers)}")
        )
    if role_models:
        pins = ", ".join(f"{role}={model}" for role, model in sorted(role_models.items()))
        findings.append(pass_finding(f"booley.toml [models.roles] pins: {pins}"))
    return findings
