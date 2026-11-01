"""Bind each observation of a work item to the Test Run it observed (ADR 0072).

A manifest v4 work item records the Test Runs it executes. A result names
the Test of every observation; this module is the one rule that says which
recorded Test Run each observation belongs to, used when a result is written,
when it is authenticated against its work item, and when acceptance facts are
built.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from booley.flows.test_runs import discovered_test_run, test_run_from_document

from .codec import SimulationCampaignIntegrityError

__all__ = ["observation_test_runs"]


def observation_test_runs(
    item: Mapping[str, object], observations: Sequence[Mapping[str, object]]
) -> tuple[tuple[str, str], ...]:
    """The ``(name, fingerprint)`` of the Test Run each observation observed.

    The n-th observation of a Test is the n-th Test Run of that Test in the
    work item; an observation without a Test is the item as a whole and takes
    its first Test Run. A Test that an unfiltered Cocotb batch discovered ran
    under the batch's unnamed invocation and is named with its seed.

    Raises :class:`SimulationCampaignIntegrityError` when an observation has
    no Test Run in its work item.
    """
    runs = cast(tuple[Mapping[str, object], ...], item["test_runs"])
    unnamed = runs[0] if len(runs) == 1 and runs[0]["test"] is None else None
    taken: dict[object, int] = {}
    bound: list[tuple[str, str]] = []
    for observation in observations:
        test = observation["test"]
        if unnamed is not None and test is not None:
            run = discovered_test_run(cast(str, test), test_run_from_document(unnamed))
            bound.append((run.name, run.fingerprint))
            continue
        matching = [run for run in runs if test is None or run["test"] == test]
        position = taken.get(test, 0)
        taken[test] = position + 1
        if position >= len(matching):
            raise SimulationCampaignIntegrityError(
                "a result observation has no Test Run in its work item"
            )
        run = matching[position]
        bound.append((cast(str, run["name"]), cast(str, run["fingerprint"])))
    return tuple(bound)
