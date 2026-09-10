"""Exercise the public suite-validation interface with disposable authored suites."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

VALIDATOR = Path(__file__).resolve().parents[2] / "qa" / "validate.py"


def test_empty_suite_is_not_success(tmp_path):
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "coverage.yaml" in result.stderr
    assert not (tmp_path / "index.json").exists()


def test_unknown_structural_key_is_rejected(tmp_path):
    (tmp_path / "coverage.yaml").write_text("format_version: 1\ncapabilties: []\n")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "capabilties" in result.stderr


def test_structurally_invalid_scenario_is_rejected(tmp_path):
    (tmp_path / "coverage.yaml").write_text(
        "format_version: 1\ncapabilities:\n- id: PRODUCT-ARTIFACT-INSTALLATION\n  title: Install\n"
        "  sources: [https://example.com/contract]\n  contract: Exact release\n"
        "  applicability: All Configured Scenarios\n"
    )
    scenario = tmp_path / "scenarios" / "sample"
    scenario.mkdir(parents=True)
    (scenario / "scenario.yaml").write_text("format_version: 1\nscenario_id: sample\nstepps: []\n")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "scenario.yaml" in result.stderr
    assert "stepps" in result.stderr


def test_opaque_capability_id_is_rejected(tmp_path):
    write_suite(tmp_path)
    coverage = tmp_path / "coverage.yaml"
    text = coverage.read_text().replace("PRODUCT-ARTIFACT-INSTALLATION", "H-01")
    coverage.write_text(text)
    scenario = tmp_path / "scenarios/sample/scenario.yaml"
    scenario.write_text(scenario.read_text().replace("PRODUCT-ARTIFACT-INSTALLATION", "H-01"))

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "capability ID must be semantic uppercase kebab case" in result.stderr


def write_suite(root):
    """Copy the independent literal authoring example into disposable storage."""
    import shutil

    import yaml

    shutil.copytree(Path(__file__).with_name("fixtures"), root, dirs_exist_ok=True)
    return yaml.safe_load((root / "scenarios/sample/scenario.yaml").read_text())


def write_scenario(root, scenario):
    """Replace the disposable Scenario fixture."""
    import yaml

    (root / "scenarios/sample/scenario.yaml").write_text(yaml.safe_dump(scenario))


def run_validator(root):
    """Run the suite validator against disposable authored data."""
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_prerequisites_reject_later_checks_and_step_ids(tmp_path):
    import yaml

    scenario = write_suite(tmp_path)
    scenario["steps"][1]["requires"] = ["restore"]
    (tmp_path / "scenarios/sample/scenario.yaml").write_text(yaml.safe_dump(scenario))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "earlier check" in result.stderr


def test_recovery_cannot_depend_transitively_on_negative_pass(tmp_path):
    import yaml

    scenario = write_suite(tmp_path)
    scenario["steps"][2]["requires"] = ["negative"]
    (tmp_path / "scenarios/sample/scenario.yaml").write_text(yaml.safe_dump(scenario))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "recovery" in result.stderr


def test_scenario_run_must_select_supporting_checks(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["check_sets"][0]["checks"].remove("baseline")
    write_scenario(tmp_path, scenario)
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "baseline" in result.stderr


def test_budget_includes_contingency_and_protects_cleanup(tmp_path):
    import yaml

    scenario = write_suite(tmp_path)
    scenario["phases"][0]["minutes"] = 460
    (tmp_path / "scenarios/sample/scenario.yaml").write_text(yaml.safe_dump(scenario))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "budget" in result.stderr


def test_asset_symlink_escape_is_rejected(tmp_path):
    import yaml

    scenario = write_suite(tmp_path)
    (tmp_path / "scenarios/sample/escape").symlink_to("/etc/passwd")
    scenario["steps"][0]["assets"] = [{"path": "escape", "audience": "scenario-operator"}]
    (tmp_path / "scenarios/sample/scenario.yaml").write_text(yaml.safe_dump(scenario))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "contained" in result.stderr


def test_unused_capability_is_not_coverage(tmp_path):
    import yaml

    write_suite(tmp_path)
    path = tmp_path / "coverage.yaml"
    data = yaml.safe_load(path.read_text())
    data["capabilities"].append(
        {
            "id": "HOST-BOOTSTRAP",
            "title": "Bootstrap",
            "sources": ["https://example.com/bootstrap"],
            "contract": "Idempotence",
            "applicability": "All hosts",
        }
    )
    path.write_text(yaml.safe_dump(data))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "HOST-BOOTSTRAP" in result.stderr


def test_scenario_schema_rejects_misspelled_agent_provider(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["configured_scenarios"][0]["parameters"]["agent_provder"] = "Claude"
    write_scenario(tmp_path, scenario)
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "agent_provder" in result.stderr


@pytest.mark.parametrize(
    "target",
    ["configured_scenario_id", "parameter", "pre_run_requirement", "exclusion_reason"],
)
def test_scenario_schema_rejects_whitespace_only_configured_scenario_strings(tmp_path, target):
    scenario = write_suite(tmp_path)
    configured = scenario["configured_scenarios"][0]
    if target == "configured_scenario_id":
        configured["id"] = " "
    elif target == "parameter":
        configured["parameters"]["agent_provider"] = " "
    elif target == "pre_run_requirement":
        configured["pre_run_requirements"][0] = " "
    else:
        configured["exclusions"] = [{"check": "baseline", "reason": " "}]
    write_scenario(tmp_path, scenario)
    result = run_validator(tmp_path)
    assert result.returncode == 1
    assert "does not match" in result.stderr


def test_coverage_index_is_derived_after_validation(tmp_path):
    write_suite(tmp_path)
    destination = tmp_path.parent / (tmp_path.name + "-index.json")
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--root",
            str(tmp_path),
            "--coverage-index",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    index = json.loads(destination.read_text())
    assert index["PRODUCT-ARTIFACT-INSTALLATION"]["checks"] == [
        "sample.baseline",
        "sample.negative",
        "sample.removed",
        "sample.restored",
    ]
    assert index["PRODUCT-ARTIFACT-INSTALLATION"]["configured_scenarios"] == ["sample-linux-cli"]


def test_required_check_cannot_disappear_from_all_configured_scenarios(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["check_sets"][0]["checks"].remove("removed")
    write_scenario(tmp_path, scenario)
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "removed" in result.stderr


def test_configured_scenario_rejects_unknown_check_set(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["configured_scenarios"][0]["check_sets"] = ["missing"]
    write_scenario(tmp_path, scenario)
    result = run_validator(tmp_path)
    assert result.returncode == 1
    assert "unknown check set missing" in result.stderr


def test_configured_scenario_ids_are_unique_across_scenarios(tmp_path):
    import yaml

    scenario = write_suite(tmp_path)
    scenario["scenario_id"] = "second"
    second = tmp_path / "scenarios" / "second"
    second.mkdir()
    (second / "scenario.yaml").write_text(yaml.safe_dump(scenario))
    result = run_validator(tmp_path)
    assert result.returncode == 1
    assert "Configured Scenarios: duplicate IDs" in result.stderr


def test_scenario_rejects_duplicate_checks_across_sets(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["check_sets"].append({"id": "duplicate", "checks": ["baseline"]})
    scenario["configured_scenarios"][0]["check_sets"].append("duplicate")
    write_scenario(tmp_path, scenario)
    result = run_validator(tmp_path)
    assert result.returncode == 1
    assert "duplicate values" in result.stderr


def test_scenario_rejects_qualified_check_id(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["check_sets"][0]["checks"][0] = "another-scenario.baseline"
    write_scenario(tmp_path, scenario)
    result = run_validator(tmp_path)
    assert result.returncode == 1
    assert "another-scenario.baseline" in result.stderr


def test_scenario_rejects_unused_check_set(tmp_path):

    scenario = write_suite(tmp_path)
    scenario["check_sets"].append({"id": "unused", "checks": ["unused"]})
    write_scenario(tmp_path, scenario)
    result = run_validator(tmp_path)
    assert result.returncode == 1
    assert "unused check sets ['unused']" in result.stderr


def test_duplicate_yaml_key_cannot_hide_authored_selection(tmp_path):
    write_suite(tmp_path)
    path = tmp_path / "scenarios/sample/scenario.yaml"
    path.write_text(path.read_text() + "\nconfigured_scenarios: []\n")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "duplicate YAML key" in result.stderr


def test_coverage_index_cannot_replace_authored_inventory(tmp_path):
    write_suite(tmp_path)
    destination = tmp_path / "coverage.yaml"
    before = destination.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--root",
            str(tmp_path),
            "--coverage-index",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert destination.read_bytes() == before


def test_shared_asset_is_explicit_and_confined(tmp_path):
    import hashlib

    import yaml

    scenario = write_suite(tmp_path)
    shared = tmp_path / "shared/references"
    shared.mkdir(parents=True)
    asset = shared / "common.md"
    asset.write_text("Shared public instructions.\n")
    scenario["steps"][0]["assets"] = [
        {
            "base": "shared",
            "path": "references/common.md",
            "audience": "scenario-operator",
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        }
    ]
    path = tmp_path / "scenarios/sample/scenario.yaml"
    path.write_text(yaml.safe_dump(scenario))
    command = [sys.executable, str(VALIDATOR), "--root", str(tmp_path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    scenario["steps"][0]["assets"][0]["path"] = "../coverage.yaml"
    path.write_text(yaml.safe_dump(scenario))
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 1
    assert "contained" in result.stderr


def test_shared_directory_cannot_redirect_outside_suite(tmp_path):
    import yaml

    scenario = write_suite(tmp_path)
    (tmp_path / "shared").symlink_to("/etc", target_is_directory=True)
    scenario["steps"][0]["assets"] = [
        {"base": "shared", "path": "passwd", "audience": "scenario-operator"}
    ]
    (tmp_path / "scenarios/sample/scenario.yaml").write_text(yaml.safe_dump(scenario))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "contained" in result.stderr
