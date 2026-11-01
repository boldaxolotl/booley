"""Environment-selected gateway policy for Codex Specialists (ADR 0080).

``BOOLEY_PRIMARY_AUTH=gateway`` routes Codex traffic through an operator-run
gateway. URL and token come from the Sandbox environment and are never
persisted. This module imports nothing from ``config/agent.py`` (no cycle).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

GATEWAY_AUTH = "gateway"
GATEWAY_URL_ENV = "BOOLEY_GATEWAY_URL"
GATEWAY_TOKEN_ENV = "BOOLEY_GATEWAY_TOKEN"
GATEWAY_TOKEN_COMMAND_ENV = "BOOLEY_GATEWAY_TOKEN_COMMAND"


class GatewayConfigError(ValueError):
    """The gateway policy is selected but its environment is incomplete or unsafe."""


@dataclass(frozen=True)
class GatewaySettings:
    """A validated gateway endpoint and exactly one token source.

    Neither the token nor the command (it may embed a secret argument) appears
    in ``repr``.
    """

    url: str
    token: str | None = field(default=None, repr=False)
    token_command: str | None = field(default=None, repr=False)

    @property
    def token_source(self) -> str:
        """The environment variable that supplies the token, for status text."""
        return GATEWAY_TOKEN_COMMAND_ENV if self.token_command else GATEWAY_TOKEN_ENV


def _validated_url(raw: str) -> str:
    """Return *raw* when it is a plain https URL (no userinfo, query or fragment); never echo it."""
    message = f"{GATEWAY_URL_ENV} must be an https:// URL with a host and no credentials"
    if any(character.isspace() or not character.isprintable() for character in raw):
        raise GatewayConfigError(message)
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
    except ValueError as exc:
        raise GatewayConfigError(message) from exc
    if (
        parts.scheme != "https"
        or not hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise GatewayConfigError(message)
    return raw


def _validated_token_command(raw: str) -> str:
    """Return *raw* when it splits into at least one word; never echo it."""
    try:
        words = shlex.split(raw)
    except ValueError:
        words = []
    if not words:
        raise GatewayConfigError(
            f"{GATEWAY_TOKEN_COMMAND_ENV} must be a command that prints the gateway token"
        )
    return raw


def gateway_settings_from_env(environ: Mapping[str, str] = os.environ) -> GatewaySettings:
    """Validate ``BOOLEY_GATEWAY_URL`` and one token source.

    The token source is ``BOOLEY_GATEWAY_TOKEN`` or ``BOOLEY_GATEWAY_TOKEN_COMMAND``,
    exactly one of them; a blank value counts as unset. Raises
    :class:`GatewayConfigError` naming the missing or invalid variable. Values
    are stripped; neither the token nor the command is included in any message.
    """
    url = (environ.get(GATEWAY_URL_ENV) or "").strip()
    if not url:
        raise GatewayConfigError(
            f"{GATEWAY_AUTH!r} auth (BOOLEY_PRIMARY_AUTH=gateway) needs {GATEWAY_URL_ENV} "
            "(the gateway's https:// base URL)"
        )
    url = _validated_url(url)
    token = (environ.get(GATEWAY_TOKEN_ENV) or "").strip()
    command = (environ.get(GATEWAY_TOKEN_COMMAND_ENV) or "").strip()
    prefix = f"{GATEWAY_AUTH!r} auth (BOOLEY_PRIMARY_AUTH=gateway)"
    if token and command:
        raise GatewayConfigError(
            f"{prefix}: set exactly one of {GATEWAY_TOKEN_ENV} and {GATEWAY_TOKEN_COMMAND_ENV}"
        )
    if not token and not command:
        raise GatewayConfigError(
            f"{prefix} needs {GATEWAY_TOKEN_ENV} or {GATEWAY_TOKEN_COMMAND_ENV}"
        )
    if command:
        return GatewaySettings(url=url, token_command=_validated_token_command(command))
    return GatewaySettings(url=url, token=token)


GATEWAY_MODEL_ENV = "BOOLEY_GATEWAY_MODEL"
GATEWAY_EFFORT_ENV = "BOOLEY_GATEWAY_EFFORT"
# Named variants of Codex's ``ReasoningEffort`` (``model_reasoning_effort``).
# Source: codex-rs/protocol/src/openai_models.rs at tag rust-v0.160.0, the
# Codex release the Sandbox pins in ``data/docker/agent-clis-package.json``.
# Codex also parses any other non-empty string as a model-defined custom
# effort; Booley accepts only the named variants, because the value reaches a
# ``-c`` override. Whether the gateway's model supports a level is the
# gateway's to reject.
CODEX_EFFORT_LEVELS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "persistent",
)


@dataclass(frozen=True)
class GatewayModelPin:
    """Optional model id and Codex reasoning effort that override every tier and role."""

    model: str | None = None
    effort: str | None = None


def gateway_model_pin_from_env(environ: Mapping[str, str] = os.environ) -> GatewayModelPin:
    """Validate ``BOOLEY_GATEWAY_MODEL`` and ``BOOLEY_GATEWAY_EFFORT`` (both optional).

    Blank means unset. The effort is lower-cased and must be a Codex level (it
    reaches a ``-c model_reasoning_effort="..."`` override). The model id must
    have no whitespace or non-printable character. Raises
    :class:`GatewayConfigError` naming the variable, never echoing its value.
    """
    model = (environ.get(GATEWAY_MODEL_ENV) or "").strip() or None
    if model and any(c.isspace() or not c.isprintable() for c in model):
        raise GatewayConfigError(
            f"{GATEWAY_MODEL_ENV} must be a model id without whitespace or control characters"
        )
    effort = (environ.get(GATEWAY_EFFORT_ENV) or "").strip().lower() or None
    if effort and effort not in CODEX_EFFORT_LEVELS:
        raise GatewayConfigError(
            f"{GATEWAY_EFFORT_ENV} must be one of {', '.join(CODEX_EFFORT_LEVELS)}"
        )
    return GatewayModelPin(model=model, effort=effort)
