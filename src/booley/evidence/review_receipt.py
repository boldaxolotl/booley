"""Persist and evaluate the dependency-neutral Reviewer evidence contract."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.evidence.review_guides import ReviewGuide, declared_guide_names, guide_digest

_TICKET_FILE = "ticket.md"
REVIEW_DETAIL_VERSION = 4
# Bump whenever you change which proposals the Reviewer filters (see
# ``Reviewer._filter_review_issues``). A stored receipt is valid only for the
# filtering rules it ran under, because proposals filtered under old rules were
# never kept: older receipts must get a fresh review.
FILTERING_SEMANTICS_REVISION = 1


class ReviewContextError(OSError):
    """Persisted review context can no longer be read."""


@dataclass(frozen=True)
class ReviewInvocation:
    """Inputs whose identity determines one Reviewer contract."""

    work_dir: Path
    category: str
    focus: str
    scope: tuple[str, ...]
    mode: str
    spec_path: Path | None = None
    spec_text: str | None = None
    steering: str = ""
    # Every guide the review runs, so editing one makes the receipt stale.
    guides: tuple[ReviewGuide, ...] = ()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _resolved(path: Path | None, work_dir: Path) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else work_dir / path


def _ticket_path(work_dir: Path) -> Path | None:
    logs_value = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_value:
        return None
    candidate = _resolved(Path(logs_value) / _TICKET_FILE, work_dir)
    return candidate if candidate is not None and candidate.is_file() else None


def _document_digest(path: Path | None) -> str:
    if path is None or not path.exists():
        return _digest("")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewContextError(f"Could not read persisted Reviewer context: {exc}") from exc


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scope_hashes(work_dir: Path, scope: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for raw in sorted(scope):
        normalized = raw.replace("\\", "/").removeprefix("./")
        path = work_dir / normalized
        hashes[normalized] = _document_digest(path)
    return hashes


def build_review_contract_detail(invocation: ReviewInvocation) -> dict[str, Any]:
    """Build the canonical persisted identity for a Reviewer invocation."""
    ticket = _ticket_path(invocation.work_dir)
    spec = _resolved(invocation.spec_path, invocation.work_dir)
    scope = tuple(sorted(path.replace("\\", "/").removeprefix("./") for path in invocation.scope))
    contract: dict[str, Any] = {
        "version": REVIEW_DETAIL_VERSION,
        "filtering_semantics_revision": FILTERING_SEMANTICS_REVISION,
        "category": invocation.category,
        "focus": invocation.focus,
        "scope": list(scope),
        "scope_hashes": _scope_hashes(invocation.work_dir, scope),
        "mode": invocation.mode,
        "ticket_source": str(ticket.resolve()) if ticket else "",
        "ticket_digest": _document_digest(ticket),
        "spec_source": str(spec.resolve()) if spec else "",
        "spec_digest": _document_digest(spec),
        "steering_digest": _digest(invocation.steering),
        "guides": [
            {
                "kind": guide.kind,
                "name": guide.name,
                "path": guide.path,
                "digest": guide_digest(guide),
            }
            for guide in invocation.guides
        ],
        "declared_guides": declared_guide_names(invocation.category, invocation.focus),
    }
    if invocation.spec_text is not None:
        # Inline text has no file: so the contract carries the reviewed text and the receipt can reuse it.
        contract.update(
            spec_text=invocation.spec_text,
            spec_source="",
            spec_digest=_text_digest(invocation.spec_text),
        )
    return contract
