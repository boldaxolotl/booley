"""Validate retained Taxi Cocotb-batch and MCP Simulation Campaign evidence."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_OBSERVATION_PREVIEW_LIMIT = 32
_OBSERVATION_FIELDS = {
    "test",
    "execution",
    "failure_class",
    "functional",
    "assertions",
    "assertion_count",
    "cycle_count",
    "detail",
}
_ARTIFACT_FIELDS = {"path_base", "path", "bytes", "sha256", "kind", "owner"}
_ARTIFACT_BASES = {"report_invocation", "reports_root", "external_origin_target"}
# Campaign artifact name -> typed reference kind; ``coverage`` exists only for coverage runs.
_REQUIRED_ARTIFACTS = {
    "manifest": "simulation_campaign_manifest",
    "simulation": "simulation_projection",
}
_OPTIONAL_ARTIFACTS = {"coverage": "coverage_campaign_reference"}


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_cocotb_batch(
    manifest_path: Path,
    interrupted_attempt_path: Path,
    interrupted_result_path: Path,
    resumed_result_path: Path,
) -> None:
    """Require one interrupted batch followed by one whole-batch retry."""
    manifest = _load(manifest_path)
    work_items = manifest.get("work_items")
    _need(isinstance(work_items, list) and len(work_items) == 1, "expected one work item")
    work_item = work_items[0]
    _need(isinstance(work_item, dict), "Cocotb work item is invalid")
    _need(work_item.get("kind") == "cocotb_batch", "work item is not a Cocotb batch")

    interrupted = _load(interrupted_attempt_path)
    resumed = _load(resumed_result_path)
    _need(
        interrupted.get("work_item_id")
        == resumed.get("work_item_id")
        == work_item.get("work_item_id"),
        "attempts do not bind the one batch work item",
    )
    _need(
        interrupted.get("attempt_id") != resumed.get("attempt_id"), "batch retry reused an attempt"
    )
    _need(
        interrupted.get("$schema") == "booley.simulation-attempt/v1",
        "interrupted attempt schema differs",
    )
    _need(not interrupted_result_path.exists(), "interrupted batch has a terminal result")
    _need(resumed.get("state") == "completed", "resumed batch did not complete")
    observations = resumed.get("observations")
    _need(
        isinstance(observations, list) and len(observations) > 1,
        "batch has no per-test observations",
    )
    names = []
    for observation in observations:
        _need(isinstance(observation, dict), "Cocotb observation is invalid")
        name = observation.get("test")
        _need(isinstance(name, str) and name, "Cocotb observation has no test name")
        names.append(name)
    _need(len(names) == len(set(names)), "Cocotb observation names repeat")
    selection = work_item.get("selection")
    _need(isinstance(selection, dict), "Cocotb selection is missing")
    _need(selection.get("kind") == "named", "Cocotb selection is not exact named selection")
    _need(selection.get("names") == names, "Cocotb observations differ from exact selection")
    _need(
        all(observation.get("attempt_id") is None for observation in observations),
        "per-test observations claim independent Simulation Attempts",
    )


def validate_mcp_response(response_path: Path, max_bytes: int) -> None:
    """Require bounded typed artifact references and independent observation fields."""
    raw = response_path.read_bytes()
    _need(0 < len(raw) <= max_bytes, "MCP response exceeds the declared bound")
    response = _load(response_path)
    envelope = response.get("result", response)
    _need(isinstance(envelope, dict), "MCP result envelope is missing")
    content = envelope.get("content")
    _need(isinstance(content, list) and content, "MCP text card is missing")
    text = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    # MCP clients serialize the structured payload under either spelling.
    structured = envelope.get("structuredContent", envelope.get("structured_content"))
    _need(isinstance(structured, dict), "MCP structured output is missing")
    reports = structured.get("reports")
    _need(isinstance(reports, list) and reports, "MCP structured report is missing")
    report = reports[0]
    _need(isinstance(report, dict), "MCP structured report is invalid")
    exit_code = report.get("exit_code")
    _need(isinstance(exit_code, int), "MCP exit code is missing")
    _need(f"EXIT_CODE: {exit_code}" in text, "MCP text card exit code differs")
    detail = report.get("detail")
    _need(isinstance(detail, dict), "MCP report detail is missing")
    campaigns = detail.get("campaigns")
    _need(isinstance(campaigns, dict) and campaigns, "MCP campaign details are missing")
    for campaign in campaigns.values():
        _validate_mcp_campaign(campaign)


def _validate_mcp_campaign(value: object) -> None:
    _need(isinstance(value, dict), "MCP campaign detail is invalid")
    _validate_mcp_artifacts(value)
    _need(
        value.get("grade") in {"pass", "fail", "error", "inconclusive"},
        "MCP campaign grade is invalid",
    )
    _need(isinstance(value.get("complete"), bool), "MCP campaign completion is missing")
    _validate_mcp_observations(value)


def _validate_mcp_artifacts(campaign: dict[str, object]) -> None:
    owner = campaign.get("campaign_id")
    _need(isinstance(owner, str) and owner, "MCP campaign identity is missing")
    digest = campaign.get("manifest_sha256")
    _need(
        isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None,
        "MCP manifest digest is invalid",
    )
    artifacts = campaign.get("artifacts")
    _need(isinstance(artifacts, dict), "MCP campaign artifacts are missing")
    known = _REQUIRED_ARTIFACTS | _OPTIONAL_ARTIFACTS
    _need(set(artifacts) <= set(known), "MCP campaign artifacts are unexpected")
    for key in _REQUIRED_ARTIFACTS:
        _need(key in artifacts, f"MCP {key} pointer is missing")
    for key, reference in artifacts.items():
        _validate_mcp_artifact(key, reference, known[key], owner)


def _validate_mcp_artifact(key: str, reference: object, kind: str, owner: str) -> None:
    _need(
        isinstance(reference, dict) and set(reference) == _ARTIFACT_FIELDS,
        f"MCP {key} pointer is not a typed artifact reference",
    )
    _need(reference["path_base"] in _ARTIFACT_BASES, f"MCP {key} pointer base is invalid")
    _need(reference["kind"] == kind, f"MCP {key} pointer kind differs")
    _need(reference["owner"] == owner, f"MCP {key} pointer owner differs")
    path = reference["path"]
    _need(isinstance(path, str) and path, f"MCP {key} pointer path is missing")
    relative = PurePosixPath(path)
    _need(
        not relative.is_absolute() and ".." not in relative.parts,
        f"MCP {key} pointer path escapes",
    )
    _need(
        type(reference["bytes"]) is int and reference["bytes"] > 0,
        f"MCP {key} pointer size is invalid",
    )
    digest = reference["sha256"]
    _need(
        isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None,
        f"MCP {key} pointer digest is invalid",
    )


def _validate_mcp_observations(value: dict[str, object]) -> None:
    observations = value.get("observations")
    total = value.get("observation_total")
    truncated = value.get("observations_truncated")
    _need(isinstance(observations, list), "MCP observation preview is missing")
    _need(
        isinstance(total, int) and not isinstance(total, bool), "MCP observation total is invalid"
    )
    _need(isinstance(truncated, bool), "MCP observation truncation flag is missing")
    _need(
        len(observations) == min(total, _OBSERVATION_PREVIEW_LIMIT),
        "MCP observation preview length disagrees",
    )
    _need(truncated == (total > len(observations)), "MCP observation truncation flag disagrees")
    for observation in observations:
        _validate_mcp_observation(observation)
    counts = value.get("observation_counts")
    _need(isinstance(counts, dict), "MCP observation counts are missing")
    totals = []
    for key in ("execution", "functional", "assertions"):
        counter = counts.get(key)
        _need(isinstance(counter, dict) and counter, f"MCP {key} observations are missing")
        _need(
            all(
                isinstance(name, str) and isinstance(count, int) and count >= 0
                for name, count in counter.items()
            ),
            f"MCP {key} observation counts are invalid",
        )
        totals.append(sum(counter.values()))
        preview = Counter(str(item[key]) for item in observations)
        _need(
            all(counter.get(name, 0) >= count for name, count in preview.items()),
            f"MCP {key} counts contradict preview",
        )
        if not truncated:
            _need(dict(counter) == dict(preview), f"MCP {key} counts differ from preview")
    _need(len(set(totals)) == 1 and totals[0] == total > 0, "MCP observation totals disagree")


def _validate_mcp_observation(value: object) -> None:
    _need(
        isinstance(value, dict) and set(value) == _OBSERVATION_FIELDS,
        "MCP observation fields differ",
    )
    _need(
        value["test"] is None or isinstance(value["test"], str), "MCP observation test is invalid"
    )
    _need(
        value["execution"]
        in {
            "completed",
            "timeout",
            "crash",
            "setup_error",
            "blocked_by_build",
            "aborted",
            "not_run",
        },
        "MCP execution observation is invalid",
    )
    _need(
        value["failure_class"] in {None, "design", "infrastructure"},
        "MCP failure class is invalid",
    )
    _need(
        value["functional"] in {"pass", "fail", "inconclusive", "not_observed"},
        "MCP functional observation is invalid",
    )
    _need(
        value["assertions"] in {"clean", "dirty", "not_observed"},
        "MCP assertion observation is invalid",
    )
    count = value["assertion_count"]
    _need(
        isinstance(count, int) and not isinstance(count, bool) and count >= 0,
        "MCP assertion count is invalid",
    )
    cycles = value["cycle_count"]
    _need(
        cycles is None or (type(cycles) is int and cycles >= 0),
        "MCP cycle count is invalid",
    )
    _need(isinstance(value["detail"], dict), "MCP observation detail is invalid")
