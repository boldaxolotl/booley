#!/usr/bin/env python3
"""Validate and replace one unsealed Public QA cleanup ledger."""

from __future__ import annotations

import argparse
from collections import Counter
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


def validate_retained_resources(
    current: dict[str, Any], candidate: dict[str, Any], ledger_path: Path
) -> None:
    """Reject a strict candidate that drops an identity from the current ledger."""
    try:
        triage.validate_definition(
            current, "run-record.schema.json", "cleanupLedgerWrite", str(ledger_path)
        )
    except triage.TriageError:
        return
    current_identities = Counter(resource["identity"] for resource in current["resources"])
    candidate_identities = Counter(resource["identity"] for resource in candidate["resources"])
    if missing := current_identities - candidate_identities:
        raise triage.TriageError(
            f"{ledger_path}: candidate drops existing resource identities {sorted(missing.elements())}"
        )


def replace_cleanup_ledger(run_root: Path, source: Path) -> None:
    """Publish a complete valid candidate without exposing partial state."""
    with exclusive_file_lock(run_root / ".run-records.lock"):
        if (run_root / "run-manifest.json").exists():
            raise triage.TriageError(f"{run_root}: sealed Scenario Run is immutable")
        run_path = run_root / "run.json"
        run = triage.read_json(run_path)
        triage.validate_definition(run, "run-record.schema.json", "run", str(run_path))
        candidate = triage.read_json(source)
        if not isinstance(candidate, dict):
            raise triage.TriageError(f"{source}: cleanup ledger must be a JSON object")
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
        ledger_path = run_root / "cleanup-ledger.json"
        current = triage.read_json(ledger_path)
        triage.validate_definition(
            current, "run-record.schema.json", "cleanupLedger", str(ledger_path)
        )
        validate_retained_resources(current, candidate, ledger_path)
        triage.atomic_json(ledger_path, candidate)


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
