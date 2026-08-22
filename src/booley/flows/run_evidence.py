"""Compact provenance recorded for one EDA target execution."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.core.boundary import BoundaryError, as_dict, as_int, require_str

from .baseline_worktree import git_full_sha

if TYPE_CHECKING:
    from booley.fusesoc.fusesoc_registry import ResolvedFile

FLOW_RUN_EVIDENCE_VERSION = 1
RUN_EVIDENCE_DETAIL = "_run_evidence"
BASELINE_RUN_EVIDENCE_DETAIL = "_baseline_run_evidence"


def digest_resolved_inputs(files: Iterable[ResolvedFile], build_root: Path) -> str:
    """Hash the exact staged files a Flow will dispatch, including stable names."""
    digest = hashlib.sha256()
    for resolved_file in sorted(files, key=lambda item: item.name):
        data = resolved_file.absolute(build_root).read_bytes()
        digest.update(resolved_file.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


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

    @classmethod
    def from_dict(cls, value: Any) -> FlowRunEvidence | None:
        """Parse persisted evidence, returning ``None`` for an invalid cache record."""
        record = as_dict(value)
        if record is None or as_int(record.get("version")) != FLOW_RUN_EVIDENCE_VERSION:
            return None
        try:
            return cls(
                run_id=require_str(record, "run_id"),
                source_revision=require_str(record, "source_revision"),
                source_sha256=require_str(record, "source_sha256"),
                recipe_sha256=require_str(record, "recipe_sha256"),
            )
        except BoundaryError:
            return None


def build_flow_run_evidence(
    *,
    flow: str,
    target: str,
    recipe_sha256: str,
    source_sha256: str,
    work_dir: Path,
    run_id: str | None = None,
) -> FlowRunEvidence:
    """Capture source and recipe provenance immediately before dispatch."""
    if not flow or not target or not recipe_sha256 or not source_sha256:
        raise ValueError("flow, target, recipe_sha256, and source_sha256 must be non-empty")
    resolved_work_dir = work_dir.resolve()
    resolved_run_id = run_id or os.environ.get("BOOLEY_RUN_ID", "").strip()
    if not resolved_run_id:
        resolved_run_id = f"{flow}-{uuid.uuid4().hex}"
    return FlowRunEvidence(
        run_id=resolved_run_id,
        source_revision=git_full_sha("HEAD", resolved_work_dir) or "unversioned",
        source_sha256=source_sha256,
        recipe_sha256=recipe_sha256,
    )
