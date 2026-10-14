"""Saved waiver requests validate before touching approval or candidate storage."""

import json
from pathlib import Path

import pytest

from booley.goals.proposals import Proposal, ProposalError, encode, load_proposal

FIXTURE = Path(__file__).parents[1] / "runtime/fixtures/directory_migration/waiver-proposal-v1"


def legacy_payload():
    return json.loads((FIXTURE / "payload.json").read_bytes())


def test_legacy_waiver_proposal_and_decision_keep_exact_bytes(tmp_path):
    raw = (FIXTURE / "payload.json").read_bytes()
    proposal = Proposal.from_json(json.loads(raw))
    assert encode(proposal.to_json()) == raw
    assert (
        proposal.payload_digest
        == "99bdf7554f555886daad421b48f5eb4ba7bdefa8344974ad83428df0c8acc4b9"
    )
    root = tmp_path / "proposals" / proposal.id
    root.mkdir(parents=True)
    (root / "payload.json").write_bytes(raw)
    (root / "state.json").write_bytes((FIXTURE / "state.json").read_bytes())
    saved = load_proposal(tmp_path, proposal.id)
    assert saved.state == "approved"
    assert saved.decision.payload_digest == proposal.payload_digest
    assert encode(saved.metadata()) == (FIXTURE / "state.json").read_bytes()


@pytest.mark.parametrize("field", tuple(legacy_payload()["waiver"]))
@pytest.mark.parametrize("malformed", ["missing", "type"])
def test_saved_binding_refuses_bad_fields(field, malformed):
    raw = legacy_payload()
    if malformed == "missing":
        del raw["waiver"][field]
    else:
        raw["waiver"][field] = 123
    with pytest.raises(ProposalError, match=f"waiver.{field}"):
        Proposal.from_json(raw)


@pytest.mark.parametrize("field", tuple(legacy_payload()["waiver"]["candidate"]))
@pytest.mark.parametrize("malformed", ["missing", "type"])
def test_candidate_snapshot_refuses_bad_fields(field, malformed):
    raw = legacy_payload()
    if malformed == "missing":
        del raw["waiver"]["candidate"][field]
    else:
        raw["waiver"]["candidate"][field] = None
    with pytest.raises(ProposalError, match=f"waiver.candidate.{field}"):
        Proposal.from_json(raw)


@pytest.mark.parametrize("section", ["campaign_reference", "policy"])
@pytest.mark.parametrize("malformed", ["missing", "type"])
def test_snapshot_identity_fields_are_validated(section, malformed):
    raw = legacy_payload()
    for field in tuple(raw["waiver"][section]):
        changed = legacy_payload()
        if malformed == "missing":
            del changed["waiver"][section][field]
        else:
            changed["waiver"][section][field] = None
        with pytest.raises(ProposalError, match=f"waiver.{section}.{field}"):
            Proposal.from_json(changed)


def test_binding_is_deeply_immutable_and_json_is_detached():
    from dataclasses import FrozenInstanceError

    raw = legacy_payload()
    proposal = Proposal.from_json(raw)
    before = encode(proposal.to_json())
    raw["waiver"]["candidate"]["evidence_refs"].append("caller mutation")
    raw["waiver"]["policy"]["files"]["injected"] = "caller mutation"
    raw["waiver"]["target_surface"]["files"].append("caller mutation")
    output = proposal.to_json()
    output["waiver"]["candidate"]["evidence_refs"].clear()
    output["waiver"]["campaign_reference"]["path"] = "output mutation"
    output["waiver"]["policy"]["config"]["directory"] = "output mutation"
    output["waiver"]["target_surface"]["files"].clear()
    assert encode(proposal.to_json()) == before
    with pytest.raises(FrozenInstanceError):
        proposal.waiver.approval_root = "/changed"
    with pytest.raises(FrozenInstanceError):
        proposal.waiver.candidate.source = "changed.sv"


def test_nullable_surface_empty_candidate_values_and_fingerprint_extensions_are_preserved():
    raw = legacy_payload()
    raw["waiver"]["target_surface"] = None
    raw["waiver"]["candidate"]["evidence_refs"] = []
    raw["waiver"]["candidate"]["justification"] = "  retain whitespace and Unicode λ  "
    raw["waiver"]["policy"]["extension"] = {"array": [True, None, 1.5, {"text": "λ"}]}
    assert Proposal.from_json(raw).to_json() == raw


@pytest.mark.parametrize("kind", ["waiver", "relax"])
def test_waiver_presence_must_agree_with_kind(kind):
    raw = legacy_payload()
    raw["kind"] = kind
    if kind == "waiver":
        raw["waiver"] = None
    else:
        raw["after_arg"] = {
            "family": "coverage",
            "target": "top",
            "tests": "all",
            "metrics": {"line": 100},
        }
    with pytest.raises(ProposalError, match="kind disagrees with waiver"):
        Proposal.from_json(raw)


@pytest.mark.parametrize("section", [None, "candidate", "campaign_reference"])
def test_unknown_fields_fail_without_dropping_saved_bytes(section):
    raw = legacy_payload()
    target = raw["waiver"] if section is None else raw["waiver"][section]
    target["future_field"] = "cannot silently discard"
    with pytest.raises(ProposalError, match="unknown fields"):
        Proposal.from_json(raw)


@pytest.mark.parametrize(
    "section,field", [("candidate", "proposal_count"), ("campaign_reference", "bytes")]
)
def test_numeric_fields_refuse_bool(section, field):
    raw = legacy_payload()
    raw["waiver"][section][field] = True
    with pytest.raises(ProposalError, match=f"waiver.{section}.{field}"):
        Proposal.from_json(raw)


def test_binding_constructors_validate_before_publication():
    from dataclasses import replace

    from booley.goals.waiver_binding import JsonSnapshot

    proposal = Proposal.from_json(legacy_payload())
    with pytest.raises(ProposalError, match="approval_root"):
        replace(proposal.waiver, approval_root=None)
    with pytest.raises(ProposalError, match=r"candidate\.source"):
        replace(proposal.waiver.candidate, source=123)
    with pytest.raises(ProposalError, match=r"candidate\.evidence_refs"):
        replace(proposal.waiver.candidate, evidence_refs=["mutable"])
    with pytest.raises(ProposalError, match=r"waiver\.policy\.config"):
        replace(proposal.waiver, policy=JsonSnapshot({}, where="policy"))
    with pytest.raises(ProposalError, match="WaiverBinding"):
        replace(proposal, waiver=legacy_payload()["waiver"])


@pytest.mark.parametrize("operation", ["approve", "recover"])
@pytest.mark.parametrize("malformed", ["missing", "type"])
def test_malformed_saved_binding_refuses_before_external_locks_or_mutation(
    layout, monkeypatch, operation, malformed
):
    from booley.goals.apply import recover
    from booley.goals.paths import record_paths
    from tests.goals.test_goal_waivers import prepared, propose
    from tests.goals.test_proposals import approve, interrupted_transaction

    env, candidate, _ = prepared(layout)
    view = propose(layout, env, candidate)
    if operation == "recover":
        interrupted_transaction(layout, env, view)
    root = record_paths(layout.control, layout.record.id).root
    payload_path = root / "proposals" / view.proposal.id / "payload.json"
    raw = json.loads(payload_path.read_bytes())
    if malformed == "missing":
        del raw["waiver"]["approval_root"]
    else:
        raw["waiver"]["approval_root"] = 123
    payload_path.write_bytes(encode(raw))

    def forbidden(*_args, **_kwargs):
        pytest.fail("malformed binding reached external waiver storage")

    for method in ("locking", "prepare", "validate_effect"):
        monkeypatch.setattr(env.waivers, method, forbidden)
    before = {path: path.read_bytes() for path in layout.main.rglob("*") if path.is_file()}
    with pytest.raises(ProposalError, match=r"waiver\.approval_root"):
        if operation == "recover":
            recover(env.store, layout.record.id, env)
        else:
            approve(layout, env, view.proposal.id)
    assert before == {path: path.read_bytes() for path in layout.main.rglob("*") if path.is_file()}


def test_mcp_summary_keeps_legacy_waiver_shape(tmp_path):
    from booley.mcp.goal_changes import _wire_summary as _summary

    raw = legacy_payload()
    proposal = Proposal.from_json(raw)
    root = tmp_path / "proposals" / proposal.id
    root.mkdir(parents=True)
    (root / "payload.json").write_bytes((FIXTURE / "payload.json").read_bytes())
    (root / "state.json").write_bytes((FIXTURE / "state.json").read_bytes())
    summary = json.loads(_summary(load_proposal(tmp_path, proposal.id)))
    assert summary["waiver"] == raw["waiver"]
    assert (
        summary["commit_files"]
        == "Commit the installed approval/proof files under " + raw["waiver"]["approval_root"]
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_snapshot_number_names_nested_field(value):
    raw = legacy_payload()
    raw["waiver"]["policy"]["extension"] = {"items": [value]}
    with pytest.raises(ProposalError, match=r"waiver\.policy\.extension\.items\[0\]"):
        Proposal.from_json(raw)


def test_snapshot_constructor_validates_and_preserves_authored_json_types():
    from booley.goals.waiver_binding import JsonSnapshot

    raw = {"values": [10**80, 1.5, True, None]}
    snapshot = JsonSnapshot(raw, where="waiver.policy")
    assert snapshot.to_json() == raw
    assert [type(value) for value in snapshot.to_json()["values"]] == [
        int,
        float,
        bool,
        type(None),
    ]
    with pytest.raises(ProposalError, match=r"waiver\.policy"):
        JsonSnapshot([], where="waiver.policy")
