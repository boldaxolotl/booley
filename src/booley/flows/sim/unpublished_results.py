"""Read what a Simulation Flow run left behind without publishing it (ADR 0072).

Goal Mode registers every campaign a bound run starts and later asks what it
durably observed (:mod:`booley.flows.simulation_record_results`). The Simulation Flow
starts a **Simulation Campaign** (``record/manifest.json``): each finished
work item leaves an authenticated result
(:func:`booley.flows.sim.record.read_durable_results`).

:func:`read_unpublished_results` is the reader entry points hand to Goal
Mode.
"""

from __future__ import annotations

from pathlib import Path

from booley.flows.simulation_record_results import DurableCampaignResults

from .record import read_durable_results

__all__ = ["read_unpublished_results"]


def read_unpublished_results(manifest_path: Path, manifest_sha256: str) -> DurableCampaignResults:
    """The durable results of the Simulation Campaign registered at *manifest_path*.

    Raises :class:`~booley.flows.simulation_record_results.CampaignBusyError` while a
    scheduler owns the campaign,
    :class:`~booley.flows.simulation_record_results.OlderBooleyRecordError` when an older
    Booley wrote it, and ``ValueError`` when the path is not a
    Simulation Campaign manifest (``record/manifest.json``), the campaign is
    not the registered one, or it cannot be authenticated.
    """
    return read_durable_results(manifest_path, manifest_sha256)
