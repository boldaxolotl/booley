"""What one Simulation Flow invocation decided about its Test Runs (ADR 0072).

One :class:`TestRunInvocation` belongs to one invocation of the Simulation
Flow. It holds the decisions that later steps of the same invocation must
reuse rather than make again: the Test Runs resolved for each Target (a drawn
seed is never drawn twice) and the Test Runs each planned simulator process
runs.

Domain values only: no CLI, process or Sandbox imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from booley.flows.test_runs import TestRun

__all__ = ["TestRunInvocation"]


@dataclass
class TestRunInvocation:
    """The Test Run decisions of one Simulation Flow invocation."""

    __test__ = False

    #: Resolved Test Runs by ``(checkout, Target)``; resolved once, never redrawn.
    resolved: dict[tuple[str, str], tuple[TestRun, ...]] = field(default_factory=dict)
    #: Each candidate Target's effective Default Seed, recorded when its Test
    #: Runs are resolved. Reports use it to tell a plain run from a seeded one.
    default_seeds: dict[str, int | None] = field(default_factory=dict)
