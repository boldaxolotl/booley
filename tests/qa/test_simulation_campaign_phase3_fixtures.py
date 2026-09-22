"""Protect Phase 3 Simulation Campaign Public QA fixtures and contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest

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


def _require_python_modules(*names: str) -> None:
    missing = [name for name in names if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip("missing required Python module prerequisites: " + ", ".join(missing))


def _owned_project(tmp_path: Path, fixture: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    project_config = project / ".booley_project"
    project_config.mkdir()
    shutil.copyfile(project / "tests.toml", project_config / "tests.toml")
    return project


def _instrument_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiler: str,
) -> Path:
    executable = shutil.which(compiler)
    assert executable is not None
    wrappers = tmp_path / "compiler-wrappers"
    wrappers.mkdir()
    log = tmp_path / f"{compiler}-calls.txt"
    wrapper = wrappers / compiler
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {shlex.quote(compiler)} >> \"$BOOLEY_QA_COMPILER_LOG\"\n"
        f"exec {shlex.quote(executable)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrappers}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOLEY_QA_COMPILER_LOG", str(log))
    return log


def _run_owned_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fixture: Path,
    target: str,
    tests: tuple[str, ...],
    compiler: str,
) -> tuple[Path, Path]:
    project = _owned_project(tmp_path, fixture)
    report_root = tmp_path / "reports"
    compiler_log = _instrument_compiler(tmp_path, monkeypatch, compiler)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    result = SimulateFlow().execute(
        SimRequest(
            target=target,
            work_dir=project,
            report_dir=report_root,
            test=tests,
            timeout_ms=120_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    campaigns = tuple(report_root.glob(f"sim/*/targets/{target}/campaign"))
    assert len(campaigns) == 1
    return campaigns[0], compiler_log


def _assert_shared_campaign(
    campaign: Path,
    compiler_log: Path,
    *,
    compiler: str,
    tests: tuple[str, ...],
) -> None:
    assert compiler_log.read_text(encoding="utf-8").splitlines() == [compiler]
    manifest_path = campaign / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = "sha256:" + hashlib.sha256(
        manifest_path.read_bytes().rstrip(b"\n")
    ).hexdigest()
    assert [item["selection"]["names"] for item in manifest["work_items"]] == [
        [name] for name in tests
    ]

    build_results = tuple(campaign.glob("build-variants/*/attempts/*/build-result.json"))
    build_attempts = tuple(campaign.glob("build-variants/*/attempts/*/build-attempt.json"))
    assert len(build_results) == len(build_attempts) == 1
    build_path = build_results[0]
    build = json.loads(build_path.read_text(encoding="utf-8"))
    build_sha256 = "sha256:" + hashlib.sha256(build_path.read_bytes()).hexdigest()
    bundle_path = build_path.parent / build["bundle"]["manifest_path"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert build["state"] == "ready"
    assert build["manifest_sha256"] == bundle["manifest_sha256"] == manifest_sha256
    assert build["bundle"]["sharing"] == bundle["sharing"] == "shared_variant"
    assert build["bundle"]["bundle_id"] == bundle["bundle_id"]

    results = tuple(campaign.glob("work-items/*/result.json"))
    attempts = tuple(campaign.glob("work-items/*/attempts/*/attempt.json"))
    assert len(results) == len(attempts) == len(tests)
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in results]
    assert {item["manifest_sha256"] for item in documents} == {manifest_sha256}
    assert {item["build_result"]["sha256"] for item in documents} == {build_sha256}
    assert {item["build_result"]["sharing"] for item in documents} == {"shared_variant"}
    assert {item["bundle_id"] for item in documents} == {bundle["bundle_id"]}
    assert len({item["attempt_id"] for item in documents}) == len(tests)


def test_taxi_fixture_runs_real_verilator_simulation_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_tools("verilator", "make", "g++")
    _require_python_modules("fusesoc", "edalize")
    campaign, compiler_log = _run_owned_campaign(
        tmp_path,
        monkeypatch,
        fixture=TAXI,
        target="sim_campaign_verilator",
        tests=("first", "second"),
        compiler="verilator",
    )
    _assert_shared_campaign(
        campaign,
        compiler_log,
        compiler="verilator",
        tests=("first", "second"),
    )


def test_uart_fixture_runs_real_icarus_simulation_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_tools("iverilog", "vvp", "make")
    _require_python_modules("fusesoc", "edalize")
    campaign, compiler_log = _run_owned_campaign(
        tmp_path,
        monkeypatch,
        fixture=UART,
        target="sim_campaign_uart",
        tests=("alpha", "beta"),
        compiler="iverilog",
    )
    _assert_shared_campaign(
        campaign,
        compiler_log,
        compiler="iverilog",
        tests=("alpha", "beta"),
    )


def test_owned_targets_and_policy_fragments_are_explicit() -> None:
    taxi_core = yaml.safe_load((TAXI / "campaign.core").read_text().split("\n", 1)[1])
    taxi_target = taxi_core["targets"]["sim_campaign_verilator"]
    assert taxi_core["filesets"]["tb"]["files"] == [
        "campaign_tb.sv",
        {"campaign_main.cpp": {"file_type": "cppSource"}},
    ]
    assert taxi_target["default_tool"] == "verilator"
    assert taxi_target["flow_options"] == {
        "tool": "verilator",
        "verilator_options": ["--timing"],
    }
    assert taxi_target["toplevel"] == "campaign_tb"
    assert (
        '[sim_campaign_verilator]\n'
        'tests = ["first", "second", "slow-first", "slow-fail", "slow-last"]'
        in (TAXI / "tests.toml").read_text()
    )

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
    _write_isolated_attempts(manifest, attempts, results)
    validator.validate_runtime_isolation(manifest, attempts, results)
    _assert_immutable_validation(tmp_path, validator, attempts[0])
    _write_legacy_attempts(manifest, attempts, results)
    validator.validate_legacy_builds(manifest, attempts, results)


def _write_isolated_attempts(manifest, attempts, results) -> None:
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


def _assert_immutable_validation(tmp_path, validator, attempt) -> None:
    environment = tmp_path / "environment.json"
    immutable_result = tmp_path / "immutable-result.json"
    _write_json(environment, {"BOOLEY_RUN_CWD": "run-0"})
    _write_json(immutable_result, {"grade": "fail", "state": "setup_error"})
    validator.validate_immutable_failure(attempt, immutable_result, environment)
    _write_json(environment, {"BOOLEY_BUILD_ROOT": "secret-build"})
    with pytest.raises(ValueError, match="disclosed build root"):
        validator.validate_immutable_failure(attempt, immutable_result, environment)


def _write_legacy_attempts(manifest, attempts, results) -> None:
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
            [
                "campaign.verilator-single-build",
                "campaign.heavy-cap",
                "campaign.attempt-isolation",
                "campaign.continue-after-failure",
                "campaign.cocotb-batch-resume",
                "campaign.mcp-structured-pointers",
            ],
            ["taxi-ubuntu-codex-cli"],
        ),
        "uart": (
            "uart-simulation-campaign",
            [
                "campaign.runtime-input-isolation",
                "campaign.literal-cwd-serialization",
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


def test_taxi_direct_verilator_smoke_is_supplementary(tmp_path: Path) -> None:
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


def test_uart_direct_icarus_smoke_is_supplementary(tmp_path: Path) -> None:
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
