"""Closed trace recipes shared by Simulation orchestration and run-halves."""

from __future__ import annotations

from enum import StrEnum


class TraceMode(StrEnum):
    """One coherent build/run recipe for a traced simulation."""

    VCD_FIFO = "vcd_fifo"
    NATIVE_FST = "native_fst"
