"""Pure builders for the tracked files written when Waiver Candidates are approved.

ADR 0066 promotes each human-accepted Waiver Candidate into an Approved Waiver:
one ``[[approval]]`` record appended to ``<source>.toml`` in the approval
directory and, for ``unreachable`` records, one ``review`` proof file at
``proofs/<waiver-id>.md``.  This module renders those bytes and nothing else:
it performs no filesystem, Git, or Ticket Board work, so the approve operation
owns every I/O policy and can re-render identical bytes during crash recovery.

Every rendered document is re-parsed with :mod:`tomllib` before it is
returned, so an escaping defect fails here instead of producing a file the
waiver loader rejects later.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from booley.config.coverage_waiver_inputs import (
    APPROVAL_DOCUMENT_FIELDS,
    is_safe_relative_posix,
    is_sha256,
)
from booley.core.boundary import BoundaryError, as_str, require_dict, require_list
from booley.flows.sim.coverage_campaign import decode_coverage_point_id
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_epoch

APPROVAL_SCHEMA = "booley.coverage-waivers/v1"
REVIEW_PROOF_KIND = "review"
_PROOF_DIRECTORY = "proofs"
_WAIVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# TOML basic strings forbid U+0000-U+001F (tab excepted) and U+007F raw; the
# short escapes are preferred where TOML defines one.
_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

WaiverReason = Literal["excluded", "unreachable"]


class WaiverPromotionError(ValueError):
    """Base class for every refusal to render promotion files."""


class InvalidPromotionInputError(WaiverPromotionError):
    """A promotion input violates the closed approval-record contract."""


class ApprovalDocumentMalformedError(WaiverPromotionError):
    """The existing approval file cannot be parsed or appended to safely."""


class ApprovalDocumentMismatchError(WaiverPromotionError):
    """The existing approval file is bound to another schema, source, or source hash."""


class WaiverCollisionError(WaiverPromotionError):
    """A new record reuses a waiver id or Target-and-point binding already present."""


def _require_text(value: object, field: str, *, single_line: bool = True) -> str:
    """Return *value* as a non-empty UTF-8-encodable string, or raise."""
    text = as_str(value)
    if not text:
        raise InvalidPromotionInputError(f"{field} must be a non-empty string")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidPromotionInputError(f"{field} is not valid Unicode text") from exc
    if single_line and _CONTROL_RE.search(text):
        raise InvalidPromotionInputError(f"{field} must not contain control characters")
    return text


def _require_sha256(value: object, field: str) -> str:
    if not is_sha256(value):
        raise InvalidPromotionInputError(f"{field} must be a lowercase sha256: digest")
    assert isinstance(value, str)
    return value


def _require_waiver_id(value: object) -> str:
    text = as_str(value)
    if text is None or _WAIVER_ID_RE.fullmatch(text) is None:
        raise InvalidPromotionInputError(
            f"waiver id {value!r} must be 1-128 of [A-Za-z0-9._-] starting alphanumeric"
        )
    return text


def _require_canonical_timestamp(value: object) -> str:
    text = _require_text(value, "approved_at")
    try:
        canonical = rfc3339_from_epoch(parse_timestamp(text).timestamp())
    except ValueError as exc:
        raise InvalidPromotionInputError("approved_at must be RFC 3339") from exc
    if canonical != text:
        raise InvalidPromotionInputError("approved_at must be canonical UTC RFC 3339 with Z")
    return text


def _require_point_source(point_id: object) -> str:
    """Return the RTL source named by one exact cp1 point id, or raise."""
    identity = decode_coverage_point_id(point_id)
    if identity is None:
        raise InvalidPromotionInputError("point_id must be one exact cp1 Coverage Point id")
    location = require_dict(identity.get("location"), field="location")
    return str(location["source"])


def waiver_file_path(source: str) -> str:
    """Return the approval-directory-relative waiver file path for RTL *source*."""
    if not is_safe_relative_posix(source):
        raise InvalidPromotionInputError(f"source {source!r} is not a safe relative POSIX path")
    return f"{source}.toml"


def review_proof_path(waiver_id: str) -> str:
    """Return the approval-directory-relative review proof path for *waiver_id*."""
    return f"{_PROOF_DIRECTORY}/{_require_waiver_id(waiver_id)}.md"


@dataclass(frozen=True)
class ApprovalStamps:
    """Who approved a promotion, when, and under which review reference."""

    approved_by: str
    approved_at: str
    approval_ref: str

    def __post_init__(self) -> None:
        _require_text(self.approved_by, "approved_by")
        _require_canonical_timestamp(self.approved_at)
        _require_text(self.approval_ref, "approval_ref")


@dataclass(frozen=True)
class WaiverPromotionRecord:
    """One validated ``[[approval]]`` record ready to append to its waiver file.

    ``proof_sha256`` is required for ``unreachable`` (it fingerprints the
    review proof at :attr:`proof_reference`) and forbidden for ``excluded``.
    """

    waiver_id: str
    target: str
    point_id: str
    reason: WaiverReason
    justification: str
    stamps: ApprovalStamps
    proof_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_waiver_id(self.waiver_id)
        _require_text(self.target, "target")
        _require_point_source(self.point_id)
        _require_text(self.justification, "justification", single_line=False)
        if self.reason == "unreachable":
            _require_sha256(self.proof_sha256, "proof_sha256")
        elif self.reason == "excluded":
            if self.proof_sha256 is not None:
                raise InvalidPromotionInputError("excluded approvals carry no proof")
        else:
            raise InvalidPromotionInputError("reason must be excluded or unreachable")

    @property
    def source(self) -> str:
        """RTL source path encoded in :attr:`point_id`."""
        return _require_point_source(self.point_id)

    @property
    def proof_reference(self) -> str | None:
        """Approval-directory-relative proof path, or ``None`` for ``excluded``."""
        return review_proof_path(self.waiver_id) if self.reason == "unreachable" else None

    def as_toml_mapping(self) -> dict[str, object]:
        """Return the record exactly as :mod:`tomllib` parses the rendered block."""
        record: dict[str, object] = {
            "id": self.waiver_id,
            "target": self.target,
            "point_id": self.point_id,
            "reason": self.reason,
            "justification": self.justification,
            "approved_by": self.stamps.approved_by,
            "approved_at": self.stamps.approved_at,
            "approval_ref": self.stamps.approval_ref,
        }
        if self.proof_reference is not None:
            record["proof"] = {
                "kind": REVIEW_PROOF_KIND,
                "reference": self.proof_reference,
                "sha256": self.proof_sha256,
            }
        return record


@dataclass(frozen=True)
class WaiverCandidateEvidence:
    """The Campaign binding and Analyst argument behind one accepted candidate.

    The justification and evidence refs come from the Sandbox-writable
    candidate record and are rendered as unverified text (ADR 0066).
    """

    waiver_id: str
    campaign_id: str
    manifest_sha256: str
    point_store_sha256: str
    target: str
    point_id: str
    source: str
    source_sha256: str
    reason: WaiverReason
    justification: str
    evidence_refs: tuple[str, ...]
    proposal_count: int

    def __post_init__(self) -> None:
        _require_waiver_id(self.waiver_id)
        for field in ("campaign_id", "target"):
            _require_text(getattr(self, field), field)
        for field in ("manifest_sha256", "point_store_sha256", "source_sha256"):
            _require_sha256(getattr(self, field), field)
        waiver_file_path(self.source)
        if _require_point_source(self.point_id) != self.source:
            raise InvalidPromotionInputError("point_id names a different source")
        if self.reason not in {"excluded", "unreachable"}:
            raise InvalidPromotionInputError("reason must be excluded or unreachable")
        _require_text(self.justification, "justification", single_line=False)
        if not isinstance(self.evidence_refs, tuple):
            raise InvalidPromotionInputError("evidence_refs must be a tuple of strings")
        for ref in self.evidence_refs:
            _require_text(ref, "evidence_refs[]", single_line=False)
        count = self.proposal_count
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise InvalidPromotionInputError("proposal_count must be a positive integer")


@dataclass(frozen=True)
class WaiverPromotion:
    """The approval record and optional review-proof bytes for one candidate."""

    record: WaiverPromotionRecord
    proof: bytes | None


# ---------------------------------------------------------------------------
# TOML emission
# ---------------------------------------------------------------------------


def toml_basic_string(value: str) -> str:
    """Return *value* as one TOML basic string that :mod:`tomllib` reads back exactly.

    Quotes, backslashes, and every control character (newline and tab
    included) are escaped; other Unicode passes through for UTF-8 encoding.
    """
    parts: list[str] = []
    for char in value:
        if char in _SHORT_ESCAPES:
            parts.append(_SHORT_ESCAPES[char])
        elif _CONTROL_RE.fullmatch(char):
            parts.append(f"\\u{ord(char):04X}")
        else:
            parts.append(char)
    return '"' + "".join(parts) + '"'


def _toml_lines(pairs: Iterable[tuple[str, object]]) -> list[str]:
    return [f"{key} = {toml_basic_string(str(value))}" for key, value in pairs]


def _approval_block(record: WaiverPromotionRecord) -> str:
    """Render one ``[[approval]]`` block, plus its proof sub-table when present."""
    mapping = record.as_toml_mapping()
    proof = mapping.pop("proof", None)
    lines = ["[[approval]]", *_toml_lines(mapping.items())]
    if isinstance(proof, Mapping):
        lines.extend(["", "[approval.proof]", *_toml_lines(proof.items())])
    return "\n".join(lines) + "\n"


def _document_header(source: str, source_sha256: str) -> str:
    pairs = (("schema", APPROVAL_SCHEMA), ("source", source), ("source_sha256", source_sha256))
    return "\n".join(_toml_lines(pairs)) + "\n"


# ---------------------------------------------------------------------------
# Existing-document checks
# ---------------------------------------------------------------------------


def _parse_toml(raw: bytes, label: str) -> dict[str, object]:
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ApprovalDocumentMalformedError(f"{label} is not valid UTF-8 TOML: {exc}") from exc


def _existing_approvals(
    existing: bytes, source: str, source_sha256: str
) -> list[dict[str, object]]:
    """Return the existing approval tables after checking the file's binding."""
    document = _parse_toml(existing, "existing approval file")
    if set(document) != APPROVAL_DOCUMENT_FIELDS:
        raise ApprovalDocumentMalformedError("existing approval file fields are not closed v1")
    for field, expected in (
        ("schema", APPROVAL_SCHEMA),
        ("source", source),
        ("source_sha256", source_sha256),
    ):
        if document[field] != expected:
            raise ApprovalDocumentMismatchError(
                f"existing approval file {field} is {document[field]!r}, expected {expected!r}"
            )
    try:
        approvals = require_list(document["approval"], field="approval")
        return [require_dict(item, field="approval") for item in approvals]
    except BoundaryError as exc:
        raise ApprovalDocumentMalformedError(str(exc)) from exc


def _check_collisions(
    existing: Sequence[Mapping[str, object]], records: Sequence[WaiverPromotionRecord]
) -> None:
    """Refuse duplicate ids or Target-and-point bindings; never rename."""
    ids = {str(item.get("id")) for item in existing}
    bindings = {(str(item.get("target")), str(item.get("point_id"))) for item in existing}
    for record in records:
        if record.waiver_id in ids:
            raise WaiverCollisionError(f"waiver id {record.waiver_id!r} already exists")
        binding = (record.target, record.point_id)
        if binding in bindings:
            raise WaiverCollisionError(
                f"waiver {record.waiver_id!r} duplicates an approval for the same "
                "Target and Coverage Point"
            )
        ids.add(record.waiver_id)
        bindings.add(binding)


def _verify_rendered(
    rendered: bytes,
    records: Sequence[WaiverPromotionRecord],
    source: str,
    source_sha256: str,
) -> None:
    """Re-parse the output and require the new records to read back exactly."""
    document = _parse_toml(rendered, "approval file with appended records")
    approvals = document.get("approval")
    expected = [record.as_toml_mapping() for record in records]
    header = (document.get("schema"), document.get("source"), document.get("source_sha256"))
    intact = header == (APPROVAL_SCHEMA, source, source_sha256)
    if not intact or not isinstance(approvals, list) or approvals[-len(expected) :] != expected:
        raise AssertionError("rendered approval file does not read back as the intended records")


def render_approval_document(
    existing: bytes | None,
    *,
    source: str,
    source_sha256: str,
    records: Sequence[WaiverPromotionRecord],
) -> bytes:
    """Return the ``<source>.toml`` bytes with *records* appended.

    With no *existing* file, a new ``booley.coverage-waivers/v1`` document is
    emitted.  Otherwise *existing* must be bound to the same schema, source,
    and ``source_sha256``; its bytes are kept verbatim as the prefix and the
    new ``[[approval]]`` blocks are appended.  Records are emitted in waiver-id
    order so identical inputs always render identical bytes.
    """
    waiver_file_path(source)
    _require_sha256(source_sha256, "source_sha256")
    if not records:
        raise InvalidPromotionInputError("at least one approval record is required")
    for record in records:
        if record.source != source:
            raise InvalidPromotionInputError(
                f"waiver {record.waiver_id!r} point source {record.source!r} is not {source!r}"
            )
    ordered = sorted(records, key=lambda record: record.waiver_id)
    prior = [] if existing is None else _existing_approvals(existing, source, source_sha256)
    _check_collisions(prior, ordered)
    if existing is None:
        prefix = _document_header(source, source_sha256)
    else:
        prefix = existing.decode("utf-8")
        prefix = prefix if not prefix or prefix.endswith("\n") else prefix + "\n"
    blocks = "".join(f"\n{_approval_block(record)}" for record in ordered)
    rendered = (prefix + blocks).encode("utf-8")
    if existing is not None and not rendered.startswith(existing):
        raise AssertionError("existing approval bytes were not preserved as the prefix")
    try:
        _verify_rendered(rendered, ordered, source, source_sha256)
    except ApprovalDocumentMalformedError as exc:
        # Valid input TOML that cannot take ``[[approval]]`` (an inline array).
        raise ApprovalDocumentMalformedError(
            f"existing approval file cannot be appended to: {exc}"
        ) from exc
    return rendered


# ---------------------------------------------------------------------------
# Review proof
# ---------------------------------------------------------------------------


def _inline_code(text: str) -> str:
    """Return *text* as a Markdown code span that no backtick run can close early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padded = f" {text} " if text.startswith("`") or text.endswith("`") else text
    return f"{fence}{padded}{fence}"


def _fenced_block(text: str) -> list[str]:
    """Return *text* inside a code fence longer than any backtick run it holds."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", text.removesuffix("\n"), fence]


def _binding_lines(candidate: WaiverCandidateEvidence) -> list[str]:
    fields = (
        ("Waiver id", candidate.waiver_id),
        ("Reason", candidate.reason),
        ("Target", candidate.target),
        ("Coverage Point", candidate.point_id),
        ("Source", candidate.source),
        ("Source SHA-256", candidate.source_sha256),
        ("Campaign", candidate.campaign_id),
        ("Campaign manifest SHA-256", candidate.manifest_sha256),
        ("Point store SHA-256", candidate.point_store_sha256),
        ("Proposal count", str(candidate.proposal_count)),
    )
    return [f"- {label}: {_inline_code(value)}" for label, value in fields]


def _approval_lines(stamps: ApprovalStamps) -> list[str]:
    fields = (
        ("Approved by", stamps.approved_by),
        ("Approved at", stamps.approved_at),
        ("Review reference", stamps.approval_ref),
    )
    return [f"- {label}: {_inline_code(value)}" for label, value in fields]


def _evidence_lines(candidate: WaiverCandidateEvidence) -> list[str]:
    if not candidate.evidence_refs:
        return ["The candidate record cites no evidence references."]
    # One JSON string per line keeps every reference unambiguous, whatever it holds.
    refs = "\n".join(json.dumps(ref, ensure_ascii=False) for ref in candidate.evidence_refs)
    return _fenced_block(refs)


def render_review_proof(candidate: WaiverCandidateEvidence, stamps: ApprovalStamps) -> bytes:
    """Return the deterministic Markdown ``review`` proof for one accepted candidate."""
    lines = [
        f"# Coverage waiver review proof: {candidate.waiver_id}",
        "",
        f"This is the `{REVIEW_PROOF_KIND}` proof for Approved Waiver "
        f"{_inline_code(candidate.waiver_id)} in {_inline_code(waiver_file_path(candidate.source))}.",
        "Booley wrote it when a human accepted the Waiver Candidate at Ticket review",
        "(ADR 0066). The human approval below is the authority for this waiver.",
        "",
        "## Binding",
        "",
        *_binding_lines(candidate),
        "",
        "## Approval",
        "",
        *_approval_lines(stamps),
        "",
        "## Analyst argument (unverified)",
        "",
        "Copied from the Waiver Candidate record. Booley cannot verify this text.",
        "",
        *_fenced_block(candidate.justification),
        "",
        "## Cited evidence (unverified)",
        "",
        *_evidence_lines(candidate),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_promotion(candidate: WaiverCandidateEvidence, stamps: ApprovalStamps) -> WaiverPromotion:
    """Return the approval record and, for ``unreachable``, its review-proof bytes."""
    proof = render_review_proof(candidate, stamps) if candidate.reason == "unreachable" else None
    proof_sha256 = None if proof is None else f"sha256:{hashlib.sha256(proof).hexdigest()}"
    record = WaiverPromotionRecord(
        waiver_id=candidate.waiver_id,
        target=candidate.target,
        point_id=candidate.point_id,
        reason=candidate.reason,
        justification=candidate.justification,
        stamps=stamps,
        proof_sha256=proof_sha256,
    )
    return WaiverPromotion(record=record, proof=proof)
