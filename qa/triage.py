#!/usr/bin/env python3
"""Deterministic record mechanics for human-owned Public QA triage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from booley.core.boundary import as_dict, as_str, is_str_list
from booley.runtime.timefmt import utc_now_rfc3339

try:
    from .result_projection import effective_status, project_results
    from .run_suite import RunSuiteError as TriageError
    from .run_suite import transitive_requirements, validate_run_suite
    from .triage_lock import exclusive_file_lock
except ImportError:
    from result_projection import effective_status, project_results
    from run_suite import RunSuiteError as TriageError
    from run_suite import transitive_requirements, validate_run_suite
    from triage_lock import exclusive_file_lock

__all__ = ["transitive_requirements"]

RUN_FORMAT_VERSION = 2
TRIAGE_FORMAT_VERSION = 1
NONPASS = {"fail", "blocked", "unavailable"}
FAIL_DISPOSITIONS = {"product-defect", "documentation-defect", "duplicate-finding"}
INCOMPLETE_DISPOSITIONS = {
    "qa-invalidating-defect",
    "infrastructure-failure",
    "operator-failure",
    "unresolved",
}
NEUTRAL_DISPOSITIONS = {
    "qa-improvement",
    "expected-observation",
    "friction",
    "impression",
    "win",
}
DISPOSITIONS = (
    FAIL_DISPOSITIONS | INCOMPLETE_DISPOSITIONS | NEUTRAL_DISPOSITIONS | {"recording-error"}
)
REQUIRED_RUN_FILES = {
    "run.json",
    "operator-state.json",
    "check-results.jsonl",
    "observations.jsonl",
    "cleanup-ledger.json",
    "evidence-manifest.json",
    "run-summary.md",
}
BORROWED_PRESERVATION_CHECK_IDS = {"cleanup.preserve-borrowed", "cleanup-preservation"}


@dataclass(frozen=True)
class RunRecords:
    root: Path
    manifest_hash: str
    run: dict[str, Any]
    state: dict[str, Any]
    manifest: dict[str, Any]
    results: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...]
    evidence_paths: frozenset[str]
    cleanup: dict[str, Any]

    @property
    def run_id(self) -> str:
        return str(self.run["run_id"])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise TriageError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise TriageError(f"{path}: expected a JSON object")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        raise TriageError(f"{path}: {error}") from error
    records = []
    for number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise TriageError(f"{path}:{number}: {error}") from error
        if not isinstance(record, dict):
            raise TriageError(f"{path}:{number}: expected a JSON object")
        records.append(record)
    return records


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def atomic_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    content = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    atomic_text(path, content)


def load_schema(filename: str) -> dict[str, Any]:
    return read_json(Path(__file__).with_name(filename))


def validate_definition(value: Any, schema_name: str, definition: str, context: str) -> None:
    schema = load_schema(schema_name)
    validator_schema = {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    errors = sorted(
        Draft202012Validator(validator_schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(error.message for error in errors)
        raise TriageError(f"{context}: {detail}")


def validate_unique(records: list[dict[str, Any]], field: str, context: str) -> None:
    values = [record[field] for record in records]
    if len(values) != len(set(values)):
        raise TriageError(f"{context}: duplicate {field}")


def contained(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise TriageError(f"{root}: uncontained path {relative}")
    return path


def validate_evidence_reference(run_root: Path, reference: str) -> Path:
    """Resolve one existing regular file beneath the run-owned evidence root."""
    relative = Path(reference)
    evidence_root = run_root / "evidence"
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "evidence":
        raise TriageError(f"{run_root}: invalid evidence reference {reference}")
    if reference != relative.as_posix():
        raise TriageError(f"{run_root}: noncanonical evidence reference {reference}")
    if ".." in relative.parts:
        raise TriageError(f"{run_root}: invalid evidence traversal {reference}")
    path = run_root / relative
    if not path.resolve().is_relative_to(evidence_root.resolve()) or not path.is_file():
        raise TriageError(f"{run_root}: missing or uncontained evidence {reference}")
    return path


def check_result_evidence_refs(result: dict[str, Any]) -> tuple[str, ...]:
    """Extract every evidence-bearing path field from a Check Result."""
    references = [*result["evidence_refs"], *result["recovery_refs"]]
    if deviation := result.get("deviation"):
        references.extend(deviation["evidence_refs"])
    if preservation := result.get("borrowed_preservation"):
        for field in (
            "setup_evidence_refs",
            "end_evidence_refs",
            "pre_run_absence_evidence_refs",
        ):
            references.extend(preservation.get(field, []))
    return tuple(references)


def validate_preseal_evidence_refs(
    run_root: Path, references: tuple[str, ...] | list[str]
) -> None:
    """Require every mutable-record reference to name existing run-owned evidence."""
    for reference in references:
        validate_evidence_reference(run_root, reference)


def validate_evidence(run_root: Path, manifest: dict[str, Any]) -> frozenset[str]:
    paths = []
    for entry in manifest["entries"]:
        path = validate_evidence_reference(run_root, entry["path"])
        if path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
            raise TriageError(f"{path}: evidence identity changed")
        paths.append(entry["path"])
    if len(paths) != len(set(paths)):
        raise TriageError(f"{run_root}: duplicate evidence path")
    return frozenset(paths)


def validate_result_links(results: list[dict[str, Any]], run_root: Path) -> None:
    seen = set()
    for result in results:
        result_id = result["check_result_id"]
        for cause in result["caused_by_result_ids"]:
            if cause not in seen:
                raise TriageError(f"{run_root}: {result_id}: cause must be an earlier result")
        seen.add(result_id)
    project_results(results, str(run_root))


def validate_observation_links(
    observations: list[dict[str, Any]], result_ids: set[str], run_root: Path
) -> None:
    seen = set()
    for observation in observations:
        observation_id = observation["observation_id"]
        linked = set(observation["check_result_ids"]) | set(observation["caused_by_result_ids"])
        if unknown := linked - result_ids:
            raise TriageError(f"{run_root}: {observation_id}: unknown results {sorted(unknown)}")
        corrected = observation["corrects_observation_id"]
        if corrected is not None and corrected not in seen:
            raise TriageError(
                f"{run_root}: {observation_id}: correction must name an earlier observation"
            )
        seen.add(observation_id)


def validate_evidence_refs(
    records: list[dict[str, Any]], evidence_paths: frozenset[str], id_field: str, run_root: Path
) -> None:
    for record in records:
        refs = set(
            check_result_evidence_refs(record)
            if record.get("record_type") == "check-result"
            else record["evidence_refs"]
        )
        if unknown := refs - evidence_paths:
            raise TriageError(
                f"{run_root}: {record[id_field]}: unknown evidence {sorted(unknown)}"
            )


def validate_borrowed_preservation_claims(
    run: dict[str, Any], results: list[dict[str, Any]], run_root: Path
) -> None:
    for result in project_results(results, str(run_root)).surviving_heads:
        if result["check_id"] not in BORROWED_PRESERVATION_CHECK_IDS:
            continue
        status = result["status"]
        if status not in {"pass", "unavailable"}:
            continue
        claim = result.get("borrowed_preservation") or {}
        if status == "pass":
            required = ("scoped_resource_identities", "setup_evidence_refs", "end_evidence_refs")
        else:
            required = ("pre_run_absence_assessment", "pre_run_absence_evidence_refs")
        missing = [key for key in required if not claim.get(key)]
        if status == "unavailable" and not claim.get("pre_run_absence_assessment", "").strip():
            missing = sorted(set(missing) | {"pre_run_absence_assessment"})
        if missing:
            raise TriageError(
                f"{run_root}: {result['check_result_id']}: borrowed preservation "
                f"{status} lacks {', '.join(missing)}"
            )
        refs = set(result["evidence_refs"])
        claim_refs = (
            set(claim["setup_evidence_refs"] + claim["end_evidence_refs"])
            if status == "pass"
            else set(claim["pre_run_absence_evidence_refs"])
        )
        if not claim_refs <= refs:
            raise TriageError(
                f"{run_root}: {result['check_result_id']}: borrowed preservation "
                "evidence must be linked from the Check Result"
            )
        if status == "unavailable" and not claim_refs <= set(run["admission_evidence"]):
            raise TriageError(
                f"{run_root}: {result['check_result_id']}: borrowed preservation "
                "absence assessment must be recorded during admission"
            )


def parse_run_files(
    run_root: Path, evidence_override: dict[str, Any] | None = None
) -> tuple[dict[str, Any], ...]:
    run = read_json(run_root / "run.json")
    state = read_json(run_root / "operator-state.json")
    cleanup = read_json(run_root / "cleanup-ledger.json")
    evidence = (
        evidence_override
        if evidence_override is not None
        else read_json(run_root / "evidence-manifest.json")
    )
    results = read_jsonl(run_root / "check-results.jsonl")
    observations = read_jsonl(run_root / "observations.jsonl")
    validate_definition(run, "run-record.schema.json", "run", str(run_root / "run.json"))
    validate_definition(
        state, "run-record.schema.json", "operatorState", str(run_root / "operator-state.json")
    )
    validate_definition(
        cleanup, "run-record.schema.json", "cleanupLedger", str(run_root / "cleanup-ledger.json")
    )
    validate_definition(
        evidence,
        "run-record.schema.json",
        "evidenceManifest",
        str(run_root / "evidence-manifest.json"),
    )
    for index, result in enumerate(results, 1):
        validate_definition(result, "run-record.schema.json", "checkResult", f"result {index}")
    for index, observation in enumerate(observations, 1):
        validate_definition(
            observation, "run-record.schema.json", "observation", f"observation {index}"
        )
    return run, state, cleanup, evidence, results, observations


def validate_run_contents(
    run_root: Path, evidence_override: dict[str, Any] | None = None
) -> tuple[dict[str, Any], ...]:
    run, state, cleanup, evidence, results, observations = parse_run_files(
        run_root, evidence_override
    )
    run_id = run["run_id"]
    all_records = [state, cleanup, evidence, *results, *observations]
    if any(record["run_id"] != run_id for record in all_records):
        raise TriageError(f"{run_root}: inconsistent run_id")
    validate_unique(results, "check_result_id", str(run_root / "check-results.jsonl"))
    validate_unique(observations, "observation_id", str(run_root / "observations.jsonl"))
    validate_result_links(results, run_root)
    result_ids = {result["check_result_id"] for result in results}
    validate_observation_links(observations, result_ids, run_root)
    observed_checks = {result["check_id"] for result in results}
    if missing := set(run["selected_check_ids"]) - observed_checks:
        raise TriageError(f"{run_root}: selected Checks without results {sorted(missing)}")
    evidence_paths = validate_evidence(run_root, evidence)
    validate_evidence_refs(results, evidence_paths, "check_result_id", run_root)
    validate_evidence_refs(observations, evidence_paths, "observation_id", run_root)
    for index, resource in enumerate(cleanup["resources"], 1):
        unknown = set(resource.get("safe_shutdown_evidence_refs", [])) - evidence_paths
        if unknown:
            raise TriageError(
                f"{run_root}: {resource_identity(resource, index)}: "
                f"unknown shutdown evidence {sorted(unknown)}"
            )
    return run, state, cleanup, evidence, results, observations, evidence_paths


def validate_terminal_state(state: dict[str, Any], run_root: Path) -> None:
    if state["stage"] != "finish" or state["status"] != "complete":
        raise TriageError(f"{run_root}: run is not terminal")
    if state["completed_at"] is None:
        raise TriageError(f"{run_root}: terminal state lacks completed_at")
    if state["active_assignments"] or state["outstanding_mutations"]:
        raise TriageError(f"{run_root}: terminal state has active work")


def validate_run(run_root: Path) -> RunRecords:
    manifest_path = run_root / "run-manifest.json"
    manifest = read_json(manifest_path)
    validate_definition(manifest, "run-record.schema.json", "runManifest", str(manifest_path))
    manifest_files = set(manifest["files"])
    if manifest_files != REQUIRED_RUN_FILES:
        raise TriageError(
            f"{manifest_path}: final record hashes differ from contract: "
            f"missing={sorted(REQUIRED_RUN_FILES - manifest_files)}, "
            f"extra={sorted(manifest_files - REQUIRED_RUN_FILES)}"
        )
    for name, expected in manifest["files"].items():
        path = contained(run_root, name)
        if not path.is_file() or sha256_file(path) != expected:
            raise TriageError(f"{path}: sealed record changed")
    values = validate_run_contents(run_root)
    run, state, cleanup, evidence, results, observations, evidence_paths = values
    validate_terminal_state(state, run_root)
    validate_manifest_counts(manifest, results, observations, evidence)
    if manifest["run_id"] != run["run_id"]:
        raise TriageError(f"{manifest_path}: run_id does not match run.json")
    if manifest["execution_status"] != state["execution_status"]:
        raise TriageError(f"{manifest_path}: execution status mismatch")
    if manifest["cleanup_status"] != state["cleanup_status"]:
        raise TriageError(f"{manifest_path}: cleanup status mismatch")
    if manifest["evidence_manifest_sha256"] != sha256_file(run_root / "evidence-manifest.json"):
        raise TriageError(f"{manifest_path}: evidence manifest mismatch")
    return RunRecords(
        run_root.resolve(),
        sha256_file(manifest_path),
        run,
        state,
        manifest,
        tuple(results),
        tuple(observations),
        evidence_paths,
        cleanup,
    )


def validate_manifest_counts(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    expected = {
        "check_results": len(results),
        "observations": len(observations),
        "evidence": len(evidence["entries"]),
    }
    if manifest["record_counts"] != expected:
        raise TriageError("run-manifest.json: record counts do not match sealed records")


def source_evidence_refs(
    results: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = defaultdict(list)
    for record, id_field in [
        *((result, "check_result_id") for result in results),
        *((observation, "observation_id") for observation in observations),
    ]:
        references = (
            check_result_evidence_refs(record)
            if id_field == "check_result_id"
            else record["evidence_refs"]
        )
        for path in references:
            sources[path].append(record[id_field])
    return {path: sorted(set(ids)) for path, ids in sources.items()}


def build_evidence_manifest(
    run_root: Path, run_id: str, results: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    evidence_root = run_root / "evidence"
    if not evidence_root.is_dir():
        raise TriageError(f"{evidence_root}: missing evidence directory")
    sources = source_evidence_refs(results, observations)
    entries = []
    for path in sorted(item for item in evidence_root.rglob("*") if item.is_file()):
        relative = path.relative_to(run_root).as_posix()
        entries.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "source_refs": sources.get(relative, []),
            }
        )
    unknown = sources.keys() - {entry["path"] for entry in entries}
    if unknown:
        raise TriageError(f"{run_root}: missing referenced evidence {sorted(unknown)}")
    return {
        "run_record_format_version": RUN_FORMAT_VERSION,
        "record_type": "evidence-manifest",
        "run_id": run_id,
        "entries": entries,
    }


def recording_diagnostics(results: list[dict[str, Any]]) -> list[str]:
    diagnostics = []
    for result in results:
        if result["status"] not in NONPASS:
            continue
        if not result["expected"].strip():
            diagnostics.append(f"{result['check_result_id']}: expected text is blank")
        if not result["observed"].strip():
            diagnostics.append(f"{result['check_result_id']}: observed text is blank")
        if (
            result["expected"].strip()
            and result["observed"].strip()
            and result["expected"].strip() == result["observed"].strip()
        ):
            diagnostics.append(
                f"{result['check_result_id']}: expected and observed text are identical"
            )
        if not result["evidence_refs"]:
            diagnostics.append(f"{result['check_result_id']}: no direct evidence reference")
    return diagnostics


def result_summary(item: dict[str, Any]) -> str:
    links = [f"corrects `{item['corrects_result_id']}`"] if item["corrects_result_id"] else []
    links.extend(f"caused by `{cause}`" for cause in item["caused_by_result_ids"])
    detail = (
        f"; integrity: {item['evidence_integrity']}"
        f"; review: {', '.join(item['review_reasons']) or 'none'}"
    )
    if item["deviation"] is not None:
        detail += f"; deviation: {json.dumps(item['deviation'], sort_keys=True)}"
    return (
        f"- `{item['check_result_id']}`: `{item['check_id']}` / `{item['step_id']}` "
        f"{item['status']}; evidence: {', '.join(item['evidence_refs']) or 'none'}"
        + (f"; {', '.join(links)}" if links else "")
        + detail
    )


def observation_summary(item: dict[str, Any]) -> str:
    return (
        f"- `{item['observation_id']}`: {json.dumps(item['text'])}; evidence: "
        f"{', '.join(item['evidence_refs']) or 'none'}; "
        f"results: {', '.join(item['check_result_ids']) or 'none'}; "
        f"causes: {', '.join(item['caused_by_result_ids']) or 'none'}; "
        f"corrects: {item['corrects_observation_id'] or 'none'}"
    )


def render_run_summary(
    run: dict[str, Any],
    state: dict[str, Any],
    results: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    diagnostics: list[str],
) -> str:
    counts = {
        status: sum(item["status"] == status for item in results)
        for status in ("pass", "fail", "blocked", "unavailable")
    }
    lines = [
        f"# Scenario Run {run['run_id']}",
        "",
        f"Scenario: `{run['scenario_id']}`; Configured Scenario: `{run['configured_scenario_id']}`",
        f"Product revision: `{run['product_revision']}`; suite revision: `{run['suite_revision']}`",
        f"Execution: `{state['execution_status']}`; cleanup: `{state['cleanup_status']}`",
        "",
        "## Selected Checks",
        "",
        *[f"- `{check}`" for check in sorted(run["selected_check_ids"])],
        "",
        "## Check Results",
        "",
        f"Attempts: {len(results)}; "
        + ", ".join(f"{status}: {count}" for status, count in counts.items()),
        "",
    ]
    lines.extend(result_summary(item) for item in results)
    projection = project_results(results, f"Scenario Run {run['run_id']}")
    lines.extend(["", "## Effective Check Result projection", ""])
    for group in projection.groups:
        heads = ", ".join(f"`{item['check_result_id']}`" for item in group.surviving_heads)
        status = effective_status(group.surviving_heads)
        lines.append(
            f"- Root `{group.root_id}`. Surviving heads: {heads}. Effective status: `{status}`."
        )
    lines.extend(["", "## Observations", ""])
    lines.extend(observation_summary(item) for item in observations)
    if not observations:
        lines.append("None.")
    lines.extend(["", "## Recording review", ""])
    lines.extend(f"- {message}" for message in diagnostics)
    if not diagnostics:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def build_run_manifest(
    run_root: Path,
    candidate: RunRecords,
    evidence: dict[str, Any],
    generated: dict[str, str],
    scenario_digest: str,
) -> dict[str, Any]:
    files = {
        name: (
            hashlib.sha256(generated[name].encode("utf-8")).hexdigest()
            if name in generated
            else sha256_file(run_root / name)
        )
        for name in sorted(REQUIRED_RUN_FILES)
    }
    return {
        "run_record_format_version": RUN_FORMAT_VERSION,
        "record_type": "run-manifest",
        "run_id": candidate.run["run_id"],
        "sealed_at": utc_now_rfc3339(),
        "execution_status": candidate.state["execution_status"],
        "cleanup_status": candidate.state["cleanup_status"],
        "record_counts": {
            "check_results": len(candidate.results),
            "observations": len(candidate.observations),
            "evidence": len(evidence["entries"]),
        },
        "files": files,
        "evidence_manifest_sha256": files["evidence-manifest.json"],
        "scenario_snapshot_sha256": scenario_digest,
    }


def resource_identity(resource: dict[str, Any], index: int) -> str:
    """Give legacy ledger rows a stable display identity."""
    for key in ("identity", "resource_id", "name", "path"):
        if isinstance(resource.get(key), str) and resource[key]:
            return resource[key]
    return f"resource #{index}"


def cleanup_findings(cleanup: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for index, resource in enumerate(cleanup["resources"], 1):
        disposition = resource.get("actual_disposition")
        authority = resource.get("active_authority_possible")
        shutdown_refs = resource.get("safe_shutdown_evidence_refs", [])
        if disposition == "release-failed":
            kind = "failed"
        elif disposition is None or authority is None:
            kind = "unverified"
        else:
            continue
        findings.append(
            {
                "identity": resource_identity(resource, index),
                "disposition": disposition,
                "active_authority_possible": authority,
                "safe_shutdown_evidence_refs": shutdown_refs,
                "reason": resource.get("cleanup_reason"),
                "kind": kind,
                "unresolved_authority": authority is not False and not shutdown_refs,
            }
        )
    return findings


def reconciled_cleanup_status(status: str, findings: list[dict[str, Any]]) -> str:
    if status == "failed" or any(item["kind"] == "failed" for item in findings):
        return "failed"
    if status == "unverified" or findings:
        return "unverified"
    return "complete"


def reconcile_cleanup_state(state: dict[str, Any], cleanup: dict[str, Any]) -> dict[str, Any]:
    status = reconciled_cleanup_status(state["cleanup_status"], cleanup_findings(cleanup))
    if status == state["cleanup_status"]:
        return state
    updated = {**state, "cleanup_status": status, "last_updated_at": utc_now_rfc3339()}
    if updated["status"] == "complete":
        updated["completed_at"] = updated["last_updated_at"]
    return updated


def append_cleanup_summary(summary: str, state: dict[str, Any], cleanup: dict[str, Any]) -> str:
    findings = cleanup_findings(cleanup)
    if not findings:
        return summary
    lines = ["", "## Cleanup reconciliation", "", f"Cleanup status: `{state['cleanup_status']}`."]
    for item in findings:
        lines.append(
            f"- `{item['identity']}`: disposition `{item['disposition'] or 'unknown'}`; "
            f"active authority possible `{item['active_authority_possible']}`; "
            f"reason: {item['reason'] or 'disposition or safety classification unverified'}."
        )
    return summary.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def publish_seal(
    run_root: Path, generated: dict[str, str], state: dict[str, Any], manifest: dict[str, Any]
) -> None:
    for name, content in generated.items():
        if name == "operator-state.json":
            atomic_json(run_root / name, state)
        else:
            atomic_text(run_root / name, content)
    if any(sha256_file(run_root / name) != digest for name, digest in manifest["files"].items()):
        raise TriageError(f"{run_root}: final records changed during sealing")
    atomic_json(run_root / "run-manifest.json", manifest)


def seal_run(run_root: Path, suite_root: Path | None = None) -> RunRecords:
    if suite_root is None:
        raise TriageError(f"{run_root}: seal-run requires --suite-root")
    with exclusive_file_lock(run_root / ".run-records.lock"):
        return _seal_run_unlocked(run_root, suite_root)


def _seal_run_unlocked(run_root: Path, suite_root: Path) -> RunRecords:
    """Validate and seal one run while its mutable-record lock is held."""
    manifest_path = run_root / "run-manifest.json"
    if manifest_path.exists():
        return validate_run(run_root)
    for name in REQUIRED_RUN_FILES - {"evidence-manifest.json", "run-summary.md"}:
        if not (run_root / name).is_file():
            raise TriageError(f"{run_root / name}: missing final run record")
    run = read_json(run_root / "run.json")
    results = read_jsonl(run_root / "check-results.jsonl")
    observations = read_jsonl(run_root / "observations.jsonl")
    configured, scenario_files = load_suite(suite_root)
    snapshot = {"configured_scenarios": configured, "scenario_files": scenario_files}
    evidence = build_evidence_manifest(run_root, run["run_id"], results, observations)
    values = validate_run_contents(run_root, evidence)
    run, state, cleanup, evidence, results, observations, evidence_paths = values
    validate_terminal_state(state, run_root)
    validate_run_against_suite_data(run_root, run, results, snapshot)
    validate_borrowed_preservation_claims(run, results, run_root)
    original_state = state
    state = reconcile_cleanup_state(state, cleanup)
    validate_definition(state, "run-record.schema.json", "operatorState", "operator state")
    diagnostics = recording_diagnostics(results)
    summary = render_run_summary(run, state, results, observations, diagnostics)
    summary = append_cleanup_summary(summary, state, cleanup)
    candidate = RunRecords(
        run_root, "", run, state, {}, tuple(results), tuple(observations), evidence_paths, cleanup
    )
    candidates_for_run(candidate)
    generated = {
        "evidence-manifest.json": json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        "run-summary.md": summary,
    }
    if state != original_state:
        generated["operator-state.json"] = json.dumps(state, indent=2, sort_keys=True) + "\n"
    manifest = build_run_manifest(
        run_root,
        candidate,
        evidence,
        generated,
        scenario_snapshot_digest(suite_root, scenario_files),
    )
    validate_definition(manifest, "run-record.schema.json", "runManifest", "run manifest")
    publish_seal(run_root, generated, state, manifest)
    return validate_run(run_root)


def correction_roots(
    records: tuple[dict[str, Any], ...], id_field: str, link_field: str
) -> dict[str, str]:
    roots: dict[str, str] = {}
    for record in records:
        record_id = record[id_field]
        corrected = record[link_field]
        roots[record_id] = roots[corrected] if corrected is not None else record_id
    return roots


def result_candidates(run: RunRecords) -> list[dict[str, Any]]:
    projection = project_results(run.results, str(run.root))
    referenced_causes = {
        cause for result in run.results for cause in result["caused_by_result_ids"]
    }
    candidates = []
    for group in projection.groups:
        chain = list(group.members)
        review = any(
            item["status"] in NONPASS
            or item["review_reasons"]
            or item["deviation"] is not None
            or item["evidence_integrity"] != "valid"
            or item["check_result_id"] in referenced_causes
            for item in chain
        )
        if review:
            candidates.append(
                build_result_candidate(
                    run.run_id,
                    group.root_id,
                    chain,
                    list(group.surviving_heads),
                    projection.roots,
                )
            )
    return candidates


def build_result_candidate(
    run_id: str,
    root_id: str,
    chain: list[dict[str, Any]],
    heads: list[dict[str, Any]],
    roots: dict[str, str],
) -> dict[str, Any]:
    causes = {
        f"{run_id}:check:{roots[cause]}"
        for item in chain
        for cause in item["caused_by_result_ids"]
    }
    return {
        "candidate_id": f"{run_id}:check:{root_id}",
        "run_id": run_id,
        "kind": "check-result-chain",
        "source_ids": [item["check_result_id"] for item in chain],
        "surviving_head_ids": [item["check_result_id"] for item in heads],
        "surviving_heads": [head_presentation(item) for item in heads],
        "check_id": chain[0]["check_id"],
        "effective_status": effective_status(heads),
        "historical_statuses": [item["status"] for item in chain],
        "expected": head_presentations(heads, "expected"),
        "text": head_presentations(heads, "observed"),
        "evidence_refs": sorted(
            {reference for item in chain for reference in check_result_evidence_refs(item)}
        ),
        "caused_by_candidate_ids": sorted(causes),
        "review_reasons": sorted({reason for item in chain for reason in item["review_reasons"]}),
    }


def head_presentations(heads: list[dict[str, Any]], field: str) -> str:
    """Keep legacy single-head prose while labeling every branch head."""
    if len(heads) == 1:
        return str(heads[0][field])
    return "\n".join(f"[{item['check_result_id']}] {item[field]}" for item in heads)


def head_presentation(item: dict[str, Any]) -> dict[str, Any]:
    """Retain one effective head's reviewable claims without flattening ownership."""
    return {
        "check_result_id": item["check_result_id"],
        "status": item["status"],
        "expected": item["expected"],
        "observed": item["observed"],
        "caused_by_result_ids": list(item["caused_by_result_ids"]),
        "review_reasons": list(item["review_reasons"]),
        "evidence_refs": sorted(set(check_result_evidence_refs(item))),
    }


def observation_candidates(run: RunRecords) -> list[dict[str, Any]]:
    roots = correction_roots(run.observations, "observation_id", "corrects_observation_id")
    chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in run.observations:
        chains[roots[observation["observation_id"]]].append(observation)
    candidates = []
    for root_id, chain in chains.items():
        effective = chain[-1]
        candidates.append(
            {
                "candidate_id": f"{run.run_id}:observation:{root_id}",
                "run_id": run.run_id,
                "kind": "observation-chain",
                "source_ids": [item["observation_id"] for item in chain],
                "effective_status": "observation",
                "historical_statuses": ["observation"] * len(chain),
                "text": effective["text"],
                "evidence_refs": sorted({ref for item in chain for ref in item["evidence_refs"]}),
                "caused_by_candidate_ids": sorted(
                    {
                        f"{run.run_id}:check:{cause}"
                        for item in chain
                        for cause in item["caused_by_result_ids"]
                    }
                ),
                "review_reasons": sorted(
                    {reason for item in chain for reason in item["review_reasons"]}
                ),
            }
        )
    return candidates


def candidates_for_run(run: RunRecords) -> list[dict[str, Any]]:
    candidates = [*result_candidates(run), *observation_candidates(run)]
    ids = {candidate["candidate_id"] for candidate in candidates}
    result_roots = project_results(run.results, str(run.root)).roots
    for candidate in candidates:
        normalized = []
        for cause in candidate["caused_by_candidate_ids"]:
            prefix, _, raw_id = cause.rpartition(":check:")
            root = result_roots.get(raw_id, raw_id)
            normalized_id = f"{prefix}:check:{root}"
            if normalized_id in ids:
                normalized.append(normalized_id)
        candidate["caused_by_candidate_ids"] = sorted(set(normalized))
    return sorted(candidates, key=lambda item: item["candidate_id"])


def connected_cases(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    neighbours: dict[str, set[str]] = {candidate_id: set() for candidate_id in by_id}
    for candidate in candidates:
        for cause in candidate["caused_by_candidate_ids"]:
            if cause in by_id:
                neighbours[candidate["candidate_id"]].add(cause)
                neighbours[cause].add(candidate["candidate_id"])
    return build_components(by_id, neighbours)


def build_components(
    by_id: dict[str, dict[str, Any]], neighbours: dict[str, set[str]]
) -> list[dict[str, Any]]:
    remaining = set(by_id)
    cases = []
    while remaining:
        pending = [min(remaining)]
        component = set()
        while pending:
            candidate_id = pending.pop()
            if candidate_id in component:
                continue
            component.add(candidate_id)
            pending.extend(neighbours[candidate_id] - component)
        remaining -= component
        roots = [
            item
            for item in component
            if not set(by_id[item]["caused_by_candidate_ids"]).intersection(component)
        ]
        case_id = "case-" + stable_digest(sorted(component))[:16]
        cases.append(
            {
                "case_id": case_id,
                "candidate_ids": sorted(component),
                "suspected_root_ids": sorted(roots),
            }
        )
    return sorted(cases, key=lambda item: item["case_id"])


def load_suite(suite_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    configured = []
    scenario_files = []
    for path in sorted((suite_root / "scenarios").glob("*/scenario.yaml")):
        try:
            content = path.read_bytes()
            scenario = yaml.safe_load(content)
        except (OSError, yaml.YAMLError) as error:
            raise TriageError(f"{path}: {error}") from error
        if as_dict(scenario) is None or as_str(scenario.get("scenario_id")) is None:
            raise TriageError(f"{path}: invalid Scenario")
        if not isinstance(scenario.get("steps"), list) or not scenario["steps"]:
            raise TriageError(f"{path}: Scenario has no Steps")
        validate_scenario_binding_shape(scenario, path)
        scenario_files.append(
            {
                "scenario_id": scenario["scenario_id"],
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        check_sets = {
            check_set["id"]: set(check_set.get("checks", []))
            for check_set in scenario.get("check_sets", [])
            if isinstance(check_set, dict) and isinstance(check_set.get("id"), str)
        }
        for item in scenario.get("configured_scenarios", []):
            if not isinstance(item, dict) or type(item.get("required")) is not bool:
                raise TriageError(f"{path}: invalid Configured Scenario")
            selected_sets = item.get("check_sets")
            expected_checks = None
            if selected_sets is not None:
                unknown_sets = set(selected_sets) - check_sets.keys()
                if unknown_sets:
                    raise TriageError(f"{path}: unknown Check sets {sorted(unknown_sets)}")
                expected_checks = set().union(*(check_sets[set_id] for set_id in selected_sets))
                exclusions = {
                    exclusion["check"]
                    for exclusion in item.get("exclusions", [])
                    if isinstance(exclusion, dict) and isinstance(exclusion.get("check"), str)
                }
                expected_checks -= exclusions
            configured.append(
                {
                    "scenario_id": scenario["scenario_id"],
                    "configured_scenario_id": item["id"],
                    "required": item["required"],
                    "parameters": item.get("parameters", {}),
                    "expected_check_ids": (
                        sorted(expected_checks) if expected_checks is not None else None
                    ),
                    "exclusions": item.get("exclusions", []),
                    "pre_run_requirements": [
                        *scenario.get("shared_pre_run_requirements", []),
                        *item.get("pre_run_requirements", []),
                    ],
                }
            )
    identities = [item["configured_scenario_id"] for item in configured]
    if not configured or len(identities) != len(set(identities)):
        raise TriageError(f"{suite_root}: missing or duplicate Configured Scenarios")
    scenario_ids = [item["scenario_id"] for item in scenario_files]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise TriageError(f"{suite_root}: duplicate Scenario IDs")
    return configured, scenario_files


def validate_scenario_binding_shape(scenario: dict[str, Any], path: Path) -> None:
    """Validate the fields consumed by run/suite binding, including uniqueness."""
    steps = scenario["steps"]
    if any(
        as_dict(step) is None
        or as_str(step.get("id")) is None
        or not isinstance(step.get("checks"), list)
        for step in steps
    ):
        raise TriageError(f"{path}: invalid Scenario Steps")
    step_ids = [step["id"] for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise TriageError(f"{path}: duplicate Step IDs")
    checks = [check for step in steps for check in step["checks"]]
    if any(as_dict(check) is None or as_str(check.get("id")) is None for check in checks):
        raise TriageError(f"{path}: invalid Scenario Checks")
    check_ids = [check["id"] for check in checks]
    if len(check_ids) != len(set(check_ids)):
        raise TriageError(f"{path}: duplicate Check IDs")
    for step in steps:
        requires = step.get("requires", [])
        if not is_str_list(requires) or any(item not in step_ids for item in requires):
            raise TriageError(f"{path}: {step['id']}: invalid required Steps")
    validate_configured_binding_shape(scenario, path)
    validate_check_sets_binding_shape(scenario, path, check_ids)


def validate_configured_binding_shape(scenario: dict[str, Any], path: Path) -> None:
    configured = scenario.get("configured_scenarios")
    if not isinstance(configured, list) or any(
        as_dict(item) is None or as_str(item.get("id")) is None for item in configured
    ):
        raise TriageError(f"{path}: invalid Configured Scenarios")
    for item in configured:
        selected_sets = item.get("check_sets", [])
        exclusions = item.get("exclusions", [])
        if (
            not is_str_list(selected_sets)
            or not isinstance(exclusions, list)
            or any(
                as_dict(value) is None or as_str(value.get("check")) is None
                for value in exclusions
            )
            or not isinstance(item.get("parameters", {}), dict)
            or not isinstance(item.get("pre_run_requirements", []), list)
        ):
            raise TriageError(f"{path}: {item['id']}: invalid Configured Scenario binding")
    if not isinstance(scenario.get("shared_pre_run_requirements", []), list):
        raise TriageError(f"{path}: invalid shared pre-run requirements")


def validate_check_sets_binding_shape(
    scenario: dict[str, Any], path: Path, check_ids: list[str]
) -> None:
    check_sets = scenario.get("check_sets", [])
    if not isinstance(check_sets, list) or any(
        as_dict(item) is None
        or as_str(item.get("id")) is None
        or not is_str_list(item.get("checks"))
        or any(check not in check_ids for check in item["checks"])
        for item in check_sets
    ):
        raise TriageError(f"{path}: invalid Check sets")


def scenario_snapshot_digest(suite_root: Path, files: list[dict[str, str]]) -> str:
    """Digest sorted relative Scenario paths and their content hashes."""
    return stable_digest(
        [
            {
                "path": Path(item["path"]).relative_to(suite_root.resolve()).as_posix(),
                "sha256": item["sha256"],
            }
            for item in files
        ]
    )


def validate_suite_snapshot(session: dict[str, Any]) -> None:
    for entry in session["scenario_files"]:
        path = Path(entry["path"])
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            raise TriageError(f"{path}: frozen Scenario definition changed")


def bound_scenario(session: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    matches = [item for item in session["scenario_files"] if item["scenario_id"] == scenario_id]
    if len(matches) != 1:
        raise TriageError(f"{scenario_id}: Scenario is absent from frozen suite")
    path = Path(matches[0]["path"])
    try:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != matches[0]["sha256"]:
            raise TriageError(f"{path}: frozen Scenario definition changed")
        scenario = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as error:
        raise TriageError(f"{path}: {error}") from error
    if not isinstance(scenario, dict):
        raise TriageError(f"{path}: invalid Scenario")
    return scenario


def validate_run_against_suite_data(
    root: Path,
    run: dict[str, Any],
    results: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    session: dict[str, Any],
) -> None:
    scenario = bound_scenario(session, run["scenario_id"])
    matches = [
        item
        for item in session["configured_scenarios"]
        if item["scenario_id"] == run["scenario_id"]
        and item["configured_scenario_id"] == run["configured_scenario_id"]
    ]
    if len(matches) != 1:
        raise TriageError(f"{root}: Configured Scenario is absent from bound Scenario")
    validate_run_suite(run, results, scenario, matches[0], str(root))


def validate_run_against_suite(run: RunRecords, session: dict[str, Any]) -> None:
    if (
        run.run["suite_revision"] == session["target_suite_revision"]
        and "scenario_snapshot_sha256" in run.manifest
    ):
        digest = scenario_snapshot_digest(Path(session["suite_root"]), session["scenario_files"])
        if run.manifest["scenario_snapshot_sha256"] != digest:
            raise TriageError(
                f"{run.root}: Scenario snapshot differs despite equal suite revisions"
            )
    validate_run_against_suite_data(run.root, run.run, run.results, session)


def helper_revision() -> str:
    root = Path(__file__).parent
    names = [
        "triage.py",
        "result_projection.py",
        "run_suite.py",
        "triage_cli.py",
        "triage_lock.py",
        "triage-record.schema.json",
        "run-record.schema.json",
    ]
    return stable_digest({name: sha256_file(root / name) for name in names})


def default_policy_revision() -> str:
    skill = Path(__file__).with_name("booley-qa-triage") / "SKILL.md"
    if not skill.is_file():
        raise TriageError(f"{skill}: missing triage policy")
    return sha256_file(skill)


def init_session(
    triage_root: Path,
    suite_root: Path,
    product_revision: str,
    suite_revision: str,
) -> dict[str, Any]:
    triage_root.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_file_lock(triage_root / ".triage.lock"):
            return _init_session_unlocked(
                triage_root, suite_root, product_revision, suite_revision
            )
    except TimeoutError as error:
        raise TriageError(str(error)) from error


def _init_session_unlocked(
    triage_root: Path,
    suite_root: Path,
    product_revision: str,
    suite_revision: str,
) -> dict[str, Any]:
    session_path = triage_root / "session.json"
    if session_path.exists():
        session = read_session(triage_root)
        expected = (product_revision, suite_revision, default_policy_revision())
        actual = (
            session["target_product_revision"],
            session["target_suite_revision"],
            session["qualification_policy_revision"],
        )
        if actual != expected:
            raise TriageError(f"{session_path}: existing session target differs")
        return session
    contents = [path for path in triage_root.iterdir() if path.name != ".triage.lock"]
    if contents:
        raise TriageError(f"{triage_root}: new triage root must be empty")
    configured_scenarios, scenario_files = load_suite(suite_root)
    session = {
        "triage_format_version": TRIAGE_FORMAT_VERSION,
        "record_type": "triage-session",
        "session_id": str(uuid.uuid4()),
        "target_product_revision": product_revision,
        "target_suite_revision": suite_revision,
        "qualification_policy_revision": default_policy_revision(),
        "helper_revision": helper_revision(),
        "suite_root": str(suite_root.resolve()),
        "scenario_files": scenario_files,
        "created_at": utc_now_rfc3339(),
        "configured_scenarios": configured_scenarios,
    }
    validate_definition(session, "triage-record.schema.json", "session", "triage session")
    atomic_json(session_path, session)
    atomic_jsonl(triage_root / "triage-events.jsonl", [])
    _render_session_unlocked(triage_root)
    return session


def read_session(triage_root: Path) -> dict[str, Any]:
    session = read_json(triage_root / "session.json")
    validate_definition(
        session, "triage-record.schema.json", "session", str(triage_root / "session.json")
    )
    if session["helper_revision"] != helper_revision():
        raise TriageError(f"{triage_root}: helper revision differs from frozen session")
    if session["qualification_policy_revision"] != default_policy_revision():
        raise TriageError(f"{triage_root}: qualification policy differs from frozen session")
    validate_suite_snapshot(session)
    return session


def read_events(triage_root: Path, session: dict[str, Any]) -> list[dict[str, Any]]:
    events = read_jsonl(triage_root / "triage-events.jsonl")
    validate_unique(events, "event_id", str(triage_root / "triage-events.jsonl"))
    validate_unique(events, "idempotency_key", str(triage_root / "triage-events.jsonl"))
    for index, event in enumerate(events, 1):
        validate_definition(event, "triage-record.schema.json", "event", f"event {index}")
        if event["session_id"] != session["session_id"] or event["sequence"] != index:
            raise TriageError(f"{triage_root}: event sequence or session mismatch at {index}")
    return events


def make_event(
    session: dict[str, Any], sequence: int, key: str, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{session['session_id']}:{key}"))
    return {
        "triage_format_version": TRIAGE_FORMAT_VERSION,
        "record_type": "triage-event",
        "session_id": session["session_id"],
        "sequence": sequence,
        "event_id": event_id,
        "idempotency_key": key,
        "event_type": event_type,
        "timestamp": utc_now_rfc3339(),
        "payload": payload,
    }


def append_event(
    triage_root: Path, event_type: str, payload: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    return append_events(triage_root, [(event_type, payload, idempotency_key)])[0]


def append_events(
    triage_root: Path, additions: list[tuple[str, dict[str, Any], str]]
) -> list[dict[str, Any]]:
    try:
        with exclusive_file_lock(triage_root / ".triage.lock"):
            appended = _append_events_unlocked(triage_root, additions)
    except TimeoutError as error:
        raise TriageError(str(error)) from error
    if additions:
        render_session(triage_root)
    return appended


def _append_events_unlocked(
    triage_root: Path, additions: list[tuple[str, dict[str, Any], str]]
) -> list[dict[str, Any]]:
    session = read_session(triage_root)
    events = read_events(triage_root, session)
    by_key = {event["idempotency_key"]: event for event in events}
    appended = []
    changed = False
    for event_type, payload, key in additions:
        existing = by_key.get(key)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise TriageError(f"{triage_root}: idempotency key reused for different event")
            appended.append(existing)
            continue
        event = make_event(session, len(events) + 1, key, event_type, payload)
        validate_definition(event, "triage-record.schema.json", "event", "new event")
        events.append(event)
        by_key[key] = event
        appended.append(event)
        changed = True
    if changed:
        projected_runs = {
            run_id: load_admitted_run(payload) for run_id, payload in admitted_runs(events).items()
        }
        projected_candidates = all_candidates(projected_runs)
        projected_cases = case_projection(events)
        validate_case_membership(projected_cases, projected_candidates)
        run_roles(events, projected_runs)
        atomic_jsonl(triage_root / "triage-events.jsonl", events)
    return appended


def admitted_runs(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    runs = {}
    for event in events:
        if event["event_type"] != "run-admitted":
            continue
        payload = event["payload"]
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or run_id in runs:
            raise TriageError("triage events: duplicate or invalid admitted run")
        runs[run_id] = payload
    return runs


def load_admitted_run(payload: dict[str, Any]) -> RunRecords:
    run = validate_run(Path(payload["run_root"]))
    if run.manifest_hash != payload["manifest_sha256"]:
        raise TriageError(f"{run.root}: completion manifest changed after admission")
    return run


def case_projection(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type == "case-proposed":
            add_case(cases, payload, event["event_id"])
        elif event_type == "case-merged":
            merge_cases(cases, payload, event["event_id"])
        elif event_type == "case-split":
            split_case(cases, payload, event["event_id"])
        elif event_type == "case-reopened":
            active_case(cases, payload["case_id"])["disposition"] = None
        elif event_type == "case-dispositioned":
            case = active_case(cases, payload["case_id"])
            if case["disposition"] is not None:
                raise TriageError(f"{payload['case_id']}: case already dispositioned")
            case["disposition"] = {
                **payload,
                "event_id": event["event_id"],
                "idempotency_key": event["idempotency_key"],
            }
    return cases


def add_case(cases: dict[str, dict[str, Any]], payload: dict[str, Any], event_id: str) -> None:
    case_id = payload.get("case_id")
    candidate_ids = payload.get("candidate_ids")
    if not isinstance(case_id, str) or case_id in cases:
        raise TriageError("case proposal: duplicate or invalid case_id")
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise TriageError(f"{case_id}: case requires candidates")
    cases[case_id] = {
        **payload,
        "active": True,
        "created_by_event_id": event_id,
        "disposition": None,
    }


def active_case(cases: dict[str, dict[str, Any]], case_id: str) -> dict[str, Any]:
    case = cases.get(case_id)
    if case is None or not case["active"]:
        raise TriageError(f"{case_id}: case is not active")
    return case


def merge_cases(cases: dict[str, dict[str, Any]], payload: dict[str, Any], event_id: str) -> None:
    sources = [active_case(cases, case_id) for case_id in payload.get("source_case_ids", [])]
    if len(sources) < 2 or any(case["disposition"] is not None for case in sources):
        raise TriageError("case merge requires at least two undispositioned active cases")
    for case in sources:
        case["active"] = False
    merged = {
        "case_id": payload["case_id"],
        "candidate_ids": sorted({item for case in sources for item in case["candidate_ids"]}),
        "suspected_root_ids": payload.get("suspected_root_ids", []),
        "reason": payload.get("reason", ""),
    }
    add_case(cases, merged, event_id)


def split_case(cases: dict[str, dict[str, Any]], payload: dict[str, Any], event_id: str) -> None:
    source = active_case(cases, payload["source_case_id"])
    if source["disposition"] is not None:
        raise TriageError("case split requires an undispositioned case")
    replacements = payload.get("replacements", [])
    flattened = [item for replacement in replacements for item in replacement["candidate_ids"]]
    if sorted(flattened) != sorted(source["candidate_ids"]) or len(flattened) != len(
        set(flattened)
    ):
        raise TriageError("case split must partition every source candidate exactly once")
    source["active"] = False
    for replacement in replacements:
        add_case(cases, {**replacement, "reason": payload.get("reason", "")}, event_id)


def all_candidates(runs: dict[str, RunRecords]) -> dict[str, dict[str, Any]]:
    candidates = {}
    for run in runs.values():
        for candidate in candidates_for_run(run):
            if candidate["candidate_id"] in candidates:
                raise TriageError(f"duplicate candidate {candidate['candidate_id']}")
            candidates[candidate["candidate_id"]] = candidate
    return candidates


def similarity_hints(candidates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_text: dict[str, list[str]] = defaultdict(list)
    for candidate_id, candidate in candidates.items():
        normalized = " ".join(candidate["text"].casefold().split())
        if normalized:
            by_text[normalized].append(candidate_id)
    hints = []
    for normalized, candidate_ids in sorted(by_text.items()):
        if len(candidate_ids) < 2:
            continue
        hints.append(
            {
                "hint_id": "similarity-" + stable_digest(candidate_ids)[:16],
                "candidate_ids": sorted(candidate_ids),
                "basis": "identical normalized observation text; not causal evidence",
                "normalized_text": normalized,
            }
        )
    return hints


def validate_case_membership(
    cases: dict[str, dict[str, Any]], candidates: dict[str, dict[str, Any]]
) -> None:
    memberships: dict[str, int] = defaultdict(int)
    for case in cases.values():
        if not case["active"]:
            continue
        for candidate_id in case["candidate_ids"]:
            if candidate_id not in candidates:
                raise TriageError(f"{case['case_id']}: unknown candidate {candidate_id}")
            memberships[candidate_id] += 1
    invalid = {
        candidate_id: memberships[candidate_id]
        for candidate_id in candidates
        if memberships[candidate_id] != 1
    }
    if invalid:
        raise TriageError(f"active case membership is not exhaustive and unique: {invalid}")


def active_cases(cases: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (case for case in cases.values() if case["active"]), key=lambda item: item["case_id"]
    )


def has_nonpass(case: dict[str, Any], candidates: dict[str, dict[str, Any]]) -> bool:
    return any(
        any(status in NONPASS for status in candidates[item]["historical_statuses"])
        for item in case["candidate_ids"]
    )


def validate_disposition(
    disposition: str,
    details: dict[str, Any],
    case: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    cases: dict[str, dict[str, Any]],
    runs: dict[str, RunRecords],
) -> None:
    if disposition not in DISPOSITIONS:
        raise TriageError(f"unknown disposition {disposition}")
    if disposition in FAIL_DISPOSITIONS:
        validate_finding_details(disposition, details, cases)
    elif disposition in {"qa-invalidating-defect", "qa-improvement"}:
        validate_qa_change_details(disposition, details)
    elif disposition == "recording-error":
        validate_recording_error(details, case, candidates, runs)
    elif not isinstance(details.get("reason"), str) or not details["reason"]:
        raise TriageError(f"{disposition}: nonempty reason required")
    validate_disposition_evidence(disposition, details, case, candidates, runs)
    neutral = disposition in NEUTRAL_DISPOSITIONS or (
        disposition in {"product-defect", "documentation-defect"}
        and details.get("qualification_scope") == "out-of-scope"
    )
    if neutral and has_nonpass(case, candidates):
        raise TriageError(f"{disposition}: cannot neutralize a trustworthy non-pass result")


def validate_disposition_evidence(
    disposition: str,
    details: dict[str, Any],
    case: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    runs: dict[str, RunRecords],
) -> None:
    refs = details.get("evidence_refs")
    if refs is None:
        return
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(reference, str) or not reference for reference in refs)
    ):
        raise TriageError(f"{disposition}: nonempty evidence_refs required")
    linked_runs = {candidates[item]["run_id"] for item in case["candidate_ids"]}
    allowed = {path for run_id in linked_runs for path in runs[run_id].evidence_paths}
    if unknown := set(refs) - allowed:
        raise TriageError(f"{disposition}: evidence not sealed by a linked run: {sorted(unknown)}")


def validate_finding_details(
    disposition: str, details: dict[str, Any], cases: dict[str, dict[str, Any]]
) -> None:
    if disposition == "duplicate-finding":
        duplicate = details.get("duplicate_of_case_id")
        external = details.get("existing_finding_id")
        if bool(duplicate) == bool(external):
            raise TriageError("duplicate-finding requires exactly one existing source")
        if duplicate:
            source = active_case(cases, duplicate)
            source_disposition = source["disposition"]
            if source_disposition is None or source_disposition["disposition"] not in (
                FAIL_DISPOSITIONS
            ):
                raise TriageError("duplicate-finding: source must be a disposed Finding case")
        if external:
            required = {"qualification_scope", "owner", "scope_reason"}
            if not required <= details.keys():
                raise TriageError("external duplicate requires owner and scope")
            validate_finding_scope(disposition, details)
        return
    required = {
        "summary",
        "original_text",
        "owner",
        "qualification_scope",
        "scope_reason",
        "evidence_refs",
    }
    if not required <= details.keys():
        raise TriageError(
            f"{disposition}: missing Finding fields {sorted(required - details.keys())}"
        )
    for field in ("summary", "original_text", "scope_reason"):
        if not isinstance(details[field], str) or not details[field]:
            raise TriageError(f"{disposition}: nonempty {field} required")
    validate_finding_scope(disposition, details)


def validate_finding_scope(disposition: str, details: dict[str, Any]) -> None:
    if details["owner"] not in {"project", "Booley", "documentation", "unresolved"}:
        raise TriageError(f"{disposition}: invalid owner")
    if details["qualification_scope"] not in {"in-scope", "out-of-scope", "unresolved"}:
        raise TriageError(f"{disposition}: invalid qualification scope")
    if not isinstance(details["scope_reason"], str) or not details["scope_reason"]:
        raise TriageError(f"{disposition}: nonempty scope_reason required")


def validate_qa_change_details(disposition: str, details: dict[str, Any]) -> None:
    required = {"summary", "change", "rationale", "evidence_refs", "affected_area"}
    if not required <= details.keys():
        raise TriageError(
            f"{disposition}: missing QA Change fields {sorted(required - details.keys())}"
        )
    for field in ("summary", "change", "rationale", "affected_area"):
        if not isinstance(details[field], str) or not details[field]:
            raise TriageError(f"{disposition}: nonempty {field} required")
    expected = disposition == "qa-invalidating-defect"
    if details.get("invalidates_evidence") is not expected:
        raise TriageError(f"{disposition}: invalidates_evidence must be {expected}")


def validate_recording_error(
    details: dict[str, Any],
    case: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    runs: dict[str, RunRecords],
) -> None:
    if details.get("corrected_status") not in {"pass", "blocked", "unavailable"}:
        raise TriageError(
            "recording-error: corrected_status must be pass, blocked, or unavailable"
        )
    evidence_refs = details.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise TriageError("recording-error: sealed evidence_refs required")
    linked_runs = {candidates[item]["run_id"] for item in case["candidate_ids"]}
    allowed = set.intersection(*(set(runs[run_id].evidence_paths) for run_id in linked_runs))
    if unknown := set(evidence_refs) - allowed:
        raise TriageError(f"recording-error: evidence not sealed for every linked run: {unknown}")
    if not isinstance(details.get("reason"), str) or not details["reason"]:
        raise TriageError("recording-error: reason required")


def disposition_effect(disposition: dict[str, Any], cases: dict[str, dict[str, Any]]) -> str:
    kind = disposition["disposition"]
    details = disposition["details"]
    if kind in {"product-defect", "documentation-defect"}:
        return {"in-scope": "failed", "out-of-scope": "neutral", "unresolved": "incomplete"}[
            details["qualification_scope"]
        ]
    if kind == "duplicate-finding":
        source = details.get("duplicate_of_case_id")
        if source:
            linked = cases[source].get("disposition")
            if linked is None:
                raise TriageError(f"{source}: duplicate source lacks disposition")
            return disposition_effect(linked, cases)
        return {"in-scope": "failed", "out-of-scope": "neutral", "unresolved": "incomplete"}[
            details["qualification_scope"]
        ]
    if kind in INCOMPLETE_DISPOSITIONS:
        return "incomplete"
    if kind == "recording-error":
        return "neutral" if details["corrected_status"] == "pass" else "incomplete"
    return "neutral"


def source_trace(case: dict[str, Any], candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    linked = [candidates[candidate_id] for candidate_id in case["candidate_ids"]]
    return {
        "source_case_ids": [case["case_id"]],
        "source_candidate_ids": case["candidate_ids"],
        "source_run_ids": sorted({candidate["run_id"] for candidate in linked}),
        "source_evidence_refs": sorted(
            {reference for candidate in linked for reference in candidate["evidence_refs"]}
        ),
    }


def finding_source_trace(
    source_case: dict[str, Any],
    cases: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    def canonical_case_id(case: dict[str, Any]) -> str:
        current = case
        while (
            current["disposition"] is not None
            and current["disposition"]["disposition"] == "duplicate-finding"
            and current["disposition"]["details"].get("duplicate_of_case_id")
        ):
            current = cases[current["disposition"]["details"]["duplicate_of_case_id"]]
        return current["case_id"]

    linked_cases = [source_case]
    for case in active_cases(cases):
        disposition = case["disposition"]
        if disposition is None or disposition["disposition"] != "duplicate-finding":
            continue
        if canonical_case_id(case) == source_case["case_id"]:
            linked_cases.append(case)
    candidate_ids = sorted(
        {candidate_id for case in linked_cases for candidate_id in case["candidate_ids"]}
    )
    linked_candidates = [candidates[candidate_id] for candidate_id in candidate_ids]
    return {
        "source_case_ids": sorted(case["case_id"] for case in linked_cases),
        "source_candidate_ids": candidate_ids,
        "source_run_ids": sorted({candidate["run_id"] for candidate in linked_candidates}),
        "source_evidence_refs": sorted(
            {
                reference
                for candidate in linked_candidates
                for reference in candidate["evidence_refs"]
            }
        ),
    }


def projected_findings(
    cases: dict[str, dict[str, Any]], candidates: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    findings = []
    for case in active_cases(cases):
        disposition = case["disposition"]
        if disposition is None or disposition["disposition"] not in {
            "product-defect",
            "documentation-defect",
        }:
            continue
        details = disposition["details"]
        findings.append(
            {
                "triage_format_version": TRIAGE_FORMAT_VERSION,
                "record_type": "finding",
                "finding_id": "finding-" + disposition["event_id"][:16],
                "case_id": case["case_id"],
                "kind": disposition["disposition"],
                **details,
                **finding_source_trace(case, cases, candidates),
            }
        )
    return findings


def projected_qa_changes(
    cases: dict[str, dict[str, Any]], candidates: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    changes = []
    for case in active_cases(cases):
        disposition = case["disposition"]
        if disposition is None or disposition["disposition"] not in {
            "qa-invalidating-defect",
            "qa-improvement",
        }:
            continue
        changes.append(
            {
                "triage_format_version": TRIAGE_FORMAT_VERSION,
                "record_type": "qa-change",
                "qa_change_id": "qa-change-" + disposition["event_id"][:16],
                "case_id": case["case_id"],
                "kind": disposition["disposition"],
                **disposition["details"],
                **source_trace(case, candidates),
            }
        )
    return changes


def load_session_projection(
    triage_root: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, RunRecords],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    session = read_session(triage_root)
    events = read_events(triage_root, session)
    admitted = admitted_runs(events)
    runs = {run_id: load_admitted_run(payload) for run_id, payload in admitted.items()}
    candidates = all_candidates(runs)
    cases = case_projection(events)
    validate_case_membership(cases, candidates)
    return session, events, runs, candidates, cases


def input_run_projection(
    admitted: dict[str, dict[str, Any]], runs: dict[str, RunRecords]
) -> dict[str, Any]:
    return {
        "triage_format_version": TRIAGE_FORMAT_VERSION,
        "record_type": "input-runs",
        "runs": [
            {
                **payload,
                "manifest_verified": runs[run_id].manifest_hash == payload["manifest_sha256"],
            }
            for run_id, payload in sorted(admitted.items())
        ],
    }


def progress_summary(
    session: dict[str, Any],
    runs: dict[str, RunRecords],
    candidates: dict[str, dict[str, Any]],
    cases: dict[str, dict[str, Any]],
    hints: list[dict[str, Any]],
    qualification: dict[str, Any] | None,
) -> str:
    active = active_cases(cases)
    resolved = sum(case["disposition"] is not None for case in active)
    lines = summary_header(session, runs, candidates, active, hints, resolved)
    lines.extend(summary_cleanup(runs))
    lines.extend(summary_hints(hints))
    lines.extend(summary_cases(active, candidates))
    lines.extend(summary_outputs(cases, candidates, qualification))
    return "\n".join(lines) + "\n"


def summary_cleanup(runs: dict[str, RunRecords]) -> list[str]:
    lines = ["## Cleanup reporting", ""]
    for run_id, run in sorted(runs.items()):
        lines.append(f"- `{run_id}`: `{run.manifest['cleanup_status']}`")
        for item in cleanup_findings(run.cleanup):
            safety = item["active_authority_possible"]
            unresolved = " (unresolved active authority)" if item["unresolved_authority"] else ""
            lines.append(
                f"  - `{item['identity']}`: `{item['disposition'] or 'unknown'}`; "
                f"active authority possible: `{safety}`{unresolved}; "
                f"reason: {item['reason'] or 'not recorded'}."
            )
    lines.append("")
    return lines


def summary_header(
    session: dict[str, Any],
    runs: dict[str, RunRecords],
    candidates: dict[str, dict[str, Any]],
    active: list[dict[str, Any]],
    hints: list[dict[str, Any]],
    resolved: int,
) -> list[str]:
    return [
        "# Public QA triage",
        "",
        f"Session: `{session['session_id']}`",
        f"Target product revision: `{session['target_product_revision']}`",
        f"Target suite revision: `{session['target_suite_revision']}`",
        "",
        f"Admitted Scenario Runs: {len(runs)}",
        f"Triage candidates: {len(candidates)}",
        f"Active Triage Cases: {len(active)} ({resolved} dispositioned)",
        f"Non-causal similarity hints: {len(hints)}",
        "",
    ]


def summary_hints(hints: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    if hints:
        lines.extend(["## Similarity hints", ""])
        for hint in hints:
            linked = ", ".join(f"`{item}`" for item in hint["candidate_ids"])
            lines.append(f"- {linked}: {hint['basis']}.")
        lines.append("")
    return lines


def summary_cases(
    active: list[dict[str, Any]], candidates: dict[str, dict[str, Any]]
) -> list[str]:
    lines: list[str] = []
    for case in active:
        status = case["disposition"]["disposition"] if case["disposition"] else "pending"
        lines.extend(
            [
                f"## `{case['case_id']}`",
                "",
                f"Disposition: `{status}`",
                f"Suspected roots: {', '.join(f'`{item}`' for item in case['suspected_root_ids']) or 'none'}",
                "",
            ]
        )
        for candidate_id in case["candidate_ids"]:
            candidate = candidates[candidate_id]
            evidence = ", ".join(f"`{item}`" for item in candidate["evidence_refs"]) or "none"
            statuses = ", ".join(candidate["historical_statuses"])
            lines.append(
                f"- `{candidate_id}` ({candidate['kind']}; {statuses}): "
                f"{candidate['text']} Evidence: {evidence}."
            )
        lines.append("")
    return lines


def summary_outputs(
    cases: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    qualification: dict[str, Any] | None,
) -> list[str]:
    findings = projected_findings(cases, candidates)
    changes = projected_qa_changes(cases, candidates)
    lines = [
        "## Outputs",
        "",
        f"Findings: {len(findings)}",
        f"QA Changes: {len(changes)}",
        "",
    ]
    if qualification is None:
        lines.append("Qualification has not been calculated for the current projection.")
    else:
        lines.extend(
            [
                f"Qualification: `{qualification['qualification']}`",
                "",
                "Scenario Run Outcomes:",
                "",
            ]
        )
        for run_id, outcome in sorted(qualification["scenario_run_outcomes"].items()):
            lines.append(f"- `{run_id}`: `{outcome}`")
        missing = [
            item["configured_scenario_id"]
            for item in qualification["required_configured_scenarios"]
            if item["outcome"] == "missing"
        ]
        if missing:
            lines.extend(["", "Missing required Configured Scenarios: " + ", ".join(missing)])
    return lines


def run_roles(
    events: list[dict[str, Any]], runs: dict[str, RunRecords]
) -> dict[str, dict[str, Any]]:
    roles = {
        run_id: {
            "qualification_role": "active",
            "superseded_by_run_id": None,
            "supersession_reason": None,
            "supersession_basis": None,
        }
        for run_id in runs
    }
    for event in events:
        if event["event_type"] != "run-superseded":
            continue
        payload = event["payload"]
        run_id = payload.get("run_id")
        replacement_run_id = payload.get("replacement_run_id")
        if run_id not in roles or replacement_run_id not in roles or run_id == replacement_run_id:
            raise TriageError("run supersession refers to invalid admitted runs")
        if roles[run_id]["qualification_role"] != "active":
            raise TriageError(f"{run_id}: run is already historical")
        if roles[replacement_run_id]["qualification_role"] != "active":
            raise TriageError(f"{replacement_run_id}: replacement run is not active")
        roles[payload["run_id"]] = {
            "qualification_role": "historical",
            "superseded_by_run_id": replacement_run_id,
            "supersession_reason": payload["reason"],
            "supersession_basis": payload["basis"],
        }
    return roles


def projection_digest(
    session: dict[str, Any],
    runs: dict[str, RunRecords],
    cases: dict[str, dict[str, Any]],
    roles: dict[str, dict[str, Any]],
) -> str:
    projection = {
        "session": session,
        "runs": {run_id: run.manifest_hash for run_id, run in sorted(runs.items())},
        "cases": active_cases(cases),
        "run_roles": roles,
    }
    return stable_digest(projection)


def current_qualification(events: list[dict[str, Any]], digest: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event["event_type"] != "qualification-calculated":
            continue
        if event["payload"].get("projection_digest") == digest:
            return event["payload"]["qualification"]
        return None
    return None


def render_session(triage_root: Path) -> dict[str, Any]:
    try:
        with exclusive_file_lock(triage_root / ".triage.lock"):
            return _render_session_unlocked(triage_root)
    except TimeoutError as error:
        raise TriageError(str(error)) from error


def _render_session_unlocked(triage_root: Path) -> dict[str, Any]:
    session, events, runs, candidates, cases = load_session_projection(triage_root)
    admitted = admitted_runs(events)
    atomic_json(triage_root / "input-runs.json", input_run_projection(admitted, runs))
    atomic_jsonl(triage_root / "findings.jsonl", projected_findings(cases, candidates))
    atomic_jsonl(triage_root / "qa-changes.jsonl", projected_qa_changes(cases, candidates))
    hints = similarity_hints(candidates)
    roles = run_roles(events, runs)
    digest = projection_digest(session, runs, cases, roles)
    qualification = current_qualification(events, digest)
    qualification_path = triage_root / "qualification.json"
    if qualification is None:
        qualification_path.unlink(missing_ok=True)
    else:
        atomic_json(qualification_path, qualification)
    atomic_text(
        triage_root / "triage-summary.md",
        progress_summary(session, runs, candidates, cases, hints, qualification),
    )
    pending = [case["case_id"] for case in active_cases(cases) if case["disposition"] is None]
    state = {
        "triage_format_version": TRIAGE_FORMAT_VERSION,
        "record_type": "triage-state",
        "session_id": session["session_id"],
        "status": "ready-to-qualify" if not pending else "triage",
        "pending_case_ids": pending,
        "candidate_count": len(candidates),
        "active_case_count": len(active_cases(cases)),
        "similarity_hint_count": len(hints),
        "last_sequence": len(events),
        "last_updated_at": utc_now_rfc3339(),
    }
    atomic_json(triage_root / "triage-state.json", state)
    return {
        "session": session,
        "runs": admitted,
        "candidates": candidates,
        "cases": active_cases(cases),
        "similarity_hints": hints,
        "state": state,
        "qualification": qualification,
    }


def cleanup_reasons(run: RunRecords) -> tuple[list[str], bool]:
    issues = cleanup_findings(run.cleanup)
    unresolved = [item for item in issues if item["unresolved_authority"]]
    requires_incomplete = bool(unresolved) and run.manifest["cleanup_status"] == "unverified"
    reasons = []
    if run.manifest["cleanup_status"] == "failed":
        reasons.append("Required cleanup failed.")
    elif run.manifest["cleanup_status"] == "unverified":
        reasons.append("Cleanup is unverified.")
    for item in issues:
        reasons.append(
            f"Cleanup resource {item['identity']}: "
            f"{item['disposition'] or 'unknown disposition'}; "
            f"active authority possible: {item['active_authority_possible']}."
        )
    if requires_incomplete:
        reasons.append(
            "Safe shutdown remains unresolved for: "
            + ", ".join(item["identity"] for item in unresolved)
            + "."
        )
    return reasons, requires_incomplete


def run_outcomes_and_reasons(
    runs: dict[str, RunRecords],
    candidates: dict[str, dict[str, Any]],
    cases: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    conditions: dict[str, list[str]] = {run_id: [] for run_id in runs}
    reasons: dict[str, list[str]] = {run_id: [] for run_id in runs}
    for case in active_cases(cases):
        disposition = case["disposition"]
        if disposition is None:
            raise TriageError(f"{case['case_id']}: pending disposition")
        effect = disposition_effect(disposition, cases)
        linked = {candidates[item]["run_id"] for item in case["candidate_ids"]}
        for run_id in linked:
            conditions[run_id].append(effect)
            if effect != "neutral":
                reasons[run_id].append(f"Case {case['case_id']}: {disposition['disposition']}.")
    outcomes = {}
    for run_id, run in runs.items():
        effects = conditions[run_id]
        cleanup_details, unresolved = cleanup_reasons(run)
        reasons[run_id].extend(cleanup_details)
        if run.manifest["execution_status"] != "completed":
            reasons[run_id].append(f"Execution status: {run.manifest['execution_status']}.")
        invalid_results = [
            result["check_result_id"]
            for result in run.results
            if result["evidence_integrity"] != "valid"
        ]
        for result_id in invalid_results:
            reasons[run_id].append(f"Check Result {result_id} has invalid evidence integrity.")
        if "failed" in effects:
            outcomes[run_id] = "failed"
        elif (
            "incomplete" in effects
            or run.manifest["cleanup_status"] == "failed"
            or unresolved
            or run.manifest["execution_status"] != "completed"
            or any(result["evidence_integrity"] != "valid" for result in run.results)
        ):
            outcomes[run_id] = "incomplete"
        else:
            outcomes[run_id] = "passed"
            reasons[run_id].insert(0, "All selected Checks are satisfied.")
    return outcomes, reasons


def run_outcomes(
    runs: dict[str, RunRecords],
    candidates: dict[str, dict[str, Any]],
    cases: dict[str, dict[str, Any]],
) -> dict[str, str]:
    return run_outcomes_and_reasons(runs, candidates, cases)[0]


def configured_outcome(values: list[str]) -> str:
    if "failed" in values:
        return "failed"
    if "incomplete" in values:
        return "incomplete"
    return "passed"


def qualification_projection(
    session: dict[str, Any],
    admitted: dict[str, dict[str, Any]],
    runs: dict[str, RunRecords],
    outcomes: dict[str, str],
    reasons: dict[str, list[str]],
    roles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_configured: dict[str, list[str]] = defaultdict(list)
    for run_id, outcome in outcomes.items():
        if roles[run_id]["qualification_role"] == "active":
            by_configured[runs[run_id].run["configured_scenario_id"]].append(outcome)
    required = []
    optional = []
    for configured in session["configured_scenarios"]:
        configured_id = configured["configured_scenario_id"]
        values = by_configured.get(configured_id, [])
        run_ids = sorted(
            run_id
            for run_id, run in runs.items()
            if run.run["configured_scenario_id"] == configured_id
        )
        item = {
            "scenario_id": configured["scenario_id"],
            "configured_scenario_id": configured_id,
            "required": configured["required"],
            "outcome": configured_outcome(values) if values else "missing",
            "run_ids": run_ids,
            "active_run_ids": [
                run_id for run_id in run_ids if roles[run_id]["qualification_role"] == "active"
            ],
        }
        (required if configured["required"] else optional).append(item)
    verdicts = [item["outcome"] for item in required]
    qualification = (
        "failed"
        if "failed" in verdicts
        else ("incomplete" if any(item != "passed" for item in verdicts) else "passed")
    )
    return {
        "triage_format_version": TRIAGE_FORMAT_VERSION,
        "record_type": "qualification",
        "session_id": session["session_id"],
        "target_product_revision": session["target_product_revision"],
        "target_suite_revision": session["target_suite_revision"],
        "qualification_policy_revision": session["qualification_policy_revision"],
        "helper_revision": session["helper_revision"],
        "input_runs": [
            {
                "run_id": run_id,
                "manifest_sha256": payload["manifest_sha256"],
                "configured_scenario_id": payload["configured_scenario_id"],
                "suite_revision": payload["suite_revision"],
                "suite_equivalence_reason": payload["suite_equivalence_reason"],
                **{
                    key: value
                    for key, value in roles[run_id].items()
                    if key != "supersession_basis"
                },
            }
            for run_id, payload in sorted(admitted.items())
        ],
        "compatibility_decisions": [
            {
                "run_id": run_id,
                "reason": payload["suite_equivalence_reason"],
            }
            for run_id, payload in sorted(admitted.items())
            if payload["suite_equivalence_reason"] is not None
        ],
        "supersession_decisions": [
            {
                "run_id": run_id,
                "replacement_run_id": role["superseded_by_run_id"],
                "basis": role["supersession_basis"],
                "reason": role["supersession_reason"],
            }
            for run_id, role in sorted(roles.items())
            if role["qualification_role"] == "historical"
        ],
        "reuse_decisions": [
            {"run_id": run_id, "reason": payload["reuse_reason"]}
            for run_id, payload in sorted(admitted.items())
            if payload.get("reuse_reason") is not None
        ],
        "scenario_run_outcomes": outcomes,
        "scenario_run_reasons": reasons,
        "required_configured_scenarios": required,
        "optional_configured_scenarios": optional,
        "qualification": qualification,
        "calculated_at": utc_now_rfc3339(),
    }


def admit_run(
    triage_root: Path,
    run_root: Path,
    suite_equivalence_reason: str | None,
    idempotency_key: str,
    reuse_reason: str | None = None,
) -> dict[str, Any]:
    session, events, existing_runs, _candidates, _cases = load_session_projection(triage_root)
    run = validate_run(run_root)
    validate_run_against_suite(run, session)
    if run.run_id in existing_runs:
        payload = admitted_runs(events)[run.run_id]
        if payload["manifest_sha256"] != run.manifest_hash:
            raise TriageError(f"{run.root}: admitted run identity changed")
        return render_session(triage_root)
    if run.run["product_revision"] != session["target_product_revision"]:
        raise TriageError(f"{run.root}: product revision differs from session target")
    suite_revision = run.run["suite_revision"]
    if suite_revision != session["target_suite_revision"] and not suite_equivalence_reason:
        raise TriageError(f"{run.root}: different suite revision requires equivalence reason")
    configured_ids = {item["configured_scenario_id"] for item in session["configured_scenarios"]}
    if run.run["configured_scenario_id"] not in configured_ids:
        raise TriageError(f"{run.root}: Configured Scenario is absent from target suite")
    payload = {
        "run_id": run.run_id,
        "run_root": str(run.root),
        "manifest_sha256": run.manifest_hash,
        "scenario_id": run.run["scenario_id"],
        "configured_scenario_id": run.run["configured_scenario_id"],
        "product_revision": run.run["product_revision"],
        "suite_revision": suite_revision,
        "suite_equivalence_reason": suite_equivalence_reason,
        "reuse_reason": reuse_reason,
    }
    append_admission_events(triage_root, run, payload, idempotency_key)
    return render_session(triage_root)


def append_admission_events(
    triage_root: Path,
    run: RunRecords,
    admission: dict[str, Any],
    admission_key: str,
) -> None:
    candidates = candidates_for_run(run)
    additions = [("run-admitted", admission, admission_key)]
    for case in connected_cases(candidates):
        additions.append(
            (
                "case-proposed",
                case,
                f"{admission_key}:propose:{case['case_id']}",
            )
        )
    append_events(triage_root, additions)


def read_details(path: Path) -> dict[str, Any]:
    details = read_json(path)
    if not details:
        raise TriageError(f"{path}: details cannot be empty")
    return details


def decide_case(
    triage_root: Path,
    case_id: str,
    disposition: str,
    details_path: Path,
    idempotency_key: str,
) -> dict[str, Any]:
    _session, _events, runs, candidates, cases = load_session_projection(triage_root)
    case = active_case(cases, case_id)
    details = read_details(details_path)
    payload = {"case_id": case_id, "disposition": disposition, "details": details}
    if case["disposition"] is not None:
        existing = case["disposition"]
        if existing["idempotency_key"] == idempotency_key and all(
            existing[field] == payload[field] for field in payload
        ):
            return render_session(triage_root)
        raise TriageError(f"{case_id}: case already dispositioned")
    validate_disposition(disposition, details, case, candidates, cases, runs)
    append_event(triage_root, "case-dispositioned", payload, idempotency_key)
    return render_session(triage_root)


def merge_case_ids(
    triage_root: Path,
    source_case_ids: list[str],
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if len(source_case_ids) < 2:
        raise TriageError("merge requires at least two case IDs")
    case_id = "case-" + stable_digest([*sorted(source_case_ids), reason])[:16]
    payload = {
        "case_id": case_id,
        "source_case_ids": source_case_ids,
        "reason": reason,
        "suspected_root_ids": [],
    }
    append_event(triage_root, "case-merged", payload, idempotency_key)
    return render_session(triage_root)


def split_case_from_file(
    triage_root: Path,
    source_case_id: str,
    groups_path: Path,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    value = json.loads(groups_path.read_text())
    if not isinstance(value, list) or any(not isinstance(group, list) for group in value):
        raise TriageError(f"{groups_path}: expected an array of candidate-ID arrays")
    replacements = []
    for index, group in enumerate(value, 1):
        case_id = (
            "case-" + stable_digest([source_case_id, reason, str(index), *sorted(group)])[:16]
        )
        replacements.append({"case_id": case_id, "candidate_ids": group, "suspected_root_ids": []})
    payload = {
        "source_case_id": source_case_id,
        "replacements": replacements,
        "reason": reason,
    }
    append_event(triage_root, "case-split", payload, idempotency_key)
    return render_session(triage_root)


def reopen_case(
    triage_root: Path, case_id: str, reason: str, idempotency_key: str
) -> dict[str, Any]:
    _session, events, _runs, _candidates, cases = load_session_projection(triage_root)
    case = active_case(cases, case_id)
    payload = {"case_id": case_id, "reason": reason}
    if case["disposition"] is None:
        existing = next(
            (event for event in events if event["idempotency_key"] == idempotency_key), None
        )
        if (
            existing
            and existing["event_type"] == "case-reopened"
            and existing["payload"] == payload
        ):
            return render_session(triage_root)
        raise TriageError(f"{case_id}: case is already pending")
    append_event(triage_root, "case-reopened", payload, idempotency_key)
    return render_session(triage_root)


def supersede_run(
    triage_root: Path,
    run_id: str,
    replacement_run_id: str,
    basis: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    _session, events, runs, candidates, cases = load_session_projection(triage_root)
    if basis not in {"complete-rerun", "invalid-evidence"}:
        raise TriageError("supersession basis must be complete-rerun or invalid-evidence")
    if not reason:
        raise TriageError("supersession requires a reason")
    if run_id == replacement_run_id or run_id not in runs or replacement_run_id not in runs:
        raise TriageError("supersession requires two distinct admitted runs")
    payload = {
        "run_id": run_id,
        "replacement_run_id": replacement_run_id,
        "basis": basis,
        "reason": reason,
    }
    existing = next(
        (event for event in events if event["idempotency_key"] == idempotency_key), None
    )
    if existing is not None:
        if existing["event_type"] == "run-superseded" and existing["payload"] == payload:
            return render_session(triage_root)
        raise TriageError(f"{triage_root}: idempotency key reused for different event")
    roles = run_roles(events, runs)
    if roles[run_id]["qualification_role"] != "active":
        raise TriageError(f"{run_id}: run is already historical")
    if roles[replacement_run_id]["qualification_role"] != "active":
        raise TriageError(f"{replacement_run_id}: replacement run is not active")
    source = runs[run_id].run
    replacement = runs[replacement_run_id].run
    if source["configured_scenario_id"] != replacement["configured_scenario_id"]:
        raise TriageError("supersession runs must cover the same Configured Scenario")
    outcomes = run_outcomes(runs, candidates, cases)
    if outcomes[replacement_run_id] != "passed":
        raise TriageError("replacement run must pass")
    if outcomes[run_id] == "failed" and basis != "invalid-evidence":
        raise TriageError("a trustworthy failed run cannot be superseded")
    if basis == "complete-rerun" and outcomes[run_id] != "incomplete":
        raise TriageError("complete-rerun may supersede only an incomplete run")
    append_event(triage_root, "run-superseded", payload, idempotency_key)
    return render_session(triage_root)


def finalize_session(triage_root: Path, idempotency_key: str) -> dict[str, Any]:
    session, events, runs, candidates, cases = load_session_projection(triage_root)
    pending = [case["case_id"] for case in active_cases(cases) if case["disposition"] is None]
    if pending:
        raise TriageError(f"triage has pending cases: {pending}")
    outcomes, reasons = run_outcomes_and_reasons(runs, candidates, cases)
    admitted = admitted_runs(events)
    roles = run_roles(events, runs)
    digest = projection_digest(session, runs, cases, roles)
    existing = next(
        (event for event in events if event["idempotency_key"] == idempotency_key), None
    )
    if existing is not None:
        if (
            existing["event_type"] == "qualification-calculated"
            and existing["payload"].get("projection_digest") == digest
        ):
            return render_session(triage_root)
        raise TriageError(f"{triage_root}: idempotency key reused for different event")
    qualification = qualification_projection(session, admitted, runs, outcomes, reasons, roles)
    payload = {"projection_digest": digest, "qualification": qualification}
    append_event(triage_root, "qualification-calculated", payload, idempotency_key)
    return render_session(triage_root)


if __name__ == "__main__":
    from triage_cli import main

    raise SystemExit(main())
