"""Goal waiver decisions install strict live policy and recover captured file effects."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from uuid import uuid4

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.coverage_campaign import DurableTargetIdentity, decode_coverage_campaign
from booley.flows.sim.coverage_campaign_store import (
    load_coverage_campaign,
    publish_coverage_campaign,
)
from booley.flows.sim.coverage_policy import (
    CoverageCriterion,
    CoverageThreshold,
    evaluate_coverage_campaign,
)
from booley.flows.sim.coverage_reference import (
    build_coverage_campaign_reference,
    encode_coverage_campaign_reference,
    publish_coverage_campaign_reference,
)
from booley.flows.sim.coverage_waiver_application import evaluation_json, strict_reevaluation
from booley.goals.apply import ChangeEnvironment, recover
from booley.goals.binding import bind_run
from booley.goals.change_service import create_proposal
from booley.goals.entry import EntryEnvironment
from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalError, load_proposal
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from booley.mcp.goal_freshness import GOAL_FRESHNESS_RESOLVERS
from booley.mcp.goal_waivers import GoalWaiverService
from booley.ticket_board.waiver_candidates import (
    CampaignBinding,
    WaiverProposal,
    load,
    record_proposals,
)
from tests.flows.sim.test_coverage_campaign import _POINT_ID, _valid_document
from tests.goals.conftest import enter_goals, git
from tests.goals.test_proposals import Crash, approve, interrupted_transaction

KEY = "coverage_top"
IDENTITY = DurableTargetIdentity("::top:0#top")


def prepared(layout, anchor="rtl_repository", directory="approvals", *, covered=1, total=2):
    config = layout.worktree / ".booley_project" / "booley.toml"
    config.write_text(
        f"[project]\nname = 'demo'\n[coverage.waivers]\nanchor = '{anchor}'\ndirectory = '{directory}'\n"
    )
    root = layout.worktree if anchor == "rtl_repository" else layout.worktree / ".booley_project"
    (root / directory).mkdir(parents=True, exist_ok=True)
    source = layout.worktree / "rtl" / "counter.sv"
    source.parent.mkdir()
    source.write_bytes(b"module counter; endmodule\n")
    git(layout.worktree, "add", "rtl/counter.sv")
    git(layout.worktree, "commit", "-qm", "coverage source")
    layout.record = enter_goals(
        layout,
        [
            {"family": "coverage", "target": "top", "tests": "all", "metrics": {"line": 100}},
            {"family": "lint", "target": "top"},
        ],
    )
    service = GoalWaiverService(layout.control, layout.record)
    paths = record_paths(layout.control, layout.record.id)
    campaign, source_sha = fixture_campaign(source, service, covered=covered, total=total)
    persisted, loaded = persist_campaign(paths, campaign)
    recorder = GoalEvidenceRecorder(
        bind_run(GoalStore(layout.control), layout.worktree, "coverage"),
        resolvers=GOAL_FRESHNESS_RESOLVERS,
    )
    state = DevelopmentState.load(paths.state_file, recorder.state_persistence())
    changes = state.set_criterion(
        KEY,
        False,
        detail={"coverage_campaign_reference": persisted, "evaluation": evaluation_json(campaign)},
    )
    recorder.record_changes(
        state, changes, invocation_id="coverage", producer="simulation_campaign"
    )
    state.save()
    candidate = fixture_candidate(paths, campaign, loaded, persisted, source_sha, layout.record.id)
    return (
        ChangeEnvironment(EntryEnvironment(layout.control), service),
        candidate,
        root / directory,
    )


def fixture_campaign(source, service, *, covered=1, total=2):
    document = _valid_document()
    document["collector"]["capabilities"][0].update(collection="supported", scoring="scored_v1")
    document["invocation"]["id"] = 1
    document["target"] = {"identity": str(IDENTITY), "selector": "top"}
    document["tests"]["declared"] = document["tests"]["selected"] = ["smoke"]
    document["tests"]["runs"][0]["test"] = "smoke"
    seed = copy.deepcopy(document["points"][0])
    document["points"] = [
        fixture_point(seed, index, hit=0 < index <= covered) for index in range(total)
    ]
    document["rollups"][0].update(
        total_points=total,
        eligible_points=total,
        covered_points=covered,
        percent=100 * covered / total,
    )
    source_sha = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    document["source_closure"]["rtl"][0]["sha256"] = source_sha
    campaign = decode_coverage_campaign(document, IDENTITY)
    campaign = evaluate_coverage_campaign(
        campaign,
        CoverageCriterion(IDENTITY, (CoverageThreshold("line", Fraction(100)),), None),
        service._current_waivers(),
    )
    return campaign, source_sha


def fixture_point(seed, index, *, hit):
    point = copy.deepcopy(seed)
    if index:
        point["identity"]["subject"]["basic_block"] = index
        point["identity"]["collector"]["native_key"] = f"10:basic-block-{index}"
        identity_bytes = json.dumps(
            point["identity"], sort_keys=True, separators=(",", ":")
        ).encode()
        point["id"] = "cp1:" + base64.urlsafe_b64encode(identity_bytes).decode().rstrip("=")
    if not hit:
        point["hits_by_run"] = {}
    return point


def fixture_candidate(paths, campaign, loaded, persisted, source_sha, record_id):
    item = WaiverProposal(
        _POINT_ID,
        "rtl/counter.sv",
        source_sha,
        "unreachable",
        "Reviewed fixture exclusion",
        ("rtl/counter.sv:10",),
    )
    binding = CampaignBinding(
        campaign.campaign_id,
        loaded.summary.manifest_sha256,
        loaded.summary.point_store.sha256,
        persisted["path"],
        str(IDENTITY),
        "top",
    )
    outcome = record_proposals(
        paths.root,
        record_id,
        binding,
        (item,),
        invocation_id="analyst",
        now=datetime.now(UTC),
        directory=paths.root,
    )
    return outcome.candidate_ids[0]


def persist_campaign(paths, campaign):
    reports = paths.runtime_dir / "flow-reports"
    origin = reports / "sim" / "1" / "targets" / "top"
    attempt = str(uuid4())
    nested = publish_coverage_campaign(
        origin
        / "campaign"
        / "work-items"
        / "0001-0123456789abcdef"
        / "attempts"
        / f"0001-{attempt}"
        / "coverage-campaign",
        campaign,
    ).campaign
    loaded = load_coverage_campaign(nested)
    reference = build_coverage_campaign_reference(
        simulation_campaign_id=str(uuid4()),
        simulation_manifest_sha256="sha256:" + "a" * 64,
        target_identity=str(IDENTITY),
        target_selector="top",
        origin_invocation_id=1,
        producer_invocation_id=1,
        simulation_work_item_id="item:0000:0123456789abcdef",
        simulation_attempt_id=attempt,
        origin_target_directory=origin,
        coverage_campaign_path=nested,
    )
    public = origin / "coverage.json"
    publish_coverage_campaign_reference(public, reference)
    raw = encode_coverage_campaign_reference(reference)
    persisted = {
        "path_base": "reports_root",
        "path": public.relative_to(reports).as_posix(),
        "bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "nested_campaign_sha256": reference.document["coverage_campaign"]["sha256"],
    }
    return persisted, loaded


def propose(layout, env, candidate):
    return create_proposal(
        env,
        layout.record.id,
        {
            "kind": "waiver",
            "goal_key": KEY,
            "candidate_id": candidate,
            "rationale": "approve this exact measured point",
        },
        session_key=None,
    )


def public_change(layout, env, arguments):
    from booley.mcp.goal_changes import propose_change

    result = propose_change(
        {"work_dir": str(layout.worktree), "operation": "create", **arguments}, env.entry, None
    )
    proposal_id = json.loads(result.value[0].text)["proposal_id"]
    applied = propose_change(
        {
            "work_dir": str(layout.worktree),
            "operation": "approve",
            "proposal_id": proposal_id,
            "reason": "accept exact measured fixture",
            "approval_quote": "Approve this proposal",
        },
        env.entry,
        None,
    )
    assert json.loads(applied.value[0].text)["state"] == "applied"


def test_public_relax_then_waive_preserves_current_bounds_on_reloaded_campaign(layout):
    env, candidate, _root = prepared(layout, covered=18, total=20)
    public_change(
        layout,
        env,
        {
            "kind": "relax",
            "goal_key": KEY,
            "rationale": "accept measured threshold",
            "after": {
                "family": "coverage",
                "target": "top",
                "tests": "all",
                "metrics": {"line": 90},
            },
        },
    )
    record = env.store.load(layout.record.id)
    assert build_status(env.store, record).goals[0].status == "met"
    public_change(
        layout,
        env,
        {
            "kind": "waiver",
            "goal_key": KEY,
            "candidate_id": candidate,
            "rationale": "exclude one independently reviewed zero-hit point",
        },
    )
    record = env.store.load(layout.record.id)
    state = load_goal_state(env.store, record)
    evaluation = state.criteria[KEY].detail["evaluation"]
    assert build_status(env.store, record).goals[0].status == "met"
    assert evaluation["thresholds"] == {"line": 90}
    assert evaluation["metrics"][0]["eligible_points"] == 19
    assert evaluation["metrics"][0]["covered_points"] == 18
    assert evaluation["metrics"][0]["waived_points"] == 1


def remove_policy(root):
    for path in root.rglob("*"):
        if path.is_file():
            path.unlink()


@pytest.mark.parametrize("race", ["before", "during", "after", "missing_reader"])
def test_recorder_never_stamps_removed_producer_waivers_as_fresh(layout, race):
    env, candidate, root = prepared(layout)
    approve(layout, env, propose(layout, env, candidate).proposal.id)
    record = env.store.load(layout.record.id)
    current = load_goal_state(env.store, record)
    producer = strict_reevaluation(
        env.waivers._resolved(current.criteria[KEY].detail).loaded.campaign,
        env.waivers._current_waivers(),
    )
    detail = {"evaluation": evaluation_json(producer)}
    assert detail["evaluation"]["status"] == "pass"
    resolvers = publication_race_resolvers(root, race)
    recorder = GoalEvidenceRecorder(
        bind_run(env.store, layout.worktree, "policy-race"), resolvers=resolvers
    )
    state = DevelopmentState.load(
        record_paths(layout.control, record.id).state_file, recorder.state_persistence()
    )
    if race == "before":
        remove_policy(root)
    changes = state.set_criterion(KEY, True, detail=detail)
    recorder.record_changes(state, changes, invocation_id="policy-race", producer="sim")
    state.save()
    saved = load_goal_state(env.store, record).criteria[KEY]
    assert saved.detail["evaluation"] == detail["evaluation"]
    status = build_status(env.store, record).goals[0]
    assert status.status == "stale" and "waiver policy" in status.reason
    if race != "after":
        assert "error" in saved.detail["goal_waiver_policy"]


def publication_race_resolvers(root, race):
    from booley.mcp.goal_freshness import approved_waiver_digest

    def semantics(work_dir):
        result = approved_waiver_digest(work_dir)
        if race == "during":
            remove_policy(root)
        return result

    def source(work_dir, **kwargs):
        result = DEFAULT_RESOLVERS.source(work_dir, **kwargs)
        if race == "after":
            remove_policy(root)
        return result

    return replace(
        GOAL_FRESHNESS_RESOLVERS,
        source=source,
        waiver_semantics=None if race == "missing_reader" else semantics,
    )


@pytest.mark.parametrize("race", ["during", "after"])
def test_numeric_derivation_keeps_checked_policy_or_refuses_drift(layout, monkeypatch, race):
    env, candidate, root = prepared(layout)
    approve(layout, env, propose(layout, env, candidate).proposal.id)
    view = create_proposal(
        env,
        layout.record.id,
        {
            "kind": "relax",
            "goal_key": KEY,
            "rationale": "accept current bounds",
            "after": {
                "family": "coverage",
                "target": "top",
                "tests": "all",
                "metrics": {"line": 90},
            },
        },
        session_key=None,
    )
    if race == "during":
        original = env.waivers._current_waivers
        calls = 0

        def load_with_drift():
            nonlocal calls
            value = original()
            calls += 1
            if calls == 2:
                remove_policy(root)
            return value

        monkeypatch.setattr(env.waivers, "_current_waivers", load_with_drift)
        with pytest.raises(ProposalError, match="changed during derivation"):
            approve(layout, env, view.proposal.id)
    else:
        original = env.waivers._reevaluate

        def evaluate_with_drift(*args):
            value = original(*args)
            remove_policy(root)
            return value

        monkeypatch.setattr(env.waivers, "_reevaluate", evaluate_with_drift)
        approve(layout, env, view.proposal.id)
        assert build_status(env.store, env.store.load(layout.record.id)).goals[0].status == "stale"


@pytest.mark.parametrize("attack", ["traversal", "absolute", "sibling", "candidate"])
def test_waiver_restart_confines_every_destination_before_writing(layout, attack):
    env, candidate, root = prepared(layout)
    path, transaction = interrupted_transaction(layout, env, propose(layout, env, candidate))
    effect = next(row for row in transaction["effects"] if row["label"] == "waiver")
    victim = root.parent / "victim"
    if attack == "traversal":
        effect["path"] = str(root / ".." / "victim")
    elif attack == "absolute":
        effect["path"] = str(victim)
    else:
        effect["path"] = str(root / "victim")
        if attack == "candidate":
            effect["label"] = "candidate"
    path.write_text(json.dumps(transaction))
    paths = record_paths(layout.control, layout.record.id)
    before = {item: item.read_bytes() for item in paths.root.rglob("*") if item.is_file()}
    with pytest.raises(ProposalError):
        recover(env.store, layout.record.id, env)
    assert before == {item: item.read_bytes() for item in paths.root.rglob("*") if item.is_file()}
    assert not victim.exists() and not (root / "victim").exists()
    assert not paths.changes_file.exists() and not list(root.rglob("*.toml"))


@pytest.mark.parametrize("label", ["record", "state", "ledger", "waiver"])
def test_rejected_recovery_has_no_authority_to_publish_goal_or_approval_effects(layout, label):
    from booley.flows.sim.coverage_waiver_promotion import waiver_file_path
    from booley.goals.apply_effects import FileEffect

    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    path, transaction = interrupted_transaction(layout, env, view, answer="reject")
    paths = record_paths(layout.control, layout.record.id)
    destination = {
        "record": paths.record_file,
        "state": paths.state_file,
        "ledger": paths.logs_dir / "acceptance" / "evidence" / "forged" / "record.json",
        "waiver": root / waiver_file_path("rtl/counter.sv"),
    }[label]
    before = destination.read_bytes() if destination.exists() else None
    transaction["effects"].append(
        FileEffect(destination, before, before or b"forged", label).to_json()
    )
    path.write_text(json.dumps(transaction))
    snapshot = {item: item.read_bytes() for item in paths.root.rglob("*") if item.is_file()}
    with pytest.raises(ProposalError, match="decision's authority"):
        recover(env.store, layout.record.id, env)
    assert snapshot == {
        item: item.read_bytes() for item in paths.root.rglob("*") if item.is_file()
    }
    assert not paths.changes_file.exists() and not list(root.rglob("*.toml"))


@pytest.mark.parametrize("anchor", ["rtl_repository", "project_data_repository"])
def test_immediate_live_promotion_is_strict_and_other_unmet_goal_does_not_block(layout, anchor):
    env, candidate, root = prepared(layout, anchor)
    head = git(layout.worktree, "rev-parse", "HEAD")
    view = propose(layout, env, candidate)
    assert view.proposal.waiver["approval_root"] == str(root)
    approve(layout, env, view.proposal.id)
    assert git(layout.worktree, "rev-parse", "HEAD") == head
    assert list(root.rglob("*.toml")) and list(root.rglob("*.md"))
    assert env.waivers._current_waivers().waivers[0].waiver_id == candidate
    statuses = build_status(env.store, env.store.load(layout.record.id))
    assert [goal.status for goal in statuses.goals] == ["met", "unmet"]
    state = load_goal_state(env.store, env.store.load(layout.record.id))
    assert state.criteria[KEY].detail["goal_derivation"]["source_evidence"]
    next(root.rglob("*.md")).write_text("external proof change")
    assert build_status(env.store, env.store.load(layout.record.id)).goals[0].status == "stale"


@pytest.mark.parametrize(
    "boundary",
    [
        "approved",
        "intent",
        "effect:record:0",
        "effect:waiver:1",
        "effect:waiver:2",
        "effect:ledger:3",
        "effect:state:4",
        "applied",
    ],
)
def test_each_waiver_effect_recovers_without_revalidating_mutable_inputs(layout, boundary):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)

    def crash(at):
        if at == boundary:
            raise Crash(at)

    with pytest.raises(Crash):
        approve(layout, replace(env, on_boundary=crash), view.proposal.id)
    (layout.worktree / "rtl.v").write_text("module top; wire drift; endmodule\n")
    recover(env.store, layout.record.id, env)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    recover(env.store, layout.record.id, env)
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert (
        load_proposal(record_paths(layout.control, layout.record.id).root, view.proposal.id).state
        == "applied"
    )
    assert build_status(env.store, env.store.load(layout.record.id)).goals[0].status == "stale"


def test_rejection_is_terminal_and_written_to_goal_candidate_store(layout):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    approve(layout, env, view.proposal.id, answer="reject")
    store = load(
        root, layout.record.id, directory=record_paths(layout.control, layout.record.id).root
    )
    assert not store.candidates and len(store.rejections) == 1
    assert not list(root.rglob("*.toml"))


@pytest.mark.parametrize("drift", ["source", "target", "policy", "candidate"])
def test_decision_revalidates_all_exact_bindings(layout, drift):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    if drift == "source":
        (layout.worktree / "rtl" / "counter.sv").write_text(
            "module counter; wire changed; endmodule\n"
        )
    elif drift == "target":
        (layout.worktree / "top.core").write_text(
            (layout.worktree / "top.core").read_text()
            + "parameters:\n  extra: {datatype: int, default: 1}\n"
        )
    elif drift == "policy":
        (root / "foreign.md").write_text("new policy bytes")
    else:
        path = record_paths(layout.control, layout.record.id).root / "waiver-candidates.json"
        value = json.loads(path.read_text())
        next(iter(value["candidates"].values()))["justification"] = "replacement evidence"
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, ProposalError)):
        approve(layout, env, view.proposal.id)
    assert (
        load_proposal(record_paths(layout.control, layout.record.id).root, view.proposal.id).state
        == "pending"
    )


def test_protected_approval_destination_is_refused_without_resetting_entry_digest(layout):
    env, candidate, root = prepared(layout, directory=".booley_project/hooks/approvals")
    before = env.store.load(layout.record.id)
    with pytest.raises(ProposalError, match="protected input"):
        propose(layout, env, candidate)
    assert env.store.load(layout.record.id) == before
    assert not list(root.rglob("*.toml"))


def test_unexpected_waiver_file_bytes_conflict_without_overwrite(layout):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)

    def crash(at):
        if at == "effect:waiver:1":
            raise Crash(at)

    with pytest.raises(Crash):
        approve(layout, replace(env, on_boundary=crash), view.proposal.id)
    approval = next(root.rglob("*.toml"))
    captured = approval.read_bytes()
    approval.write_bytes(b"third-party bytes\n")
    with pytest.raises(ProposalError, match="recovery conflict"):
        recover(env.store, layout.record.id, env)
    assert approval.read_bytes() == b"third-party bytes\n"
    approval.write_bytes(captured)
    recover(env.store, layout.record.id, env)
    assert build_status(env.store, env.store.load(layout.record.id)).goals[0].status == "met"


def test_numeric_coverage_relaxation_reevaluates_immutable_points_without_waiver_files(layout):
    env, _candidate, root = prepared(layout)
    before = load_goal_state(env.store, layout.record).criteria[KEY].detail
    view = create_proposal(
        env,
        layout.record.id,
        {
            "kind": "relax",
            "goal_key": KEY,
            "after": {
                "family": "coverage",
                "target": "top",
                "tests": "all",
                "metrics": {"line": 50},
            },
            "rationale": "accept the measured half coverage",
        },
        session_key=None,
    )
    approve(layout, env, view.proposal.id)
    current = env.store.load(layout.record.id)
    entry = load_goal_state(env.store, current).criteria[KEY]
    assert build_status(env.store, current).goals[0].status == "met"
    assert entry.params["metrics"] == {"line": {"min_pct": 50}}
    assert entry.detail["evaluation"]["thresholds"] == {"line": 50}
    assert entry.detail["evaluation"]["metrics"][0]["actual_percent"] == 50
    assert entry.detail["_source_fingerprint"] == before["_source_fingerprint"]
    assert not list(root.rglob("*.toml"))
