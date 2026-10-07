"""Compose Goal freshness with the strict coverage policy reader."""

from pathlib import Path

from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_waivers import CoverageRepositoryRoots, load_approved_waiver_set
from booley.goals.freshness import GoalFreshnessResolvers
from booley.goals.waiver_policy import policy_location
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.catalog import TargetCatalog


def approved_waiver_digest(work_dir: Path) -> str:
    """Read the same configured semantic policy as the Simulation producer."""
    config, _anchor = policy_location(work_dir)
    roots = CoverageRepositoryRoots(work_dir, resolve_checkout_project_dir(work_dir))
    targets = tuple(
        DurableTargetIdentity(item.identity) for item in TargetCatalog.build(work_dir).list()
    )
    return load_approved_waiver_set(config, roots, targets).digest


GOAL_FRESHNESS_RESOLVERS = GoalFreshnessResolvers(waiver_semantics=approved_waiver_digest)
