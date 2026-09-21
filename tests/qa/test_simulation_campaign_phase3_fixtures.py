"""Protect Phase 3 Simulation Campaign Public QA fixtures and contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TAXI = ROOT / "qa/scenarios/taxi/fixtures/simulation-campaign"
UART = ROOT / "qa/scenarios/uart/fixtures/simulation-campaign"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {' '.join(argv)}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _require_tools(*names: str) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        pytest.skip("missing required executable prerequisites: " + ", ".join(missing))


def test_owned_targets_and_policy_fragments_are_explicit() -> None:
    taxi_core = yaml.safe_load((TAXI / "campaign.core").read_text().split("\n", 1)[1])
    taxi_target = taxi_core["targets"]["sim_campaign_verilator"]
    assert taxi_target["default_tool"] == "verilator"
    assert taxi_target["toplevel"] == "campaign_tb"
    assert '[sim_campaign_verilator]\ntests = ["first", "second"]' in (
        TAXI / "tests.toml"
    ).read_text()

    uart_core = yaml.safe_load((UART / "campaign.core").read_text().split("\n", 1)[1])
    uart_files = uart_core["filesets"]["tb"]["files"]
    assert {next(iter(item)): item[next(iter(item))]["copyto"] for item in uart_files[1:]} == {
        "vectors/alpha.hex": "inputs/alpha.hex",
        "vectors/beta.hex": "inputs/beta.hex",
    }
    assert 'run_cwd = ".booley-qa/campaign/{campaign}/{test}/{attempt}"' in (
        UART / "runtime-inputs.toml"
    ).read_text()
    assert 'pre_sim_build_access = "immutable"' in (
        UART / "immutable-presim.toml"
    ).read_text()
    assert 'pre_sim_build_access = "legacy-per-test"' in (
        UART / "legacy-presim.toml"
    ).read_text()


def test_taxi_validator_requires_one_shared_build(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    build = tmp_path / "build-result.json"
    results = [tmp_path / "first.json", tmp_path / "second.json"]
    _write_json(
        manifest,
        {
            "work_items": [
                {"selection": {"names": ["first"]}},
                {"selection": {"names": ["second"]}},
            ]
        },
    )
    _write_json(
        build,
        {
            "$schema": "booley.bundle-build-result/v1",
            "state": "ready",
            "bundle": {"bundle_id": "bundle-1", "sharing": "shared_variant"},
        },
    )
    digest = "sha256:" + hashlib.sha256(build.read_bytes()).hexdigest()
    for index, (path, test) in enumerate(zip(results, ("first", "second"), strict=True)):
        _write_json(
            path,
            {
                "attempt_id": f"attempt-{index}",
                "build_result": {"sharing": "shared_variant", "sha256": digest},
                "observations": [{"test": test}],
            },
        )
    validator = _module("taxi_campaign_validator", TAXI / "validate_bundle.py")
    assert validator.validate(manifest, build, results, ["first", "second"])[
        "compile_count"
    ] == 1
    document = json.loads(results[1].read_text())
    document["build_result"]["sharing"] = "private_work_item"
    _write_json(results[1], document)
    with pytest.raises(ValueError, match="private build"):
        validator.validate(manifest, build, results, ["first", "second"])


def test_uart_validator_covers_isolation_immutable_and_legacy(tmp_path: Path) -> None:
    validator = _module("uart_campaign_validator", UART / "validate_campaign.py")
    manifest = tmp_path / "manifest.json"
    attempts = [tmp_path / "attempt-alpha.json", tmp_path / "attempt-beta.json"]
    results = [tmp_path / "result-alpha.json", tmp_path / "result-beta.json"]
    _write_json(
        manifest,
        {
            "workload": {
                "run_cwd": {"kind": "templated"},
                "pre_sim_build_access": "immutable",
            }
        },
    )
    for index, (attempt, result) in enumerate(zip(attempts, results, strict=True)):
        attempt_id = f"attempt-{index}"
        _write_json(
            attempt,
            {
                "attempt_id": attempt_id,
                "pre_sim_build_access": "immutable",
                "run_directory": {"owned": True, "resolved": f"run-{index}"},
            },
        )
        _write_json(
            result,
            {
                "attempt_id": attempt_id,
                "build_result": {"sharing": "shared_variant", "sha256": "sha256:shared"},
                "runtime_inputs": [
                    {"authoritative_copy": {"owner": attempt_id}, "destination": "input.hex"}
                ],
            },
        )
    validator.validate_runtime_isolation(manifest, attempts, results)

    environment = tmp_path / "environment.json"
    immutable_result = tmp_path / "immutable-result.json"
    _write_json(environment, {"BOOLEY_RUN_CWD": "run-0"})
    _write_json(immutable_result, {"grade": "fail", "state": "setup_error"})
    validator.validate_immutable_failure(attempts[0], immutable_result, environment)
    _write_json(environment, {"BOOLEY_BUILD_ROOT": "secret-build"})
    with pytest.raises(ValueError, match="disclosed build root"):
        validator.validate_immutable_failure(attempts[0], immutable_result, environment)

    _write_json(
        manifest,
        {
            "workload": {
                "run_cwd": {"kind": "templated"},
                "pre_sim_build_access": "legacy-per-test",
            }
        },
    )
    for index, (attempt, result) in enumerate(zip(attempts, results, strict=True)):
        document = json.loads(attempt.read_text())
        document["pre_sim_build_access"] = "legacy-per-test"
        _write_json(attempt, document)
        _write_json(
            result,
            {
                "build_result": {
                    "sharing": "private_work_item",
                    "build_attempt_id": f"build-{index}",
                    "sha256": f"sha256:private-{index}",
                }
            },
        )
    validator.validate_legacy_builds(manifest, attempts, results)


def test_public_checks_have_product_regression_backlinks() -> None:
    backlinks = {
        "campaign.verilator-single-build": (
            "tests/flows/sim/test_campaign_phase3.py",
            "test_shareable_variant_compiles_once_and_isolates_attempt_runtime_inputs",
        ),
        "campaign.runtime-input-isolation": (
            "tests/flows/sim/test_campaign_phase3.py",
            "test_shareable_variant_compiles_once_and_isolates_attempt_runtime_inputs",
        ),
        "campaign.presim-immutable": (
            "tests/flows/sim/test_campaign_phase3_integrity.py",
            "test_immutable_hook_compile_surface_mutation_stops_before_snapshot_launch",
        ),
        "campaign.presim-legacy-build": (
            "tests/flows/sim/test_campaign_phase3_integrity.py",
            "test_legacy_mode_builds_and_discloses_each_work_item_privately",
        ),
    }
    for _check, (relative, test_name) in backlinks.items():
        source = ROOT / relative
        assert source.is_file()
        assert f"def {test_name}" in source.read_text(encoding="utf-8")


def test_campaign_checks_are_isolated_to_representative_configurations() -> None:
    expectations = {
        "taxi": (
            "taxi-simulation-campaign",
            ["campaign.verilator-single-build"],
            ["taxi-ubuntu-codex-cli"],
        ),
        "uart": (
            "uart-simulation-campaign",
            [
                "campaign.runtime-input-isolation",
                "campaign.presim-immutable",
                "campaign.presim-legacy-build",
            ],
            ["uart-ubuntu-codex-cli"],
        ),
    }
    for scenario_name, (set_id, checks, expected_selected) in expectations.items():
        scenario = yaml.safe_load(
            (ROOT / f"qa/scenarios/{scenario_name}/scenario.yaml").read_text()
        )
        check_set = next(item for item in scenario["check_sets"] if item["id"] == set_id)
        assert check_set["checks"] == checks
        selected = [
            item["id"]
            for item in scenario["configured_scenarios"]
            if set_id in item["check_sets"]
        ]
        assert selected == expected_selected


def test_taxi_fixture_compiles_once_with_real_verilator(tmp_path: Path) -> None:
    _require_tools("verilator", "make", "g++")
    build = tmp_path / "build"
    compile_calls = [
        [
            "verilator",
            "--binary",
            "--build",
            "--timing",
            "--top-module",
            "campaign_tb",
            "--Mdir",
            str(build),
            str(TAXI / "campaign_tb.sv"),
        ]
    ]
    _run(compile_calls[0], tmp_path)
    binary = build / "Vcampaign_tb"
    before = hashlib.sha256(binary.read_bytes()).hexdigest()
    for name in ("first", "second"):
        result = _run([str(binary), f"+test={name}"], tmp_path)
        assert f"QA_VERILATOR_CAMPAIGN_TEST={name}" in result.stdout.replace(" ", "")
        assert "[SIM_RESULT] PASSED" in result.stdout
    assert len(compile_calls) == 1
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == before


def test_uart_fixture_compiles_once_with_real_icarus(tmp_path: Path) -> None:
    _require_tools("iverilog", "vvp")
    image = tmp_path / "campaign.vvp"
    compile_calls = [
        [
            "iverilog",
            "-g2012",
            "-s",
            "campaign_tb",
            "-o",
            str(image),
            str(UART / "campaign_tb.sv"),
        ]
    ]
    _run(compile_calls[0], tmp_path)
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    for name, expected in (("alpha", "000000a1"), ("beta", "000000b2")):
        run = tmp_path / name
        inputs = run / "inputs"
        inputs.mkdir(parents=True)
        shutil.copyfile(UART / f"vectors/{name}.hex", inputs / f"{name}.hex")
        result = _run(["vvp", str(image), f"+test={name}"], run)
        assert f"QA_UART_RUNTIME_INPUT={name}:{expected}" in result.stdout.replace(" ", "")
        assert "[SIM_RESULT] PASSED" in result.stdout
    assert len(compile_calls) == 1
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
