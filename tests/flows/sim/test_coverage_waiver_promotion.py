"""Promotion builder round-trips through the real approved-waiver loader (ADR 0066)."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.coverage_policy import evaluate_coverage_campaign
from booley.flows.sim.coverage_waiver_promotion import (
    ApprovalDocumentMalformedError,
    ApprovalDocumentMismatchError,
    ApprovalStamps,
    InvalidPromotionInputError,
    WaiverCandidateEvidence,
    WaiverCollisionError,
    WaiverPromotionRecord,
    build_promotion,
    render_approval_document,
    render_review_proof,
    review_proof_path,
    toml_basic_string,
    waiver_file_path,
)
from booley.flows.sim.coverage_waivers import (
    CoverageRepositoryRoots,
    CoverageWaiverConfig,
    load_approved_waiver_set,
)
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


def _candidate(**overrides: object) -> WaiverCandidateEvidence:
    fields: dict[str, object] = {
        "waiver_id": "wc-0123456789ab",
        "campaign_id": "campaign:sim_counter:12",
        "manifest_sha256": "sha256:" + "1" * 64,
        "point_store_sha256": "sha256:" + "2" * 64,
        "target": str(_TARGET),
        "point_id": _POINT_ID,
        "source": _SOURCE,
        "source_sha256": _SOURCE_SHA256,
        "reason": "unreachable",
        "justification": "Reset holds the counter; the branch cannot fire.",
        "evidence_refs": ("rtl/counter.sv:10", "sim/12/sim_counter/coverage.json"),
        "proposal_count": 3,
    }
    fields.update(overrides)
    return WaiverCandidateEvidence(**fields)  # type: ignore[arg-type]


def _write_promotion(
    roots: CoverageRepositoryRoots, candidate: WaiverCandidateEvidence
) -> tuple[Path, bytes]:
    """Write the RTL source, rendered approval file, and proof; return the file."""
    source = roots.rtl_repository / _SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"module counter; endmodule\n")
    promotion = build_promotion(candidate, _STAMPS)
    directory = roots.project_data_repository / "coverage-waivers"
    waiver_file = directory / waiver_file_path(_SOURCE)
    waiver_file.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_approval_document(
        None, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[promotion.record]
    )
    waiver_file.write_bytes(rendered)
    if promotion.proof is not None:
        proof = directory / review_proof_path(candidate.waiver_id)
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_bytes(promotion.proof)
    return waiver_file, rendered


@pytest.mark.parametrize("reason", ["unreachable", "excluded"])
def test_promoted_files_load_cleanly_and_waive_the_campaign_point(
    tmp_path: Path, reason: str
) -> None:
    roots = _roots(tmp_path)
    candidate = _candidate(reason=reason)
    _write_promotion(roots, candidate)

    approved = load_approved_waiver_set(_CONFIG, roots, known_targets=(_TARGET,))

    assert len(approved.waivers) == 1
    waiver = approved.waivers[0]
    assert (waiver.waiver_id, waiver.target, waiver.point_id, waiver.reason) == (
        candidate.waiver_id,
        _TARGET,
        _POINT_ID,
        reason,
    )
    assert waiver.waiver_file == "rtl/counter.sv.toml"
    assert waiver.provenance["approval_ref"] == _STAMPS.approval_ref
    if reason == "unreachable":
        assert waiver.provenance["proof"]["kind"] == "review"
        assert waiver.provenance["proof"]["reference"] == "proofs/wc-0123456789ab.md"
    else:
        assert "proof" not in waiver.provenance
    evaluated = evaluate_coverage_campaign(_campaign(), _criterion(), approved)
    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.points[0].disposition["waiver_id"] == candidate.waiver_id


def test_excluded_promotion_has_no_proof_bytes_or_proof_table() -> None:
    promotion = build_promotion(_candidate(reason="excluded"), _STAMPS)

    assert promotion.proof is None
    assert promotion.record.proof_reference is None
    rendered = render_approval_document(
        None, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[promotion.record]
    )
    assert b"[approval.proof]" not in rendered
    assert "proof" not in tomllib.loads(rendered.decode())["approval"][0]


def test_unreachable_proof_fingerprint_binds_the_rendered_proof() -> None:
    promotion = build_promotion(_candidate(), _STAMPS)

    assert promotion.proof is not None
    digest = f"sha256:{hashlib.sha256(promotion.proof).hexdigest()}"
    assert promotion.record.proof_sha256 == digest
    rendered = render_approval_document(
        None, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[promotion.record]
    )
    assert tomllib.loads(rendered.decode())["approval"][0]["proof"] == {
        "kind": "review",
        "reference": "proofs/wc-0123456789ab.md",
        "sha256": digest,
    }


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_append_keeps_existing_bytes_verbatim_and_loads(
    tmp_path: Path, trailing_newline: bool
) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    original = b"# hand-written comment kept as is\n" + waiver_file.read_bytes()
    original = original if trailing_newline else original.rstrip(b"\n")
    candidate = _candidate(point_id=_OTHER_POINT_ID, reason="excluded")
    record = build_promotion(candidate, _STAMPS).record

    rendered = render_approval_document(
        original, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[record]
    )
    waiver_file.write_bytes(rendered)

    assert rendered.startswith(original)
    approved = load_approved_waiver_set(_CONFIG, roots, known_targets=(_TARGET,))
    assert sorted(waiver.waiver_id for waiver in approved.waivers) == [
        "counter-basic-block",
        "wc-0123456789ab",
    ]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_sha256", "sha256:" + "f" * 64, ApprovalDocumentMismatchError),
        ("source", "rtl/other.sv", ApprovalDocumentMismatchError),
        ("schema", "booley.coverage-waivers/v2", ApprovalDocumentMismatchError),
        ("extra", "field", ApprovalDocumentMalformedError),
    ],
)
def test_append_refuses_an_existing_file_bound_elsewhere(
    tmp_path: Path, field: str, value: str, error: type[Exception]
) -> None:
    existing = _write_valid_approval(_roots(tmp_path)).read_text(encoding="utf-8")
    lines = existing.splitlines(keepends=True)
    header = [line for line in lines[:3] if not line.startswith(f"{field} =")]
    tampered = "".join([f'{field} = "{value}"\n', *header, *lines[3:]]).encode()
    record = build_promotion(_candidate(point_id=_OTHER_POINT_ID), _STAMPS).record

    with pytest.raises(error):
        render_approval_document(
            tampered, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[record]
        )


@pytest.mark.parametrize(
    ("waiver_id", "point_id"),
    [("counter-basic-block", _OTHER_POINT_ID), ("wc-0123456789ab", _POINT_ID)],
    ids=["id", "binding"],
)
def test_append_refuses_id_or_binding_collisions_without_renaming(
    tmp_path: Path, waiver_id: str, point_id: str
) -> None:
    existing = _write_valid_approval(_roots(tmp_path)).read_bytes()
    record = build_promotion(_candidate(waiver_id=waiver_id, point_id=point_id), _STAMPS).record

    with pytest.raises(WaiverCollisionError):
        render_approval_document(
            existing, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[record]
        )


def test_duplicate_new_records_collide_with_each_other() -> None:
    record = build_promotion(_candidate(), _STAMPS).record

    with pytest.raises(WaiverCollisionError):
        render_approval_document(
            None, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[record, record]
        )


def test_inline_array_existing_file_cannot_be_appended() -> None:
    existing = (
        f'schema = "booley.coverage-waivers/v1"\nsource = "{_SOURCE}"\n'
        f'source_sha256 = "{_SOURCE_SHA256}"\napproval = [{{ id = "x" }}]\n'
    ).encode()
    record = build_promotion(_candidate(), _STAMPS).record

    with pytest.raises(ApprovalDocumentMalformedError):
        render_approval_document(
            existing, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[record]
        )


@pytest.mark.parametrize("text", _TRICKY_TEXT)
def test_basic_string_escaper_round_trips_exactly(text: str) -> None:
    assert tomllib.loads(f"value = {toml_basic_string(text)}\n")["value"] == text


@pytest.mark.parametrize("text", _TRICKY_TEXT)
def test_tricky_justification_round_trips_through_document_and_loader(
    tmp_path: Path, text: str
) -> None:
    roots = _roots(tmp_path)
    _, rendered = _write_promotion(roots, _candidate(justification=text))

    assert tomllib.loads(rendered.decode())["approval"][0]["justification"] == text
    approved = load_approved_waiver_set(_CONFIG, roots, known_targets=(_TARGET,))
    assert approved.waivers[0].provenance["justification"] == text


def test_identical_inputs_render_identical_bytes_in_any_record_order() -> None:
    first = build_promotion(_candidate(), _STAMPS)
    second = build_promotion(
        _candidate(waiver_id="wc-ffffffffffff", point_id=_OTHER_POINT_ID, reason="excluded"),
        _STAMPS,
    )
    again = build_promotion(_candidate(), _STAMPS)

    def render(records: list[WaiverPromotionRecord]) -> bytes:
        return render_approval_document(
            None, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=records
        )

    assert first.proof == again.proof
    assert first.record == again.record
    assert render([first.record, second.record]) == render([second.record, again.record])


def test_review_proof_carries_binding_stamps_and_labels_unverified_text() -> None:
    candidate = _candidate(
        justification="Uses ``` fences\nand `ticks`.",
        evidence_refs=("rtl/counter.sv:10", 'odd "ref"\nwith newline'),
    )

    proof = render_review_proof(candidate, _STAMPS).decode()

    for value in (
        candidate.campaign_id,
        candidate.manifest_sha256,
        candidate.point_store_sha256,
        candidate.target,
        candidate.point_id,
        candidate.source,
        candidate.source_sha256,
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


def test_review_proof_without_evidence_refs_says_so() -> None:
    proof = render_review_proof(_candidate(evidence_refs=()), _STAMPS).decode()

    assert "cites no evidence references" in proof


@pytest.mark.parametrize(
    "build",
    [
        lambda: replace(_STAMPS, approved_at="2026-09-30T12:00:00+00:00"),
        lambda: replace(_STAMPS, approved_at="2026-09-30T12:00:00.5Z"),
        lambda: replace(_STAMPS, approved_by="Ada\nMallory"),
        lambda: replace(_STAMPS, approval_ref=""),
        lambda: _candidate(waiver_id="../escape"),
        lambda: _candidate(waiver_id="proofs/x"),
        lambda: _candidate(source="rtl/other.sv"),
        lambda: _candidate(point_id="cp1:not-a-point"),
        lambda: _candidate(reason="flaky"),
        lambda: _candidate(manifest_sha256="sha256:ABC"),
        lambda: _candidate(proposal_count=0),
        lambda: _candidate(proposal_count=True),
        lambda: _candidate(justification=""),
        lambda: _candidate(justification="\ud800"),
        lambda: _candidate(evidence_refs=["list-not-tuple"]),
        lambda: replace(build_promotion(_candidate(), _STAMPS).record, reason="excluded"),
        lambda: replace(
            build_promotion(_candidate(reason="excluded"), _STAMPS).record, reason="unreachable"
        ),
    ],
)
def test_invalid_inputs_are_rejected_at_construction(build: object) -> None:
    with pytest.raises(InvalidPromotionInputError):
        build()  # type: ignore[operator]


def test_render_rejects_records_for_another_source_and_empty_batches() -> None:
    record = build_promotion(_candidate(), _STAMPS).record

    with pytest.raises(InvalidPromotionInputError):
        render_approval_document(
            None, source="rtl/other.sv", source_sha256=_SOURCE_SHA256, records=[record]
        )
    with pytest.raises(InvalidPromotionInputError):
        render_approval_document(None, source=_SOURCE, source_sha256=_SOURCE_SHA256, records=[])
    with pytest.raises(InvalidPromotionInputError):
        render_approval_document(None, source=_SOURCE, source_sha256="nope", records=[record])


def test_path_helpers_mirror_the_loader_layout() -> None:
    assert waiver_file_path("rtl/counter.sv") == "rtl/counter.sv.toml"
    assert review_proof_path("wc-0123456789ab") == "proofs/wc-0123456789ab.md"
    with pytest.raises(InvalidPromotionInputError):
        waiver_file_path("../rtl/counter.sv")
