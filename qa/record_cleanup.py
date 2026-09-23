#!/usr/bin/env python3
"""Validate and replace one unsealed Public QA cleanup ledger."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    from . import triage
    from .triage_lock import exclusive_file_lock
except ImportError:
    import triage
    from triage_lock import exclusive_file_lock


def canonicalize_cleanup_ledger(candidate: dict[str, Any], source: Path) -> None:
    """Apply the sole cleanup-ledger write canonicalization in place."""
    resources = candidate.get("resources")
    if not isinstance(resources, list):
        return
    for index, resource in enumerate(resources, 1):
        if not isinstance(resource, dict):
            continue
        identity = resource.get("identity")
        if not isinstance(identity, str) or not identity.strip():
            raise triage.TriageError(f"{source}: resource {index}: invalid identity")
        reason = resource.get("cleanup_reason")
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            resource.pop("cleanup_reason", None)


def replace_cleanup_ledger(run_root: Path, source: Path) -> None:
    """Publish a complete valid candidate without exposing partial state."""
    with exclusive_file_lock(run_root / ".cleanup-ledger.lock"):
        if (run_root / "run-manifest.json").exists():
            raise triage.TriageError(f"{run_root}: sealed Scenario Run is immutable")
        run_path = run_root / "run.json"
        run = triage.read_json(run_path)
        triage.validate_definition(run, "run-record.schema.json", "run", str(run_path))
        candidate = triage.read_json(source)
        if candidate.get("run_id") != run["run_id"]:
            raise triage.TriageError(f"{source}: cleanup ledger belongs to a different run")
        canonicalize_cleanup_ledger(candidate, source)
        triage.validate_definition(
            candidate, "run-record.schema.json", "cleanupLedgerWrite", str(source)
        )
        for resource in candidate["resources"]:
            triage.validate_preseal_evidence_refs(
                run_root, resource.get("safe_shutdown_evidence_refs", [])
            )
        triage.atomic_json(run_root / "cleanup-ledger.json", candidate)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("candidate_json", type=Path)
    args = parser.parse_args()
    try:
        replace_cleanup_ledger(args.run_root, args.candidate_json)
    except (OSError, ValueError, TimeoutError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
