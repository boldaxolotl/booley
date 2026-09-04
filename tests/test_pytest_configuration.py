"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]


def _test_workflow() -> dict:
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / "test.yml"
    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))


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
    parallel_step = next(step for step in test_steps if step.get("name") == "Run tests (parallel)")
    assert parallel_step["env"]["RUNNER_TEMP"] == "${{ runner.temp }}"
    assert "PYTEST_ADDOPTS" not in parallel_step["env"]
    assert '--basetemp "${{ runner.temp }}/pytest"' in parallel_step["run"]


def test_ci_uses_workstealing_for_windows_module_imbalance() -> None:
    """A large Windows-only module tail must not serialize one xdist worker."""
    workflow = _test_workflow()

    test_steps = workflow["jobs"]["test"]["steps"]
    parallel_step = next(step for step in test_steps if step.get("name") == "Run tests (parallel)")

    assert (
        parallel_step["run"]
        .replace("\n", " ")
        .startswith(
            "pytest tests/ -q --tb=short -n 4 "
            "--dist=${{ matrix.os == 'windows-latest' && 'worksteal' || 'loadscope' }}"
        )
    )


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
    assert selected == {
        "tests/bwave/test_contract.py::test_native_list_metadata_crosses_single_root_python_decoder",
        "tests/bwave/test_contract.py::test_native_list_metadata_crosses_multi_root_python_decoder",
        "tests/bwave/test_contract.py::test_trace_session_accepts_native_multi_root_store",
        "tests/bwave/test_contract.py::test_total_miss_is_exit_usage_plus_marker",
        "tests/bwave/test_contract.py::test_list_tree_stderr_carries_the_scope_line",
        "tests/bwave/test_contract.py::test_build_refuses_zero_signal_vcd",
        "tests/bwave/test_contract.py::test_empty_store_marker_survives_in_binary",
        "tests/bwave/test_contract.py::test_env_errors_stay_exit_env",
        "tests/bwave/test_sessions.py::test_query_uses_default_session",
        "tests/bwave/test_sessions.py::test_query_uses_named_alias",
        "tests/bwave/test_sessions.py::test_query_explicit_overrides_session",
        "tests/bwave/test_sessions.py::test_stale_session_warning",
        "tests/bwave/test_sessions.py::test_fresh_trace_with_old_registration_does_not_warn",
        "tests/bwave/test_sessions.py::test_register_reports_trace_identity_and_age",
    }


def test_generic_python_matrix_excludes_native_bwave() -> None:
    """Compatibility legs never install Rust or execute native B-Wave."""
    workflow = _test_workflow()
    test_steps = workflow["jobs"]["test"]["steps"]
    rendered_steps = "\n".join(str(step) for step in test_steps)

    assert "Swatinem/rust-cache" not in rendered_steps
    assert "cargo build" not in rendered_steps
    pytest_steps = [
        step for step in test_steps if str(step.get("name", "")).startswith("Run tests")
    ]
    assert pytest_steps
    assert all('-m "not native_bwave"' in step["run"] for step in pytest_steps)


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
    primary_jobs = ("test", "bwave-integration")

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
    workflow = _test_workflow()
    coverage_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Run tests with coverage"
    )
    command = coverage_step["run"]

    assert "pytest-cov==7.1.0" in dev_dependencies
    assert coverage_config["patch"] == ["subprocess"]
    assert "coverage run" not in command
    assert "pytest tests/" in command
    assert "-n 4 --dist=loadscope" in command
    assert '-m "not native_bwave"' in command
    assert "--cov=booley" in command
    assert "--cov-report=" in command
    assert coverage_config["branch"] is True
    assert "--cov-fail-under=0" in command
    rendered_steps = "\n".join(str(step) for step in workflow["jobs"]["test"]["steps"])
    assert "coverage report --fail-under=80" in rendered_steps
    assert "git fetch --no-tags --unshallow origin" in rendered_steps
    assert "diff-cover coverage.xml" in rendered_steps
    assert "--fail-under=90" in rendered_steps


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
    assert canary["env"]["DURATION_BUDGET_SECONDS"] == (
        "${{ needs.changes.outputs.riscv_image == 'true' && 1080 || 600 }}"
    )
    assert '--budget-seconds "${DURATION_BUDGET_SECONDS}"' in canary["run"]
    assert steps.index(canary) > next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Upload installed agent CLI policy evidence"
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
    ibex_run = next(
        step for step in steps if step.get("name") == "Run pinned Ibex lint demo"
    )
    restore = next(
        step for step in steps if step.get("name") == "Restore RISC-V demo checkout ownership"
    )
    upload = next(step for step in steps if step.get("name") == "Upload candidate RISC-V evidence")

    gate = "needs.changes.outputs.riscv_image == 'true'"
    assert prepare["if"] == gate
    assert steps.index(prepare) < group_index
    assert ibex_prepare["if"] == gate
    assert steps.index(ibex_prepare) < group_index
    assert riscv["if"] == gate
    assert "Dockerfile.riscv" in riscv["run"]
    assert "image_contract.py" in riscv["run"]
    assert "image_size_report.py" in riscv["run"]
    assert "verify_picorv32_demo.sh" in riscv["run"]
    assert ibex_run["if"] == gate
    assert steps.index(ibex_run) > group_index
    assert "--network none" in ibex_run["run"]
    assert restore["if"] == f"always() && {gate}"
    assert upload["if"] == f"always() && {gate}"
    assert steps.index(restore) > group_index
    assert steps.index(upload) > group_index


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
    """Keep a stuck compatibility leg within the fifteen-minute CI budget."""
    workflow = _test_workflow()
    test_job = workflow["jobs"]["test"]

    assert test_job["timeout-minutes"] == 15

    pytest_steps = [
        step for step in test_job["steps"] if str(step.get("name", "")).startswith("Run tests")
    ]
    assert pytest_steps
    assert all("--timeout=60" in step["run"] for step in pytest_steps)


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
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / "deep-tests.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["mutation"]["steps"]
    run_campaign = next(
        step["run"] for step in steps if step.get("name") == "Run bounded mutation campaign"
    )
    report_results = next(
        step["run"] for step in steps if step.get("name") == "Record mutation results"
    )

    assert "campaign_status" in run_campaign
    assert "!= 124" in run_campaign
    assert "non-killed mutants" not in report_results


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
        "Run simulator ground-truth tests",
        "Run cocotb Icarus/Verilator production-image flows",
        "Run Ticket Mode production-image smoke",
    }

    assert {step["name"] for step in validations} == expected_names
    assert all(not step.get("continue-on-error", False) for step in validations)
    temp_dirs = {step["env"]["VALIDATION_TMP"] for step in validations}
    assert len(temp_dirs) == len(validations)

    rendered = "\n".join(step["run"] for step in validations)
    assert rendered.count("--name booley-ci-${{ github.run_id }}-${{ github.run_attempt }}-") >= 4
    readonly_workspace = '--mount type=bind,src="${{ github.workspace }}",dst=/work,readonly'
    assert rendered.count(readonly_workspace) >= 4
    assert "native_fst_verilator_test.py" in rendered
    assert "simulator_ground_truth_test.py" in rendered
    assert "cd /validation-tmp/project" in rendered
    cleanup_wrapper = ".github/scripts/run_with_container_cleanup.sh"
    assert all(cleanup_wrapper in step["run"] for step in validations)

    wrapper = (REPOSITORY_ROOT / cleanup_wrapper).read_text(encoding="utf-8")
    assert 'setsid -- "$@" &' in wrapper
    assert "trap 'terminate INT 130' INT" in wrapper
    assert "trap 'terminate TERM 143' TERM" in wrapper
    assert "trap cleanup EXIT" in wrapper
    assert "docker rm -f" in wrapper

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
    conditional = set(jobs) - {"changes", "ci-required"}

    changes = jobs["changes"]
    rendered_changes = "\n".join(str(step) for step in changes["steps"])
    assert "fetch-depth" in rendered_changes
    assert ".github/scripts/ci_changes.py" in rendered_changes
    assert "github.event_name != 'pull_request'" in rendered_changes

    assert changes["outputs"]["jobs"] == "${{ steps.classify.outputs.jobs }}"
    for job_name in conditional:
        job = jobs[job_name]
        needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "changes" in needs, job_name
        assert job["if"] == f"fromJSON(needs.changes.outputs.jobs)['{job_name}']", job_name

    aggregate = jobs["ci-required"]
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == {"changes", *conditional}
    rendered_aggregate = "\n".join(str(step) for step in aggregate["steps"])
    assert ".github/scripts/ci_required.py" in rendered_aggregate
    assert "toJSON(needs)" in rendered_aggregate
