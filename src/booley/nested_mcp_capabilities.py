"""Per-specialist nested-MCP capability matrix.

When a specialist (e.g. ``tb_coder``, ``coverage_analyst``) invokes its
own Codex sub-agent inside the sandbox container, that sub-agent spawns a
*nested* MCP server. Nested servers must NOT expose every Booley MCP tool — in
particular they must never expose Specialist MCP tools, because that would
let the sub-agent re-invoke its own parent (tb_coder → MCP → tb_coder →
… infinite recursion).

This module is the single source of truth for "which MCP tools does
specialist X want its nested sub-agent to see?". The developer-side
spawn path (``harness._codex_backend._ensure_nested_codex_home``) bakes
the allowlist into ``BOOLEY_NESTED_MCP_TOOLS`` env on the nested Codex
config; the nested MCP server reads that env on startup and filters
MCP-tool discovery accordingly.

Adding a new specialist?
    1. Add an entry below mapping the specialist's ``name`` to the
       MCP tools its nested agent legitimately needs.
    2. Never include another Specialist's MCP-tool name (recursion-safe).
    3. Empty tuple = nested agent gets zero MCP tools (fine — most
       specialists don't need MCP at all from inside their sub-agent).
"""

from __future__ import annotations

# Map: Specialist McpTool.name -> allowlist of MCP-tool names the nested
# sub-agent is allowed to call. Recursion safety invariant: no value in
# this dict contains a specialist's own name (or any other specialist's
# name).
NESTED_MCP_CAPABILITIES: dict[str, tuple[str, ...]] = {
    # Phase-2 virtual_signal_creator needs simulate + bwave_* to test
    # branch conditions. Other Phase-1 sub-agents don't use them but
    # they're harmless to expose at this level.
    "coverage_analyst": (
        "sim",
        "bwave",
    ),
    # Code-modifying specialist runs a pre-submit elaborate check.
    "tb_coder": ("elab",),
    # No MCP needed for these — they reason over text, not the design.
    "reviewer": (),
    "mutation_tester": (),
}


def nested_mcp_tools_for(specialist_name: str) -> list[str]:
    """Return a fresh list of nested-MCP tool names for ``specialist_name``.

    Unknown specialists get an empty list (default-deny). The list is a
    copy — callers may mutate it without affecting the matrix.
    """
    return list(NESTED_MCP_CAPABILITIES.get(specialist_name, ()))
