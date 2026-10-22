"""Goal waiver decisions install strict live policy and recover captured file effects."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim import coverage_waiver_application as application
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
from booley.flows.sim.coverage_waivers import CoverageWaiverValidationError
from booley.flows.sim.waiver_candidates import (
    CampaignBinding,
    WaiverProposal,
    candidate_id,
    load,
    record_proposals,
)
from booley.goals.apply import ChangeEnvironment, recover
from booley.goals.binding import bind_run
from booley.goals.change_service import create_proposal
from booley.goals.entry import EntryEnvironment
from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.goals.paths import record_paths
from booley.goals.proposals import Approval, Decision, ProposalError, load_proposal
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from booley.mcp import goal_waivers as service
from booley.mcp.goal_freshness import GOAL_FRESHNESS_RESOLVERS
from booley.mcp.goal_waivers import GoalWaiverService
from tests.flows.sim.test_coverage_campaign import _POINT_ID, _valid_document
from tests.goals.conftest import enter_goals, git, run_row
from tests.goals.test_proposals import Crash, approve, interrupted_transaction

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

KEY = "coverage_top"
IDENTITY = DurableTargetIdentity("::top:0#top")


def existing_approval_file(case, source_bytes):
    """Return an approval file for another hit point; ``id`` reuses the candidate's waiver id."""
    waiver_id = candidate_id(str(IDENTITY), _POINT_ID, sha256_of(source_bytes))
    fields = (
        f'id = "{waiver_id if case == "id" else "existing-waiver"}"',
        f'target = "{IDENTITY}"',
        f'point_id = "{fixture_point(_valid_document()["points"][0], 1, hit=True)["id"]}"',
        'reason = "excluded"',
        'justification = "Intentionally excluded."',
        'approved_by = "owner@example.test"',
        'approved_at = "2026-08-31T09:00:00Z"',
        'approval_ref = "review:CR-1"',
    )
    header = (
        'schema = "booley.coverage-waivers/v1"\nsource = "rtl/counter.sv"\n'
        f'source_sha256 = "{sha256_of(source_bytes)}"\n'
    )
    if case == "inline":
        return header + "approval = [{ " + ", ".join(fields) + " }]\n"
    return header + "\n[[approval]]\n" + "\n".join(fields) + "\n"


def sha256_of(raw):
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def prepared(
    layout,
    anchor="rtl_repository",
    directory="approvals",
    *,
    covered=1,
    total=2,
    protected=False,
    existing=None,
):
    protected_inputs = ""
    if protected:
        from booley.flows.sim.coverage_waivers import CoverageRepositoryRoots
        from tests.flows.sim import test_coverage_waivers as waivers

        roots = CoverageRepositoryRoots(layout.worktree, layout.worktree)
        destination = waivers._write_valid_approval(roots)
        content = destination.read_text().replace(str(waivers._TARGET), str(IDENTITY))
        other = fixture_point(_valid_document()["points"][0], 1, hit=True)["id"]
        destination.write_text(content.replace(waivers._POINT_ID, other))
        relative = destination.relative_to(layout.worktree).as_posix()
        protected_inputs = f"protected_inputs = ['{relative}']\n"
        git(layout.worktree, "add", relative)
    (layout.worktree / ".booley" / "booley.toml").write_text(
        f"[project]\nname = 'demo'\n{protected_inputs}"
        f"[coverage.waivers]\nanchor = '{anchor}'\ndirectory = '{directory}'\n"
    )
    root = layout.worktree if anchor == "rtl_repository" else layout.worktree / ".booley"
    (root / directory).mkdir(parents=True, exist_ok=True)
    source = layout.worktree / "rtl" / "counter.sv"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"module counter; endmodule\n")
    if existing:
        # Written before the Campaign is observed: the observation binds the waiver set.
        approval = root / directory / "rtl" / "counter.sv.toml"
        approval.parent.mkdir(parents=True)
        approval.write_text(existing_approval_file(existing, source.read_bytes()))
        git(layout.worktree, "add", approval.relative_to(layout.worktree).as_posix())
    git(layout.worktree, "add", "rtl/counter.sv")
    git(layout.worktree, "commit", "-qm", "coverage source")
    layout.record = enter_goals(
        layout,
        [
            {"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 100}},
            {"family": "LINT", "target": "top"},
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
        detail={
            "coverage_campaign_reference": persisted,
            "evaluation": evaluation_json(campaign),
            "test_runs": [run_row()],
        },
    )
    recorder.record_changes(
        state, changes, invocation_id="coverage", producer="simulation_campaign"
    )
    state.save()
    candidate = fixture_candidate(paths, campaign, loaded, persisted, source_sha)
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
    document["tests"]["runs"][0].update(test="smoke", test_run="smoke")
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


def fixture_candidate(paths, campaign, loaded, persisted, source_sha):
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
        binding,
        (item,),
        invocation_id="analyst",
        now=datetime.now(UTC),
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


def waiver_request(candidate, rationale):
    return {"kind": "waiver", "goal_key": KEY, "candidate_id": candidate, "rationale": rationale}


def propose(layout, env, candidate):
    return create_proposal(
        env,
        layout.record.id,
        waiver_request(candidate, "approve this exact measured point"),
        session_key=None,
    )


def relaxation(percent, rationale):
    return {
        "kind": "relax",
        "goal_key": KEY,
        "rationale": rationale,
        "after": {
            "family": "COVERAGE",
            "target": "top",
            "tests": "all",
            "metrics": {"line": percent},
        },
    }


def public_change(layout, env, arguments):
    from booley.mcp.goal_changes import propose_change

    result = propose_change(
        {"work_dir": str(layout.worktree), "operation": "create", **arguments}, env.entry, None
    )
    proposal_id = json.loads(result.value[0][0].text)["proposal_id"]
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
    assert json.loads(applied.value[0][0].text)["state"] == "applied"


# Slowest Windows CI duration is 31 s; the headroom rule asks for three times that.
@pytest.mark.timeout(120)
def test_public_relax_then_waive_preserves_current_bounds_on_reloaded_campaign(layout):
    env, candidate, _root = prepared(layout, covered=18, total=20)
    public_change(layout, env, relaxation(90, "accept measured threshold"))
    record = env.store.load(layout.record.id)
    assert build_status(env.store, record).goals[0].status == "met"
    public_change(
        layout,
        env,
        waiver_request(candidate, "exclude one independently reviewed zero-hit point"),
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


# Windows CI: the "after" case took 31 s, past half of the default 60 s budget.
@pytest.mark.timeout(120)
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
    changes = state.set_criterion(KEY, True, detail={**detail, "test_runs": [run_row()]})
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
        relaxation(90, "accept current bounds"),
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


@pytest.mark.parametrize("attack", ["traversal", "candidate"])
def test_waiver_restart_confines_every_destination_before_writing(layout, attack):
    env, candidate, root = prepared(layout)
    path, transaction = interrupted_transaction(layout, env, propose(layout, env, candidate))
    effect = next(row for row in transaction["effects"] if row["label"] == "waiver")
    victim = root.parent / "victim"
    if attack == "traversal":
        effect["path"] = str(root / ".." / "victim")
    else:
        effect["path"] = str(root / "victim")
        effect["label"] = "candidate"
    path.write_text(json.dumps(transaction))
    paths = record_paths(layout.control, layout.record.id)
    before = file_bytes(paths.root)
    with pytest.raises(ProposalError):
        recover(env.store, layout.record.id, env)
    assert before == file_bytes(paths.root)
    assert not victim.exists() and not (root / "victim").exists()
    assert not paths.changes_file.exists() and not list(root.rglob("*.toml"))


def test_rejected_recovery_has_no_authority_to_publish_goal_or_approval_effects(layout):
    from booley.flows.sim.coverage_waiver_promotion import waiver_file_path
    from booley.goals.apply_effects import FileEffect

    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    path, transaction = interrupted_transaction(layout, env, view, answer="reject")
    paths = record_paths(layout.control, layout.record.id)
    destination = root / waiver_file_path("rtl/counter.sv")
    before = destination.read_bytes() if destination.exists() else None
    transaction["effects"].append(
        FileEffect(destination, before, before or b"forged", "waiver").to_json()
    )
    path.write_text(json.dumps(transaction))
    snapshot = file_bytes(paths.root)
    with pytest.raises(ProposalError, match="durable decision authority"):
        recover(env.store, layout.record.id, env)
    assert snapshot == file_bytes(paths.root)
    assert not paths.changes_file.exists() and not list(root.rglob("*.toml"))


@pytest.mark.parametrize("anchor", ["rtl_repository", "project_data_repository"])
def test_immediate_live_promotion_is_strict_and_other_unmet_goal_does_not_block(layout, anchor):
    env, candidate, root = prepared(layout, anchor)
    head = git(layout.worktree, "rev-parse", "HEAD")
    view = propose(layout, env, candidate)
    assert view.proposal.waiver.approval_root == str(root)
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
@pytest.mark.timeout(120)  # Windows CI needs 3x the observed 30-33s runtime.
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
    before = file_bytes(root)
    recover(env.store, layout.record.id, env)
    assert before == file_bytes(root)
    assert (
        load_proposal(record_paths(layout.control, layout.record.id).root, view.proposal.id).state
        == "applied"
    )
    assert build_status(env.store, env.store.load(layout.record.id)).goals[0].status == "stale"


def test_rejection_is_terminal_and_written_to_goal_candidate_store(layout):
    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    approve(layout, env, view.proposal.id, answer="reject")
    store = load(record_paths(layout.control, layout.record.id).root)
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
    env, candidate, root = prepared(layout, directory=".booley/hooks/approvals")
    before = env.store.load(layout.record.id)
    with pytest.raises(ProposalError, match="protected input"):
        propose(layout, env, candidate)
    assert env.store.load(layout.record.id) == before
    assert not list(root.rglob("*.toml"))


def test_protected_approval_file_beneath_unprotected_root_is_refused(layout):
    env, candidate, root = prepared(layout, directory="coverage-waivers", protected=True)
    view = propose(layout, env, candidate)
    before = env.store.load(layout.record.id)
    protected = file_bytes(root)
    with pytest.raises(ProposalError, match="protected input"):
        approve(layout, env, view.proposal.id)
    assert env.store.load(layout.record.id) == before
    assert protected == file_bytes(root)
    paths = record_paths(layout.control, layout.record.id)
    assert load_proposal(paths.root, view.proposal.id).state == "approved"
    assert not paths.changes_file.exists()


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
        relaxation(50, "accept the measured half coverage"),
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


@pytest.mark.parametrize("drift", ["campaign", "candidate", "policy"])
def test_recovery_uses_captured_binding_after_producer_drift(layout, drift):
    import shutil

    env, candidate, root = prepared(layout)
    view = propose(layout, env, candidate)
    _, transaction = interrupted_transaction(layout, env, view)
    paths = record_paths(layout.control, layout.record.id)
    if drift == "campaign":
        shutil.rmtree(paths.runtime_dir / "flow-reports")
    elif drift == "candidate":
        path = paths.root / "waiver-candidates.json"
        raw = json.loads(path.read_bytes())
        next(iter(raw["candidates"].values()))["justification"] = "replaced after approval"
        path.write_text(json.dumps(raw))
    else:
        config = layout.worktree / ".booley" / "booley.toml"
        config.write_text(
            "[project]\nname = 'demo'\n[coverage.waivers]\nanchor = 'rtl_repository'\ndirectory = 'new-approvals'\n"
        )
        (layout.worktree / "new-approvals").mkdir()
    recover(env.store, layout.record.id, env)
    assert load_proposal(paths.root, view.proposal.id).state == "applied"
    for effect in transaction["effects"]:
        assert Path(effect["path"]).read_bytes() == base64.b64decode(effect["after"])
    assert list(root.rglob("*.toml"))
    assert not list((layout.worktree / "new-approvals").glob("*"))


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("id", "COV_WAIVER_ID_DUPLICATE"),
        ("stale", "COV_WAIVER_SOURCE_STALE"),
        ("inline", "COV_WAIVER_FILE_MALFORMED"),
    ],
    ids=["id", "stale", "inline"],
)
def test_existing_approval_file_refusal_names_loader_finding(layout, case, code):
    """The real loader is the only gate: a refused promotion names its finding, writes nothing."""
    env, candidate, root = prepared(layout, existing=case)
    view = propose(layout, env, candidate)
    if case == "stale":
        (layout.worktree / "rtl" / "counter.sv").write_bytes(b"module counter; // edited\n")
    before = file_bytes(root)
    with pytest.raises(ProposalError, match=code):
        approve(layout, env, view.proposal.id)
    assert file_bytes(root) == before
    assert not record_paths(layout.control, layout.record.id).changes_file.exists()


def file_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def tree_snapshot(root):
    return file_bytes(root), {
        p.relative_to(root): p.stat().st_mode for p in (root, *root.rglob("*"))
    }


def observe_prediction(monkeypatch, waiver_service, candidate, refuse):
    captured, seen = [], {}
    promotion = waiver_service._promotion_effects
    loader = application.load_approved_waiver_set

    def capture(*args):
        effects = promotion(*args)
        captured.extend(effects)
        return effects

    def observe(config, roots, targets):
        seen.update(file_bytes(getattr(roots, config.anchor) / config.directory))
        result = loader(config, roots, targets)
        assert result.waivers[0].waiver_id == candidate
        if refuse:
            raise CoverageWaiverValidationError(())
        return result

    capture_spy = Mock(side_effect=capture)
    build = Mock(wraps=service.build_promotion)
    render = Mock(wraps=service.render_approval_document)
    monkeypatch.setattr(waiver_service, "_promotion_effects", capture_spy)
    monkeypatch.setattr(application, "load_approved_waiver_set", observe)
    for module in (application, service):
        monkeypatch.setattr(module, "build_promotion", build, raising=False)
        monkeypatch.setattr(module, "render_approval_document", render, raising=False)
    return captured, seen, (capture_spy, build, render)


@pytest.mark.parametrize("anchor", ["rtl_repository", "project_data_repository"])
@pytest.mark.parametrize("refuse", [False, True])
def test_prediction_uses_captured_after_bytes_once(layout, monkeypatch, anchor, refuse):
    env, candidate, root = prepared(layout, anchor)
    proposal = propose(layout, env, candidate).proposal
    decision = Decision(
        "approve",
        "reviewed",
        Approval.AGENT_RECORDED,
        "approve",
        "2026-10-19T00:00:00Z",
        proposal.payload_digest,
        "test-session",
    )
    state = load_goal_state(env.store, env.store.load(layout.record.id))
    captured, seen, spies = observe_prediction(monkeypatch, env.waivers, candidate, refuse)
    before = tree_snapshot(root)
    expectation = pytest.raises(ProposalError) if refuse else nullcontext()
    with expectation:
        effects, _ = env.waivers.prepare(proposal, decision, state)
        assert len(effects) == 2
    assert tree_snapshot(root) == before
    assert [spy.call_count for spy in spies] == [1, 1, 1]
    assert len(seen) == 2
    assert seen == {e.path.relative_to(root): e.after for e in captured}


def test_candidate_campaign_mismatch_is_ineligible(layout):
    env, candidate, _ = prepared(layout)
    path = record_paths(layout.control, layout.record.id).root / "waiver-candidates.json"
    value = json.loads(path.read_text())
    next(iter(value["candidates"].values()))["campaign_id"] = "different-campaign"
    path.write_text(json.dumps(value))
    with pytest.raises(ProposalError, match="not eligible"):
        propose(layout, env, candidate)
