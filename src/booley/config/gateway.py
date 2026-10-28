"""Environment-selected gateway policy for Codex Specialists (ADR 0080).

``BOOLEY_PRIMARY_AUTH=gateway`` routes Codex traffic through an operator-run
gateway. URL and token come from the Sandbox environment and are never
persisted. This module imports nothing from ``config/agent.py`` (no cycle).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

GATEWAY_AUTH = "gateway"
GATEWAY_URL_ENV = "BOOLEY_GATEWAY_URL"
GATEWAY_TOKEN_ENV = "BOOLEY_GATEWAY_TOKEN"


class GatewayConfigError(ValueError):
    """The gateway policy is selected but its environment is incomplete or unsafe."""


@dataclass(frozen=True)
class GatewaySettings:
    """A validated gateway endpoint; the token never appears in ``repr``."""

    url: str
    token: str = field(repr=False)


def _validated_url(raw: str) -> str:
    """Return *raw* when it is a plain https URL; never echo it in an error."""
    message = f"{GATEWAY_URL_ENV} must be an https:// URL with a host and no credentials"
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise GatewayConfigError(message)
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
    except ValueError as exc:
        raise GatewayConfigError(message) from exc
    if parts.scheme != "https" or not hostname or "@" in parts.netloc:
        raise GatewayConfigError(message)
    return raw


def gateway_settings_from_env(environ: Mapping[str, str] = os.environ) -> GatewaySettings:
    """Validate ``BOOLEY_GATEWAY_URL`` and ``BOOLEY_GATEWAY_TOKEN``.

    Raises :class:`GatewayConfigError` naming the missing or invalid variable.
    Values are stripped; the token is never included in any message.
    """
    url = (environ.get(GATEWAY_URL_ENV) or "").strip()
    if not url:
        raise GatewayConfigError(
            f"{GATEWAY_AUTH!r} auth (BOOLEY_PRIMARY_AUTH=gateway) needs {GATEWAY_URL_ENV} "
            "(the gateway's https:// base URL)"
        )
    url = _validated_url(url)
    token = (environ.get(GATEWAY_TOKEN_ENV) or "").strip()
    if not token:
        raise GatewayConfigError(
            f"{GATEWAY_AUTH!r} auth (BOOLEY_PRIMARY_AUTH=gateway) needs {GATEWAY_TOKEN_ENV}"
        )
    return GatewaySettings(url=url, token=token)
