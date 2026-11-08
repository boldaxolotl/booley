"""Shared builder for tests that need a devcontainer spec without a host credential scan."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.runtime import auth_token
from booley.runtime import devcontainer as dc


def build_spec(
    app: str = dc.APP_NONE,
    *,
    login_source: str | None = None,
    token_source: str | None = None,
    config_source: str | None = None,
    credentials: Mapping[str, str] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Build a spec through one host-independent ``auto`` credential plan for *app*.

    The plan is constructed directly: ``auth_token.credential_plan`` reads the host
    environment and credential files, which would add ``${localEnv:}`` keys and seed
    mounts a test did not ask for. A login or token source rides in only when given,
    and *credentials* are the plan's forwarded environment values. The plan's
    ``config_seed`` stays unset: the builder reads only ``config_source``.
    """
    plan = auth_token.CredentialPlan(
        app,
        "auto",
        None,
        auth_token.allowed_credential_keys(app, "auto"),
        tuple(sorted((credentials or {}).items())),
        subscription_seed=Path(login_source) if login_source else None,
        token_seed=Path(token_source) if token_source else None,
    )
    seeds = dc.CliSeeds(plan, login_source, token_source, config_source)
    return dc.build_devcontainer_spec(app, cli_seeds=[seeds], **options)
