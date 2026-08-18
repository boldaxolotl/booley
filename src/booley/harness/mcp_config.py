"""Generate agent MCP config with the Booley MCP server entry.

The generated config tells the agent CLI (Codex or Claude) to spawn the
Booley MCP server as a child process (STDIO transport), giving the agent
structured MCP tool definitions instead of relying on CLI syntax in the prompt.
"""

from __future__ import annotations

import json
import os

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
}

# Env vars forwarded from this process into the Codex-spawned MCP server when
# set (Codex REPLACES the env, so anything not listed here is lost).
# BOOLEY_AGENT_ROLE carries the ADR 0028 admission role (ticket vs
# interactive) from the Developer Agent down to every MCP endpoint subprocess.
# The proxy vars carry the sandbox's sole egress path (booley-proxy) — without
# them every nested agent/API call under the MCP server dies on direct DNS.
_FORWARDED_ENV_VARS = (
    "BOOLEY_AGENT_ROLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


def generate_codex_config(
    enabled_mcp_tools: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Generate config.toml content for the Codex MCP server.

    Args:
        enabled_mcp_tools: If provided, only these MCP tools are exposed
            to the agent. None = all MCP tools available.
        extra_env: Additional env vars to forward to the MCP server.
            Codex replaces (not extends) the MCP process environment
            when [env] is present, so callers must pass through any
            container-level vars (BOOLEY_*, etc.) that MCP endpoints need.

    Returns:
        TOML-formatted string.
    """
    lines = [
        'web_search = "disabled"',
        "",
        "[mcp_servers.booley]",
        'command = "python"',
        'args = ["-m", "booley.mcp_server"]',
        # Codex CLI defaults to 120s MCP tool timeout — far too short for
        # long-running Flows or Specialists such as asic_synthesize / mutation_tester
        # (up to 7200s subprocess timeout).
        "tool_timeout_sec = 3600",
    ]

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

    lines.append("")
    lines.append("[mcp_servers.booley.env]")
    for key, val in sorted(merged_env.items()):
        # JSON string escaping is compatible with TOML basic strings and
        # protects Windows paths such as C:\\Users\\... from becoming invalid
        # escape sequences in the generated config.
        lines.append(f"{key} = {json.dumps(val)}")

    return "\n".join(lines) + "\n"
