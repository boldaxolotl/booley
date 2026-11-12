"""Typed audits for agent selection and model configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from booley.audit.config_common import (
    ConfigFinding,
    ConfigTableAudit,
    fail_finding,
    failure,
    pass_finding,
)
from booley.config.agent import (
    RETIRED_MODEL_KEYS,
    SPECIALISTS,
    BackendConfigError,
    parse_agent_section,
    settings_from_data,
)
from booley.config.gateway import GatewayConfigError
from booley.core.boundary import as_dict


def audit_agent_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit the Agent CLI set, default, and authentication selection."""
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
            "use only [agent].clis/default/auth",
        )
    try:
        declared = parse_agent_section(agent)
        settings_from_data({k: v for k, v in data.items() if k != "models"}, lazy=False)
    except GatewayConfigError:
        pass  # the credential-policy check reports the bad gateway variable
    except BackendConfigError as exc:
        return failure(
            str(exc),
            'set [agent] clis/default to "claude" or "codex"; auth to "auto", '
            '"subscription", "api_key" (or "gateway" for Codex), alone or per CLI in [agent.auth]',
        )
    if declared.default is None and declared.auth is None:
        return ConfigTableAudit()
    clis = f"clis: {', '.join(declared.clis)}; " if declared.clis else ""
    detail = f"; auth: {declared.auth}" if declared.auth else ""
    return ConfigTableAudit(
        (pass_finding(f"booley.toml [agent] {clis}default: {declared.default}{detail}"),)
    )


def _models_table_problem(models: dict[str, Any] | None) -> ConfigTableAudit | None:
    """Fail a ``[models]`` that is not a table or names a key that is not a Specialist."""
    if models is None:
        return failure(
            "booley.toml [models] must be a table",
            "rewrite [models] as a TOML table",
        )
    if unknown := sorted(set(models) - SPECIALISTS - RETIRED_MODEL_KEYS):
        valid = ", ".join(sorted(SPECIALISTS))
        keys = ", ".join(f"[models] {key}" for key in unknown)
        return failure(
            # Same text as the loader's error (parse_models_section): Doctor and loading agree.
            f"booley.toml [models] has an unknown key {unknown[0]!r}; valid keys are {valid}; "
            f"delete [models] {unknown[0]} or rename it to a valid key.",
            f"delete {keys}; valid keys are {valid}",
        )
    return None


def audit_models_table(data: Mapping[str, Any]) -> ConfigTableAudit:
    """Audit the per-Specialist model pins against the configured Agent CLIs."""
    raw_models = data.get("models")
    if raw_models is None:
        return ConfigTableAudit()
    models = as_dict(raw_models)
    if (problem := _models_table_problem(models)) is not None:
        return problem
    try:
        settings = settings_from_data(data, lazy=False)
    except GatewayConfigError:
        return ConfigTableAudit()  # the credential-policy check reports the bad gateway variable
    except BackendConfigError as exc:
        if "agent" in data and _agent_section_fails(data):
            return ConfigTableAudit()  # the [agent] audit reports it
        return ConfigTableAudit(
            (
                fail_finding(
                    str(exc),
                    "pin each Specialist with a claude-* or gpt-* model ID, and list its "
                    "Agent CLI in [agent] clis",
                ),
            )
        )
    findings: list[ConfigFinding] = []
    if settings.specialist_models:
        pins = ", ".join(
            f"{name}={model if isinstance(model, str) else '[' + ', '.join(model) + ']'}"
            for name, model in sorted(settings.specialist_models.items())
        )
        findings.append(pass_finding(f"booley.toml [models] pins: {pins}"))
    return ConfigTableAudit(tuple(findings))


def _agent_section_fails(data: Mapping[str, Any]) -> bool:
    """Whether the Project's Agent CLI selection is invalid even without ``[models]``."""
    try:
        settings_from_data({k: v for k, v in data.items() if k != "models"}, lazy=False)
    except BackendConfigError:
        return True
    except GatewayConfigError:
        return False
    return False
