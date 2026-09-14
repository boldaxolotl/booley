"""Track compact observed Check states from sealed public QA runs."""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from qa.triage import RunRecords, validate_run
from qa.validate import load_scenarios, resolved_configured_checks

from booley.runtime.timefmt import parse_timestamp, rfc3339_from_epoch

RESULTS = Path(__file__).with_name("results")
SCENARIOS = Path(__file__).with_name("scenarios")
STATUSES = ("fail", "pass", "blocked", "unavailable")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FIELDS = {
    "format_version",
    "run_id",
    "scenario_id",
    "configured_scenario_id",
    "completed_at",
    "product_revision",
    "suite_revision",
    "run_manifest_sha256",
    "execution_status",
    "cleanup_status",
    "checks",
}


def canonical_time(stamp: str) -> str:
    """Normalize a sealed RFC 3339 timestamp to Booley's UTC seconds."""
    if not isinstance(stamp, str):
        raise ValueError("invalid completed_at")
    try:
        return rfc3339_from_epoch(parse_timestamp(stamp).timestamp())
    except (OverflowError, ValueError) as exc:
        raise ValueError("invalid completed_at") from exc


def check_status(results: list[dict]) -> str:
    """Reduce surviving attempts, preserving every uncorrected failure."""
    corrected = {item["corrects_result_id"] for item in results if item["corrects_result_id"]}
    surviving = {item["status"] for item in results if item["check_result_id"] not in corrected}
    for status in STATUSES:
        if status in surviving:
            return status
    raise ValueError("selected Check has no surviving result")


def from_sealed_run(run: RunRecords) -> dict:
    """Derive a small observation snapshot from one validated sealed run."""
    by_check = {check_id: [] for check_id in run.run["selected_check_ids"]}
    for result in run.results:
        if result["check_id"] not in by_check:
            raise ValueError(f"{run.root}: unselected Check Result {result['check_id']}")
        by_check[result["check_id"]].append(result)
    return validate_result(
        {
            "format_version": 1,
            "run_id": run.run_id,
            "scenario_id": run.run["scenario_id"],
            "configured_scenario_id": run.run["configured_scenario_id"],
            "completed_at": canonical_time(run.state["completed_at"]),
            "product_revision": run.run["product_revision"],
            "suite_revision": run.run["suite_revision"],
            "run_manifest_sha256": run.manifest_hash,
            "execution_status": run.state["execution_status"],
            "cleanup_status": run.state["cleanup_status"],
            "checks": {
                check_id: check_status(items) for check_id, items in sorted(by_check.items())
            },
        }
    )


def validate_result(value: object) -> dict:
    """Reject malformed or path-unsafe tracked snapshots."""
    if not isinstance(value, dict) or value.keys() != FIELDS:
        raise ValueError(f"result requires exactly these fields: {sorted(FIELDS)}")
    validate_metadata(value)
    validate_checks(value["checks"])
    return value


def validate_metadata(value: dict) -> None:
    """Require safe identities and valid sealed-run metadata."""
    if type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ValueError("format_version must be 1")
    for field in ("run_id", "scenario_id", "configured_scenario_id"):
        if not isinstance(value[field], str) or not IDENTIFIER.fullmatch(value[field]):
            raise ValueError(f"invalid {field}")
    for field in ("product_revision", "suite_revision"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"invalid {field}")
    if not isinstance(value["run_manifest_sha256"], str) or not SHA256.fullmatch(
        value["run_manifest_sha256"]
    ):
        raise ValueError("invalid run_manifest_sha256")
    if canonical_time(value["completed_at"]) != value["completed_at"]:
        raise ValueError("completed_at must be second-resolution UTC RFC 3339")
    if value["execution_status"] not in ("completed", "deadline-reached", "operator-error"):
        raise ValueError("invalid execution_status")
    if value["cleanup_status"] not in ("complete", "failed"):
        raise ValueError("invalid cleanup_status")


def validate_checks(checks: object) -> None:
    """Require one supported observed status per selected Check."""
    if not isinstance(checks, dict) or not checks:
        raise ValueError("checks must be a nonempty mapping")
    if any(not isinstance(key, str) or not key for key in checks):
        raise ValueError("invalid Check ID")
    if any(status not in STATUSES for status in checks.values()):
        raise ValueError("invalid Check status")


def read_result(path: Path) -> dict:
    """Load one snapshot without accepting duplicate JSON keys."""

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{path}: duplicate JSON key {key}")
            result[key] = item
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    return validate_result(value)


def result_path(root: Path, result: dict) -> Path:
    """Place a snapshot under its Scenario and Run identities."""
    return root / result["scenario_id"] / f"{result['run_id']}.json"


def publish(run_root: Path, root: Path) -> Path:
    """Validate a seal and atomically add its derived snapshot."""
    result = from_sealed_run(validate_run(run_root))
    destination = result_path(root, result)
    if destination.exists():
        if read_result(destination) != result:
            raise ValueError(f"{destination}: run ID already has different results")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".qa-result-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def load_history(root: Path) -> list[dict]:
    """Read tracked snapshots, checking path identity and unique run IDs."""
    history = []
    seen = set()
    for path in sorted(root.glob("*/*.json")):
        result = read_result(path)
        if path != result_path(root, result):
            raise ValueError(f"{path}: path disagrees with result identity")
        if result["run_id"] in seen:
            raise ValueError(f"{path}: duplicate run ID")
        seen.add(result["run_id"])
        history.append(result)
    return sorted(history, key=lambda item: (item["completed_at"], item["run_id"]))


def current_checks(scenarios_root: Path) -> dict[str, tuple[str, list[str]]]:
    """Resolve currently selected Checks for each Configured Scenario."""
    configured = {}
    for scenario in load_scenarios(scenarios_root.parent).values():
        path = scenarios_root / scenario["scenario_id"] / "scenario.yaml"
        for item in scenario["configured_scenarios"]:
            configured[item["id"]] = (
                scenario["scenario_id"],
                resolved_configured_checks(scenario, item, path),
            )
    return configured


def render_scenario(scenario_id: str, runs: list[dict]) -> list[str]:
    """Show every run of a Scenario with comparable per-configuration deltas."""
    lines = [
        f"## Scenario: {scenario_id}",
        "",
        "| Completed (UTC) | Configured Scenario | Product | Suite | Failing Checks | Change from prior same configuration |",
        "|---|---|---|---|---:|---:|",
    ]
    previous: dict[str, int] = {}
    for run in runs:
        configured = run["configured_scenario_id"]
        failing = list(run["checks"].values()).count("fail")
        change = failing - previous[configured] if configured in previous else None
        lines.append(
            f"| {run['completed_at']} | {configured} | `{run['product_revision']}` "
            f"| `{run['suite_revision']}` | {failing} | {change if change is not None else '—'} |"
        )
        previous[configured] = failing
    if not runs:
        lines.append("| No recorded runs | — | — | — | 0 | — |")
    return [*lines, ""]


def render_configured(identifier: str, checks: list[str], runs: list[dict]) -> list[str]:
    """Render one Configured Scenario's counts and latest raw Check states."""
    lines = [
        f"## {identifier}",
        "",
        "| Completed (UTC) | Product revision | Pass | Fail | Blocked | Unavailable |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for run in runs:
        counts = {status: list(run["checks"].values()).count(status) for status in STATUSES}
        lines.append(
            f"| {run['completed_at']} | `{run['product_revision']}` | {counts['pass']} "
            f"| {counts['fail']} | {counts['blocked']} | {counts['unavailable']} |"
        )
    if not runs:
        lines.append("| No recorded runs | — | 0 | 0 | 0 | 0 |")
    latest = runs[-1] if runs else None
    title = "Latest recorded Check status"
    if latest:
        title += f" (product `{latest['product_revision']}`, suite `{latest['suite_revision']}`)"
    lines.extend(
        (
            "",
            title + ":",
            "",
            "| Check | Status | Exercised in latest run? | Ever exercised? |",
            "|---|---|---|---|",
        )
    )
    for check_id in checks:
        status = latest["checks"].get(check_id, "not exercised") if latest else "not exercised"
        ever = any(run["checks"].get(check_id) in ("pass", "fail") for run in runs)
        lines.append(
            f"| `{check_id}` | {status} | {'yes' if status in ('pass', 'fail') else 'no'} "
            f"| {'yes' if ever else 'no'} |"
        )
    return [*lines, ""]


def report(history: list[dict], scenarios_root: Path, configured_id: str | None) -> str:
    """Render observed trends and current-suite Check status."""
    selections = current_checks(scenarios_root)
    if configured_id and configured_id not in selections:
        raise ValueError(f"unknown Configured Scenario: {configured_id}")
    lines = ["# Public QA observed Check history", ""]
    allowed = {configured_id} if configured_id else set(selections)
    history = sorted(history, key=lambda item: (item["completed_at"], item["run_id"]))
    scenarios = {entry[0] for key, entry in selections.items() if key in allowed}
    for scenario_id in sorted(scenarios):
        runs = [
            item
            for item in history
            if item["scenario_id"] == scenario_id and item["configured_scenario_id"] in allowed
        ]
        lines.extend(render_scenario(scenario_id, runs))
    for identifier, (scenario_id, checks) in sorted(selections.items()):
        if configured_id and identifier != configured_id:
            continue
        runs = [
            item
            for item in history
            if item["configured_scenario_id"] == identifier and item["scenario_id"] == scenario_id
        ]
        lines.extend(render_configured(identifier, checks, runs))
    return "\n".join(lines)


def main() -> int:
    """Run the publication, validation, or reporting command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, default=RESULTS)
    command = parser.add_subparsers(dest="command", required=True)
    add = command.add_parser("add", help="record one sealed run")
    add.add_argument("run_root", type=Path)
    show = command.add_parser("report", help="show observed trends and Check statuses")
    show.add_argument("--configured-scenario")
    show.add_argument("--scenarios-root", type=Path, default=SCENARIOS)
    command.add_parser("validate", help="validate all tracked snapshots")
    args = parser.parse_args()
    try:
        if args.command == "add":
            print(publish(args.run_root, args.results_root))
        elif args.command == "report":
            print(
                report(
                    load_history(args.results_root), args.scenarios_root, args.configured_scenario
                )
            )
        else:
            print(f"Validated {len(load_history(args.results_root))} result(s).")
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
