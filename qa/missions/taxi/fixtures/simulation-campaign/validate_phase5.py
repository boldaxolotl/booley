"""Validate retained Taxi Cocotb-batch and MCP Simulation Campaign evidence."""

from __future__ import annotations

import hashlib
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
# Campaign artifact name -> typed reference kind; ``coverage`` exists only for coverage runs.
_REQUIRED_ARTIFACTS = {
    "manifest": "simulation_campaign_manifest",
    "simulation": "simulation_projection",
}
_OPTIONAL_ARTIFACTS = {"coverage": "coverage_campaign_reference"}
_ATTEMPT_SCHEMA = "booley.simulation-attempt/v1"
_RESULT_SCHEMA = "booley.simulation-result/v2"


def _load(path: Path) -> dict[str, object]:
    return _decode(path.read_bytes(), str(path))


def _decode(raw: bytes, label: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected a JSON object")
    return value


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _file_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _manifest_digest(raw: bytes) -> str:
    """Return the digest durable records bind: manifest bytes without the final newline."""
    return _file_digest(raw.rstrip(b"\n"))


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _selected_tests(manifest: dict[str, object]) -> list[object]:
    """Return every test the manifest selects, in work-item order."""
    work_items = manifest.get("work_items")
    _need(isinstance(work_items, list) and bool(work_items), "manifest work items are missing")
    names: list[object] = []
    for item in work_items:
        _need(isinstance(item, dict), "manifest work item is invalid")
        selection = item.get("selection")
        _need(isinstance(selection, dict), "manifest selection is missing")
        selected = selection.get("names")
        _need(isinstance(selected, list) and bool(selected), "manifest selection names no test")
        names.extend(selected)
    return names


def validate_cocotb_batch(campaign_path: Path, interrupted_attempt_path: Path) -> None:
    """Require one interrupted batch attempt followed by one whole-batch retry.

    ``campaign_path`` is the Campaign directory after the resume.
    ``interrupted_attempt_path`` is the copy of the first attempt's
    ``attempt.json`` taken after the interruption and before the resume.
    """
    manifest_raw = (campaign_path / "manifest.json").read_bytes()
    manifest = _decode(manifest_raw, "manifest")
    work_item = _batch_work_item(manifest)
    identity = {
        "campaign_id": manifest.get("campaign_id"),
        "manifest_sha256": _manifest_digest(manifest_raw),
        "workload_sha256": _workload_digest(manifest),
        "work_item_id": work_item.get("work_item_id"),
    }
    _need(
        isinstance(identity["campaign_id"], str) and isinstance(identity["work_item_id"], str),
        "manifest identity is missing",
    )
    directory = _work_item_directory(campaign_path, work_item)
    first, second = _attempt_pair(directory, identity)
    _need(
        interrupted_attempt_path.read_bytes() == (first / "attempt.json").read_bytes(),
        "interrupted attempt copy differs from the Campaign's first attempt",
    )
    # A work item publishes one terminal result, and it binds its final attempt.
    result = _load(directory / "result.json")
    resumed = _load(second / "attempt.json")
    _need(result.get("$schema") == _RESULT_SCHEMA, "resumed result schema differs")
    _need(
        all(result.get(key) == value for key, value in identity.items()),
        "resumed result does not bind the one batch work item",
    )
    _need(
        result.get("attempt_id") == resumed["attempt_id"]
        and result.get("attempt_ordinal") == resumed["attempt_ordinal"],
        "terminal result does not bind the resumed attempt",
    )
    _need(result.get("state") == "completed", "resumed batch did not complete")
    _validate_batch_observations(result, work_item)


def _batch_work_item(manifest: dict[str, object]) -> dict[str, object]:
    work_items = manifest.get("work_items")
    _need(isinstance(work_items, list) and len(work_items) == 1, "expected one work item")
    work_item = work_items[0]
    _need(isinstance(work_item, dict), "Cocotb work item is invalid")
    _need(work_item.get("kind") == "cocotb_batch", "work item is not a Cocotb batch")
    return work_item


def _workload_digest(manifest: dict[str, object]) -> object:
    fingerprints = manifest.get("fingerprints")
    _need(isinstance(fingerprints, dict), "manifest fingerprints are missing")
    digest = fingerprints.get("workload_sha256")
    _need(_is_digest(digest), "manifest workload fingerprint is missing")
    return digest


def _work_item_directory(campaign_path: Path, work_item: dict[str, object]) -> Path:
    """Locate the work item where the Campaign layout places it."""
    ordinal = work_item.get("ordinal")
    _need(_is_count(ordinal), "work-item ordinal is invalid")
    suffix = str(work_item["work_item_id"]).rsplit(":", 1)[-1]
    directory = campaign_path / "work-items" / f"{ordinal + 1:04d}-{suffix}"
    _need(directory.is_dir() and not directory.is_symlink(), "work-item directory is missing")
    return directory


def _attempt_pair(directory: Path, identity: dict[str, object]) -> tuple[Path, Path]:
    """Return the interrupted and resumed attempt directories after binding both."""
    attempts = sorted((directory / "attempts").iterdir(), key=lambda item: item.name)
    _need(len(attempts) == 2, "batch does not have one interrupted and one resumed attempt")
    attempt_ids = []
    for ordinal, path in enumerate(attempts, start=1):
        _need(path.is_dir() and not path.is_symlink(), "attempt directory is invalid")
        attempt = _load(path / "attempt.json")
        _need(attempt.get("$schema") == _ATTEMPT_SCHEMA, "attempt schema differs")
        _need(
            all(attempt.get(key) == value for key, value in identity.items()),
            "attempts do not bind the one batch work item",
        )
        _need(attempt.get("attempt_ordinal") == ordinal, "attempt ordinals do not follow")
        attempt_id = attempt.get("attempt_id")
        _need(
            isinstance(attempt_id, str) and path.name == f"{ordinal:04d}-{attempt_id}",
            "attempt directory does not name its Simulation Attempt",
        )
        attempt_ids.append(attempt_id)
    _need(attempt_ids[0] != attempt_ids[1], "batch retry reused an attempt")
    return attempts[0], attempts[1]


def _validate_batch_observations(result: dict[str, object], work_item: dict[str, object]) -> None:
    observations = result.get("observations")
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


def validate_mcp_response(
    response_path: Path,
    max_bytes: int,
    report_invocation: Path,
    *,
    external_origin_target: Path | None = None,
) -> None:
    """Require a bounded response whose artifact references and tests authenticate.

    ``report_invocation`` is the retained numbered invocation directory of the
    MCP run (``.../flow-reports/sim/<n>``). ``external_origin_target`` is the
    origin Campaign's Target directory, needed only when the response refers
    to a Campaign another invocation produced.
    """
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
    _need(type(exit_code) is int, "MCP exit code is missing")
    _need(f"EXIT_CODE: {exit_code}" in text, "MCP text card exit code differs")
    detail = report.get("detail")
    _need(isinstance(detail, dict), "MCP report detail is missing")
    campaigns = detail.get("campaigns")
    _need(isinstance(campaigns, dict) and campaigns, "MCP campaign details are missing")
    bases = {
        "report_invocation": report_invocation,
        "reports_root": report_invocation.parent.parent,
        "external_origin_target": external_origin_target,
    }
    for campaign in campaigns.values():
        _validate_mcp_campaign(campaign, bases)


def _validate_mcp_campaign(value: object, bases: dict[str, Path | None]) -> None:
    _need(isinstance(value, dict), "MCP campaign detail is invalid")
    _need(
        value.get("grade") in {"pass", "fail", "error", "inconclusive"},
        "MCP campaign grade is invalid",
    )
    _need(isinstance(value.get("complete"), bool), "MCP campaign completion is missing")
    _validate_mcp_observations(value)
    retained = _read_mcp_artifacts(value, bases)
    _validate_mcp_manifest_binding(value, retained)


def _read_mcp_artifacts(
    campaign: dict[str, object], bases: dict[str, Path | None]
) -> dict[str, bytes]:
    """Authenticate every Campaign artifact reference and return the retained bytes."""
    owner = campaign.get("campaign_id")
    _need(isinstance(owner, str) and owner, "MCP campaign identity is missing")
    artifacts = campaign.get("artifacts")
    _need(isinstance(artifacts, dict), "MCP campaign artifacts are missing")
    known = _REQUIRED_ARTIFACTS | _OPTIONAL_ARTIFACTS
    _need(set(artifacts) <= set(known), "MCP campaign artifacts are unexpected")
    for key in _REQUIRED_ARTIFACTS:
        _need(key in artifacts, f"MCP {key} artifact reference is missing")
    return {
        key: _read_artifact(f"MCP {key} artifact reference", reference, known[key], owner, bases)
        for key, reference in artifacts.items()
    }


def _read_artifact(
    label: str, reference: object, kind: str, owner: str, bases: dict[str, Path | None]
) -> bytes:
    """Authenticate one typed artifact reference against its retained file."""
    _need(
        isinstance(reference, dict) and set(reference) == _ARTIFACT_FIELDS,
        f"{label} is not a typed artifact reference",
    )
    _need(reference["path_base"] in bases, f"{label} base is invalid")
    _need(reference["kind"] == kind, f"{label} kind differs")
    _need(reference["owner"] == owner, f"{label} owner differs")
    path = reference["path"]
    _need(isinstance(path, str) and path, f"{label} path is missing")
    relative = PurePosixPath(path)
    _need(not relative.is_absolute() and ".." not in relative.parts, f"{label} path escapes")
    _need(_is_count(reference["bytes"]) and reference["bytes"] > 0, f"{label} size is invalid")
    _need(_is_digest(reference["sha256"]), f"{label} digest is invalid")
    base = bases[reference["path_base"]]
    _need(base is not None, f"{label} base location is not retained")
    retained = base / relative
    _need(retained.is_file() and not retained.is_symlink(), f"{label} file is not retained")
    raw = retained.read_bytes()
    _need(len(raw) == reference["bytes"], f"{label} retained size differs")
    _need(_file_digest(raw) == reference["sha256"], f"{label} retained digest differs")
    return raw


def _validate_mcp_manifest_binding(
    campaign: dict[str, object], retained: dict[str, bytes]
) -> None:
    """Bind the response to the authenticated manifest's identity and selected tests."""
    manifest = _decode(retained["manifest"], "MCP manifest artifact")
    campaign_id = campaign["campaign_id"]
    _need(manifest.get("campaign_id") == campaign_id, "MCP response names a foreign Campaign")
    _need(
        campaign.get("manifest_sha256") == _manifest_digest(retained["manifest"]),
        "MCP manifest digest differs from the retained manifest",
    )
    projection = _decode(retained["simulation"], "MCP simulation artifact")
    _need(projection.get("campaign_id") == campaign_id, "MCP projection names a foreign Campaign")
    selected = _selected_tests(manifest)
    _need(
        campaign["observation_total"] == len(selected),
        "MCP observation total differs from the manifest selection",
    )
    observed = [item["test"] for item in campaign["observations"]]
    _need(
        observed == selected[:_OBSERVATION_PREVIEW_LIMIT],
        "MCP observations differ from the manifest selection",
    )


def _validate_mcp_observations(value: dict[str, object]) -> None:
    observations = value.get("observations")
    total = value.get("observation_total")
    truncated = value.get("observations_truncated")
    _need(isinstance(observations, list), "MCP observation preview is missing")
    _need(_is_count(total), "MCP observation total is invalid")
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
            all(isinstance(name, str) and _is_count(count) for name, count in counter.items()),
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
    _need(_is_count(value["assertion_count"]), "MCP assertion count is invalid")
    cycles = value["cycle_count"]
    _need(cycles is None or _is_count(cycles), "MCP cycle count is invalid")
    _need(isinstance(value["detail"], dict), "MCP observation detail is invalid")
