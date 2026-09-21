"""Durable Simulation Campaign domain values.

Phase 1 exposes only immutable values and codecs.  Execution and persistence
are intentionally introduced by later phases.
"""

from .codec import (
    CampaignIntegrityError,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_campaign_manifest,
    decode_executable_snapshot,
    decode_simulation_attempt,
    decode_simulation_result,
    decode_simulator_bundle,
)
from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutableSnapshot,
    SimulationAttempt,
    SimulationCampaignManifest,
    SimulationCampaignPlan,
    SimulationResult,
    SimulatorBundle,
)

__all__ = [
    "BundleBuildAttempt",
    "BundleBuildResult",
    "CampaignIntegrityError",
    "ExecutableSnapshot",
    "SimulationAttempt",
    "SimulationCampaignManifest",
    "SimulationCampaignPlan",
    "SimulationResult",
    "SimulatorBundle",
    "decode_bundle_build_attempt",
    "decode_bundle_build_result",
    "decode_campaign_manifest",
    "decode_executable_snapshot",
    "decode_simulation_attempt",
    "decode_simulation_result",
    "decode_simulator_bundle",
]
