"""A Simulation Campaign cancellation names its cause, and unreadable is not absent (#1905)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import endpoint_admission
from booley.flows.sim.campaign import coordinator
from booley.flows.sim.campaign.capacity import CampaignCapacityError
from booley.flows.sim.campaign.coordinator import (
    SimulationCampaign,
    SimulationCampaignCancellationError,
)
from booley.flows.sim.campaign.scheduler import (
    BoundedCampaignScheduler,
)
from booley.flows.sim.campaign.store import CampaignStore
from booley.runtime.job_slots import (
    SlotStore,
)
from tests.flows.sim.campaign_test_support import (
    _holder_claim,
    _managed,
    _outer_claim,
    _pinned_lanes,
    _scheduled_attempt,
    _two_item_manifest,
)


class _Campaign:
    """A scheduler over a real SlotStore whose outer lane records its cancellation message."""

    def __init__(self, tmp_path: Path, *, lanes: int) -> None:
        self.manifest = _two_item_manifest()
        self.store = CampaignStore(tmp_path / "record")
        self.store.publish_manifest(self.manifest)
        project = tmp_path / "project"
        (project / ".booley").mkdir(parents=True)
        self.slots = SlotStore(tmp_path / "slots")
        self.outer = _outer_claim(self.slots)
        cause = endpoint_admission._admission_cancellation_cause(self.slots, self.outer)
        admission = replace(
            _managed(self.slots, self.outer, holders=lanes),
            cancellation=lambda: cause() is not None,
            cancellation_cause=cause,
        )
        claim = _holder_claim(lanes)
        # The outer lane's item runs under the Flow's claim; every other lane's takes its own.
        self.capacity = _pinned_lanes(
            admission,
            self.manifest,
            project / ".booley",
            lambda _item: None if threading.current_thread().name.endswith("outer") else claim,
            lanes=lanes,
        )
        self.outer_message: str | None = None
        self.scheduler: BoundedCampaignScheduler | None = None

    def run(self, items: int, trigger: Callable[[], None]) -> None:
        """Run *items* work items; the outer lane fires *trigger* and records its message."""

        def execute(_attempt, child_id) -> None:
            if child_id is not None:
                deadline = time.monotonic() + 5.0  # idle child lane until cancelled
                while not self.capacity._shutdown.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return
            trigger()
            # The scheduler's await loop has now run its argument-less cancel_waiters,
            # which must not mask the cause that triggered the cancellation.
            deadline = time.monotonic() + 5.0
            while not self.capacity._shutdown.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                coordinator.raise_if_cancelled_before_publication()
            except CampaignCapacityError as exc:
                self.outer_message = str(exc)

        self.scheduler = BoundedCampaignScheduler(
            self.capacity,
            allocate=lambda item: _scheduled_attempt(self.store, item),
            execute=execute,
        )
        try:
            self.scheduler.run(self.manifest.document["work_items"][:items])
        except Exception:  # noqa: BLE001 -- the cancelled campaign may fail; the message is the subject
            pass
        finally:
            self.slots.release(self.outer)


def test_outer_lane_stop_requested_names_cause(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, lanes=1)

    def fail_an_item() -> None:
        assert campaign.scheduler is not None
        campaign.scheduler._fail_campaign(RuntimeError("an item failed"))

    campaign.run(1, trigger=fail_an_item)

    assert campaign.outer_message is not None
    assert "execution cancelled before result publication" in campaign.outer_message
    assert "outer lane" in campaign.outer_message
    assert "stop requested" in campaign.outer_message
    assert "RuntimeError" in campaign.outer_message


def test_outer_lane_lease_lost_names_slot(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, lanes=1)
    campaign.run(1, trigger=campaign.outer.lease_health.lost.set)

    assert campaign.outer_message is not None
    assert "outer lane" in campaign.outer_message
    assert "lease lost" in campaign.outer_message
    assert campaign.outer.path.name in campaign.outer_message


def test_outer_lane_missing_token_names_slot(tmp_path: Path, monkeypatch) -> None:
    campaign = _Campaign(tmp_path, lanes=1)
    path = campaign.outer.path
    real_absent = campaign.slots.token_absent
    withdrawn = threading.Event()
    # Report the entry gone without unlinking it: Windows may still hold it open.
    monkeypatch.setattr(
        campaign.slots, "token_absent", lambda token: withdrawn.is_set() or real_absent(token)
    )
    campaign.run(1, trigger=withdrawn.set)

    assert campaign.outer_message is not None
    assert "outer lane" in campaign.outer_message
    assert "token missing" in campaign.outer_message
    assert path.name in campaign.outer_message
    # A really missing file (released by the run) is still absent.
    assert real_absent(campaign.outer) is True


def test_outer_worker_failure_names_its_cause(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, lanes=1)

    def boom() -> None:
        raise RuntimeError("outer boom")

    campaign.run(1, trigger=boom)

    cause = campaign.capacity.cancellation_cause()
    assert cause == "campaign stop requested after a work item failed (RuntimeError)"


def test_child_cancellation_cause_names_a_lost_lease(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, lanes=1)
    lost = threading.Event()
    permit = SimpleNamespace(
        lease_health=SimpleNamespace(lost=lost),
        token=SimpleNamespace(job_class="memory", path=Path("child.json")),
    )
    try:
        assert campaign.capacity._child_cancellation_cause(permit) is None
        lost.set()
        cause = campaign.capacity._child_cancellation_cause(permit)
        assert cause == "Job Slot lease lost: memory/child.json"
    finally:
        campaign.slots.release(campaign.outer)


def test_cancelled_campaign_stops_before_publication() -> None:
    request = SimpleNamespace(
        admission=SimpleNamespace(
            cancellation=lambda: True,
            cancellation_cause=lambda: "Job Slot token missing: memory/entry.json",
        )
    )
    with pytest.raises(
        SimulationCampaignCancellationError, match="cancelled: Job Slot token missing"
    ):
        SimulationCampaign._raise_if_cancelled(request)  # type: ignore[arg-type]
