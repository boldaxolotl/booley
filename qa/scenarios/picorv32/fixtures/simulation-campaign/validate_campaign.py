"""Read-only structural validator for retained Simulation Campaign QA evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _load(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value, raw


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _work_item_names(manifest: dict[str, object]) -> list[str]:
    items = manifest.get("work_items")
    _need(isinstance(items, list), "manifest work_items are missing")
    names: list[str] = []
    for item in items:
        _need(isinstance(item, dict), "manifest work item is invalid")
        selection = item.get("selection")
        _need(isinstance(selection, dict), "work-item selection is missing")
        selected = selection.get("names")
        _need(
            isinstance(selected, list) and len(selected) == 1,
            "ordinary work item must select one test",
        )
        name = selected[0]
        _need(isinstance(name, str), "work-item test name is invalid")
        names.append(name)
    return names


def _manifest_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()


def validate_backlinks(
    manifest_path: Path,
    summary_path: Path,
    projection_path: Path,
) -> dict[str, object]:
    """Authenticate summary and compatibility backlinks to one manifest."""
    manifest, manifest_raw = _load(manifest_path)
    summary, _summary_raw = _load(summary_path)
    projection, _projection_raw = _load(projection_path)
    digest = _manifest_digest(manifest_raw)
    _need(
        summary.get("campaign_id") == manifest.get("campaign_id"),
        "summary identifies a different Simulation Campaign",
    )
    _need(summary.get("manifest_sha256") == digest, "summary manifest digest differs")
    _need(
        Path(str(projection.get("campaign_manifest", ""))).resolve() == manifest_path.resolve(),
        "compatibility manifest backlink differs",
    )
    _need(
        Path(str(projection.get("campaign_summary", ""))).resolve() == summary_path.resolve(),
        "compatibility summary backlink differs",
    )
    return {"campaign_id": manifest.get("campaign_id"), "manifest_sha256": digest}


def validate_interrupted_resume(
    completed_result_before: Path,
    completed_result_after: Path,
    *,
    completed_attempts_before: int,
    completed_attempts_after: int,
    interrupted_attempts_before: int,
    interrupted_attempts_after: int,
) -> None:
    """Prove completed work stayed immutable while interrupted work retried."""
    _need(
        completed_result_before.read_bytes() == completed_result_after.read_bytes(),
        "completed result changed during resume",
    )
    _need(
        completed_attempts_before == completed_attempts_after,
        "completed work received another attempt",
    )
    _need(
        interrupted_attempts_after == interrupted_attempts_before + 1,
        "interrupted work did not receive exactly one retry",
    )


def validate_rejection(
    *,
    exit_code: int,
    attempts_before: list[str],
    attempts_after: list[str],
    simulator_started: bool,
    diagnostic: str,
) -> None:
    """Validate duplicate-selection or workload-mismatch fail-closed evidence."""
    _need(exit_code == 2, "rejection did not exit 2")
    _need(attempts_before == attempts_after, "rejection created an attempt")
    _need(not simulator_started, "rejection launched a simulator")
    _need(bool(diagnostic.strip()), "rejection diagnostic is missing")


def validate_criteria_journal(
    *,
    required_suite: list[str],
    observed_tests: list[str],
    transaction_ids_before: list[str],
    transaction_ids_after: list[str],
) -> None:
    """Check that only a complete Required Simulation Suite can add acceptance."""
    complete = set(required_suite).issubset(observed_tests)
    added = len(transaction_ids_after) - len(transaction_ids_before)
    _need(added == (1 if complete else 0), "Criteria journal scope differs")


def validate(manifest_path: Path, expected: list[str]) -> dict[str, object]:
    _need(len(expected) == len(set(expected)), "duplicate expected test name")
    manifest, raw = _load(manifest_path)
    _need(raw.endswith(b"\n"), "manifest is not newline-terminated")
    _need(
        manifest.get("$schema") == "booley.simulation-campaign-manifest/v1",
        "unexpected manifest schema",
    )
    _need(_work_item_names(manifest) == expected, "work-item order differs")
    required = manifest.get("required_suite")
    _need(isinstance(required, dict), "Required Simulation Suite is missing")
    fingerprints = manifest.get("fingerprints")
    _need(isinstance(fingerprints, dict), "manifest fingerprints are missing")
    _need(
        isinstance(fingerprints.get("workload_sha256"), str),
        "workload fingerprint is missing",
    )
    return {
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": _manifest_digest(raw),
        "selection": expected,
        "required_suite": required.get("names"),
        "work_items": len(expected),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expected-test", action="append", required=True)
    args = parser.parse_args()
    try:
        result = validate(args.manifest, args.expected_test)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"Simulation Campaign evidence invalid: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
