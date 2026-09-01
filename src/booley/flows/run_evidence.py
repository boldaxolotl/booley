"""Compact provenance recorded for one EDA target execution."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .baseline_worktree import git_full_sha
from .source_fingerprint import compute_source_fingerprint

FLOW_RUN_EVIDENCE_VERSION = 1
RUN_EVIDENCE_DETAIL = "_run_evidence"
BASELINE_RUN_EVIDENCE_DETAIL = "_baseline_run_evidence"


def _source_digest(work_dir: Path, target: str) -> str:
    fingerprint = compute_source_fingerprint(work_dir, target=target)
    payload = {
        "algorithm": fingerprint.get("algorithm"),
        "rtl": fingerprint.get("rtl", {}).get("digest"),
        "tb": fingerprint.get("tb", {}).get("digest"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FlowSourceEvidence:
    """Stable source provenance captured before command materialization."""

    source_revision: str
    source_sha256: str


def capture_flow_source_evidence(work_dir: Path, target: str) -> FlowSourceEvidence:
    """Inspect one Target and freeze the source provenance shared with dry-run."""
    if not target:
        raise ValueError("target must be non-empty")
    resolved_work_dir = work_dir.resolve()
    return FlowSourceEvidence(
        source_revision=git_full_sha("HEAD", resolved_work_dir) or "unversioned",
        source_sha256=_source_digest(resolved_work_dir, target),
    )


@dataclass(frozen=True)
class FlowRunEvidence:
    """Minimal provenance for artifacts produced by one EDA run."""

    run_id: str
    source_revision: str
    source_sha256: str
    recipe_sha256: str
    version: int = FLOW_RUN_EVIDENCE_VERSION

    def as_dict(self) -> dict[str, str | int]:
        """Return the JSON-compatible evidence record."""
        return {
            "version": self.version,
            "run_id": self.run_id,
            "source_revision": self.source_revision,
            "source_sha256": self.source_sha256,
            "recipe_sha256": self.recipe_sha256,
        }


def build_flow_run_evidence(
    *,
    flow: str,
    target: str,
    recipe_sha256: str,
    work_dir: Path,
    run_id: str | None = None,
    source_evidence: FlowSourceEvidence | None = None,
) -> FlowRunEvidence:
    """Capture source and recipe provenance immediately before dispatch."""
    if not flow or not target or not recipe_sha256:
        raise ValueError("flow, target, and recipe_sha256 must be non-empty")
    source = source_evidence or capture_flow_source_evidence(work_dir, target)
    resolved_run_id = run_id or os.environ.get("BOOLEY_RUN_ID", "").strip()
    if not resolved_run_id:
        resolved_run_id = f"{flow}-{uuid.uuid4().hex}"
    return FlowRunEvidence(
        run_id=resolved_run_id,
        source_revision=source.source_revision,
        source_sha256=source.source_sha256,
        recipe_sha256=recipe_sha256,
    )
