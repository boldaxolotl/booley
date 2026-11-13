"""Each Simulation Campaign work item holds its own Memory Reservation (#3772, ADR 0087).

Under ``BOOLEY_DEV_MEMORY_ADMISSION=1`` the Flow keeps one reservation for its
shared builds, then drops to zero and each later item takes a claim of its own.
The Campaign, slot store, Flow claim and foreign Jobs are real; only the
simulators are fake.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config.jobs import SlotCaps
from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import AdmissionContext, AdmissionGate
from booley.flows.endpoint_session import _attach_memory_warning
from booley.flows.job_estimates import JobRequest
from booley.flows.sim.campaign.capacity import LeaseLostError
from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.model import (
    SimulationCampaignManifest,
    create_simulation_campaign_plan,
)
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    CLASS_MEMORY,
    ClaimAbortedError,
    ClaimLostError,
    MemoryClaim,
)
from booley.runtime.resource_history import SCHEMA
from booley.runtime.supervised_execution import current_supervised_execution
from tests.flows.sim.campaign_test_support import (
    _PARALLEL_SYNC_TIMEOUT_S,
    _manifest_for,
    _parallel_environment,
    _ParallelExecution,
    _ParallelGroup,
    _validated_campaign,
    _wait_until,
)

NAMES = ("alpha", "beta", "gamma")
BUDGET_MIB = 1000.0
# History peaks (MiB); the estimator adds 25 %. The build is the largest phase.
BUILD_PEAK = 400.0
RUN_PEAKS = {"alpha": 100.0, "beta": 200.0, "gamma": 160.0}
FLOW_REQUESTS = (JobRequest("sim", "sim", test_runs=NAMES),)


def _record(target: str, phase: str, peak: float, *, test_run: str = "", flags=()) -> dict:
    return {
        "key": {"target": target, "phase": phase, "flags": list(flags), "test_run": test_run},
        "peak_memory_mb": peak,
        "wall_s": 1.0,
    }


def _history(*targets: str) -> list[dict]:
    """The history of a Campaign whose Targets are *targets*."""
    return [
        record
        for target in targets
        for record in (
            _record(target, "sim_build", BUILD_PEAK),
            *(_record(target, "sim_run", peak, test_run=name) for name, peak in RUN_PEAKS.items()),
        )
    ]


def _sizer(selector: str, runs: Sequence[object], flags: tuple[str, ...]) -> JobRequest:
    """The Flow's ``work_item_job_request``, without a Target plan to read."""
    return JobRequest("sim", selector, test_runs=tuple(run.name for run in runs), flags=flags)  # type: ignore[attr-defined]


class _ObservedGroup(_ParallelGroup):
    """A fake simulator group that reports to its scenario while an item runs."""

    def planning_disclosure(self):
        return getattr(self.state, "disclosures", {}).get(self.names[0], {})

    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
        self.state.observe(self.names[0])
        return super().launch_snapshot(snapshot_root, run_cwd)


class _ObservedExecution(_ParallelExecution):
    @contextmanager
    def ordinary_group(self, _handle, names, _test_runs=None):
        yield _ObservedGroup(self.state, names)


@dataclass
class _Scenario:
    """One Flow admission with its slot store, and what each item saw while it ran."""

    tmp_path: Path
    project: Path
    state: SimpleNamespace
    admission: AdmissionContext
    store: job_slots.SlotStore
    seen: dict[str, dict[str, list[object]]] = field(default_factory=dict)
    hooks: dict[str, Callable[[], None]] = field(default_factory=dict)

    @property
    def flow_token(self) -> job_slots.SlotToken:
        assert isinstance(self.admission.outer_token, job_slots.SlotToken)
        return self.admission.outer_token

    def observe(self, name: str) -> None:
        """Record who holds memory while item *name* runs (the Flow apart from the items)."""
        holders = self.store.observe(CLASS_MEMORY)[0]
        flow = [_held(token) for token in holders if token.lease_id == self.flow_token.lease_id]
        items = [_held(token) for token in holders if token.lease_id != self.flow_token.lease_id]
        self.seen[name] = {"flow": flow, "items": items}
        if name in self.hooks:
            self.hooks[name]()

    def close_queue(self) -> None:
        """Refuse every claim that would wait: the Flow's own store now has a queue bound of zero."""
        flow_store = self.admission.slot_store
        assert flow_store is not None
        flow_store.caps = replace(flow_store.caps, queue_max=0)

    def record(self) -> CampaignStore:
        return CampaignStore(self.tmp_path / "reports" / "000001" / "targets/sim/record")

    def request(
        self, manifest: SimulationCampaignManifest, number: int = 1
    ) -> NewCampaignRunRequest:
        invocation = self.tmp_path / "reports" / f"{number:06d}"
        invocation.mkdir(parents=True, exist_ok=True)
        return NewCampaignRunRequest(
            create_simulation_campaign_plan(manifest),
            self.project,
            invocation.parent,
            CampaignPolicy(),
            invocation,
            self.admission,
        )

    def campaign(self) -> SimulationCampaign:
        executor = OrdinaryHdlSerialExecutor(
            invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            execution_factory=lambda _options: _ObservedExecution(self.state),  # type: ignore[arg-type,return-value]
        )
        return SimulationCampaign(executor, work_item_request=_sizer)


def _own_lanes(scenario: _Scenario) -> list[bool]:
    """Per item in manifest order: did it run as a lane of its own (child execution)?"""
    paths = sorted(scenario.record().root.glob("work-items/*/attempts/*/attempt.json"))
    return [json.loads(path.read_bytes())["child_execution_id"] is not None for path in paths]


def _held(token: job_slots.SlotToken) -> object:
    """What a holder reserves: MiB, or ``"alone"``."""
    assert token.memory is not None
    return "alone" if token.memory.run_alone else token.memory.reservation_mb


@contextmanager
def _scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flow_requests: Sequence[JobRequest] | None = FLOW_REQUESTS,
    history: Sequence[dict] = (),
    switch: bool = True,
) -> Iterator[_Scenario]:
    """The Flow of a Campaign, admitted under a 1,000 MiB Memory Budget."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    project, state = _parallel_environment(tmp_path, "alpha", 0)
    state.barrier = threading.Barrier(1)  # one item at a time: nothing waits for a peer
    booley_dir = project / ".booley"
    (booley_dir / "booley.toml").write_text('[jobs]\nmemory_budget = "1000m"\n', encoding="utf-8")
    path = booley_dir / "runtime" / "resources" / "history.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"$schema": SCHEMA, "records": list(history)}), encoding="utf-8")
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: state.handle),
    )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution._run_hook", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    if switch:
        monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
    else:
        monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION", raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", "a" * 32)
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.delenv(job_slots.INHERITED_SLOT_ENV, raising=False)
    monkeypatch.setattr(endpoint_admission, "resolve_common_booley_dir", lambda: booley_dir)
    prepared = (
        None
        if flow_requests is None
        else SimpleNamespace(invocation=SimpleNamespace(job_requests=lambda: tuple(flow_requests)))
    )
    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir), SlotCaps(max_heavy=1))
    with AdmissionGate(AsicSynthesizeFlow(), prepared).enter() as admission:  # type: ignore[arg-type]
        scenario = _Scenario(tmp_path, project, state, admission, store)
        state.observe = scenario.observe
        yield scenario


class _ForeignJob:
    """A Job of another process: it queues for a claim and holds it until released.

    ``started_after`` lists the items finished when its claim started (``None``
    while it has not started).
    """

    def __init__(self, scenario: _Scenario, claim: MemoryClaim) -> None:
        self.started_after: list[str] | None = None
        self._scenario = scenario
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(claim,), daemon=True)
        self._thread.start()
        _wait_until(lambda: bool(scenario.store.snapshot(CLASS_MEMORY)[1]))

    def _run(self, claim: MemoryClaim) -> None:
        store = self._scenario.store
        try:
            token = store.acquire(
                CLASS_MEMORY,
                pid=os.getpid(),
                memory=claim,
                poll_interval=0.01,
                should_abort=self._release.is_set,
            )
        except ClaimAbortedError:
            return  # finished before it ever ran
        self.started_after = list(self._scenario.state.completion_order)
        self._release.wait(_PARALLEL_SYNC_TIMEOUT_S)
        store.release(token)

    def release(self) -> None:
        self._release.set()

    def finish(self) -> None:
        self._release.set()
        self._thread.join(_PARALLEL_SYNC_TIMEOUT_S)
        assert not self._thread.is_alive()


def _alone() -> MemoryClaim:
    return MemoryClaim(budget_mb=BUDGET_MIB, run_alone=True)


@contextmanager
def _deadline(scenario: _Scenario) -> Iterator[None]:
    """Cancel a Campaign still waiting after the suite's sync bound (it would hang)."""
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        scenario.store.release(scenario.flow_token)

    timer = threading.Timer(_PARALLEL_SYNC_TIMEOUT_S, expire)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        assert not expired.is_set(), "the Campaign waited past its deadline"


def test_items_after_the_shared_build_hold_their_own_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        outcome = scenario.campaign().run(scenario.request(_manifest_for(NAMES)))
        (still_held,) = scenario.store.observe(CLASS_MEMORY)[0]
        assert _held(still_held) == 0
        assert scenario.store.observe(CLASS_MEMORY)[1] == []
        assert scenario.store.observe(CLASS_HEAVY) == ([], [])

    assert outcome.complete is True
    # The first item builds under the Flow, which the finished build lowered to the
    # largest run still to come; the others hold their own run estimate (no build).
    assert scenario.seen == {
        "alpha": {"flow": [250.0], "items": []},
        "beta": {"flow": [0], "items": [250.0]},
        "gamma": {"flow": [0], "items": [200.0]},
    }
    # Only the items that took their own claim ran as a lane of their own.
    assert _own_lanes(scenario) == [False, True, True]


def test_resumed_campaign_items_take_their_own_claims_after_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume has no Flow estimate (run-alone); its frozen manifest says what it traces."""
    history = [
        *_history("sim"),
        *(
            _record("sim", "sim_run", peak, test_run=name, flags=("trace",))
            for name, peak in {"alpha": 300.0, "beta": 400.0, "gamma": 360.0}.items()
        ),
    ]
    document = json.loads(canonical_json_bytes(_manifest_for(NAMES).document))
    document.pop("fingerprints")
    document["workload"]["trace"] = True
    manifest = finalize_manifest(document)
    with _scenario(tmp_path, monkeypatch, flow_requests=None, history=history) as scenario:
        store = scenario.record()
        store.publish_manifest(manifest)
        validated = _validated_campaign(store, manifest, scenario.project, scenario.state.handle)
        invocation = scenario.tmp_path / "reports" / "000001"
        outcome = scenario.campaign().run(
            ResumeCampaignRunRequest(
                validated,
                create_simulation_campaign_plan(manifest),
                scenario.project,
                invocation.parent,
                CampaignPolicy(),
                invocation,
                scenario.admission,
            )
        )

    assert outcome.complete is True
    # The Flow was run-alone until the build settled; the traced run keys are larger.
    assert scenario.seen == {
        "alpha": {"flow": ["alone"], "items": []},
        "beta": {"flow": [0], "items": [500.0]},
        "gamma": {"flow": [0], "items": [450.0]},
    }


def test_over_budget_item_runs_alone_with_the_job_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = [*_history("sim"), _record("sim", "sim_run", 900.0, test_run="beta")]
    with _scenario(tmp_path, monkeypatch, history=history) as scenario:
        scenario.campaign().run(scenario.request(_manifest_for(NAMES)))
        result = EndpointOutcome(report_text="RESULT: PASS")
        _attach_memory_warning(result, scenario.admission)

    assert scenario.seen["beta"]["items"] == ["alone"]
    assert scenario.seen["gamma"]["items"] == [200.0]
    warning = "Memory estimate 1125 MiB exceeds the Memory Budget of 1000 MiB; this Job ran alone."
    assert result.detail["memory_admission_warning"] == warning
    assert result.report_text.splitlines() == ["RESULT: PASS", warning]


def _disclosures() -> dict[str, dict]:
    return {
        name: {
            "planner": f"fake-{name}",
            "scratch_inputs": [],
            "generated_files": [],
            "tool_provenance": {"kind": "fake", "version": "1", "contract_version": "1"},
            "cleanup": {"removed": True},
        }
        for name in NAMES
    }


def _private_manifest() -> SimulationCampaignManifest:
    """Three items that each build privately (the legacy Pre-Sim build access)."""
    document = json.loads(
        canonical_json_bytes(_manifest_for(NAMES, access="legacy-per-test").document)
    )
    document.pop("fingerprints")
    document["planning_disclosures"] = list(_disclosures().values())
    return finalize_manifest(document)


def test_campaign_completes_beside_a_queued_foreign_job(tmp_path: Path) -> None:
    for case in ("alone", "sized", "no_queue_bound", "private", "several"):
        with pytest.MonkeyPatch.context() as monkeypatch:
            _complete_beside_a_foreign_job(tmp_path, monkeypatch, case)


def _complete_beside_a_foreign_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A foreign run-alone Job queued ahead of the items runs mid-Campaign; the items wait for it."""
    options: dict = {"alone": {"flow_requests": None, "history": ()}}.get(
        case, {"history": _history("sim")}
    )
    manifests = [_private_manifest() if case == "private" else _manifest_for(NAMES)]
    if case == "several":  # a second Campaign, in its own store, with a build of its own
        manifests.append(manifests[0])
    with _scenario(tmp_path / case, monkeypatch, **options) as scenario:
        scenario.state.disclosures = _disclosures()
        foreign = _ForeignJob(scenario, _alone())
        if case == "no_queue_bound":
            scenario.close_queue()

        def release_once_an_item_waits() -> None:
            _wait_until(lambda: foreign.started_after is not None)
            _wait_until(lambda: bool(scenario.store.snapshot(CLASS_MEMORY)[1]))
            foreign.release()

        try:
            campaign = scenario.campaign()
            requests = [scenario.request(m, number) for number, m in enumerate(manifests, 1)]
            releaser = threading.Thread(target=release_once_an_item_waits)
            releaser.start()
            with _deadline(scenario):
                for request in requests:
                    campaign.publish_new(request)
                for request in requests:
                    outcome = campaign.run(request)
                    assert outcome.complete is True
            releaser.join(_PARALLEL_SYNC_TIMEOUT_S)
        finally:
            foreign.finish()

    ran = scenario.state.completion_order
    assert len(ran) == 3 * len(manifests)
    assert foreign.started_after is not None and len(foreign.started_after) < len(ran), case
    assert scenario.store.observe(CLASS_MEMORY)[1] == []
    if case == "private":
        # Each item builds inside itself, so its claim covers the build too.
        assert [seen["items"] for seen in scenario.seen.values()] == [[500.0]] * 3


def test_lost_work_item_lease_stops_only_that_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:

        def lose_the_lease() -> None:
            (own,) = [
                token
                for token in scenario.store.observe(CLASS_MEMORY)[0]
                if token.lease_id != scenario.flow_token.lease_id
            ]
            with scenario.store._lease_gate(own) as acquired:
                assert acquired
                own.path.unlink()
            scope = current_supervised_execution()
            assert scope is not None
            _wait_until(scope.cancelled)

        scenario.hooks["beta"] = lose_the_lease
        with pytest.raises(LeaseLostError):
            scenario.campaign().run(scenario.request(_manifest_for(NAMES)))
        assert scenario.admission.cancellation_cause() is None

    # The next item ran and finished; the lost one published no result.
    assert scenario.state.completion_order == ["alpha", "beta", "gamma"]
    assert len(list(scenario.record().root.glob("work-items/*/result.json"))) == 2


def test_lost_flow_lease_withdraws_the_item_claim_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        foreign = _ForeignJob(scenario, _alone())
        waited: list[bool] = []

        def lose_the_flow_lease() -> None:
            # The foreign Job starts once the Flow dropped; the next item queues behind it.
            _wait_until(lambda: foreign.started_after is not None)
            _wait_until(lambda: bool(scenario.store.snapshot(CLASS_MEMORY)[1]))
            waited.append(True)
            scenario.store.release(scenario.flow_token)

        killer = threading.Thread(target=lose_the_flow_lease)
        try:
            campaign = scenario.campaign()
            killer.start()
            # The item's wait ends as aborted, or as lost when the withdrawal lands first.
            with pytest.raises((ClaimAbortedError, ClaimLostError)):
                campaign.run(scenario.request(_manifest_for(NAMES)))
            killer.join(_PARALLEL_SYNC_TIMEOUT_S)
            assert waited == [True]
            cause = scenario.admission.cancellation_cause()
            assert cause is not None and scenario.flow_token.path.name in cause
            assert scenario.store.observe(CLASS_MEMORY)[1] == []
        finally:
            foreign.finish()

    assert scenario.state.completion_order == ["alpha"]


def test_switch_unset_campaign_takes_no_memory_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _scenario(tmp_path, monkeypatch, history=_history("sim"), switch=False) as scenario:
        outcome = scenario.campaign().run(scenario.request(_manifest_for(NAMES)))

    assert outcome.complete is True
    assert scenario.admission.memory_sizing is None
    assert scenario.seen == {name: {"flow": [], "items": []} for name in NAMES}
    assert _own_lanes(scenario) == [False] * 3
    assert scenario.store.observe(CLASS_MEMORY) == ([], [])


def _flow_claim(scenario: _Scenario):
    """A ``FlowClaim`` over the Flow's admission."""
    from booley.flows.sim.campaign.flow_claim import FlowClaim

    assert scenario.admission.memory_sizing is not None
    return FlowClaim(scenario.admission, scenario.admission.memory_sizing, _sizer)


def test_flow_claim_waits_only_for_the_builds_still_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_for(NAMES)
    items = manifest.document["work_items"]
    other = tmp_path / "other-store"
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        claim = _flow_claim(scenario)
        # Two Campaigns published, each owing its shared build.
        claim.expect(tmp_path, manifest, items)
        claim.expect(other, manifest, items)
        assert not claim.release()
        claim.settle(tmp_path, items[0])
        assert not claim.release()
        # A resumed Campaign that already built its variant has nothing pending.
        claim.expect(other, manifest, [])
        assert claim.release()
        assert _held(scenario.store.observe(CLASS_MEMORY)[0][0]) == 0
        assert claim.release()  # idempotent
        # A drop the store refuses (the Flow's lease is gone) leaves items under the Flow.
        scenario.store.release(scenario.flow_token)
        assert not _flow_claim(scenario).release()


def test_coverage_aggregate_reserves_its_build_and_coverage_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_for(NAMES)
    (item, *_) = manifest.document["work_items"]
    traced = json.loads(canonical_json_bytes(manifest.document))
    traced.pop("fingerprints")
    traced["workload"].update(coverage=True, trace=True)
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        claim = _flow_claim(scenario)
        claim.settle(tmp_path, item)  # its variant is built, yet an aggregate builds inside itself
        settled = claim.item_claim(tmp_path, manifest, item)
        building = claim.item_claim(tmp_path, manifest, {**item, "kind": "coverage_aggregate"})
        flagged = claim.item_claim(tmp_path, finalize_manifest(traced), item)

    assert [claim and claim.reservation_mb for claim in (settled, building)] == [125.0, 500.0]
    # No history under the coverage and trace flags: the item runs alone.
    assert flagged is not None and flagged.run_alone
