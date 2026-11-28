"""Two-role Simulation Records."""

import json
from types import SimpleNamespace

import pytest

SIMULATION_RECORD_ID = "01234567-89ab-4def-8123-456789abcdef"


def _add_observed_cycles(monkeypatch):
    from dataclasses import replace

    from tests.flows.sim.simulation_record_test_support import _Group

    original = _Group.launch_bundle

    def with_cycles(group, *args):
        outcome = original(group, *args)
        return replace(
            outcome,
            tests=tuple(
                replace(test, cycles=17, cycle_status="observed") for test in outcome.tests
            ),
        )

    monkeypatch.setattr(_Group, "launch_bundle", with_cycles)


def _two_role_simulation_records(tmp_path, monkeypatch):
    """A candidate and a Cycle Count baseline Simulation Record sharing one invocation."""
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    _add_observed_cycles(monkeypatch)
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    invocation = tmp_path / "reports/1"

    def baseline_role(document):
        document["simulation_record_id"] = SIMULATION_RECORD_ID
        document["target"].update(role="cycle_count_baseline", revision="baseline")

    _, baseline = _successful_pre_sim_record(
        baseline_root,
        monkeypatch,
        "immutable",
        transform=baseline_role,
        invocation_directory=invocation,
    )
    _, candidate = _successful_pre_sim_record(
        candidate_root,
        monkeypatch,
        "immutable",
        invocation_directory=invocation,
    )
    assert candidate.aggregate_grade == "pass"
    return invocation, candidate, baseline


def test_two_role_hooks_share_one_card_and_sim_json_projection(tmp_path, monkeypatch):
    from booley.flows.sim.execution.pre_sim import TAIL_MAX_BYTES
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.record import collect_pre_sim_firings

    invocation, candidate, baseline = _two_role_simulation_records(tmp_path, monkeypatch)
    firings = collect_pre_sim_firings(candidate.manifest_path, baseline.manifest_path)
    assert len(firings) == 2

    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"])
    # Private seam: the card and sim.json projection are built only inside
    # run_prepared_simulation, which needs a real admission and reservation; this test
    # hands the projection two already-published records.
    result = flow._simulation_record_endpoint_outcome([candidate, baseline])
    detail = result.detail
    assert detail["pre_sim_total"] == detail["pre_sim_current"] == 2
    assert detail["pre_sim_historical"] == 0
    assert set(detail["pre_sim_roles"]) == {"candidate", "cycle_count_baseline"}
    # Each role's manifest reference carries the Simulation Record kind.
    assert {
        reference["kind"]
        for role in detail["pre_sim_roles"].values()
        for reference in role["manifests"]
    } == {"simulation_record_manifest"}
    card = result.report_text.splitlines()
    card_hooks = [line for line in card if line.startswith("pre_run_commands ")]
    assert sorted(card_hooks) == sorted(detail["pre_sim_lines"])
    assert card.count("Candidate:") == card.count("Cycle Count baseline:") == 1

    # Bounded output: previews carry no tails, persisted tails stay within the cap.
    assert all(
        "stdout_tail" not in run and "stderr_tail" not in run for run in detail["pre_sim_runs"]
    )
    assert len(json.dumps(detail["pre_sim_runs"]).encode()) <= 16384
    for firing in firings:
        for tail in (firing.document["stdout_tail"], firing.document["stderr_tail"]):
            assert 0 < len(tail.encode()) <= TAIL_MAX_BYTES
    assert {(run["role"], run["attempt_id"]) for run in detail["pre_sim_runs"]} == {
        (firing.document["role"], firing.document["attempt_id"]) for firing in firings
    }


def test_an_escaping_work_item_id_is_refused_before_use(tmp_path):
    from booley.flows.sim.record.codec import (
        SimulationRecordIntegrityError,
        canonical_json_bytes,
    )
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.test_simulation_record_manifest_codec import _manifest

    document = _manifest()
    escaping = "item:0000:x/../../e"
    document["work_items"][0]["work_item_id"] = escaping
    store = SimulationRecordStore(tmp_path / "reports/1/targets/sim/record")
    store.root.mkdir(parents=True)
    store.manifest_path.write_bytes(canonical_json_bytes(document))

    with pytest.raises(SimulationRecordIntegrityError, match="not one contained path component"):
        store.work_item_directory(escaping)
