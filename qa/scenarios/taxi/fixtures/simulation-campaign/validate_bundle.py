"""Read-only validator for retained Taxi shared-bundle campaign evidence."""

from __future__ import annotations

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


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate(
    manifest_path: Path,
    build_result_path: Path,
    result_paths: list[Path],
    expected_tests: list[str],
) -> dict[str, object]:
    """Prove one ready shared build is bound by every expected test result."""
    _need(len(expected_tests) == len(set(expected_tests)), "expected tests contain duplicates")
    manifest, _ = _load(manifest_path)
    build, build_raw = _load(build_result_path)
    _need(
        build.get("$schema") == "booley.bundle-build-result/v1",
        "unexpected build-result schema",
    )
    _need(build.get("state") == "ready", "shared build is not ready")
    bundle = build.get("bundle")
    _need(isinstance(bundle, dict), "shared build has no bundle")
    _need(bundle.get("sharing") == "shared_variant", "build is not shared_variant")
    items = manifest.get("work_items")
    _need(isinstance(items, list), "manifest work_items are missing")
    manifest_tests = []
    for item in items:
        _need(isinstance(item, dict), "manifest work item is invalid")
        selection = item.get("selection")
        _need(isinstance(selection, dict), "manifest selection is missing")
        names = selection.get("names")
        _need(isinstance(names, list) and len(names) == 1, "work item is not one test")
        manifest_tests.append(names[0])
    _need(manifest_tests == expected_tests, "manifest test order differs")

    expected_digest = _digest(build_raw)
    result_tests = []
    attempt_ids = set()
    for path in result_paths:
        result, _ = _load(path)
        reference = result.get("build_result")
        _need(isinstance(reference, dict), "result build reference is missing")
        _need(reference.get("sharing") == "shared_variant", "result uses a private build")
        _need(reference.get("sha256") == expected_digest, "result build digest differs")
        observations = result.get("observations")
        _need(
            isinstance(observations, list) and len(observations) == 1,
            "ordinary result must contain one observation",
        )
        observation = observations[0]
        _need(isinstance(observation, dict), "result observation is invalid")
        result_tests.append(observation.get("test"))
        attempt_ids.add(result.get("attempt_id"))
    _need(result_tests == expected_tests, "result test order differs")
    _need(len(attempt_ids) == len(expected_tests), "simulation attempts are not independent")
    return {
        "build_result_sha256": expected_digest,
        "bundle_id": bundle.get("bundle_id"),
        "compile_count": 1,
        "tests": result_tests,
    }
