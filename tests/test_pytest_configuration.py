"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

import configparser
import ntpath
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
import warnings
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]


def _workflow(filename: str) -> dict:
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / filename
    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))


def _test_workflow() -> dict:
    return _workflow("test.yml")


def _deep_tests_workflow() -> dict:
    return _workflow("deep-tests.yml")


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _suite_config(pytestconfig: pytest.Config):
    config_path = Path(__file__).with_name("conftest.py")
    return next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if Path(getattr(plugin, "__file__", "")) == config_path
    )


def test_claude_sdk_floor_owns_windows_launcher_safety() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "claude-agent-sdk>=0.2.140" in project["project"]["dependencies"]


def test_development_wheel_can_build_without_repository_extras() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "build>=1.0" in project["project"]["dependencies"]


def test_windows_worker_temp_shares_workspace_drive(monkeypatch, pytestconfig) -> None:
    """FuseSoC cannot relativize a temp core across Windows drive letters."""
    suite_config = _suite_config(pytestconfig)
    workspace = Path("D:/workspace")
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    monkeypatch.setattr(suite_config.sys, "platform", "win32")
    monkeypatch.setattr(suite_config.tempfile, "tempdir", "C:/system-temp")
    monkeypatch.setattr(suite_config.Path, "cwd", lambda: workspace)

    worker_temp = suite_config._xdist_worker_temp_base()

    ntpath.relpath(str(worker_temp / "project.core"), str(workspace / "build"))


def test_ci_pytest_temp_uses_runner_volume() -> None:
    """pytest's controller temp must share the Windows checkout volume."""
    workflow = _test_workflow()

    test_steps = workflow["jobs"]["test"]["steps"]
    pytest_steps = [step for step in test_steps if "pytest tests/" in str(step.get("run", ""))]
    assert pytest_steps
    assert all(step["env"]["RUNNER_TEMP"] == "${{ runner.temp }}" for step in pytest_steps)
    assert all("PYTEST_ADDOPTS" not in step["env"] for step in pytest_steps)
    assert all('--basetemp "${{ runner.temp }}/pytest"' in step["run"] for step in pytest_steps)


def test_ci_uses_workstealing_for_windows_module_imbalance() -> None:
    """A large Windows-only module tail must not serialize one xdist worker."""
    workflow = _test_workflow()

    test_steps = workflow["jobs"]["test"]["steps"]
    parallel_step = next(
        step for step in test_steps if step.get("name") == "Run duration-balanced shard"
    )

    assert "pytest tests/ -q --tb=short -n 4 --dist=worksteal" in parallel_step["run"].replace(
        "\n", " "
    )
    assert "--ci-shard-count" in parallel_step["run"]


def test_sidecar_proofs_have_cancellation_cleanup() -> None:
    workflow = _test_workflow()
    step = next(
        candidate
        for candidate in workflow["jobs"]["sidecar-smoke"]["steps"]
        if candidate.get("name") == "Run sidecar behavior and hardening proofs"
    )

    assert step["env"]["BOOLEY_DOCKER_NAME_PREFIX"] == (
        "booley-ci-${{ github.run_id }}-${{ github.run_attempt }}-sidecar"
    )
    assert ".github/scripts/run_with_container_cleanup.sh" in step["run"]
    assert '"${BOOLEY_DOCKER_NAME_PREFIX}"' in step["run"]
    assert "tests/docker/test_sidecar_image_helpers.py" in step["run"]


_EXPECTED_NATIVE_BWAVE_NODES = {
    "tests/bwave/test_waveform_store_streaming.py::test_streaming_conversion_produces_queryable_store",
    "tests/bwave/test_waveform_store_streaming.py::test_trace_session_streams_into_its_own_cache_destination",
    "tests/bwave/test_cli.py::test_issue_1108_real_json_cap_preserves_native_warnings[limit0]",
    "tests/bwave/test_cli.py::test_issue_1108_real_json_cap_preserves_native_warnings[limit1]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra0]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra1]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra2]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra3]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra4]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra5]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra6]",
    "tests/bwave/test_cli.py::test_issue_1108_wrapper_preserves_native_input_errors[extra7]",
    "tests/bwave/test_contract.py::test_native_list_metadata_crosses_single_root_python_decoder",
    "tests/bwave/test_contract.py::test_native_list_metadata_crosses_multi_root_python_decoder",
    "tests/bwave/test_contract.py::test_trace_session_accepts_native_multi_root_store",
    "tests/bwave/test_contract.py::test_total_miss_is_exit_usage_plus_marker",
    "tests/bwave/test_contract.py::test_list_tree_stderr_carries_the_scope_line",
    "tests/bwave/test_contract.py::test_build_refuses_zero_signal_vcd",
    "tests/bwave/test_contract.py::test_empty_store_marker_survives_in_binary",
    "tests/bwave/test_contract.py::test_env_errors_stay_exit_env",
    "tests/bwave/test_contract.py::test_native_open_ended_range_includes_tail_clock_events",
    "tests/bwave/test_sessions.py::test_query_uses_default_session",
    "tests/bwave/test_sessions.py::test_query_uses_named_alias",
    "tests/bwave/test_sessions.py::test_query_explicit_overrides_session",
    "tests/bwave/test_sessions.py::test_stale_session_warning",
    "tests/bwave/test_sessions.py::test_fresh_trace_with_old_registration_does_not_warn",
    "tests/bwave/test_sessions.py::test_register_reports_trace_identity_and_age",
}


def test_native_bwave_marker_selects_only_real_binary_tests() -> None:
    """The native integration job owns every test that executes B-Wave."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "native_bwave",
            "tests/bwave",
        ],
        cwd=REPOSITORY_ROOT,
        env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PYTEST_XDIST_")
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    selected = {line for line in result.stdout.splitlines() if line.startswith("tests/")}
    assert selected == _EXPECTED_NATIVE_BWAVE_NODES


def test_generic_python_matrix_excludes_native_bwave() -> None:
    """Compatibility legs never install Rust or execute native B-Wave."""
    workflow = _test_workflow()
    test_steps = workflow["jobs"]["test"]["steps"]
    rendered_steps = "\n".join(str(step) for step in test_steps)

    assert "Swatinem/rust-cache" not in rendered_steps
    assert "cargo build" not in rendered_steps
    pytest_steps = [step for step in test_steps if "pytest " in str(step.get("run", ""))]
    assert pytest_steps
    assert "not native_bwave" in workflow["jobs"]["test"]["env"]["TEST_MARK_EXPRESSION"]
    assert all(
        '-m "not native_bwave"' in step["run"]
        or '-m "${{ env.TEST_MARK_EXPRESSION }}"' in step["run"]
        for step in pytest_steps
    )


def test_bwave_integration_prebuilds_and_runs_native_tests_without_skips() -> None:
    """The dedicated Linux job owns the binary and fails on a skipped test."""
    workflow = _test_workflow()
    job = workflow["jobs"]["bwave-integration"]
    rendered_steps = "\n".join(str(step) for step in job["steps"])

    assert job["runs-on"] == "ubuntu-latest"
    assert "cargo build --locked" in rendered_steps
    assert "test -x crates/bwave/target/debug/bwave" in rendered_steps
    assert "pytest crates/bwave/tests/test_*.py" in rendered_steps
    assert "pytest tests/ -m native_bwave" in rendered_steps
    assert rendered_steps.count(".github/scripts/assert_junit.py") == 2
    assert rendered_steps.count("--max-skips 0") == 2


def test_primary_pytest_commands_emit_timing_and_junit_data() -> None:
    """CI retains test-level evidence for later scheduling decisions."""
    workflow = _test_workflow()
    primary_jobs = ("test", "coverage-shards", "bwave-integration")

    for job_name in primary_jobs:
        pytest_commands = [
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if "pytest " in str(step.get("run", ""))
        ]
        assert pytest_commands, job_name
        assert all("--durations=30" in command for command in pytest_commands), job_name
        assert all("--junitxml=" in command for command in pytest_commands), job_name


def test_coverage_leg_combines_xdist_and_subprocess_coverage() -> None:
    """The coverage leg is parallel without dropping child-process data."""
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    coverage_config = project["tool"]["coverage"]["run"]
    coverage_paths = project["tool"]["coverage"]["paths"]["source"]
    workflow = _test_workflow()
    coverage_step = next(
        step
        for step in workflow["jobs"]["coverage-shards"]["steps"]
        if step.get("name") == "Run duration-balanced coverage shard"
    )
    command = coverage_step["run"]

    assert "pytest-cov==7.1.0" in dev_dependencies
    assert coverage_config["patch"] == ["subprocess"]
    assert "coverage run" not in command
    assert "pytest tests/" in command
    assert "-n 4 --dist=loadscope" in command
    assert '-m "${{ env.TEST_MARK_EXPRESSION }}"' in command
    assert "--cov=booley" in command
    assert "--cov-report=" in command
    assert "--ci-shard-count" in command
    assert coverage_config["branch"] is True
    assert coverage_config["relative_files"] is True
    assert coverage_paths == ["src/booley", "*/src/booley"]
    assert "--cov-fail-under=0" in command
    rendered_steps = "\n".join(str(step) for step in workflow["jobs"]["coverage"]["steps"])
    assert "coverage combine" in rendered_steps
    assert "coverage xml" in rendered_steps
    assert "coverage report --fail-under=80" in rendered_steps
    assert "git fetch --no-tags --unshallow origin" in rendered_steps
    assert "diff-cover coverage.xml" in rendered_steps
    assert "--fail-under=90" in rendered_steps
    changed_line_step = next(
        step
        for step in workflow["jobs"]["coverage"]["steps"]
        if step.get("name") == "Enforce 90% changed-line coverage"
    )
    assert changed_line_step["env"]["BASE_SHA"] == "${{ needs.changes.outputs.diff_base }}"


def test_image_pytest_commands_install_configured_plugins() -> None:
    """Image validations can load strict repository pytest configuration."""
    workflow = _test_workflow()
    validations = next(
        step["parallel"] for step in workflow["jobs"]["bwave-smoke"]["steps"] if "parallel" in step
    )

    for validation in validations:
        command = validation["run"]
        if "pytest" in command and "docker run" in command:
            assert "pytest-asyncio" in command, validation["name"]

    host_install = next(
        step
        for step in workflow["jobs"]["bwave-smoke"]["steps"]
        if step.get("name") == "Install host-side test dependencies"
    )
    assert "pytest==9.1.1" in host_install["run"]
    assert "pytest-asyncio" in host_install["run"]


def test_bwave_smoke_enforces_cached_path_duration_budget() -> None:
    """A cached image smoke regression fails before reaching the broad safety timeout."""
    workflow = _test_workflow()
    job = workflow["jobs"]["bwave-smoke"]
    steps = job["steps"]

    start = next(step for step in steps if step.get("name") == "Start duration budget clock")
    canary = next(step for step in steps if step.get("name") == "Enforce duration budget")

    assert job["timeout-minutes"] == 180
    assert "started_at_epoch=$(date +%s)" in start["run"]
    assert canary["if"] == "always() && steps.runtime-base.outputs.build != 'true'"
    assert ".github/scripts/ci_duration_budget.py" in canary["run"]
    assert canary["env"]["STANDARD_BUDGET_SECONDS"] == 600
    assert '--budget-seconds "${budget}"' in canary["run"]
    assert steps.index(canary) > next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Upload installed agent CLI policy evidence"
    )


def test_standard_size_ceiling_runs_without_riscv_gate() -> None:
    workflow = _test_workflow()
    steps = workflow["jobs"]["bwave-smoke"]["steps"]

    size_contract = next(
        step for step in steps if step.get("name") == "Enforce standard image size ceiling"
    )

    assert "if" not in size_contract
    assert "--runtime-image sandbox=booley-test" in size_contract["run"]
    assert "--limit-image sandbox" in size_contract["run"]
    assert "--limits .github/contracts/image-size-limits.toml" in size_contract["run"]


def test_riscv_measurement_dispatch_reaches_the_classifier() -> None:
    """Explicit timing arms must force the RISC-V lane, not only tune its cache."""
    workflow = _test_workflow()
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["riscv_measurement"]
    classify_step = next(
        step
        for step in workflow["jobs"]["changes"]["steps"]
        if step.get("name") == "Classify changed paths"
    )

    assert dispatch["options"] == ["automatic", "cold"]
    assert dispatch["default"] == "automatic"
    assert (
        "--riscv-measurement \"${{ inputs.riscv_measurement || 'automatic' }}\""
        in classify_step["run"]
    )


def test_riscv_image_lane_is_path_gated() -> None:
    """The slow derived-image contract runs only when its owning inputs change."""
    workflow = _test_workflow()
    assert workflow["jobs"]["changes"]["outputs"]["riscv_image"] == (
        "${{ steps.classify.outputs.riscv_image }}"
    )
    steps = workflow["jobs"]["bwave-smoke"]["steps"]
    group_index, group = next(
        (index, step) for index, step in enumerate(steps) if "parallel" in step
    )
    riscv = next(
        step
        for step in group["parallel"]
        if step.get("name") == "Run RISC-V candidate image contract"
    )
    prepare = next(
        step
        for step in steps
        if step.get("name") == "Prepare exact reviewed PicoRV32 candidate contract"
    )
    ibex_prepare = next(
        step for step in steps if step.get("name") == "Prepare exact reviewed Ibex candidate"
    )
    resolve = next(step for step in steps if step.get("id") == "riscv-tooling")
    ibex_run = next(step for step in steps if step.get("name") == "Run pinned Ibex lint demo")
    restore = next(
        step for step in steps if step.get("name") == "Restore RISC-V demo checkout ownership"
    )
    upload = next(step for step in steps if step.get("name") == "Upload candidate RISC-V evidence")

    gate = "needs.changes.outputs.riscv_image == 'true'"
    assert prepare["if"] == gate
    assert steps.index(prepare) < group_index
    assert ibex_prepare["if"] == gate
    assert steps.index(ibex_prepare) < group_index
    assert resolve["if"] == gate
    assert steps.index(resolve) < group_index
    assert riscv["if"] == gate
    assert "Dockerfile.riscv" in riscv["run"]
    assert "--progress rawjson" in riscv["run"]
    assert "riscv-tool-substrate-build.raw.jsonl" in riscv["run"]
    assert "wheel-overlay-build.raw.jsonl" in riscv["run"]
    assert "riscv-tool-substrate-metadata.json" in riscv["run"]
    assert "wheel-overlay-metadata.json" in riscv["run"]
    assert "verify_riscv_image_contract.sh" in riscv["run"]
    assert "run_picorv32_ci_demo.sh" in riscv["run"]
    assert '--build-context "riscv-tooling=${TOOLING_CONTEXT}"' in riscv["run"]
    assert ibex_run["if"] == gate
    assert steps.index(ibex_run) > group_index
    assert "--network none" in ibex_run["run"]
    assert restore["if"] == f"always() && {gate}"
    assert upload["if"] == f"always() && {gate}"
    assert steps.index(restore) > group_index


def test_riscv_timing_retains_all_validation_phases_and_parallel_lanes() -> None:
    workflow = _test_workflow()
    steps = workflow["jobs"]["bwave-smoke"]["steps"]
    group = next(step for step in steps if "parallel" in step)
    rendered_lanes = "\n".join(step["run"] for step in group["parallel"])
    helper = (REPOSITORY_ROOT / ".github/scripts/verify_riscv_image_contract.sh").read_text(
        encoding="utf-8"
    )
    demo = (REPOSITORY_ROOT / ".github/scripts/run_picorv32_ci_demo.sh").read_text(
        encoding="utf-8"
    )

    assert rendered_lanes.count("--topology parallel") == len(group["parallel"])
    timed_lanes = [
        step
        for step in group["parallel"]
        if step.get("name") != "Run RISC-V candidate image contract"
    ]
    assert all(
        step["run"].startswith("python .github/scripts/riscv_phase_metrics.py timed-run")
        for step in timed_lanes
    )
    assert "--name riscv_tool_substrate --topology nested" in rendered_lanes
    assert "--name wheel_overlay --topology nested" in rendered_lanes
    assert "--name image_contract_size_resources --topology nested" in rendered_lanes
    assert "--name picorv32_runtime_demo --topology nested" in rendered_lanes
    assert "image_contract.py" in helper
    assert "image_size_report.py" in helper
    assert "image_runtime_resources.py" in helper
    assert "verify_picorv32_demo.sh" in demo

    ibex = next(step for step in steps if step.get("name") == "Run pinned Ibex lint demo")
    finalizer = next(
        step for step in steps if step.get("name") == "Finalize RISC-V phase evidence"
    )
    upload = next(step for step in steps if step.get("name") == "Upload candidate RISC-V evidence")
    assert "--name ibex_runtime --topology post-group" in ibex["run"]
    assert "riscv_phase_metrics.py finalize" in finalizer["run"]
    assert "phases.json" in finalizer["run"]
    assert "--tooling-source" in finalizer["run"]
    assert steps.index(finalizer) < steps.index(upload)


def test_matrix_uses_test_only_dependencies() -> None:
    """Compatibility legs do not install linting or mutation-only packages."""
    workflow = _test_workflow()
    install_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Install package with test dependencies"
    )

    assert install_step["run"] == 'pip install -e ".[test]"'


def test_matrix_enforces_the_ci_duration_budget() -> None:
    """Bound full suites at twenty minutes and shard/compatibility legs at fifteen."""
    workflow = _test_workflow()
    test_job = workflow["jobs"]["test"]

    assert test_job["timeout-minutes"] == "${{ matrix.mode == 'full' && 20 || 15 }}"
    assert _workflow("full-python-matrix.yml")["jobs"]["test"]["timeout-minutes"] == 20

    pytest_steps = [step for step in test_job["steps"] if "pytest " in str(step.get("run", ""))]
    assert pytest_steps
    assert all("--timeout=60" in step["run"] for step in pytest_steps)


def test_pr_compatibility_matrix_is_pairwise() -> None:
    workflow = _test_workflow()
    jobs = workflow["jobs"]
    shard_input = workflow[True]["workflow_dispatch"]["inputs"]["windows_shard_count"]
    benchmark_input = workflow[True]["workflow_dispatch"]["inputs"]["windows_shard_benchmark"]

    assert jobs["test"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.changes.outputs.test_matrix) }}"
    )
    assert workflow["jobs"]["changes"]["outputs"]["test_matrix"] == (
        "${{ steps.classify.outputs.test_matrix }}"
    )
    assert shard_input["default"] == "8"
    assert shard_input["options"] == ["4", "6", "8"]
    assert benchmark_input["default"] is False
    classify_step = next(
        step for step in jobs["changes"]["steps"] if step.get("name") == "Classify changed paths"
    )
    assert "--windows-shard-benchmark" in classify_step["run"]


def test_windows_shards_are_exactly_verified() -> None:
    jobs = _test_workflow()["jobs"]
    test_verify_steps = jobs["test-verify"]["steps"]
    test_verify_download = next(
        step for step in test_verify_steps if "pattern" in step.get("with", {})
    )
    test_verifier = "\n".join(str(step) for step in test_verify_steps).replace("\n", " ")

    assert test_verify_download["with"]["pattern"] == "shard-windows-*"
    assert '--group windows --shard-count "${WINDOWS_SHARD_COUNT}"' in test_verifier
    assert "--group coverage" not in test_verifier
    verify_step = next(
        step
        for step in test_verify_steps
        if step.get("name") == "Prove every sharded test ran exactly once"
    )
    assert verify_step["env"]["WINDOWS_SHARD_COUNT"] == (
        "${{ inputs.windows_shard_count || '8' }}"
    )


def test_coverage_shards_have_dedicated_matrix() -> None:
    jobs = _test_workflow()["jobs"]
    coverage_shards = jobs["coverage-shards"]
    coverage_entries = coverage_shards["strategy"]["matrix"]["include"]

    assert coverage_entries == [
        {
            "name": f"ubuntu-3.13-coverage-{index + 1}-of-3",
            "python": "3.13",
            "group": "coverage",
            "shard_index": index,
            "shard_count": 3,
        }
        for index in range(3)
    ]
    assert coverage_shards["needs"] == "changes"
    assert coverage_shards["runs-on"] == "ubuntu-latest"
    assert coverage_shards["timeout-minutes"] == 15
    assert coverage_shards["strategy"]["fail-fast"] is False


def test_coverage_job_exactly_verifies_before_combining() -> None:
    jobs = _test_workflow()["jobs"]
    coverage = jobs["coverage"]
    coverage_steps = coverage["steps"]
    manifest_download = next(
        step
        for step in coverage_steps
        if step.get("with", {}).get("pattern") == "shard-coverage-*"
    )
    raw_download = next(
        step
        for step in coverage_steps
        if step.get("with", {}).get("pattern") == "coverage-shard-*"
    )
    coverage_verifier = next(
        step for step in coverage_steps if step.get("name") == "Verify coverage shards"
    )
    install = next(
        step for step in coverage_steps if step.get("name") == "Install coverage gate dependencies"
    )
    combine = next(
        step
        for step in coverage_steps
        if step.get("name") == "Combine raw coverage and enforce global ratchet"
    )

    assert coverage["needs"] == ["changes", "coverage-shards"]
    assert "test" not in coverage["needs"]
    assert "test-verify" not in coverage["needs"]
    assert manifest_download["with"]["path"] == "${{ runner.temp }}/shards"
    assert manifest_download["with"].get("merge-multiple", False) is False
    assert "--group coverage --shard-count 3" in coverage_verifier["run"].replace("\n", " ")
    assert raw_download["with"]["path"] == "${{ runner.temp }}/coverage"
    assert raw_download["with"]["merge-multiple"] is True
    assert coverage_steps.index(manifest_download) < coverage_steps.index(coverage_verifier)
    assert coverage_steps.index(coverage_verifier) < coverage_steps.index(install)
    assert coverage_steps.index(coverage_verifier) < coverage_steps.index(raw_download)
    assert coverage_steps.index(raw_download) < coverage_steps.index(combine)


def test_primary_pytest_jobs_share_evidence_publishing() -> None:
    jobs = _test_workflow()["jobs"]
    evidence_steps = {
        job_name: next(
            step
            for step in jobs[job_name]["steps"]
            if step.get("name") == "Publish pytest evidence"
        )
        for job_name in ("test", "coverage-shards")
    }

    assert all(step["if"] == "always()" for step in evidence_steps.values())
    assert all(
        step["uses"] == "./.github/actions/publish-pytest-evidence"
        for step in evidence_steps.values()
    )
    assert evidence_steps["test"]["with"]["publish-shard"] == "${{ matrix.mode == 'shard' }}"
    assert evidence_steps["coverage-shards"]["with"]["publish-shard"] == "true"

    action_path = REPOSITORY_ROOT / ".github/actions/publish-pytest-evidence/action.yml"
    rendered_action = action_path.read_text(encoding="utf-8")
    assert rendered_action.count("actions/upload-artifact@") == 3
    assert ".github/scripts/assert_junit.py" in rendered_action
    assert "always() && inputs.publish-shard == 'true'" in rendered_action


def test_ci_records_queue_and_runner_minutes() -> None:
    workflow = _test_workflow()
    metrics_job = workflow["jobs"]["ci-metrics"]

    assert workflow["permissions"]["actions"] == "read"
    assert metrics_job["needs"] == "ci-required"
    assert metrics_job["if"] == "always()"
    assert metrics_job["steps"][-1]["uses"] == "./.github/actions/collect-ci-metrics"

    action_path = REPOSITORY_ROOT / ".github/actions/collect-ci-metrics/action.yml"
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    rendered = "\n".join(str(step) for step in action["runs"]["steps"])
    assert ".github/scripts/ci_run_metrics.py" in rendered
    assert "/attempts/${GITHUB_RUN_ATTEMPT}/jobs?per_page=100" in rendered
    assert "GITHUB_STEP_SUMMARY" in rendered
    upload = next(
        step
        for step in action["runs"]["steps"]
        if step.get("name") == "Upload CI timing telemetry"
    )
    assert upload["with"]["retention-days"] == 90


def test_full_cartesian_matrix_remains_scheduled_and_manual() -> None:
    path = REPOSITORY_ROOT / ".github/workflows/full-python-matrix.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]

    assert "schedule" in workflow[True]
    assert "workflow_dispatch" in workflow[True]
    assert matrix["os"] == ["ubuntu-latest", "windows-latest"]
    assert matrix["python"] == ["3.11", "3.13", "3.14"]
    assert workflow["jobs"]["metrics"]["if"] == "always()"
    proof = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Prove the full suite executed"
    )
    assert "--min-tests 10000" in proof["run"]
    assert workflow["jobs"]["metrics"]["steps"][-1]["uses"] == (
        "./.github/actions/collect-ci-metrics"
    )


def test_exhaustive_recovery_marker_retains_a_pr_subset() -> None:
    paths = [
        "tests/ticket_board/test_acceptance_journal_recovery.py",
        "tests/ticket_board/test_completion.py",
    ]

    def collect(expression: str) -> set[str]:
        env = os.environ.copy()
        # A nested pytest process must not impersonate its parent xdist worker:
        # tests/conftest.py would otherwise delete the parent's private temp
        # root when the nested collection session exits.
        env.pop("PYTEST_XDIST_WORKER", None)
        env.pop("PYTEST_XDIST_TESTRUNUID", None)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", expression, *paths],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            env=env,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return {line for line in result.stdout.splitlines() if line.startswith("tests/")}

    exhaustive = collect("exhaustive_recovery")
    representative = collect("not exhaustive_recovery")

    assert len(exhaustive) == 32
    assert "test_retry_survives_every_semantic_checkpoint[before-normalized]" in "\n".join(
        representative
    )
    assert (
        "test_retry_survives_each_repository_boundary[before-project-candidate-preparation-False]"
        in "\n".join(representative)
    )


def test_lint_job_uses_quality_only_dependencies() -> None:
    """The fast lint job does not install test or mutation dependencies."""
    workflow = _test_workflow()
    install_step = next(
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if step.get("name") == "Install pinned CI quality tools"
    )

    assert install_step["run"] == 'pip install -e ".[quality]"'


def test_scheduled_mutation_campaign_treats_its_time_budget_as_success() -> None:
    """A bounded scheduled campaign reports incomplete mutants without failing."""
    job = _deep_tests_workflow()["jobs"]["mutation"]
    run_campaign = _named_step(job, "Run bounded mutation campaign")["run"]
    report_results = _named_step(job, "Record mutation results")["run"]

    assert "campaign_status" in run_campaign
    assert "!= 124" in run_campaign
    assert "non-killed mutants" not in report_results


def test_scheduled_mutation_campaign_runs_mutmut_as_a_module() -> None:
    """Spawned test helpers must not re-execute the mutmut console script.

    A spawn child re-runs a console-script ``__main__`` from the test's
    temporary working directory, where mutmut cannot find its configuration.
    """
    job = _deep_tests_workflow()["jobs"]["mutation"]
    run_campaign = _named_step(job, "Run bounded mutation campaign")["run"]

    assert "python -m mutmut run" in run_campaign
    assert re.search(r"(?<!-m )\bmutmut run\b", run_campaign) is None


def _tracked_repository_paths() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def _mutmut_config() -> dict[str, list[str]]:
    """Read setup.cfg's ``[mutmut]`` lists the way mutmut's own reader does."""
    parser = configparser.ConfigParser()
    parser.read(REPOSITORY_ROOT / "setup.cfg", encoding="utf-8")
    return {
        key: [line for line in value.split("\n") if line] for key, value in parser.items("mutmut")
    }


def test_mutmut_configuration_lives_only_in_setup_cfg() -> None:
    """pyproject.toml bytes define the Sandbox base contract; mutmut stays out.

    mutmut reads setup.cfg only when pyproject.toml has no ``[tool.mutmut]``
    table, so a reintroduced table would silently shadow the real campaign
    configuration as well as rebuild the base image on every tuning edit.
    """
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "mutmut" not in project.get("tool", {})
    assert _mutmut_config()["only_mutate"] == ["src/booley/harness/setup/*.py"]
    assert _mutmut_config()["pytest_add_cli_args_test_selection"] == ["tests/harness/"]


def test_mutation_sandbox_mirrors_every_tracked_checkout_entry() -> None:
    """mutmut's ``mutants/`` copy stays a faithful Booley source checkout.

    The copied pyproject marks ``mutants/`` as a source checkout, so importing
    Booley there needs ``VERSION``, and harness tests read docs, workflows and
    Sandbox Image build inputs. A tracked path that mutmut neither mutates nor
    copies breaks the scheduled campaign before it tests a single mutant.
    """
    mutmut = _mutmut_config()
    # mutmut always copies these alongside the configured source paths.
    copied = [
        *mutmut["source_paths"],
        "tests/",
        "setup.cfg",
        "pyproject.toml",
        *mutmut["also_copy"],
    ]

    def covered(path: str) -> bool:
        return any(
            path.startswith(entry) if entry.endswith("/") else path == entry for entry in copied
        )

    missing = [path for path in _tracked_repository_paths() if not covered(path)]
    assert missing == [], (
        "mutmut would not copy these tracked paths into mutants/; add their "
        "top-level entry to setup.cfg [mutmut] also_copy"
    )


def test_mutation_sandbox_copy_order_and_build_output_exclusion() -> None:
    """Every ``also_copy`` entry is copyable in order and skips Cargo outputs."""
    also_copy = _mutmut_config()["also_copy"]
    tracked = _tracked_repository_paths()

    for index, entry in enumerate(also_copy):
        # Each entry names tracked content, never an untracked build tree.
        if entry.endswith("/"):
            assert any(path.startswith(entry) for path in tracked), entry
        else:
            assert entry in tracked, entry
        # shutil.copy2 does not create parents; an earlier directory copy must.
        parent = Path(entry.rstrip("/")).parent.as_posix()
        if not entry.endswith("/") and parent != ".":
            assert any(
                earlier.endswith("/") and earlier.startswith(f"{parent}/")
                for earlier in also_copy[:index]
            ), entry

    build_outputs = ("crates/bwave/target/", "crates/bwave/fuzz/target/")
    for entry in also_copy:
        assert not any(output.startswith(entry) for output in build_outputs), entry


def test_bwave_differential_installs_the_simulators_it_must_not_skip() -> None:
    """The zero-skip differential gate needs Icarus and Verilator present."""
    job = _deep_tests_workflow()["jobs"]["bwave-fuzz"]
    names = [step.get("name") for step in job["steps"]]
    install = _named_step(job, "Install differential oracle simulators")["run"]
    assertion = _named_step(job, "Assert differential tests executed without skips")["run"]

    assert "iverilog" in install
    assert "verilator" in install
    # The Verilator oracle compiles its model with g++ directly.
    assert "g++" in install
    assert "--max-skips 0" in assertion
    assert names.index("Install differential oracle simulators") < names.index(
        "Run existing simulator oracle differential suite"
    )


def test_image_validations_run_in_an_isolated_native_parallel_group() -> None:
    """Production-image checks overlap without sharing writable state."""
    workflow = _test_workflow()
    steps = workflow["jobs"]["bwave-smoke"]["steps"]
    group = next(step for step in steps if "parallel" in step)
    validations = [
        step
        for step in group["parallel"]
        if step.get("name") != "Run RISC-V candidate image contract"
    ]
    expected_names = {
        "Run OpenROAD physical runtime probe",
        "Run installed agent CLI policy probe",
        "Run Verible production-image end-to-end test",
        "Run sandbox isolation suite",
        "Run FIFO pipeline smoke test",
        "Run native FST/Verilator cross-validation",
        "Run Verilator compiler and native coverage acceptance",
        "Run Coverage Campaign release matrix and production collector",
        "Run simulator ground-truth tests",
        "Run cocotb Icarus/Verilator production-image flows",
        "Run Goal Mode production-image smoke",
    }

    assert {step["name"] for step in validations} == expected_names
    assert all(not step.get("continue-on-error", False) for step in validations)
    temp_dirs = {step["env"]["VALIDATION_TMP"] for step in validations}
    assert len(temp_dirs) == len(validations)

    rendered = "\n".join(step["run"] for step in validations)
    assert rendered.count("--name booley-ci-${{ github.run_id }}-${{ github.run_attempt }}-") >= 4
    readonly_workspace = '--mount type=bind,src="${{ github.workspace }}",dst=/work,readonly'
    assert rendered.count(readonly_workspace) >= 4
    coverage = next(step for step in validations if step["name"].startswith("Run Coverage"))
    assert "test_verilator_release_matrix.py" in coverage["run"]
    assert "test_verilator_coverage_collector_smoke.py" in coverage["run"]
    assert "test_verilator_compiler_cache_smoke.py" in coverage["run"]
    assert "assert_junit.py" in coverage["run"]
    assert "--min-tests 21 --max-skips 0" in coverage["run"]
    goal_mode = next(step for step in validations if step["name"].startswith("Run Goal Mode"))
    assert "BOOLEY_GOAL_MODE_SMOKE=1" in goal_mode["run"]
    assert "BOOLEY_GOAL_MODE_PREVIEW=1" in goal_mode["run"]
    assert "test_goal_mode_image_smoke.py" in goal_mode["run"]
    assert "native_fst_verilator_test.py" in rendered
    assert "simulator_ground_truth_test.py" in rendered
    assert "cd /validation-tmp/project" in rendered
    cleanup_wrapper = ".github/scripts/run_with_container_cleanup.sh"
    assert all(cleanup_wrapper in step["run"] for step in validations)
    for step in validations:
        tokens = shlex.split(step["run"])
        assert tokens.index(cleanup_wrapper) > tokens.index("--")
        assert "&&" not in tokens


def test_image_validation_cleanup_wrapper_terminates_containers() -> None:
    cleanup_wrapper = ".github/scripts/run_with_container_cleanup.sh"
    wrapper = (REPOSITORY_ROOT / cleanup_wrapper).read_text(encoding="utf-8")
    assert 'setsid -- "$@" &' in wrapper
    assert "trap 'terminate INT 130' INT" in wrapper
    assert "trap 'terminate TERM 143' TERM" in wrapper
    assert "trap cleanup EXIT" in wrapper
    assert "docker rm -f" in wrapper


def test_image_validation_parallel_group_has_cleanup() -> None:
    workflow = _test_workflow()
    steps = workflow["jobs"]["bwave-smoke"]["steps"]
    cleanup = next(
        step for step in steps if step.get("name") == "Clean up image validation containers"
    )
    assert cleanup["if"] == "always()"
    assert "docker rm -f" in cleanup["run"]
    assert "github.run_id" in cleanup["run"]
    assert "github.run_attempt" in cleanup["run"]


@pytest.mark.skipif(os.name == "nt", reason="exercises the Linux CI process-group wrapper")
def test_image_validation_wrapper_preserves_failure_and_cleans_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-child EXIT boundary cleans Docker state without hiding failure."""
    docker_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "${DOCKER_LOG}"\n'
        'if [[ "$1" == "ps" ]]; then printf "container-id\\n"; fi\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    monkeypatch.setenv("DOCKER_LOG", str(docker_log))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    result = subprocess.run(
        [
            str(REPOSITORY_ROOT / ".github/scripts/run_with_container_cleanup.sh"),
            "booley-ci-123-1-fifo",
            "bash",
            "-c",
            "exit 7",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
    )

    assert result.returncode == 7
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "ps -aq --filter name=^/booley-ci-123-1-fifo",
        "rm -f container-id",
    ]


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX signals and process groups")
def test_image_validation_wrapper_cleans_promptly_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM interrupts wait, kills an ignoring child group, and cleans Docker."""
    docker_log = tmp_path / "docker.log"
    child_pid_path = tmp_path / "child.pid"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "${DOCKER_LOG}"\n'
        'if [[ "$1" == "ps" ]]; then printf "container-id\\n"; fi\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    monkeypatch.setenv("DOCKER_LOG", str(docker_log))
    monkeypatch.setenv("CHILD_PID_PATH", str(child_pid_path))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    process = subprocess.Popen(
        [
            str(REPOSITORY_ROOT / ".github/scripts/run_with_container_cleanup.sh"),
            "booley-ci-123-1-fifo",
            "bash",
            "-c",
            'echo $$ > "$CHILD_PID_PATH"; trap "" INT TERM; sleep 30',
        ],
        cwd=REPOSITORY_ROOT,
    )
    for _ in range(200):
        if child_pid_path.exists():
            break
        time.sleep(0.01)
    assert child_pid_path.exists(), "wrapped child did not start"

    process.terminate()
    assert process.wait(timeout=5) == 143
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid_path.read_text(encoding="utf-8")), 0)
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls[-2:] == [
        "ps -aq --filter name=^/booley-ci-123-1-fifo",
        "rm -f container-id",
    ]


def test_change_aware_jobs_feed_an_always_running_aggregate() -> None:
    """Conditional jobs never leave the stable required check unresolved."""
    workflow = _test_workflow()
    jobs = workflow["jobs"]
    conditional = set(jobs) - {
        "changes",
        "release-semantic",
        "ci-required",
        "ci-metrics",
    }

    changes = jobs["changes"]
    rendered_changes = "\n".join(str(step) for step in changes["steps"])
    assert "fetch-depth" in rendered_changes
    assert ".github/scripts/ci_changes.py" in rendered_changes
    assert "github.event_name != 'pull_request'" in rendered_changes
    classify_step = next(
        step for step in changes["steps"] if step.get("name") == "Classify changed paths"
    )
    assert classify_step["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert '--event-name "${EVENT_NAME}"' in classify_step["run"]

    assert changes["outputs"]["jobs"] == "${{ steps.classify.outputs.jobs }}"
    for job_name in conditional:
        job = jobs[job_name]
        needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "changes" in needs, job_name
        assert job["if"] == f"fromJSON(needs.changes.outputs.jobs)['{job_name}']", job_name

    semantic = jobs["release-semantic"]
    assert semantic["needs"] == "changes"
    assert semantic["timeout-minutes"] == 2

    aggregate = jobs["ci-required"]
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == {"changes", "release-semantic", *conditional}
    rendered_aggregate = "\n".join(str(step) for step in aggregate["steps"])
    assert ".github/scripts/ci_required.py" in rendered_aggregate
    assert "toJSON(needs)" in rendered_aggregate

    metrics = jobs["ci-metrics"]
    assert metrics["needs"] == "ci-required"
    assert metrics["if"] == "always()"


def _apply_suite_warning_filters() -> None:
    """Install pyproject's pytest warning filters the way pytest applies them."""
    from _pytest.config import parse_warning_filter

    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for spec in project["tool"]["pytest"]["ini_options"]["filterwarnings"]:
        warnings.filterwarnings(*parse_warning_filter(spec, escape=False))


def test_tool_owned_sqlite_finalizer_warning_is_ignored() -> None:
    """A late-finalized coverage.py SQLite connection must not fail a green shard."""
    observed = (
        "Exception ignored in: <sqlite3.Connection object at 0x7f6240eee890>\n"
        "Enable tracemalloc to get traceback where the object was allocated."
    )
    with warnings.catch_warnings():
        _apply_suite_warning_filters()
        warnings.warn(pytest.PytestUnraisableExceptionWarning(observed), stacklevel=1)


def test_other_unraisable_exceptions_still_fail_the_suite() -> None:
    """The SQLite ignore must stay narrow: other leaked finalizers remain errors."""
    with warnings.catch_warnings():
        _apply_suite_warning_filters()
        with pytest.raises(pytest.PytestUnraisableExceptionWarning):
            warnings.warn(
                pytest.PytestUnraisableExceptionWarning(
                    "Exception ignored in: <_io.FileIO name='x' mode='rb' closefd=True>"
                ),
                stacklevel=1,
            )


def test_booley_never_uses_sqlite() -> None:
    """The SQLite unraisable ignore is safe only while Booley owns no connections."""
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for root in ("src", "tests")
        for path in (REPOSITORY_ROOT / root).rglob("*.py")
        if re.search(r"^\s*(import|from)\s+sqlite3\b", path.read_text(encoding="utf-8"), re.M)
    ]

    assert offenders == []


def test_windows_timeout_diagnostics_are_retained_after_failure() -> None:
    workflow = _test_workflow()
    action = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/actions/publish-pytest-evidence/action.yml").read_text(
            encoding="utf-8"
        )
    )
    assert workflow["jobs"]["test"]["env"]["PYTHONFAULTHANDLER"] == "1"
    directory = "${{ runner.temp }}/pytest-timeouts"
    pytest_steps = [
        step
        for step in workflow["jobs"]["test"]["steps"]
        if "pytest tests/" in str(step.get("run", ""))
        or "pytest tests/architecture" in str(step.get("run", ""))
    ]
    assert len(pytest_steps) == 3
    assert all(step["env"]["BOOLEY_PYTEST_EVIDENCE_DIR"] == directory for step in pytest_steps)
    assert "BOOLEY_PYTEST_EVIDENCE_DIR" not in workflow["jobs"]["test"]["env"]
    uploads = [
        step
        for step in action["runs"]["steps"]
        if step.get("with", {}).get("path") == directory + "/"
    ]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["if-no-files-found"] == "ignore"


def test_scheduled_windows_matrix_retains_crash_diagnostics() -> None:
    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/full-python-matrix.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    execution = next(step for step in steps if "pytest tests/" in str(step.get("run", "")))
    assert execution["env"]["PYTHONFAULTHANDLER"] == "1"
    assert execution["env"]["BOOLEY_PYTEST_EVIDENCE_DIR"] == "${{ runner.temp }}/pytest-timeouts"
    uploads = [
        step
        for step in steps
        if step.get("with", {}).get("path") == "${{ runner.temp }}/pytest-timeouts/"
    ]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["if-no-files-found"] == "ignore"


def test_windows_pytest_legs_enforce_timeout_headroom(pytestconfig: pytest.Config) -> None:
    """Windows timeouts kill the worker silently, so CI fails tests that drift near one."""
    guard = "${{ runner.os == 'Windows' && '--timeout-headroom=0.5' || '' }}"
    primary = _test_workflow()["jobs"]["test"]["steps"]
    scheduled = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/full-python-matrix.yml").read_text(encoding="utf-8")
    )["jobs"]["test"]["steps"]
    pytest_steps = [
        step for step in [*primary, *scheduled] if str(step.get("run", "")).startswith("pytest ")
    ]

    assert len(pytest_steps) == 4
    assert all(guard in step["run"] for step in pytest_steps)
    assert "tests.timeout_headroom" in _suite_config(pytestconfig).pytest_plugins


@pytest.mark.parametrize(
    ("options", "environment_timeout", "expected"),
    [
        ([], None, "120s"),
        (["--timeout=0"], None, "0.0s"),
        (["--timeout=600"], None, "600.0s"),
        (["-o", "timeout=0"], None, "0.0s"),
        (["-o", "timeout=900"], None, "900.0s"),
        ([], "0", "0.0s"),
        ([], "600", "600.0s"),
        (["--timeout=60", "-o", "timeout=900"], "600", "60.0s"),
    ],
)
def test_suite_default_timeout_preserves_explicit_settings(
    options: list[str], environment_timeout: str | None, expected: str
) -> None:
    """The 120 s fallback must preserve explicit CLI, environment, and ini settings."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")
    }
    if environment_timeout is not None:
        environment["PYTEST_TIMEOUT"] = environment_timeout
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "tests/test_timeout_headroom.py::test_guard_is_off_without_the_option",
            *options,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    if expected == "0.0s":
        assert "\ntimeout:" not in result.stdout
    else:
        assert f"\ntimeout: {expected}\n" in result.stdout


def test_verilator_image_acceptance_declares_its_tool_sized_ceiling() -> None:
    """Its tests allow 300 s tool calls, so they must not inherit the 120 s default."""
    runs = [
        run
        for run in _nested_runs(_test_workflow()["jobs"]["bwave-smoke"])
        if "test_verilator_compiler_cache_smoke" in run
    ]

    assert len(runs) == 1
    assert "--timeout=900" in runs[0]


def _nested_runs(node: object) -> list[str]:
    """Return every ``run`` script, including steps inside parallel groups."""
    if isinstance(node, list):
        return [run for child in node for run in _nested_runs(child)]
    if not isinstance(node, dict):
        return []
    own = [node["run"]] if isinstance(node.get("run"), str) else []
    return own + [
        run for key, child in node.items() if key != "run" for run in _nested_runs(child)
    ]
