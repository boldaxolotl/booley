"""Exercise the public suite-validation interface with disposable authored suites."""

import json
import subprocess
import sys
from pathlib import Path

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
        "format_version: 1\ncapabilities:\n- id: H-01\n  title: Install\n"
        "  sources: [https://example.com/contract]\n  contract: Exact release\n"
        "  applicability: All profiles\n"
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


def write_suite(root):
    """Copy the independent literal authoring example into disposable storage."""
    import shutil

    import yaml

    shutil.copytree(Path(__file__).with_name("fixtures"), root, dirs_exist_ok=True)
    return (
        yaml.safe_load((root / "scenarios/sample/scenario.yaml").read_text()),
        yaml.safe_load((root / "profiles.yaml").read_text()),
    )


def test_prerequisites_reject_later_checks_and_step_ids(tmp_path):
    import yaml

    scenario, _ = write_suite(tmp_path)
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

    scenario, _ = write_suite(tmp_path)
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


def test_profile_must_select_supporting_checks(tmp_path):
    import yaml

    _, profiles = write_suite(tmp_path)
    profiles["profiles"][0]["runs"][0]["checks"].remove("sample.baseline")
    (tmp_path / "profiles.yaml").write_text(yaml.safe_dump(profiles))
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

    scenario, _ = write_suite(tmp_path)
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

    scenario, _ = write_suite(tmp_path)
    (tmp_path / "scenarios/sample/escape").symlink_to("/etc/passwd")
    scenario["steps"][0]["assets"] = [{"path": "escape", "audience": "coordinator"}]
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
            "id": "H-02",
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
    assert "H-02" in result.stderr


def test_profile_schema_rejects_misspelled_provider(tmp_path):
    import yaml

    _, profiles = write_suite(tmp_path)
    profiles["profiles"][0]["runs"][0]["provder"] = "Claude"
    (tmp_path / "profiles.yaml").write_text(yaml.safe_dump(profiles))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "provder" in result.stderr


def test_coverage_index_is_derived_after_validation(tmp_path):
    write_suite(tmp_path)
    destination = tmp_path / "index.json"
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
    assert index["H-01"]["checks"] == [
        "sample.baseline",
        "sample.negative",
        "sample.removed",
        "sample.restored",
    ]
    assert index["H-01"]["profiles"] == ["core"]


def test_required_check_cannot_disappear_from_all_profiles(tmp_path):
    import yaml

    _, profiles = write_suite(tmp_path)
    profiles["profiles"][0]["runs"][0]["checks"].remove("sample.removed")
    profiles["profiles"][0]["runs"][0]["supporting_steps"].remove("cleanup")
    (tmp_path / "profiles.yaml").write_text(yaml.safe_dump(profiles))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "sample.removed" in result.stderr
