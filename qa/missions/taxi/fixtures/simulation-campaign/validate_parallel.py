"""Validate retained Taxi bounded-parallel Simulation Campaign evidence.

The timeline document is the operator's own measurement of one parallel run:

- ``intervals``: one measured simulator process per Simulation Attempt with
  ``test``, ``start_ns``, ``end_ns``, ``work_item_id``, ``attempt_id``,
  ``child_execution_id`` (``null`` for an attempt that ran on the outer Job's
  own claim), ``run_directory``, ``relative_output``, ``token``,
  ``output_text``, and ``output`` (``path`` and ``sha256`` of the retained
  copy of the attempt's output file, relative to the timeline document).

``token`` must equal the interval's test name: the fixture writes the test
name into its output file, and the attempt's run log is searched for
``+test=<token>``.

Every other input is a document Booley wrote, read in place or from a copy.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_EVIDENCE_FIELDS = {"path", "bytes", "sha256", "kind", "owner"}
_SHARED_OUTPUT = "qa-shared-name.txt"
_EXPECTED_TESTS = ["slow-first", "slow-fail", "slow-last"]
_EXPECTED_GRADES = ["pass", "fail", "pass"]


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _file_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _is_timestamp(value: object) -> bool:
    return type(value) is int


def _is_name(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _relative(path: object, label: str) -> PurePosixPath:
    """Return a contained relative path, rejecting absolute and parent-escaping ones."""
    _need(_is_name(path), f"{label} path is missing")
    relative = PurePosixPath(path)
    _need(not relative.is_absolute() and ".." not in relative.parts, f"{label} path escapes")
    return relative


def _read_retained(root: Path, path: object, label: str) -> bytes:
    """Read one retained regular file named by a relative path below ``root``."""
    retained = root / _relative(path, label)
    _need(retained.is_file() and not retained.is_symlink(), f"{label} file is not retained")
    return retained.read_bytes()


def _intervals(document: dict[str, object]) -> list[dict[str, object]]:
    value = document.get("intervals")
    _need(isinstance(value, list) and value, "process intervals are missing")
    intervals: list[dict[str, object]] = []
    for item in value:
        _need(isinstance(item, dict), "process interval is invalid")
        _need(
            _is_timestamp(item.get("start_ns"))
            and _is_timestamp(item.get("end_ns"))
            and item["start_ns"] < item["end_ns"],
            "process interval bounds are invalid",
        )
        child = item.get("child_execution_id")
        _need(child is None or _is_name(child), "child identity is invalid")
        intervals.append(item)
    return intervals


def validate_attempt_isolation(timeline_path: Path, campaign_path: Path) -> None:
    """Require one common relative output name with distinct attempt-owned content.

    ``campaign_path`` is the Campaign directory holding ``manifest.json`` and
    ``work-items/``.
    """
    intervals = _intervals(_load(timeline_path))
    _need(len(intervals) >= 3, "attempt-isolation case requires at least three attempts")
    relative_paths = {item.get("relative_output") for item in intervals}
    _need(relative_paths == {_SHARED_OUTPUT}, "relative output names differ")
    directories = [item.get("run_directory") for item in intervals]
    tokens = [item.get("token") for item in intervals]
    work_items = [item.get("work_item_id") for item in intervals]
    _need(all(_is_name(value) for value in directories), "run directory is missing")
    _need(all(_is_name(value) for value in tokens), "attempt token is missing")
    _need(all(_is_name(value) for value in work_items), "work-item identity is missing")
    _need(len(set(directories)) == len(directories), "parallel run directories collide")
    _need(len(set(tokens)) == len(tokens), "parallel attempt tokens collide")
    _need(len(set(work_items)) == len(intervals), "process intervals reuse a work item")
    attempts = _attempt_directories(campaign_path)
    for item in intervals:
        _need(item.get("token") == item.get("test"), "attempt token is not the test name")
        _need(item.get("token") == item.get("output_text"), "attempt output is contaminated")
        attempt_id = item.get("attempt_id")
        _need(
            isinstance(attempt_id, str) and attempt_id in attempts,
            "Simulation Attempt identity is missing",
        )
        _validate_owned_run_directory(item, attempts[attempt_id])
        _validate_retained_output(item, timeline_path.parent)
        _validate_attempt_log(item, attempts[attempt_id], tokens)


def _attempt_directories(campaign_path: Path) -> dict[str, Path]:
    """Index the Campaign's Simulation Attempt directories by attempt id."""
    attempts: dict[str, Path] = {}
    for path in sorted(campaign_path.glob("work-items/*/attempts/*/attempt.json")):
        attempt_id = _load(path).get("attempt_id")
        _need(
            isinstance(attempt_id, str) and path.parent.name.endswith(f"-{attempt_id}"),
            "attempt directory does not name its Simulation Attempt",
        )
        _need(attempt_id not in attempts, "Simulation Attempt identity repeats")
        attempts[attempt_id] = path.parent
    _need(bool(attempts), "Campaign has no Simulation Attempts")
    return attempts


def _validate_owned_run_directory(item: dict[str, object], attempt_directory: Path) -> None:
    attempt = _load(attempt_directory / "attempt.json")
    _need(
        attempt.get("$schema") == "booley.simulation-attempt/v1",
        "Simulation Attempt schema differs",
    )
    _need(attempt.get("work_item_id") == item["work_item_id"], "attempt work item differs")
    _need(
        attempt.get("child_execution_id") == item["child_execution_id"],
        "attempt child identity differs",
    )
    run = attempt.get("run_directory")
    _need(isinstance(run, dict), "attempt run directory is missing")
    _need(run.get("owned") is True, "run directory is not attempt-owned")
    resolved = run.get("resolved")
    _need(resolved == item["run_directory"], "measured run directory differs from the attempt")
    _need(
        str(item["attempt_id"]) in PurePosixPath(str(resolved)).name,
        "run directory is not attempt-scoped",
    )


def _validate_retained_output(item: dict[str, object], evidence_root: Path) -> None:
    token = item["token"]
    artifact = item.get("output")
    _need(isinstance(artifact, dict), "output evidence is missing")
    path = artifact.get("path")
    _need(
        isinstance(path, str) and str(item["attempt_id"]) in path,
        "output path is not attempt-scoped",
    )
    _need(_is_digest(artifact.get("sha256")), "output digest is invalid")
    raw = _read_retained(evidence_root, path, "output")
    _need(_file_digest(raw) == artifact["sha256"], "output retained digest differs")
    _need(raw.rstrip(b"\n") == str(token).encode(), "output retained token differs")


def _validate_attempt_log(
    item: dict[str, object], attempt_directory: Path, tokens: list[object]
) -> None:
    """Authenticate the attempt's own run log and require only its own token in it."""
    result = _load(attempt_directory.parents[1] / "result.json")
    attempt_id = item["attempt_id"]
    _need(result.get("attempt_id") == attempt_id, "Simulation Result names another attempt")
    _need(result.get("work_item_id") == item["work_item_id"], "result work item differs")
    evidence = result.get("evidence")
    _need(isinstance(evidence, list), "Simulation Result evidence is missing")
    logs = [
        entry
        for entry in evidence
        if isinstance(entry, dict) and entry.get("kind") == "live_run_log"
    ]
    _need(len(logs) == 1, "Simulation Result does not reference one run log")
    raw = _read_attempt_evidence(logs[0], attempt_id, attempt_directory)
    text = raw.decode(errors="replace")
    _need(f"+test={item['token']}" in text, "log evidence omits the attempt token")
    _need(
        not any(f"+test={other}" in text for other in tokens if other != item["token"]),
        "log evidence is contaminated",
    )


def _need_attempt_evidence(reference: object, attempt_id: object) -> None:
    """Check one attempt-relative typed artifact reference without reading its file."""
    _need(
        isinstance(reference, dict) and set(reference) == _EVIDENCE_FIELDS,
        "evidence is not a typed artifact reference",
    )
    _need(reference["owner"] == attempt_id, "evidence owner differs from the attempt")
    _need(_is_name(reference["kind"]), "evidence kind is missing")
    _relative(reference["path"], "evidence")
    _need(type(reference["bytes"]) is int and reference["bytes"] > 0, "evidence size is invalid")
    _need(_is_digest(reference["sha256"]), "evidence digest is invalid")


def _read_attempt_evidence(
    reference: object, attempt_id: object, attempt_directory: Path
) -> bytes:
    """Authenticate one attempt-relative artifact reference against its retained file."""
    _need_attempt_evidence(reference, attempt_id)
    raw = _read_retained(attempt_directory, reference["path"], "evidence")
    _need(len(raw) == reference["bytes"], "evidence retained size differs")
    _need(_file_digest(raw) == reference["sha256"], "evidence retained digest differs")
    return raw


def validate_continue_after_failure(
    manifest_path: Path, summary_path: Path, result_paths: list[Path]
) -> None:
    """Require pass/fail/pass terminals rendered in immutable manifest order."""
    manifest_raw = manifest_path.read_bytes()
    manifest = _load(manifest_path)
    expected_ids = _manifest_work_item_ids(manifest)
    results = [_load(path) for path in result_paths]
    _need(len(results) == len(_EXPECTED_TESTS), "not every work item published a terminal result")
    result_order = [_single_observation(item).get("test") for item in results]
    grades = [item.get("grade") for item in results]
    _need(result_order == _EXPECTED_TESTS, "result order differs from the manifest")
    _need(grades == _EXPECTED_GRADES, "terminal grades differ")
    # Durable records bind the manifest by its bytes without the trailing newline.
    manifest_digest = _file_digest(manifest_raw.rstrip(b"\n"))
    identity = {
        "campaign_id": manifest.get("campaign_id"),
        "manifest_sha256": manifest_digest,
        "workload_sha256": _workload_digest(manifest),
    }
    _need(_is_name(identity["campaign_id"]), "manifest Campaign identity is missing")
    for result, work_item_id in zip(results, expected_ids, strict=True):
        _validate_result_identity(result, work_item_id, identity)
    attempt_ids = [result["attempt_id"] for result in results]
    _need(len(set(attempt_ids)) == len(attempt_ids), "Simulation Results share an attempt")
    _validate_summary(_load(summary_path), expected_ids, identity)


def _manifest_work_item_ids(manifest: dict[str, object]) -> list[object]:
    work_items = manifest.get("work_items")
    _need(isinstance(work_items, list), "manifest work items are missing")
    manifest_order = []
    for item in work_items:
        _need(isinstance(item, dict), "manifest work item is invalid")
        selection = item.get("selection")
        _need(isinstance(selection, dict), "manifest selection is missing")
        manifest_order.extend(selection.get("names", []))
    _need(manifest_order == _EXPECTED_TESTS, "manifest order differs from pass/fail/pass")
    return [item.get("work_item_id") for item in work_items]


def _workload_digest(manifest: dict[str, object]) -> object:
    fingerprints = manifest.get("fingerprints")
    _need(isinstance(fingerprints, dict), "manifest fingerprints are missing")
    digest = fingerprints.get("workload_sha256")
    _need(_is_digest(digest), "manifest workload fingerprint is missing")
    return digest


def _single_observation(result: dict[str, object]) -> dict[str, object]:
    observations = result.get("observations")
    _need(
        isinstance(observations, list)
        and len(observations) == 1
        and isinstance(observations[0], dict),
        "Simulation Result does not carry one observation",
    )
    return observations[0]


def _validate_result_identity(
    result: dict[str, object], work_item_id: object, identity: dict[str, object]
) -> None:
    _need(
        result.get("$schema") == "booley.simulation-result/v2", "Simulation Result schema differs"
    )
    _need(result.get("state") == "completed", "Simulation Result is not a completed terminal")
    attempt_id = result.get("attempt_id")
    evidence = result.get("evidence")
    _need(
        all(result.get(key) == value for key, value in identity.items())
        and result.get("work_item_id") == work_item_id
        and _is_name(attempt_id)
        and isinstance(evidence, list)
        and bool(evidence),
        "Simulation Results do not authenticate manifest/work-item/attempt identity",
    )
    for reference in evidence:
        _need_attempt_evidence(reference, attempt_id)


def _validate_summary(
    summary: dict[str, object], expected_ids: list[object], identity: dict[str, object]
) -> None:
    _need(
        summary.get("$schema") == "booley.simulation-campaign-summary/v1",
        "summary schema differs",
    )
    _need(summary.get("aggregate_grade") == "fail", "summary is not a strict failure")
    _need(
        summary.get("manifest_sha256") == identity["manifest_sha256"],
        "summary manifest digest differs",
    )
    _need(
        summary.get("campaign_id") == identity["campaign_id"]
        and summary.get("workload_sha256") == identity["workload_sha256"],
        "summary identifies a different Simulation Campaign",
    )
    # ``completed`` lists work items in manifest order; it is the summary's ordering.
    _need(summary.get("completed") == expected_ids, "summary completion identities differ")
    _need(
        summary.get("complete") is True
        and summary.get("pending") == []
        and summary.get("interrupted") == [],
        "summary reports unfinished work",
    )
