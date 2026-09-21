"""Read-only structural checks for retained UART Simulation Campaign evidence."""

from __future__ import annotations

import json
from pathlib import Path


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_runtime_isolation(
    manifest_path: Path,
    attempt_paths: list[Path],
    result_paths: list[Path],
) -> None:
    """Require owned distinct run directories and one shared build binding."""
    manifest = _load(manifest_path)
    workload = manifest.get("workload")
    _need(isinstance(workload, dict), "manifest workload is missing")
    run_cwd = workload.get("run_cwd")
    _need(isinstance(run_cwd, dict), "manifest run_cwd is missing")
    _need(run_cwd.get("kind") == "templated", "run_cwd is not templated")
    attempts = [_load(path) for path in attempt_paths]
    results = [_load(path) for path in result_paths]
    _need(len(attempts) == len(results) == 2, "runtime Check requires two attempts")
    directories = []
    for attempt in attempts:
        run_directory = attempt.get("run_directory")
        _need(isinstance(run_directory, dict), "attempt run directory is missing")
        _need(run_directory.get("owned") is True, "attempt run directory is not owned")
        directories.append(run_directory.get("resolved"))
    _need(len(set(directories)) == 2, "attempt run directories collide")
    build_digests = set()
    for result in results:
        attempt_id = result.get("attempt_id")
        reference = result.get("build_result")
        _need(isinstance(reference, dict), "result build reference is missing")
        _need(
            reference.get("sharing") == "shared_variant",
            "result does not use a shared Simulator Bundle",
        )
        build_digests.add(reference.get("sha256"))
        inputs = result.get("runtime_inputs")
        _need(isinstance(inputs, list) and inputs, "result runtime inputs are missing")
        for binding in inputs:
            _need(isinstance(binding, dict), "runtime-input binding is invalid")
            copy = binding.get("authoritative_copy")
            _need(isinstance(copy, dict), "authoritative runtime-input copy is missing")
            _need(copy.get("owner") == attempt_id, "runtime input has a foreign owner")
    _need(len(build_digests) == 1, "runtime attempts do not bind one shared build")


def validate_immutable_failure(
    attempt_path: Path,
    result_path: Path,
    environment_path: Path,
) -> None:
    """Require default immutable mode to refuse build mutation without a pass."""
    attempt = _load(attempt_path)
    result = _load(result_path)
    environment = _load(environment_path)
    _need(attempt.get("pre_sim_build_access") == "immutable", "attempt is not immutable")
    _need("BOOLEY_BUILD_ROOT" not in environment, "immutable hook disclosed build root")
    _need(result.get("grade") == "fail", "mutation attempt did not fail")
    _need(result.get("state") == "setup_error", "mutation failure is not setup_error")


def validate_legacy_builds(
    manifest_path: Path,
    attempt_paths: list[Path],
    result_paths: list[Path],
) -> None:
    """Require disclosed private per-test builds and forbid shared claims."""
    manifest = _load(manifest_path)
    workload = manifest.get("workload")
    _need(isinstance(workload, dict), "manifest workload is missing")
    _need(
        workload.get("pre_sim_build_access") == "legacy-per-test",
        "manifest does not disclose legacy-per-test",
    )
    attempts = [_load(path) for path in attempt_paths]
    _need(len(attempts) == len(result_paths) == 2, "legacy Check requires two attempts")
    _need(
        all(item.get("pre_sim_build_access") == "legacy-per-test" for item in attempts),
        "attempt access mode differs",
    )
    references = []
    for path in result_paths:
        result = _load(path)
        reference = result.get("build_result")
        _need(isinstance(reference, dict), "legacy result build reference is missing")
        _need(
            reference.get("sharing") == "private_work_item",
            "legacy result makes a shared Simulator Bundle claim",
        )
        references.append((reference.get("build_attempt_id"), reference.get("sha256")))
    _need(len(set(references)) == 2, "legacy tests did not receive distinct private builds")


def validate_literal_cwd_serialization(
    manifest_path: Path,
    timeline_path: Path,
) -> None:
    """Require shared literal-CWD attempts to serialize without global serialization."""
    manifest = _load(manifest_path)
    workload = manifest.get("workload")
    _need(isinstance(workload, dict), "manifest workload is missing")
    run_cwd = workload.get("run_cwd")
    _need(isinstance(run_cwd, dict), "manifest run_cwd is missing")
    _need(run_cwd.get("kind") == "literal", "run_cwd is not literal")
    timeline = _load(timeline_path)
    shared = timeline.get("shared_intervals")
    unrelated = timeline.get("unrelated_intervals")
    _need(isinstance(shared, list) and len(shared) == 2, "expected two shared intervals")
    _need(isinstance(unrelated, list) and unrelated, "unrelated interval is missing")
    resolved = set()
    for interval in shared:
        _need(isinstance(interval, dict), "shared interval is invalid")
        start = interval.get("start_ns")
        end = interval.get("end_ns")
        _need(
            isinstance(start, int) and isinstance(end, int) and start < end,
            "shared interval bounds are invalid",
        )
        resolved.add(interval.get("run_directory"))
    _need(len(resolved) == 1, "shared attempts do not resolve to one literal directory")
    ordered = sorted(shared, key=lambda item: item["start_ns"])
    _need(ordered[0]["end_ns"] <= ordered[1]["start_ns"], "literal CWD attempts overlap")
    overlaps = False
    for other in unrelated:
        _need(isinstance(other, dict), "unrelated interval is invalid")
        start = other.get("start_ns")
        end = other.get("end_ns")
        _need(
            isinstance(start, int) and isinstance(end, int) and start < end,
            "unrelated interval bounds are invalid",
        )
        overlaps = overlaps or any(
            start < interval["end_ns"] and interval["start_ns"] < end
            for interval in shared
        )
    _need(overlaps, "evidence does not prove unrelated work can overlap")
