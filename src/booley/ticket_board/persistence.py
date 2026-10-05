"""Crash-safe persistence primitives for Ticket control-plane records.

The primitives live in :mod:`booley.runtime.atomic_files` so non-Ticket record
owners can use them without importing Ticket Board; they are re-exported here
so existing ``from booley.ticket_board.persistence import X`` callers keep
working unchanged.
"""

from __future__ import annotations

from booley.runtime.atomic_files import (
    WriteOnceConflictError,
    atomic_replace_bytes,
    atomic_write_once,
    durable_unlink,
)

__all__ = [
    "WriteOnceConflictError",
    "atomic_replace_bytes",
    "atomic_write_once",
    "durable_unlink",
]
