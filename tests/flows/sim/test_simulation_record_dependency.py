"""Two-role Simulation Records and baseline prerequisites."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.flows.sim.simulation_record_test_support import prerequisite_documents

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
    from booley.flows.sim.record.model import create_simulation_record_plan
    from booley.flows.sim.record.store import SimulationRecordStore
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
    manifest = SimulationRecordStore(baseline.manifest_path.parent).load_manifest()
    prerequisites = prerequisite_documents(
        create_simulation_record_plan(manifest), baseline.manifest_path.relative_to(invocation)
    )
    _, candidate = _successful_pre_sim_record(
        candidate_root,
        monkeypatch,
        "immutable",
        transform=lambda document: document.update(prerequisites=prerequisites),
        invocation_directory=invocation,
    )
    assert candidate.aggregate_grade == "pass"
    return invocation, candidate, baseline


def test_two_role_hooks_share_one_card_and_sim_json_projection(tmp_path, monkeypatch):
    from booley.flows.sim.execution.pre_sim import TAIL_MAX_BYTES
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.record import collect_pre_sim_firings

    invocation, candidate, baseline = _two_role_simulation_records(tmp_path, monkeypatch)
    firings = collect_pre_sim_firings(candidate.manifest_path)
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


def _cycle_baseline_manifest(names):
    """A Cycle Count baseline record with one work item per Test in *names*."""
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.planning import finalize_manifest
    from tests.flows.sim.simulation_record_test_support import _manifest_for
    from tests.flows.sim.test_simulation_record_manifest_codec import _sha

    document = json.loads(canonical_json_bytes(_manifest_for(names).document))
    document.pop("fingerprints")
    document["simulation_record_id"] = SIMULATION_RECORD_ID
    document["target"].update(role="cycle_count_baseline", revision="baseline")
    for ordinal, item in enumerate(document["work_items"]):
        item.update(target=document["target"], role="cycle_count_baseline", revision="baseline")
        identity = {
            key: value
            for key, value in item.items()
            if key not in {"work_item_id", "fingerprint_sha256"}
        }
        item["fingerprint_sha256"] = _sha(identity)
        item["work_item_id"] = f"item:{ordinal:04d}:" + _sha(identity).removeprefix("sha256:")[:16]
    return finalize_manifest(document)


def _candidate_manifest(names, prerequisites):
    """A candidate record over *names* that depends on *prerequisites*."""
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.planning import finalize_manifest
    from tests.flows.sim.simulation_record_test_support import _manifest_for

    document = json.loads(canonical_json_bytes(_manifest_for(names).document))
    document.pop("fingerprints")
    document["prerequisites"] = list(prerequisites)
    return finalize_manifest(document)


def _run_simulation_record(tmp_path, monkeypatch, manifest, invocation):
    from types import SimpleNamespace

    from booley.flows.sim.record.coordinator import (
        NewSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from booley.flows.sim.record.model import create_simulation_record_plan
    from tests.flows.sim.simulation_record_test_support import _admission, _executor, _handle

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (build / "simv").write_bytes(b"image")
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.simulation_target_environment",
        lambda _handle: {},
    )
    request = NewSimulationRecordRunRequest(
        create_simulation_record_plan(manifest),
        project,
        invocation.parent,
        SimulationRecordPolicy(),
        invocation,
        _admission(),
    )
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    return SimulationRecord(_executor(build, counters)).run(request), counters


def _published_baseline(tmp_path, monkeypatch):
    """Run a two-Test Cycle Count baseline; return it, its prerequisite documents and invocation."""
    from booley.flows.sim.record.model import create_simulation_record_plan
    from booley.flows.sim.record.store import SimulationRecordStore

    _add_observed_cycles(monkeypatch)
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    baseline, _ = _run_simulation_record(
        tmp_path, monkeypatch, _cycle_baseline_manifest(("alpha", "beta")), invocation
    )
    manifest = SimulationRecordStore(baseline.manifest_path.parent).load_manifest()
    prerequisites = prerequisite_documents(
        create_simulation_record_plan(manifest), baseline.manifest_path.relative_to(invocation)
    )
    return baseline, prerequisites, invocation


def _shared_baseline_candidate(tmp_path, monkeypatch):
    """Run a baseline, then a candidate linking both of its work items.

    Returns the baseline outcome, the candidate's prerequisite documents, the candidate
    outcome and the candidate run's execution counters.
    """
    baseline, prerequisites, invocation = _published_baseline(tmp_path, monkeypatch)
    candidate, counters = _run_simulation_record(
        tmp_path, monkeypatch, _candidate_manifest(("alpha", "beta"), prerequisites), invocation
    )
    return baseline, prerequisites, candidate, counters


def test_relative_cycle_criteria_on_two_tests_share_one_baseline_simulation_record(
    tmp_path, monkeypatch
):
    """Two relative Cycle Count Criteria of one Target link two work items of one baseline.

    Both prerequisites name the same baseline record, which used to be refused
    as a duplicate ("prerequisite identities must be unique") or run as a cycle.
    """
    _baseline, prerequisites, candidate, counters = _shared_baseline_candidate(
        tmp_path, monkeypatch
    )

    assert len(prerequisites) == 2
    assert len({item["simulation_record_id"] for item in prerequisites}) == 1
    assert candidate.aggregate_grade == "pass"
    facts = candidate.acceptance_facts.document["prerequisites"]
    assert [fact["cycle_observation"]["test"] for fact in facts] == ["alpha", "beta"]
    assert counters["launch"] == 2  # the shared baseline already ran; only the candidate runs


def test_resume_authenticates_a_shared_baseline_simulation_record_once(tmp_path, monkeypatch):
    """Resuming a candidate whose links share one baseline manifest walks it once."""
    from types import SimpleNamespace

    from booley.flows.sim.record import resume
    from tests.flows.sim.simulation_record_test_support import _handle

    baseline, _prerequisites, candidate, _counters = _shared_baseline_candidate(
        tmp_path, monkeypatch
    )
    project = tmp_path / "project"
    baseline_checkout = tmp_path / "baseline-checkout"
    baseline_checkout.mkdir()
    revisions = {project.resolve(): "abc123", baseline_checkout.resolve(): "baseline"}
    monkeypatch.setattr(resume, "git_full_sha", lambda _ref, root: revisions[Path(root).resolve()])
    monkeypatch.setattr(
        resume.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )

    validated = resume.validate_resume_manifest(
        candidate.manifest_path,
        project_root=project,
        revision_root=lambda target: (
            project if target["role"] == "candidate" else baseline_checkout
        ),
    )

    assert [node.path for node in validated.prerequisites] == [baseline.manifest_path]


def _prerequisite_entry(manifest_path, record_id, target, work_item_id):
    """A cycle-count baseline prerequisite link to *manifest_path* with placeholder size and digest."""
    return {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": manifest_path,
            "bytes": 1,
            "sha256": "sha256:" + "a" * 64,
            "kind": "simulation_record_manifest",
            "owner": record_id,
        },
        "simulation_record_id": record_id,
        "target": target,
        "required_observation": "cycle_count",
        "work_item_id": work_item_id,
    }


def test_cycle_prerequisite_refuses_a_baseline_manifest_whose_digest_disagrees(
    tmp_path, monkeypatch
):
    """A prerequisite link naming a digest other than the baseline manifest's is unauthenticated."""
    from booley.flows.sim.record.codec import SimulationRecordIntegrityError

    _baseline, prerequisites, invocation = _published_baseline(tmp_path, monkeypatch)
    tampered = [
        {**entry, "manifest": {**entry["manifest"], "sha256": "sha256:" + "f" * 64}}
        for entry in prerequisites
    ]

    with pytest.raises(SimulationRecordIntegrityError, match="unauthenticated"):
        _run_simulation_record(
            tmp_path, monkeypatch, _candidate_manifest(("alpha", "beta"), tampered), invocation
        )


def test_a_simulation_record_that_lists_itself_as_prerequisite_is_a_cycle(tmp_path, monkeypatch):
    """Sharing a prerequisite record is allowed; depending on oneself is not."""
    from booley.flows.sim.record.codec import SimulationRecordIntegrityError
    from tests.flows.sim.simulation_record_test_support import _manifest_for

    own = _manifest_for(("alpha",)).document
    baseline = _cycle_baseline_manifest(("alpha",))
    entry = _prerequisite_entry(
        "targets/self/record/manifest.json",
        own["simulation_record_id"],
        baseline.document["target"],
        baseline.document["work_items"][0]["work_item_id"],
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    with pytest.raises(SimulationRecordIntegrityError, match="cyclic"):
        _run_simulation_record(
            tmp_path, monkeypatch, _candidate_manifest(("alpha",), [entry]), invocation
        )


def _write_candidate_with_prerequisite(tmp_path, manifest_path):
    """A candidate manifest on disk linking one prerequisite at *manifest_path*."""
    from booley.flows.sim.record.codec import canonical_json_bytes
    from tests.flows.sim.test_simulation_record_manifest_codec import _manifest

    document = _manifest()
    entry = _prerequisite_entry(
        manifest_path,
        SIMULATION_RECORD_ID,
        {**document["target"], "role": "cycle_count_baseline"},
        "item:0000:0123456789abcdef",
    )
    document["prerequisites"] = [entry]
    path = tmp_path / "reports/1/targets/sim/record/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(document))
    return path


@pytest.mark.parametrize("walk", ["resume-walk", "pre-sim-walk"])
def test_an_escaping_prerequisite_manifest_path_is_refused_before_use(tmp_path, walk):
    """Decode no longer checks prerequisite paths, so each walk checks the one it opens."""
    from booley.flows.sim.record import collect_pre_sim_firings
    from booley.flows.sim.record.codec import SimulationRecordIntegrityError
    from booley.flows.sim.record.resume import validate_resume_manifest

    path = _write_candidate_with_prerequisite(
        tmp_path, "../outside/targets/x/record/manifest.json"
    )
    walk_function = (
        collect_pre_sim_firings
        if walk == "pre-sim-walk"
        else lambda manifest: validate_resume_manifest(manifest, project_root=tmp_path)
    )

    with pytest.raises(
        SimulationRecordIntegrityError, match=r"is not targets/<Target>/record/manifest\.json"
    ):
        walk_function(path)


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
