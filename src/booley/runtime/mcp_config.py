"""Generate agent MCP config with the Booley MCP server entry.

The generated config tells the agent CLI (Codex or Claude) to spawn the
Booley MCP server as a child process (STDIO transport), giving the agent
structured MCP tool definitions instead of relying on CLI syntax in the prompt.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from functools import lru_cache
from types import ModuleType

from booley.config.gateway import GATEWAY_TOKEN_ENV

logger = logging.getLogger(__name__)

HTTP_PORT_ENV = "BOOLEY_MCP_HTTP_PORT"
DEFAULT_HTTP_PORT = 8814
HTTP_ENDPOINT_PATH = "/mcp"


def http_port() -> int:
    """Return the configured loopback HTTP transport port."""
    raw = os.environ.get(HTTP_PORT_ENV, "")
    try:
        port = int(raw)
    except ValueError:
        if raw:
            logger.warning("Ignoring invalid %s=%r", HTTP_PORT_ENV, raw)
        return DEFAULT_HTTP_PORT
    return port if 0 < port < 65536 else DEFAULT_HTTP_PORT


# Codex replaces (not extends) the MCP server's environment when [env]
# is present. Without PATH/HOME the spawned python process can't start.  PATH
# is only a fallback here: generate_codex_config preserves the issued Session
# Runtime's actual PATH so image/project toolchains remain visible to B-tools.
#
# BOOLEY_MCP_NESTED=1 tells the spawned MCP server it is a sub-agent (not
# an outer harness session) and should skip orphan-lock reconciliation.
# Without this, the nested server bogusly reconciles the parent's
# in-flight MCP endpoint start events.
_BASELINE_ENV: dict[str, str] = {
    "PATH": "/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/agent",
    "PYTHONUSERBASE": "/home/agent/.local",
    "BOOLEY_MCP_NESTED": "1",
    "CODEX_MCP_PROTOCOL_VERSION": "2026-07-28",
}

# Env vars forwarded from this process into the Codex-spawned MCP server when
# set (Codex REPLACES the env, so anything not listed here is lost).
# The proxy vars carry the sandbox's sole egress path (booley-proxy) — without
# them every nested agent/API call under the MCP server dies on direct DNS.
_FORWARDED_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _codex_server_env(
    enabled_mcp_tools: list[str] | None,
    extra_env: dict[str, str] | None,
) -> dict[str, str]:
    """Build the complete environment for Codex's Booley MCP child."""
    merged_env = dict(_BASELINE_ENV)
    parent_path = os.environ.get("PATH")
    if parent_path:
        merged_env["PATH"] = parent_path
    for var in _FORWARDED_ENV_VARS:
        val = os.environ.get(var)
        if val:
            merged_env[var] = val
    if extra_env:
        merged_env.update(extra_env)
    if enabled_mcp_tools is not None:
        merged_env["BOOLEY_MCP_TOOLS"] = ",".join(enabled_mcp_tools)
    return merged_env


@lru_cache(maxsize=1)
def _codex_toml_parser() -> ModuleType:
    """Resolve the compatible parser only when Codex config parsing is needed."""
    try:
        import tomli

        probe = "probe = {\n enabled=true,\n}\n"
        if tomli.loads(probe) != {"probe": {"enabled": True}}:
            raise ValueError("Codex TOML capability probe returned an unexpected value")
    except Exception as exc:  # Translate installation/probe failures, not user config.
        raise RuntimeError(
            "Incompatible Booley installation: Codex config requires tomli>=2.4.0 with "
            "TOML1.1; reinstall dependencies or rebuild the compatible base"
        ) from exc
    return tomli


def _warning_preference(existing_config: str | None) -> bool:
    """Recover only the destination's root preference; interrupted scratch heals."""
    if existing_config is None or existing_config == "":
        return True
    parser = _codex_toml_parser()
    try:
        parsed = parser.loads(existing_config)
    except parser.TOMLDecodeError:
        logger.warning(
            "Cannot recover warning preference from malformed scratch Codex config; regenerating"
        )
        return True
    value = parsed.get("suppress_unstable_features_warning", True)
    if not isinstance(value, bool):
        raise ValueError("suppress_unstable_features_warning must be a boolean")
    return value


GATEWAY_PROVIDER_ID = "booley_gateway"


_SPECIALIST_AUTH_TIMEOUT_MS = 20000  # a cold-container import can exceed the 5 s default


def gateway_provider_toml(url: str, token_cwd: str, *, timeout_ms: int) -> str:
    """The ``booley_gateway`` provider tables: URL plus the token helper as ``auth``.

    ``auth.command`` prints the token from Codex's environment, so it is never
    written to disk. ``-P`` stops a Project ``booley/`` directory shadowing us.
    """
    table = f"model_providers.{GATEWAY_PROVIDER_ID}"
    lines = [
        f"[{table}]",
        'name = "Booley gateway"',
        f"base_url = {json.dumps(url)}",
        'wire_api = "responses"',
        "",
        f"[{table}.auth]",
        f"command = {json.dumps(sys.executable)}",
        'args = ["-P", "-m", "booley.runtime.gateway_token"]',
        f"cwd = {json.dumps(token_cwd)}",
        f"timeout_ms = {timeout_ms}",
    ]
    return "\n".join(lines) + "\n"


def _gateway_provider_lines(url: str, token_cwd: str) -> list[str]:
    """Specialist provider tables plus the shell-tool token exclusion."""
    provider = gateway_provider_toml(url, token_cwd, timeout_ms=_SPECIALIST_AUTH_TIMEOUT_MS)
    return [
        "",
        *provider.splitlines(),
        "",
        # Codex 0.160 ignores its default *TOKEN* filter, so exclude explicitly.
        "[shell_environment_policy]",
        f"exclude = [{json.dumps(GATEWAY_TOKEN_ENV)}]",
    ]


def _booley_server_lines(
    enabled_mcp_tools: list[str] | None, extra_env: dict[str, str] | None
) -> list[str]:
    """The ``[mcp_servers.booley]`` table and its ``[...env]`` table."""
    lines = [
        "",
        "[mcp_servers.booley]",
        'command = "python"',
        'args = ["-m", "booley.mcp.server"]',
        # Codex CLI defaults to 120s MCP tool timeout — far too short for
        # long-running Flows or Specialists such as asic_synthesize / mutation_tester
        # (up to 7200s subprocess timeout).
        "tool_timeout_sec = 3600",
        "",
        "[mcp_servers.booley.env]",
    ]
    for key, val in sorted(_codex_server_env(enabled_mcp_tools, extra_env).items()):
        # JSON string escaping is compatible with TOML basic strings and
        # protects Windows paths such as C:\\Users\\... from becoming invalid
        # escape sequences in the generated config.
        lines.append(f"{key} = {json.dumps(val)}")
    return lines


def generate_codex_config(
    enabled_mcp_tools: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    *,
    existing_config: str | None = None,
    gateway_url: str | None = None,
    gateway_cwd: str = "/",
    booley_server: bool = True,
) -> str:
    """Generate config.toml content for the Codex MCP server.

    Args:
        enabled_mcp_tools: If provided, only these MCP tools are exposed
            to the agent. None = all MCP tools available.
        extra_env: Additional env vars to forward to the MCP server.
            Codex replaces (not extends) the MCP process environment
            when [env] is present, so callers must pass through any
            container-level vars (BOOLEY_*, etc.) that MCP endpoints need.
        gateway_url: When set, route model traffic through the gateway at this
            https base URL (``BOOLEY_PRIMARY_AUTH=gateway``).
        gateway_cwd: Working directory of the gateway token command.
        booley_server: False omits the Booley MCP server tables, leaving the
            root keys, features and gateway provider (a run with no tools).

    Returns:
        TOML-formatted string.
    """
    preference = _warning_preference(existing_config)
    lines = [
        f"suppress_unstable_features_warning = {str(preference).lower()}",
        'web_search = "disabled"',
        *([f'model_provider = "{GATEWAY_PROVIDER_ID}"'] if gateway_url else []),
        "",
        "[features]",
        "mcp_2026_07_28 = true",
        *(["fast_mode = false"] if gateway_url else []),
    ]
    if booley_server:
        lines.extend(_booley_server_lines(enabled_mcp_tools, extra_env))

    if gateway_url:
        lines.extend(_gateway_provider_lines(gateway_url, gateway_cwd))

    return "\n".join(lines) + "\n"
