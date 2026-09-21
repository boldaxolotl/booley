"""Durable Simulation Campaign domain values.

Phase 1 exposes only immutable values and codecs.  Execution and persistence
are intentionally introduced by later phases.
"""

from .codec import (
    SimulationCampaignIntegrityError,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_executable_snapshot,
    decode_simulation_attempt,
    decode_simulation_campaign_manifest,
    decode_simulation_result,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_executable_snapshot,
    encode_simulation_attempt,
    encode_simulation_campaign_document,
    encode_simulation_campaign_manifest,
    encode_simulation_result,
    encode_simulator_bundle,
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
    create_simulation_campaign_plan,
)

__all__ = [
    "BundleBuildAttempt",
    "BundleBuildResult",
    "ExecutableSnapshot",
    "SimulationAttempt",
    "SimulationCampaignIntegrityError",
    "SimulationCampaignManifest",
    "SimulationCampaignPlan",
    "SimulationResult",
    "SimulatorBundle",
    "create_simulation_campaign_plan",
    "decode_bundle_build_attempt",
    "decode_bundle_build_result",
    "decode_executable_snapshot",
    "decode_simulation_attempt",
    "decode_simulation_campaign_manifest",
    "decode_simulation_result",
    "decode_simulator_bundle",
    "encode_bundle_build_attempt",
    "encode_bundle_build_result",
    "encode_executable_snapshot",
    "encode_simulation_attempt",
    "encode_simulation_campaign_document",
    "encode_simulation_campaign_manifest",
    "encode_simulation_result",
    "encode_simulator_bundle",
]
