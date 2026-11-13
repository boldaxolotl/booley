"""The Flow's Memory Reservation across a Campaign, and each work item's own (#3772, ADR 0087).

The Flow reserves memory for preparation, the shared builds and any item that
runs before the running Campaign's last build is settled. Then it drops to zero, keeping its claim
for its lease and cancellation, and each later item takes a claim of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import MemorySizing
from booley.flows.test_runs import TestRun
from booley.runtime.job_slots import MemoryClaim, SlotToken

from .model import SimulationCampaignManifest
from .test_run_binding import work_item_test_runs

#: Sizes a work item from its Target selector, Test Runs and flags (``None``: runs alone).
WorkItemRequest = Callable[[str, Sequence[TestRun], tuple[str, ...]], JobRequest | None]

# Items whose shared build runs inside the first of them (an aggregate builds inside itself).
_SHARED_BUILD_KINDS = frozenset({"ordinary_hdl", "cocotb_batch"})


class FlowClaim:
    """One Flow's claim and the shared builds (Campaign store, build variant) it waits for.

    A build is *expected* once its Campaign is published or about to run, and
    *settled* when an item of it has returned (built, reused, recovered or failed).

    It takes no lock. ``item_claim`` runs only under the scheduler's start turn,
    and before the drop only the turn holder runs an item, so only it calls
    ``settle`` and ``release``. After the drop ``release`` returns at the one-way
    ``_dropped`` flag, and concurrent ``settle`` calls only add to sets: the worst
    case is an item that reserves its build, the safe side.
    """

    def __init__(
        self, admission: AdmissionContext, sizing: MemorySizing, work_item_request: WorkItemRequest
    ) -> None:
        self._admission = admission
        self._sizing = sizing
        self._work_item_request = work_item_request
        self._unsettled: dict[Path, set[str]] = {}
        self._settled: set[tuple[Path, str]] = set()
        self._dropped = False

    def expect(
        self,
        store_root: Path,
        manifest: SimulationCampaignManifest,
        items: Iterable[Mapping[str, object]],
    ) -> None:
        """Replace the shared builds *items* of one Campaign still need."""
        workload = cast(Mapping[str, object], manifest.document["workload"])
        shared = workload["pre_sim_build_access"] == "immutable"
        self._unsettled[store_root] = {
            cast(str, item["build_variant_id"])
            for item in items
            if shared
            and item["kind"] in _SHARED_BUILD_KINDS
            and (store_root, cast(str, item["build_variant_id"])) not in self._settled
        }

    def settle(self, store_root: Path, item: Mapping[str, object]) -> None:
        """Record that the shared build of *item*'s variant is settled."""
        variant = cast(str, item["build_variant_id"])
        self._settled.add((store_root, variant))
        self._unsettled.get(store_root, set()).discard(variant)

    def release(self, store_root: Path) -> bool:
        """Drop the Flow's reservation to zero once the Campaign at *store_root* has no unsettled build.

        Later Campaigns of the invocation do not hold the Flow's reservation:
        their own building item reserves its build. True from then on. False
        while this Campaign has an unsettled build or when the drop is refused
        (the Flow's lease is gone): an item then runs under the Flow's claim,
        and the next item tries again.
        """
        if self._dropped:
            return True
        if self._unsettled.get(store_root):
            return False
        store, token = self._admission.slot_store, self._admission.outer_token
        if store is None or not isinstance(token, SlotToken):
            raise RuntimeError("a sized Flow holds a Memory Reservation token")
        self._dropped = store.shrink_reservation(token, 0)
        return self._dropped

    def item_claim(
        self, store_root: Path, manifest: SimulationCampaignManifest, item: Mapping[str, object]
    ) -> MemoryClaim | None:
        """The claim *item* takes before it runs; ``None`` while it runs under the Flow's.

        An item reserves its build when it builds inside itself: a private build,
        a coverage aggregate, Pre-Sim Commands, or a shared build nobody expected.
        """
        if not self.release(store_root):
            return None
        document = manifest.document
        workload = cast(Mapping[str, object], document["workload"])
        recipe = cast(Mapping[str, object], workload["source_recipe"])
        build = (
            item["kind"] not in _SHARED_BUILD_KINDS
            or workload["pre_sim_build_access"] != "immutable"
            or bool(recipe["pre_sim_commands"])
            or (store_root, cast(str, item["build_variant_id"])) not in self._settled
        )
        flags = tuple(flag for flag in ("coverage", "trace") if workload[flag] is True)
        selector = cast(Mapping[str, str], document["target"])["selector"]
        request = self._work_item_request(selector, work_item_test_runs(item), flags)
        return self._sizing.plan_work_item(request, build=build)
