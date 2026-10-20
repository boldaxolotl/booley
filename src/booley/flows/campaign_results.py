"""What a Simulation Campaign durably observed, as its consumers read it (ADR 0072).

A campaign that was cancelled or killed never returns an outcome, yet every
work item that finished left an authenticated result behind. Goal Mode must
account for those results, and it may not depend on a concrete Flow: these
are the flow-neutral values it is handed, and the reader the composing entry
point supplies. The Simulation Flow implements the reader
(:func:`booley.flows.sim.campaign.read_durable_results`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CampaignBusyError",
    "CampaignResultsReader",
    "DurableCampaignResults",
    "StartedCampaign",
]


class CampaignBusyError(RuntimeError):
    """A scheduler owns the Simulation Campaign: its results are still being written."""


@dataclass(frozen=True)
class StartedCampaign:
    """A Simulation Campaign whose manifest is published and whose work is about to run.

    ``target_names`` are the names its Target goes by (selector and
    identity); ``candidate`` is false for a Cycle Count baseline campaign.
    """

    manifest_path: Path
    campaign_id: str
    manifest_sha256: str
    target_names: tuple[str, ...]
    candidate: bool


@dataclass(frozen=True)
class DurableCampaignResults:
    """The terminal results one Simulation Campaign holds on disk.

    ``results`` names every work item with a terminal result and that
    result's digest; ``rows`` are the Test Run rows of their observations
    (``name``, ``fingerprint`` and ``grade`` at least), empty for a campaign
    that names no Test Runs. ``complete`` says every work item has a terminal
    result: nothing more can come out of the campaign.
    """

    manifest_sha256: str
    campaign_id: str
    results: tuple[tuple[str, str], ...]
    rows: tuple[Mapping[str, object], ...]
    complete: bool = False


#: Authenticate and return the terminal results of the campaign whose
#: manifest is at the path and has the given digest. Raises
#: :class:`CampaignBusyError` while a scheduler owns the campaign and
#: ``ValueError`` when the campaign is gone, is another one, or any of its
#: results cannot be authenticated.
CampaignResultsReader = Callable[[Path, str], DurableCampaignResults]
