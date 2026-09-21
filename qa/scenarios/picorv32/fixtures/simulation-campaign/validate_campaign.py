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


def validate(manifest_path: Path, expected: list[str]) -> dict[str, object]:
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
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
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
        parser.exit(2, f"campaign evidence invalid: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
