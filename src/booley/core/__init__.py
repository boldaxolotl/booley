"""Booley core layer — foundational, dependency-free types and helpers.

``booley.core`` is the bottom of the dependency graph: it imports nothing from
``booley.harness`` or ``booley.dev_support``. Those upper
layers depend on ``core``; ``core`` never depends on them. Shared data types
that more than one layer needs (agent-call contracts, completion
policy) live here so MCP endpoints and evidence consumers can use the same contracts without importing command entry points.
"""
