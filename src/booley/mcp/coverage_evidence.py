"""Process-bound adapter for one stateful Coverage evidence session."""

import json
import os
from collections.abc import Mapping
from pathlib import Path

from booley.core.boundary import require_dict, require_str
from booley.flows.sim.coverage_analysis_input import (
    CoverageSourceClosure,
    coverage_sources,
    read_coverage_campaign,
    verified_source_snapshot,
)
from booley.flows.sim.coverage_campaign import freeze_coverage_mapping
from booley.flows.sim.coverage_evidence import CoverageEvidenceError, CoverageEvidenceSession

_ACTIVE_SESSION: tuple[tuple[str, str, str], CoverageEvidenceSession] | None = None
_TERMINAL_ERROR: tuple[tuple[str, str, str], str] | None = None


def query_active_coverage_evidence(value: Mapping[str, object]) -> dict[str, object]:
    """Query the Campaign bound to this isolated nested MCP server."""
    global _ACTIVE_SESSION, _TERMINAL_ERROR

    campaign_path = os.environ.get("BOOLEY_COVERAGE_CAMPAIGN", "")
    project_root = os.environ.get("BOOLEY_COVERAGE_PROJECT", "")
    source_snapshot = os.environ.get("BOOLEY_COVERAGE_SOURCE_SNAPSHOT", "")
    if not campaign_path or not project_root:
        raise CoverageEvidenceError("No active Coverage Campaign is bound to this tool")
    key = (campaign_path, project_root, source_snapshot)
    audit_path = os.environ.get("BOOLEY_COVERAGE_AUDIT", "")
    if _TERMINAL_ERROR is not None and _TERMINAL_ERROR[0] == key:
        _write_terminal_audit(key, audit_path, _TERMINAL_ERROR[1])
        raise CoverageEvidenceError(f"Evidence session already failed: {_TERMINAL_ERROR[1]}")
    try:
        if _ACTIVE_SESSION is None or _ACTIVE_SESSION[0] != key:
            _ACTIVE_SESSION = (key, _load_session(campaign_path, project_root, source_snapshot))
            _TERMINAL_ERROR = None
        result = _ACTIVE_SESSION[1].query(value)
    except (OSError, ValueError) as exc:
        _TERMINAL_ERROR = (key, str(exc))
        _write_terminal_audit(key, audit_path, str(exc))
        raise
    if audit_path:
        _write_audit(Path(audit_path), _ACTIVE_SESSION[1].analysis_scope())
    return result


def _write_terminal_audit(key: tuple[str, str, str], audit_path: str, error: str) -> None:
    if not audit_path:
        return
    scope = (
        _ACTIVE_SESSION[1].analysis_scope()
        if _ACTIVE_SESSION is not None and _ACTIVE_SESSION[0] == key
        else {}
    )
    _write_audit(Path(audit_path), {**scope, "terminal_error": error})


def _load_session(
    campaign_path: str, project_root: str, source_snapshot: str
) -> CoverageEvidenceSession:
    loaded = read_coverage_campaign(Path(campaign_path))
    if not source_snapshot:
        sources = coverage_sources(loaded.campaign, Path(project_root))
        return CoverageEvidenceSession(loaded.campaign, sources)
    document = require_dict(
        json.loads(Path(source_snapshot).read_text(encoding="utf-8")),
        field="verified source snapshot",
    )
    sources = CoverageSourceClosure(
        require_str(document, "target_identity"),
        freeze_coverage_mapping(require_dict(document.get("files"), field="files")),
    )
    sources = verified_source_snapshot(loaded.campaign, sources)
    if sources is None:
        raise CoverageEvidenceError("Verified source snapshot disagrees with Campaign")
    return CoverageEvidenceSession(loaded.campaign, sources)


def _write_audit(path: Path, value: Mapping[str, object]) -> None:
    """Publish the disposable evidence audit atomically for the parent Analyst."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
