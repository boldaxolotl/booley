"""Booley core layer — foundational, dependency-free types and helpers.

``booley.core`` is the bottom of the dependency graph: it imports nothing from
``booley.harness``, ``booley.dev_support``, or ``booley.ticket_board``. Those upper
layers depend on ``core``; ``core`` never depends on them. Shared data types
that more than one layer needs (agent-call contracts, per-ticket completion
policy) live here so MCP endpoints and ticket_board no longer have to import *upward*
into harness/ — the import cycle that forced ~265 function-local imports.
"""
