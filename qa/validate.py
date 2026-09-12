"""Validate authored QA assets without executing or qualifying scenarios."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator


def validate_capability_id(value: str) -> None:
    """Require a readable semantic identifier rather than a category sequence."""
    segments = value.split("-")
    if (
        len(segments) < 2
        or any(not segment.isalnum() for segment in segments)
        or any(segment != segment.upper() for segment in segments)
        or any(not any(character.isalpha() for character in segment) for segment in segments)
    ):
        raise ValueError(
            f"coverage.yaml: capability ID must be semantic uppercase kebab case: {value}"
        )


def read_yaml(path: Path) -> object:
    """Reject duplicate keys and alias indirection before interpreting authored data."""
    text = path.read_text(encoding="utf-8")
    if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
        raise ValueError(f"{path}: YAML aliases are not explicit authored selections")
    tree = yaml.compose(text)
    pending = [tree] if tree else []
    while pending:
        node = pending.pop()
        if isinstance(node, yaml.MappingNode):
            keys = set()
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    raise ValueError(f"{path}: YAML mapping keys must be scalar strings")
                if key.value in keys:
                    raise ValueError(f"{path}: duplicate YAML key {key.value}")
                keys.add(key.value)
                pending.append(value)
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)
    return yaml.safe_load(text)


def record(value: object, fields: dict, context: str) -> dict:
    """Validate a small metadata record without another schema family."""
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected mapping")
    if value.keys() != fields.keys():
        raise ValueError(
            f"{context}: missing {sorted(fields.keys() - value.keys())}; "
            f"unknown {sorted(value.keys() - fields.keys())}"
        )
    for key, expected in fields.items():
        if type(value[key]) is not expected or (expected is str and not value[key].strip()):
            raise ValueError(f"{context}: invalid {key}")
    return value


def unique(records: list, context: str) -> None:
    ids = [item["id"] for item in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{context}: duplicate IDs")


def strings(values: list, context: str, nonempty: bool = True) -> None:
    if (nonempty and not values) or any(type(item) is not str or not item for item in values):
        raise ValueError(f"{context}: expected nonempty strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{context}: duplicate values")


def metadata(root: Path, filename: str, field: str) -> dict:
    data = read_yaml(root / filename)
    record(data, {"format_version": int, field: list}, filename)
    if data["format_version"] != 1 or not data[field]:
        raise ValueError(f"{filename}: expected format_version 1 and nonempty {field}")
    return data


def resolved_configured_checks(scenario: dict, configured: dict, path: Path) -> list[str]:
    """Resolve one Configured Scenario without cross-Scenario references."""
    check_sets = {item["id"]: item["checks"] for item in scenario["check_sets"]}
    checks = []
    for set_id in configured["check_sets"]:
        if set_id not in check_sets:
            raise ValueError(f"{path}: {configured['id']}: unknown check set {set_id}")
        checks.extend(check_sets[set_id])
    strings(checks, f"{path}: {configured['id']}.resolved checks")
    return checks


def resolved_pre_run_requirements(scenario: dict, configured: dict) -> list[str]:
    """Combine Scenario-wide and Configured Scenario-specific admission gates."""
    return [
        *scenario.get("shared_pre_run_requirements", []),
        *configured["pre_run_requirements"],
    ]


def validate_configured_scenarios(scenario: dict, path: Path) -> None:
    """Require complete, explicit Configured Scenarios."""
    unique(scenario["check_sets"], f"{path}: check sets")
    strings(
        [check for check_set in scenario["check_sets"] for check in check_set["checks"]],
        f"{path}: checks across check sets",
    )
    unique(scenario["configured_scenarios"], f"{path}: Configured Scenarios")
    known = {check["id"]: step for step in scenario["steps"] for check in step["checks"]}
    selected_anywhere = set()
    used_sets = set()
    for configured in scenario["configured_scenarios"]:
        strings(
            resolved_pre_run_requirements(scenario, configured),
            f"{path}: {configured['id']}.resolved pre-run requirements",
        )
        selected = set(resolved_configured_checks(scenario, configured, path))
        selected_anywhere.update(selected)
        used_sets.update(configured["check_sets"])
        if missing := selected - known.keys():
            raise ValueError(f"{path}: {configured['id']}: unknown checks {sorted(missing)}")
        excluded = [item["check"] for item in configured["exclusions"]]
        if len(excluded) != len(set(excluded)) or set(excluded) - known.keys():
            raise ValueError(f"{path}: {configured['id']}: duplicate or unknown exclusion")
        if selected.intersection(excluded):
            raise ValueError(
                f"{path}: {configured['id']}: a check cannot be selected and excluded"
            )
        for check_id in selected:
            missing = set(known[check_id].get("requires", [])) - selected
            if missing:
                raise ValueError(
                    f"{path}: {configured['id']}.{check_id}: "
                    f"missing supporting checks {sorted(missing)}"
                )
    if missing := known.keys() - selected_anywhere:
        raise ValueError(f"{path}: checks selected nowhere: {sorted(missing)}")
    all_sets = {item["id"] for item in scenario["check_sets"]}
    if unused := all_sets - used_sets:
        raise ValueError(f"{path}: unused check sets {sorted(unused)}")


def contained(base: Path, name: str) -> Path:
    """Resolve an authored relative path without permitting symlink escape."""
    candidate = base / name
    if Path(name).is_absolute() or not candidate.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"{base}: path must be contained: {name}")
    return candidate


def validate_source(base: Path, reference: str, root: Path) -> None:
    parsed = urlsplit(reference)
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"{base}: invalid public source {reference}")
        return
    destination = (base / parsed.path).resolve()
    if not destination.is_relative_to(root.resolve()) or not destination.is_file():
        raise ValueError(f"{base}: missing or uncontained source {reference}")


def validate_assets(scenario: dict, path: Path, root: Path) -> None:
    """Validate public sources, frozen pins and explicitly disclosed files."""
    validate_source(path.parent, scenario["source"], root.parent)
    for item in scenario["inputs"]:
        validate_source(path.parent, item["source"], root.parent)
        pattern = {"git": r"[0-9a-f]{40}|[0-9a-f]{64}", "sha256": r"[0-9a-f]{64}"}.get(
            item["kind"]
        )
        if pattern and not re.fullmatch(pattern, item["value"]):
            raise ValueError(f"{path}: {item['id']}: expected exact {item['kind']} pin")
    for step in scenario["steps"]:
        for check in step["checks"]:
            validate_source(path.parent, check["authority_ref"], root.parent)
            for reference in check.get("navigation_refs", []):
                validate_source(path.parent, reference, root.parent)
            contained(path.parent, check["capture"])
        for asset in step.get("assets", []):
            base = root / "shared" if asset.get("base", "scenario") == "shared" else path.parent
            if not base.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"{path}: asset base must be contained in suite")
            target = contained(base, asset["path"])
            if not target.is_file():
                raise ValueError(f"{path}: missing asset {target}")
            content = target.read_bytes()
            if "sha256" in asset and hashlib.sha256(content).hexdigest() != asset["sha256"]:
                raise ValueError(f"{path}: asset digest mismatch: {target}")
            variables = set(re.findall(rb"\{\{\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*\}\}", content))
            allowed = {name.encode() for name in asset.get("substitutions", [])}
            if variables - allowed:
                raise ValueError(f"{path}: unresolved substitutions in {target}")


def validate_order(scenario: dict, path: Path) -> None:
    """Prerequisites are earlier check IDs, never steps or future results."""
    earlier = set()
    step_ids = set()
    ancestors = {}
    for step in scenario["steps"]:
        if step["id"] in step_ids:
            raise ValueError(f"{path}: duplicate step {step['id']}")
        step_ids.add(step["id"])
        for requirement in step.get("requires", []):
            if requirement not in earlier:
                raise ValueError(f"{path}: {step['id']}: {requirement} is not an earlier check")
        dependencies = set(step.get("requires", []))
        for requirement in step.get("requires", []):
            dependencies.update(ancestors[requirement])
        if recovery := step.get("recovery"):
            baseline = recovery["baseline"]
            detections = set(recovery["detection"])
            if baseline not in earlier or not detections <= earlier:
                raise ValueError(
                    f"{path}: recovery requires earlier baseline and detection identities"
                )
            if baseline not in dependencies or detections & dependencies:
                raise ValueError(
                    f"{path}: recovery must depend on baseline and remain reachable after detection failure"
                )
        for check in step["checks"]:
            if check["id"] in earlier:
                raise ValueError(f"{path}: duplicate check {check['id']}")
            earlier.add(check["id"])
            ancestors[check["id"]] = dependencies


def validate_budget(scenario: dict, path: Path) -> None:
    """Charge all work and contingency once, with a protected cleanup phase."""
    budget = scenario["budget"]
    phases = {phase["id"]: phase["minutes"] for phase in scenario["phases"]}
    if len(phases) != len(scenario["phases"]):
        raise ValueError(f"{path}: duplicate phase")
    total = sum(phases.values()) + budget["contingency_minutes"]
    if total > budget["deadline_minutes"] or total > 480:
        raise ValueError(f"{path}: budget totals {total} minutes")
    if phases.get("cleanup") != budget["cleanup_minutes"]:
        raise ValueError(f"{path}: cleanup phase must match budget reserve")
    if scenario["phases"][-1]["id"] != "cleanup":
        raise ValueError(f"{path}: cleanup must be the last phase")
    if budget["cleanup_start_minutes"] + budget["cleanup_minutes"] > budget["deadline_minutes"]:
        raise ValueError(f"{path}: cleanup exceeds deadline")
    if "final_regression_start_minutes" in budget:
        final = phases.get("final", 0)
        if (
            not final
            or budget["final_regression_start_minutes"] + final > budget["cleanup_start_minutes"]
        ):
            raise ValueError(f"{path}: final regression infringes cleanup reserve")
    assigned = set()
    for step in scenario["steps"]:
        phase = step["phase"]
        if phase not in phases:
            raise ValueError(f"{path}: unknown phase {phase}")
        assigned.add(phase)
        if step.get("timeout_minutes", 0) > phases[phase]:
            raise ValueError(f"{path}: {step['id']}: timeout exceeds phase budget")
    if assigned != phases.keys():
        raise ValueError(f"{path}: unassigned phase budget")


def validate(root: Path, scenario_id: str | None = None) -> dict:
    """Read the suite's required coverage inventory; raise on invalid input."""
    data = metadata(root, "coverage.yaml", "capabilities")
    for capability in data["capabilities"]:
        record(
            capability,
            {"id": str, "title": str, "sources": list, "contract": str, "applicability": str},
            "coverage.yaml",
        )
        validate_capability_id(capability["id"])
        strings(capability["sources"], "coverage.yaml sources")
        for source in capability["sources"]:
            validate_source(root, source, root.parent)
    unique(data["capabilities"], "coverage.yaml")

    scenarios = load_scenarios(root)
    if scenario_id is not None and scenario_id not in scenarios:
        raise ValueError(f"{root}: unknown scenario {scenario_id}")
    represented = {
        capability
        for scenario in scenarios.values()
        for step in scenario["steps"]
        for check in step["checks"]
        for capability in check["capabilities"]
    }
    declared = {capability["id"] for capability in data["capabilities"]}
    if represented != declared:
        raise ValueError(
            f"coverage.yaml: uncovered {sorted(declared - represented)}; "
            f"unknown {sorted(represented - declared)}"
        )

    return coverage_index(root, scenarios, declared)


def load_scenarios(root: Path) -> dict:
    schema = json.loads((Path(__file__).parent / "scenario.schema.json").read_text())
    scenarios = {}
    for path in sorted((root / "scenarios").glob("*/scenario.yaml")):
        scenario = read_yaml(path)
        errors = list(Draft202012Validator(schema).iter_errors(scenario))
        if errors:
            raise ValueError(f"{path}: " + "; ".join(e.message for e in errors))
        validate_order(scenario, path)
        validate_budget(scenario, path)
        validate_assets(scenario, path, root)
        validate_configured_scenarios(scenario, path)
        if scenario["scenario_id"] in scenarios:
            raise ValueError(f"{path}: duplicate scenario identity")
        scenarios[scenario["scenario_id"]] = scenario
    if not scenarios:
        raise ValueError(f"{root}: no scenarios found")
    all_configured = [
        configured
        for scenario in scenarios.values()
        for configured in scenario["configured_scenarios"]
    ]
    unique(all_configured, f"{root}: Configured Scenarios")
    return scenarios


def coverage_index(root: Path, scenarios: dict, declared: set[str]) -> dict:
    index = {}
    for capability in sorted(declared):
        checks = {
            scenario["scenario_id"] + "." + check["id"]
            for scenario in scenarios.values()
            for step in scenario["steps"]
            for check in step["checks"]
            if capability in check["capabilities"]
        }
        configured_scenarios = []
        for scenario in scenarios.values():
            prefix = scenario["scenario_id"] + "."
            for configured in scenario["configured_scenarios"]:
                selected = {
                    prefix + check
                    for check in resolved_configured_checks(scenario, configured, root)
                }
                if checks.intersection(selected):
                    configured_scenarios.append(configured["id"])
        index[capability] = {
            "checks": sorted(checks),
            "configured_scenarios": sorted(configured_scenarios),
        }
    return index


def publish_index(destination: Path, index: dict) -> None:
    """Publish only complete validated index bytes, leaving old output on failure."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".qa-index-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(index, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except (OSError, ValueError):
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    """Return a diagnostic exit status suitable for authoring and CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--scenario", help="Authoring aid; validates references against the suite")
    parser.add_argument("--coverage-index", type=Path)
    args = parser.parse_args()
    try:
        index = validate(args.root, args.scenario)
        if args.coverage_index:
            if args.scenario:
                raise ValueError("coverage index requires whole-suite validation; omit --scenario")
            destination = args.coverage_index.resolve()
            if destination.is_relative_to(args.root.resolve()):
                raise ValueError("coverage index must be outside authored suite inputs")
            publish_index(args.coverage_index, index)
        print(
            "Authoring validation passed; no scenarios executed."
            if args.scenario
            else "Suite asset validation passed; no scenarios executed."
        )
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
