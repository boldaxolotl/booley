"""A Simulation Campaign cancellation names its cause, and unreadable is not absent (#1905)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from booley.config.jobs import SlotCaps
from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign import coordinator
from booley.flows.sim.campaign.capacity import HeavyCapacity, HeavyCapacityError
from booley.flows.sim.campaign.child_protocol import ChildExecutionRegistry
from booley.flows.sim.campaign.scheduler import BoundedCampaignScheduler
from booley.flows.sim.campaign.store import CampaignStore
from booley.runtime.execution_records import ExecutionId
from booley.runtime.job_slots import CLASS_HEAVY, SlotStore
from tests.flows.sim.test_campaign_phase3 import _two_item_manifest
from tests.flows.sim.test_campaign_phase4 import _scheduled_attempt


class _Campaign:
    """A scheduler over a real SlotStore whose outer lane records its cancellation message."""

    def __init__(self, tmp_path: Path, *, max_heavy: int) -> None:
        self.manifest = _two_item_manifest()
        self.store = CampaignStore(tmp_path / "campaign")
        self.store.publish_manifest(self.manifest)
        project = tmp_path / "project"
        (project / ".booley").mkdir(parents=True)
        registry = ChildExecutionRegistry(self.manifest, project)
        self.slots = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=max_heavy))
        self.outer = self.slots.acquire(
            CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32)
        )
        cause = endpoint_admission._admission_cancellation_cause(self.slots, self.outer)
        admission = AdmissionContext(
            "managed",
            self.slots,
            self.outer,
            max_heavy,
            "interactive",
            "a" * 32,
            5.0,
            lambda: cause() is not None,
            cause,
        )
        self.capacity = HeavyCapacity(
            admission, terminal_proof=registry.is_terminal, recover_child=registry.cancel
        )
        self.registry = registry
        self.outer_message: str | None = None

    def run(self, items: int, trigger: Callable[[], None], child_failure: bool = False) -> None:
        """Run *items* work items; the outer lane fires *trigger* and records its message."""

        def execute(_attempt, child_id) -> None:
            if child_id is not None:
                if child_failure:
                    raise RuntimeError("child boom")
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
            except HeavyCapacityError as exc:
                self.outer_message = str(exc)

        scheduler = BoundedCampaignScheduler(
            self.capacity,
            self.registry,
            allocate=lambda item: _scheduled_attempt(self.store, item),
            execute=execute,
        )
        try:
            scheduler.run(self.manifest.document["work_items"][:items])
        except Exception:  # noqa: BLE001 -- the cancelled campaign may fail; the message is the subject
            pass
        finally:
            self.slots.release(self.outer)


def test_outer_lane_stop_requested_names_cause(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, max_heavy=2)
    campaign.run(2, trigger=lambda: time.sleep(0.2), child_failure=True)

    assert campaign.outer_message is not None
    assert "execution cancelled before result publication" in campaign.outer_message
    assert "outer lane" in campaign.outer_message
    assert "stop requested" in campaign.outer_message
    assert "RuntimeError" in campaign.outer_message


def test_outer_lane_lease_lost_names_slot(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, max_heavy=1)
    campaign.run(1, trigger=campaign.outer.lease_health.lost.set)

    assert campaign.outer_message is not None
    assert "outer lane" in campaign.outer_message
    assert "lease lost" in campaign.outer_message
    assert campaign.outer.path.name in campaign.outer_message


def test_outer_lane_missing_token_names_slot(tmp_path: Path, monkeypatch) -> None:
    campaign = _Campaign(tmp_path, max_heavy=1)
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
    campaign = _Campaign(tmp_path, max_heavy=2)

    def boom() -> None:
        raise RuntimeError("outer boom")

    campaign.run(2, trigger=boom)

    cause = campaign.capacity.cancellation_cause()
    assert cause == "campaign stop requested after a work item failed (RuntimeError)"


def test_child_cancellation_cause_names_a_lost_lease(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, max_heavy=1)
    scheduler = BoundedCampaignScheduler(
        campaign.capacity, campaign.registry, allocate=lambda item: item, execute=lambda *_: None
    )
    lost = threading.Event()
    permit = SimpleNamespace(
        lease_health=SimpleNamespace(lost=lost),
        token=SimpleNamespace(job_class="heavy", path=Path("child.json")),
    )
    try:
        assert scheduler._child_cancellation_cause(permit) is None
        lost.set()
        assert (
            scheduler._child_cancellation_cause(permit) == "Job Slot lease lost: heavy/child.json"
        )
    finally:
        campaign.slots.release(campaign.outer)
