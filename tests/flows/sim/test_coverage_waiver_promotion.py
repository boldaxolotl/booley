"""Promotion builder round-trips through the real approved-waiver loader (ADR 0066)."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from booley.flows.sim.coverage_policy import evaluate_coverage_campaign
from booley.flows.sim.coverage_waiver_promotion import (
    ApprovalStamps,
    WaiverPromotion,
    WaiverPromotionError,
    build_promotion,
    render_approval_document,
    render_review_proof,
    review_proof_path,
    waiver_file_path,
)
from booley.flows.sim.coverage_waivers import (
    CoverageRepositoryRoots,
    CoverageWaiverConfig,
    load_approved_waiver_set,
)
from booley.flows.sim.waiver_candidates import CampaignBinding, WaiverCandidate, WaiverProposal
from tests.flows.sim.test_coverage_waivers import (
    _OTHER_POINT_ID,
    _POINT_ID,
    _SOURCE_SHA256,
    _TARGET,
    _campaign,
    _criterion,
    _roots,
    _write_valid_approval,
)

_CONFIG = CoverageWaiverConfig("project_data_repository", "coverage-waivers")
_SOURCE = "rtl/counter.sv"
_STAMPS = ApprovalStamps(
    approved_by="Ada Reviewer <ada@example.test>",
    approved_at="2026-09-30T12:00:00Z",
    approval_ref="ticket:counter-fix@0123456789abcdef",
)
_TRICKY_TEXT = [
    'He said "unreachable".',
    "back\\slash and \\n literal",
    "line one\nline two\r\nline three",
    "tab\there",
    "unicode: Ünïcödé — 日本語 🚀",
    "triple \"\"\" quotes and ''' singles",
    "control \x00\x01\x1b\x7f chars",
    "```\nfenced ``` text\n````",
]


def _candidate(**proposal: object) -> WaiverCandidate:
    """Return a recorded Waiver Candidate; *proposal* overrides its proposal fields."""
    fields: dict[str, object] = {
        "point_id": _POINT_ID,
        "source": _SOURCE,
        "source_sha256": _SOURCE_SHA256,
        "reason": "unreachable",
        "justification": "Reset holds the counter; the branch cannot fire.",
        "evidence_refs": ("rtl/counter.sv:10", "sim/12/sim_counter/coverage.json"),
    }
    fields.update(proposal)
    binding = CampaignBinding(
        "campaign:sim_counter:12",
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "campaign.json",
        str(_TARGET),
        "sim_counter",
    )
    return WaiverCandidate(
        binding,
        WaiverProposal(**fields),
        3,
        "2026-09-30T11:00:00Z",
        "2026-09-30T11:30:00Z",
        "inv-1",  # type: ignore[arg-type]
    )


def _write_promotion(
    roots: CoverageRepositoryRoots, candidate: WaiverCandidate
) -> tuple[Path, bytes]:
    """Write the RTL source, rendered approval file, and proof; return the file."""
    source = roots.rtl_repository / _SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"module counter; endmodule\n")
    promotion = build_promotion(candidate, _STAMPS)
    directory = roots.project_data_repository / "coverage-waivers"
    waiver_file = directory / waiver_file_path(_SOURCE)
    waiver_file.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_approval_document(None, promotion)
    waiver_file.write_bytes(rendered)
    if promotion.proof is not None:
        proof = directory / review_proof_path(candidate.candidate_id)
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_bytes(promotion.proof)
    return waiver_file, rendered


@pytest.mark.parametrize("reason", ["unreachable", "excluded"])
def test_promoted_files_load_cleanly_and_waive_the_campaign_point(
    tmp_path: Path, reason: str
) -> None:
    roots = _roots(tmp_path)
    candidate = _candidate(reason=reason)
    promotion = build_promotion(candidate, _STAMPS)
    _, rendered = _write_promotion(roots, candidate)

    approved = load_approved_waiver_set(_CONFIG, roots, known_targets=(_TARGET,))

    assert len(approved.waivers) == 1
    waiver = approved.waivers[0]
    assert (waiver.waiver_id, waiver.target, waiver.point_id, waiver.reason) == (
        candidate.candidate_id,
        _TARGET,
        _POINT_ID,
        reason,
    )
    assert waiver.waiver_file == "rtl/counter.sv.toml"
    assert waiver.provenance["approval_ref"] == _STAMPS.approval_ref
    record = tomllib.loads(rendered.decode())["approval"][0]
    if reason == "unreachable":
        assert promotion.proof is not None
        assert record["proof"] == {
            "kind": "review",
            "reference": f"proofs/{candidate.candidate_id}.md",
            "sha256": f"sha256:{hashlib.sha256(promotion.proof).hexdigest()}",
        }
        assert waiver.provenance["proof"]["kind"] == "review"
    else:
        assert promotion.proof is None
        assert b"[approval.proof]" not in rendered
        assert "proof" not in record
        assert "proof" not in waiver.provenance
    evaluated = evaluate_coverage_campaign(_campaign(), _criterion(), approved)
    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.points[0].disposition["waiver_id"] == candidate.candidate_id


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_append_keeps_existing_bytes_verbatim_and_loads(
    tmp_path: Path, trailing_newline: bool
) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    original = b"# hand-written comment kept as is\n" + waiver_file.read_bytes()
    original = original if trailing_newline else original.rstrip(b"\r\n")
    candidate = _candidate(point_id=_OTHER_POINT_ID, reason="excluded")

    rendered = render_approval_document(original, build_promotion(candidate, _STAMPS))
    waiver_file.write_bytes(rendered)

    assert rendered.startswith(original)
    approved = load_approved_waiver_set(_CONFIG, roots, known_targets=(_TARGET,))
    assert sorted(waiver.waiver_id for waiver in approved.waivers) == sorted(
        ["counter-basic-block", candidate.candidate_id]
    )


@pytest.mark.parametrize("text", _TRICKY_TEXT)
def test_tricky_justification_round_trips_through_document_and_loader(
    tmp_path: Path, text: str
) -> None:
    roots = _roots(tmp_path)
    _, rendered = _write_promotion(roots, _candidate(justification=text))

    assert tomllib.loads(rendered.decode())["approval"][0]["justification"] == text
    approved = load_approved_waiver_set(_CONFIG, roots, known_targets=(_TARGET,))
    assert approved.waivers[0].provenance["justification"] == text


def test_review_proof_carries_binding_stamps_and_labels_unverified_text() -> None:
    candidate = _candidate(
        justification="Uses ``` fences\nand `ticks`.",
        evidence_refs=("rtl/counter.sv:10", 'odd "ref"\nwith newline'),
    )

    proof = render_review_proof(candidate, _STAMPS).decode()

    for value in (
        candidate.binding.campaign_id,
        candidate.binding.manifest_sha256,
        candidate.binding.point_store_sha256,
        candidate.binding.target_identity,
        candidate.proposal.point_id,
        candidate.proposal.source,
        candidate.proposal.source_sha256,
        candidate.candidate_id,
        _STAMPS.approved_by,
        _STAMPS.approved_at,
        _STAMPS.approval_ref,
        "rtl/counter.sv.toml",
    ):
        assert value in proof
    assert "- Proposal count: `3`" in proof
    assert "## Analyst argument (unverified)" in proof
    assert "## Cited evidence (unverified)" in proof
    assert "````text\nUses ``` fences\nand `ticks`.\n````" in proof
    assert '"odd \\"ref\\"\\nwith newline"' in proof
    assert proof.endswith("\n")
    assert proof.encode() == render_review_proof(candidate, _STAMPS)
    assert (
        "cites no evidence references"
        in render_review_proof(_candidate(evidence_refs=()), _STAMPS).decode()
    )


def test_unsafe_source_path_is_the_renderer_refusal() -> None:
    assert waiver_file_path(_SOURCE) == "rtl/counter.sv.toml"
    assert review_proof_path("wc-0123456789ab") == "proofs/wc-0123456789ab.md"
    promotion = build_promotion(_candidate(source="../rtl/counter.sv", reason="excluded"), _STAMPS)
    assert isinstance(promotion, WaiverPromotion)

    with pytest.raises(WaiverPromotionError):
        waiver_file_path("../rtl/counter.sv")
    with pytest.raises(WaiverPromotionError):
        render_approval_document(None, promotion)
