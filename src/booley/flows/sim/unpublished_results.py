"""Read what a Simulation Flow run left behind without publishing it (ADR 0072).

Goal Mode registers every campaign a bound run starts and later asks what it
durably observed (:mod:`booley.flows.campaign_results`). The Simulation Flow
starts two kinds:

- a **Simulation Campaign** (``campaign/manifest.json``): each finished work
  item leaves an authenticated result
  (:func:`booley.flows.sim.campaign.read_durable_results`);
- a **Coverage Campaign collected on its own** (``coverage.json``): its runs
  are durable only once the whole campaign is published, in one document.
  Before that it has no result; after it, it is complete. It holds no lock
  while it collects and cannot be resumed.

:func:`read_unpublished_results` is the reader entry points hand to Goal
Mode; :func:`started_coverage_campaign` names the second kind for
registration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from booley.flows.campaign_results import DurableCampaignResults, StartedCampaign

from .campaign import SimulationCampaignIntegrityError, read_durable_results
from .coverage_acceptance import coverage_run_grade
from .coverage_campaign import CoverageCampaignValidationError
from .coverage_campaign_store import CoverageCampaignStoreError, load_coverage_campaign

__all__ = ["read_unpublished_results", "started_coverage_campaign"]

_COVERAGE_DOCUMENT = "coverage.json"


def _coverage_identity(document: Path) -> tuple[str, str]:
    """The campaign id and digest a Coverage Campaign at *document* is registered under.

    The document does not exist when collection starts, so the campaign is
    known by where it will be published.
    """
    digest = hashlib.sha256(document.resolve().as_posix().encode("utf-8")).hexdigest()
    return f"coverage-{digest[:32]}", f"sha256:{digest}"


def started_coverage_campaign(target_root: Path, names: tuple[str, ...]) -> StartedCampaign:
    """The Coverage Campaign about to be collected into *target_root*, for registration."""
    document = target_root / _COVERAGE_DOCUMENT
    campaign_id, digest = _coverage_identity(document)
    return StartedCampaign(document, campaign_id, digest, names, candidate=True)


def read_unpublished_results(manifest_path: Path, manifest_sha256: str) -> DurableCampaignResults:
    """The durable results of the campaign registered at *manifest_path*.

    Raises :class:`~booley.flows.campaign_results.CampaignBusyError` while a
    scheduler owns a Simulation Campaign, and ``ValueError`` when the
    campaign is not the registered one or cannot be authenticated.
    """
    if manifest_path.name != _COVERAGE_DOCUMENT:
        return read_durable_results(manifest_path, manifest_sha256)
    campaign_id, digest = _coverage_identity(manifest_path)
    if digest != manifest_sha256:
        raise SimulationCampaignIntegrityError(
            "Coverage Campaign is not the one that was registered"
        )
    if not manifest_path.exists():
        return DurableCampaignResults(digest, campaign_id, (), ())
    try:
        campaign = load_coverage_campaign(manifest_path).campaign
        content = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except (CoverageCampaignStoreError, CoverageCampaignValidationError, OSError) as exc:
        raise SimulationCampaignIntegrityError(
            f"Coverage Campaign cannot be authenticated: {exc}"
        ) from exc
    rows = tuple(
        {
            "name": run.test_run,
            "test": run.test,
            "variant": run.variant,
            "seed": run.seed,
            "fingerprint": run.fingerprint,
            "grade": coverage_run_grade(run),
        }
        for run in campaign.runs
    )
    results = ((_COVERAGE_DOCUMENT, f"sha256:{content}"),)
    return DurableCampaignResults(digest, campaign_id, results, rows, complete=True)
