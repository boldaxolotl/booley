"""What a Simulation Record durably observed, as its consumers read it (ADR 0072).

A record that was cancelled or killed never returns an outcome, yet every
work item that finished left an authenticated result behind. A consumer that
accounts for those results may not depend on a concrete Flow: these are the
flow-neutral values it is handed, and the reader the composing entry point
supplies. The Simulation Flow implements the reader
(:func:`booley.flows.sim.record.read_durable_results`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DurableSimulationRecordResults",
    "OlderBooleyRecordError",
    "SimulationRecordBusyError",
    "SimulationRecordResultsReader",
]


class SimulationRecordBusyError(RuntimeError):
    """A scheduler owns the Simulation Record: its results are still being written."""


class OlderBooleyRecordError(ValueError):
    """A record an older Booley wrote: intact, only too old.

    Never report it as unreadable or corrupt. It lets a consumer name the refusal
    without importing the Flow that raises it.
    """


@dataclass(frozen=True)
class DurableSimulationRecordResults:
    """The terminal results one Simulation Record holds on disk.

    ``results`` names every work item with a terminal result and that
    result's digest; ``rows`` are the Test Run rows of their observations
    (``name``, ``fingerprint`` and ``grade`` at least), empty for a record
    that names no Test Runs. ``complete`` says every work item has a terminal
    result: nothing more can come out of the record.
    """

    manifest_sha256: str
    simulation_record_id: str
    results: tuple[tuple[str, str], ...]
    rows: tuple[Mapping[str, object], ...]
    complete: bool = False


#: Authenticate and return the terminal results of the record whose
#: manifest is at the path and has the given digest. Raises
#: :class:`SimulationRecordBusyError` while a scheduler owns the record,
#: :class:`OlderBooleyRecordError` when an older Booley wrote the record, and
#: ``ValueError`` when it is gone, is another one, or any of its results cannot be
#: authenticated.
SimulationRecordResultsReader = Callable[[Path, str], DurableSimulationRecordResults]
