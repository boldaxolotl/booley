#!/usr/bin/env python3
"""Validate and append one Public QA Check Result before sealing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import triage
    from .triage_lock import exclusive_file_lock
except ImportError:
    import triage
    from triage_lock import exclusive_file_lock


def append_check_result(run_root: Path, suite_root: Path, source: Path) -> None:
    """Reject an invalid row without changing the existing result log."""
    with exclusive_file_lock(run_root / ".check-results.lock"):
        if (run_root / "run-manifest.json").exists():
            raise triage.TriageError(f"{run_root}: sealed Scenario Run is immutable")
        run = triage.read_json(run_root / "run.json")
        triage.validate_definition(run, "run-record.schema.json", "run", str(run_root))
        result = triage.read_json(source)
        triage.validate_definition(result, "run-record.schema.json", "checkResult", str(source))
        if result["run_id"] != run["run_id"]:
            raise triage.TriageError(f"{source}: result belongs to a different run")
        destination = run_root / "check-results.jsonl"
        previous = triage.read_jsonl(destination)
        for index, item in enumerate(previous, 1):
            triage.validate_definition(
                item, "run-record.schema.json", "checkResult", f"{destination}:{index}"
            )
        proposed = [*previous, result]
        triage.validate_unique(proposed, "check_result_id", str(destination))
        triage.validate_result_links(proposed, run_root)
        configured, scenario_files = triage.load_suite(suite_root)
        triage.validate_run_against_suite_data(
            run_root,
            run,
            proposed,
            {"configured_scenarios": configured, "scenario_files": scenario_files},
        )
        if (
            result["corrects_result_id"] is not None
            and "correction-chain" not in result["review_reasons"]
        ):
            raise triage.TriageError(f"{source}: correction needs correction-chain review reason")
        if result["corrects_result_id"] is None and "correction-chain" in result["review_reasons"]:
            raise triage.TriageError(f"{source}: correction-chain needs a correction target")
        line = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # Replace only after the complete candidate is durable; retain every prior byte.
        triage.atomic_text(destination, (destination.read_bytes() + line).decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("result_json", type=Path)
    parser.add_argument("--suite-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        append_check_result(args.run_root, args.suite_root, args.result_json)
    except (OSError, ValueError, TimeoutError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
