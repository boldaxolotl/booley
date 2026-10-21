"""Pure builders for the tracked files written when Waiver Candidates are approved.

ADR 0066 promotes each human-accepted Waiver Candidate into an Approved Waiver:
one ``[[approval]]`` record appended to ``<source>.toml`` in the approval
directory and, for ``unreachable`` records, one ``review`` proof file at
``proofs/<waiver-id>.md``.  This module renders those bytes and nothing else:
it performs no filesystem or Git work, so the approve operation
owns every I/O policy and can re-render identical bytes during crash recovery.

The renderer does not validate what it appends to.  The caller loads the
rendered bytes with the real Approved Waiver loader on an overlay before
anything is written; that loader is the only validation gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from booley.config.coverage_waiver_inputs import is_safe_relative_posix
from booley.flows.sim.waiver_candidates import WaiverCandidate

APPROVAL_SCHEMA = "booley.coverage-waivers/v1"
REVIEW_PROOF_KIND = "review"
_PROOF_DIRECTORY = "proofs"
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


class WaiverPromotionError(ValueError):
    """The renderer refuses to build promotion files, e.g. for an unsafe source path."""


def waiver_file_path(source: str) -> str:
    """Return the approval-directory-relative waiver file path for RTL *source*."""
    if not is_safe_relative_posix(source):
        raise WaiverPromotionError(f"source {source!r} is not a safe relative POSIX path")
    return f"{source}.toml"


def review_proof_path(waiver_id: str) -> str:
    """Return the approval-directory-relative review proof path for *waiver_id*."""
    return f"{_PROOF_DIRECTORY}/{waiver_id}.md"


@dataclass(frozen=True)
class ApprovalStamps:
    """Who approved a promotion, when, and under which review reference."""

    approved_by: str
    approved_at: str
    approval_ref: str


@dataclass(frozen=True)
class WaiverPromotion:
    """The recorded candidate, its approval stamps, and optional review-proof bytes.

    The justification and evidence refs come from the Sandbox-writable
    candidate record and are rendered as unverified text (ADR 0066).
    """

    candidate: WaiverCandidate
    stamps: ApprovalStamps
    proof: bytes | None

    def as_toml_mapping(self) -> dict[str, object]:
        """Return the ``[[approval]]`` record exactly as :mod:`tomllib` parses it."""
        waiver_id = self.candidate.candidate_id
        record: dict[str, object] = {
            "id": waiver_id,
            "target": self.candidate.binding.target_identity,
            "point_id": self.candidate.proposal.point_id,
            "reason": self.candidate.proposal.reason,
            "justification": self.candidate.proposal.justification,
            "approved_by": self.stamps.approved_by,
            "approved_at": self.stamps.approved_at,
            "approval_ref": self.stamps.approval_ref,
        }
        if self.proof is not None:
            record["proof"] = {
                "kind": REVIEW_PROOF_KIND,
                "reference": review_proof_path(waiver_id),
                "sha256": f"sha256:{hashlib.sha256(self.proof).hexdigest()}",
            }
        return record


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


def _approval_block(promotion: WaiverPromotion) -> str:
    """Render one ``[[approval]]`` block, plus its proof sub-table when present."""
    mapping = promotion.as_toml_mapping()
    proof = mapping.pop("proof", None)
    lines = ["[[approval]]", *_toml_lines(mapping.items())]
    if isinstance(proof, Mapping):
        lines.extend(["", "[approval.proof]", *_toml_lines(proof.items())])
    return "\n".join(lines) + "\n"


def _document_header(source: str, source_sha256: str) -> str:
    pairs = (("schema", APPROVAL_SCHEMA), ("source", source), ("source_sha256", source_sha256))
    return "\n".join(_toml_lines(pairs)) + "\n"


def render_approval_document(existing: bytes | None, promotion: WaiverPromotion) -> bytes:
    """Return the ``<source>.toml`` bytes with *promotion*'s record appended.

    With no *existing* file, a new ``booley.coverage-waivers/v1`` document is
    emitted.  Otherwise its bytes are kept verbatim as the prefix, plus a
    newline when the last line is unterminated.  The result is not parsed or
    checked here: the real Approved Waiver loader is the gate.
    """
    proposal = promotion.candidate.proposal
    waiver_file_path(proposal.source)
    if existing is None:
        prefix = _document_header(proposal.source, proposal.source_sha256).encode()
    else:
        prefix = existing if not existing or existing.endswith(b"\n") else existing + b"\n"
    return prefix + f"\n{_approval_block(promotion)}".encode()


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


def _binding_lines(candidate: WaiverCandidate) -> list[str]:
    binding, proposal = candidate.binding, candidate.proposal
    fields = (
        ("Waiver id", candidate.candidate_id),
        ("Reason", proposal.reason),
        ("Target", binding.target_identity),
        ("Coverage Point", proposal.point_id),
        ("Source", proposal.source),
        ("Source SHA-256", proposal.source_sha256),
        ("Campaign", binding.campaign_id),
        ("Campaign manifest SHA-256", binding.manifest_sha256),
        ("Point store SHA-256", binding.point_store_sha256),
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


def _evidence_lines(candidate: WaiverCandidate) -> list[str]:
    if not candidate.proposal.evidence_refs:
        return ["The candidate record cites no evidence references."]
    # One JSON string per line keeps every reference unambiguous, whatever it holds.
    refs = "\n".join(
        json.dumps(ref, ensure_ascii=False) for ref in candidate.proposal.evidence_refs
    )
    return _fenced_block(refs)


def render_review_proof(candidate: WaiverCandidate, stamps: ApprovalStamps) -> bytes:
    """Return the deterministic Markdown ``review`` proof for one accepted candidate."""
    waiver_id = candidate.candidate_id
    lines = [
        f"# Coverage waiver review proof: {waiver_id}",
        "",
        f"This is the `{REVIEW_PROOF_KIND}` proof for Approved Waiver "
        f"{_inline_code(waiver_id)} in {_inline_code(waiver_file_path(candidate.proposal.source))}.",
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
        *_fenced_block(candidate.proposal.justification),
        "",
        "## Cited evidence (unverified)",
        "",
        *_evidence_lines(candidate),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_promotion(candidate: WaiverCandidate, stamps: ApprovalStamps) -> WaiverPromotion:
    """Return the promotion for *candidate*; only ``unreachable`` gets review-proof bytes."""
    proof = (
        render_review_proof(candidate, stamps)
        if candidate.proposal.reason == "unreachable"
        else None
    )
    return WaiverPromotion(candidate, stamps, proof)
