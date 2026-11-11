"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

import configparser
import fnmatch
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

from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

REPOSITORY_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / ".github/scripts"))

from ci_changes import (  # pyright: ignore[reportMissingImports]
    CATEGORIES,
    build_test_matrix,
    required_jobs,
)


def _workflow(filename: str) -> dict:
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / filename
    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))


def _test_workflow() -> dict:
    return _workflow("test.yml")


def _deep_tests_workflow() -> dict:
    return _workflow("deep-tests.yml")


_RUNNER_OS_CHOICE = re.compile(r"\$\{\{ runner\.os == '(\w+)' && (.+?) \|\| (.+?) \}\}")
_LINUX_SCHEDULER_OUTPUT = "needs.changes.outputs.linux_scheduler"
# Six coverage shards keep a shard plus the combine step off the queue's critical path.
COVERAGE_SHARD_COUNT = 6


def _render_expressions(run: str, runner_os: str, scheduler: str) -> str:
    """Resolve the scheduler output and the `runner.os == X && A || B` choices."""

    def operand(text: str) -> str:
        return scheduler if text == _LINUX_SCHEDULER_OUTPUT else text.strip("'")

    def choose(match: re.Match[str]) -> str:
        return operand(match[2] if match[1] == runner_os else match[3])

    resolved = _RUNNER_OS_CHOICE.sub(choose, run)
    return resolved.replace("${{ " + _LINUX_SCHEDULER_OUTPUT + " }}", scheduler)


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _uses_step(job: dict, action: str) -> dict:
    """Return the first step that runs the given action (`uses` prefix, e.g. 'actions/checkout@')."""
    return next(step for step in job["steps"] if str(step.get("uses", "")).startswith(action))


def _setup_step_limits(job: dict) -> list[int | None]:
    """Step limits of checkout, setup-python and the package install, in that order."""
    return [
        _uses_step(job, "actions/checkout@").get("timeout-minutes"),
        _uses_step(job, "actions/setup-python@").get("timeout-minutes"),
        _named_step(job, "Install package with test dependencies").get("timeout-minutes"),
    ]


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

    windows_command = _render_expressions(parallel_step["run"], "Windows", "loadscope")

    assert "pytest tests/ -q --tb=short -n 4 --dist=worksteal" in windows_command.replace(
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
    "tests/bwave/test_waveform_store_streaming.py::test_declared_stream_builds_queryable_store",
    "tests/bwave/test_waveform_store_streaming.py::test_reopened_stream_fails_instead_of_merging",
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
    "tests/bwave/test_contract.py::test_value_past_the_trace_end_is_exit_usage_plus_marker",
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
    "tests/bwave/test_scope_relative_globs.py::test_relative_glob_selects_the_list_tree[list]",
    "tests/bwave/test_scope_relative_globs.py::test_relative_glob_selects_the_list_tree[value]",
    "tests/bwave/test_scope_relative_globs.py::test_relative_glob_selects_the_list_tree[find]",
    "tests/bwave/test_scope_relative_globs.py::test_relative_glob_selects_the_list_tree[sample]",
    "tests/bwave/test_scope_relative_globs.py::test_relative_glob_selects_the_list_tree[stats]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_resolves_relative_glob",
    "tests/bwave/test_scope_relative_globs.py::test_glob_total_miss_suggests_anchored_form[u_core.*]",
    "tests/bwave/test_scope_relative_globs.py::test_glob_total_miss_suggests_anchored_form[tb.*]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_total_miss_suggests_anchored_form",
    "tests/bwave/test_scope_relative_globs.py::test_anchored_globs_select_as_before[TOP.tb.dut.*]",
    "tests/bwave/test_scope_relative_globs.py::test_anchored_globs_select_as_before[*dut.*]",
    "tests/bwave/test_scope_relative_globs.py::test_bare_name_suffix_match_unchanged",
    "tests/bwave/test_scope_relative_globs.py::test_gui_keeps_anchored_and_bare_matching[TOP.tb.dut.*-expected0]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_keeps_anchored_and_bare_matching[*dut.*-expected1]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_keeps_anchored_and_bare_matching[bus-expected2]",
}


def test_native_bwave_marker_selects_only_real_binary_tests() -> None:
    """The native integration job owns every test that executes B-Wave."""
    result = run_bounded_child(
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
        timeout=SUBPROCESS_TIMEOUT_S,
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
    assert "coverage==7.17.0" in dev_dependencies
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
    # The sysmon core and the Linux timing model are what make one instrumented
    # 3.14 run cheaper than the plain 3.14 shards plus separate coverage shards.
    assert workflow["jobs"]["coverage-shards"]["env"]["COVERAGE_CORE"] == "sysmon"
    assert "--ci-timing-model .github/ci/linux-test-timings.json" in " ".join(command.split())
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


def _requirement_names(relative: str) -> set[str]:
    """Return the distribution names a CI requirements file installs, following nested ``-r``."""
    path = REPOSITORY_ROOT / relative
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("-r "):
            nested = (path.parent / line.removeprefix("-r ").strip()).relative_to(REPOSITORY_ROOT)
            names |= _requirement_names(nested.as_posix())
        elif line:
            names.add(re.split(r"[=<>!~\s]", line, maxsplit=1)[0].lower())
    return names


def test_image_pytest_commands_install_configured_plugins() -> None:
    """Image validations can load strict repository pytest configuration."""
    workflow = _test_workflow()
    validations = next(
        step["parallel"] for step in workflow["jobs"]["bwave-smoke"]["steps"] if "parallel" in step
    )
    plain = ".github/ci/requirements-runners.txt"
    release = ".github/ci/requirements-runners-release.txt"

    for validation in validations:
        command = validation["run"]
        if "pytest" in command and "docker run" in command:
            expected = release if "coverage-release" in command else plain
            assert f"pip install --break-system-packages -r /work/{expected}" in command, (
                validation["name"]
            )
            required = {"pytest", "pytest-asyncio"}
            if expected == release:
                required |= {"pytest-timeout", "hypothesis"}
            assert required <= _requirement_names(expected), validation["name"]

    host_install = next(
        step
        for step in workflow["jobs"]["bwave-smoke"]["steps"]
        if step.get("name") == "Install host-side test dependencies"
    )
    assert f"pip install -r {plain}" in host_install["run"]
    assert {"pytest", "pytest-asyncio"} <= _requirement_names(plain)


def test_bwave_smoke_enforces_cached_path_duration_budget() -> None:
    """A cached image smoke regression fails before reaching the broad safety timeout."""
    workflow = _test_workflow()
    job = workflow["jobs"]["bwave-smoke"]
    steps = job["steps"]

    start = next(step for step in steps if step.get("name") == "Start test-phase duration clock")
    canary = next(step for step in steps if step.get("name") == "Enforce duration budget")

    assert job["timeout-minutes"] == 180
    assert "started_at_epoch=$(date +%s)" in start["run"]
    assert canary["if"] == (
        "always() && steps.runtime-base.outputs.build != 'true' && "
        "steps.duration-clock.outcome == 'success'"
    )
    assert ".github/scripts/ci_duration_budget.py" in canary["run"]
    assert canary["env"]["STANDARD_BUDGET_SECONDS"] == 670
    assert canary["env"]["STANDARD_WARN_SECONDS"] == 640
    assert '--budget-seconds "${STANDARD_BUDGET_SECONDS}"' in canary["run"]
    assert '--warn-seconds "${STANDARD_WARN_SECONDS}"' in canary["run"]
    assert steps.index(canary) > next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Upload installed agent CLI policy evidence"
    )


def _bwave_smoke_clock_seconds(*, build_seconds: int, validation_seconds: int) -> int:
    """Return the budget-clock reading for a synthetic run of the real step order.

    Every top-level ``Build …`` step takes ``build_seconds``, the parallel
    validation group takes ``validation_seconds``, and every other step 1 s.
    """
    steps = _test_workflow()["jobs"]["bwave-smoke"]["steps"]
    names = [step.get("name", "") for step in steps]
    clock = names.index("Start test-phase duration clock")
    enforce = names.index("Enforce duration budget")

    def duration(step: dict) -> int:
        if "parallel" in step:
            return validation_seconds
        return build_seconds if step.get("name", "").startswith("Build ") else 1

    return sum(duration(step) for step in steps[clock + 1 : enforce])


@pytest.mark.parametrize(
    ("build_seconds", "validation_seconds", "passed"),
    [(900, 450, True), (60, 700, False)],
    ids=["slow-build-normal-tests", "fast-build-slow-tests"],
)
def test_bwave_smoke_budget_times_tests_not_image_builds(
    build_seconds: int, validation_seconds: int, *, passed: bool
) -> None:
    """Runner build variance cannot fail the budget; a test-time regression does."""
    from ci_duration_budget import evaluate  # pyright: ignore[reportMissingImports]

    budget_step = next(
        step
        for step in _test_workflow()["jobs"]["bwave-smoke"]["steps"]
        if step.get("name") == "Enforce duration budget"
    )
    elapsed = _bwave_smoke_clock_seconds(
        build_seconds=build_seconds, validation_seconds=validation_seconds
    )
    result = evaluate(
        "bwave-smoke",
        started_at=1_000,
        now=1_000 + elapsed,
        budget=budget_step["env"]["STANDARD_BUDGET_SECONDS"],
        warn=budget_step["env"]["STANDARD_WARN_SECONDS"],
    )

    assert result.passed is passed


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
    """The slow derived-image contract runs only when its owning inputs change.

    It runs as its own job, so bwave-smoke keeps no RISC-V step, and the job
    is selected through the classifier's job policy like every other lane.
    """
    workflow = _test_workflow()
    assert workflow["jobs"]["changes"]["outputs"]["riscv_image"] == (
        "${{ steps.classify.outputs.riscv_image }}"
    )
    job = workflow["jobs"]["riscv-image"]
    assert job["if"] == "fromJSON(needs.changes.outputs.jobs)['riscv-image']"
    assert job["needs"] == ["changes", "build-dist"]
    assert "riscv-image" in workflow["jobs"]["ci-required"]["needs"]

    smoke = yaml.safe_dump(workflow["jobs"]["bwave-smoke"])
    for marker in ("riscv", "RISC-V", "ibex", "picorv32"):
        assert marker not in smoke, marker

    steps = job["steps"]
    names = [step.get("name") for step in steps]
    riscv = steps[names.index("Run RISC-V candidate image contract")]
    ibex_run = steps[names.index("Run pinned Ibex lint demo")]
    assert names.index("Prepare exact reviewed PicoRV32 candidate contract") < names.index(
        "Run RISC-V candidate image contract"
    )
    assert names.index("Prepare exact reviewed Ibex candidate") < names.index(
        "Run pinned Ibex lint demo"
    )
    assert "Dockerfile.riscv" in riscv["run"]
    assert "verify_riscv_image_contract.sh" in riscv["run"]
    assert "run_picorv32_ci_demo.sh" in riscv["run"]
    assert '--build-context "riscv-tooling=${TOOLING_CONTEXT}"' in riscv["run"]
    assert "--network none" in ibex_run["run"]
    for name in ("Restore RISC-V demo checkout ownership", "Upload candidate RISC-V evidence"):
        assert steps[names.index(name)]["if"] == "always()"
    helper = (REPOSITORY_ROOT / ".github/scripts/verify_riscv_image_contract.sh").read_text(
        encoding="utf-8"
    )
    demo = (REPOSITORY_ROOT / ".github/scripts/run_picorv32_ci_demo.sh").read_text(
        encoding="utf-8"
    )

    assert "image_contract.py" in helper
    assert "image_size_report.py" in helper
    assert "image_runtime_resources.py" in helper
    assert "verify_picorv32_demo.sh" in demo


def test_matrix_uses_test_only_dependencies() -> None:
    """Compatibility legs do not install linting or mutation-only packages."""
    workflow = _test_workflow()
    install_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Install package with test dependencies"
    )

    assert install_step["run"] == 'pip install -e ".[test]" -c .github/ci/constraints.txt'


def test_matrix_enforces_the_ci_duration_budget() -> None:
    """Bound full suites at thirty minutes and shard/compatibility legs at twenty-five."""
    workflow = _test_workflow()
    test_job = workflow["jobs"]["test"]

    assert test_job["timeout-minutes"] == "${{ matrix.mode == 'full' && 30 || 25 }}"
    assert _workflow("full-python-matrix.yml")["jobs"]["test"]["timeout-minutes"] == 30

    pytest_steps = [step for step in test_job["steps"] if "pytest " in str(step.get("run", ""))]
    assert pytest_steps
    assert all("--timeout=60" in step["run"] for step in pytest_steps)


def test_test_job_bounds_setup_and_pytest_steps() -> None:
    """Setup steps get 5/5/6 minutes in every mode; shard and compatibility pytest get 12."""
    job = _test_workflow()["jobs"]["test"]

    assert _setup_step_limits(job) == [5, 5, 6]
    for name in ("Run focused Windows compatibility suite", "Run duration-balanced shard"):
        assert _named_step(job, name).get("timeout-minutes") == 12
    # `full` keeps the job-level 30-minute bound instead of a step limit.
    assert "timeout-minutes" not in _named_step(job, "Run full compatibility suite")


def test_flake_stress_repeats_changed_modules_on_linux_and_windows() -> None:
    """Changed test modules repeat across workers on both OS families before merge."""
    workflow = _test_workflow()
    job = workflow["jobs"]["flake-stress"]
    classify = _named_step(workflow["jobs"]["changes"], "Classify changed paths")
    stress = _named_step(job, "Repeat changed test modules")
    command = " ".join(stress["run"].split())
    shard = _named_step(workflow["jobs"]["test"], "Run duration-balanced shard")

    assert classify["env"]["HEAD_REF"] == "${{ github.head_ref }}"
    assert '--head-ref "${HEAD_REF}"' in classify["run"]
    assert {(leg["os"], leg["python"]) for leg in job["strategy"]["matrix"]["include"]} == {
        ("ubuntu-latest", "3.14"),
        ("windows-latest", "3.14"),
    }
    assert stress["shell"] == "bash"
    assert stress["env"]["STRESS_MODULES"] == "${{ needs.changes.outputs.stress_modules }}"
    assert stress["env"]["STRESS_COUNT"] == "${{ needs.changes.outputs.stress_count }}"
    assert '"${modules[@]}"' in command
    assert '--count="${STRESS_COUNT}"' in command
    assert "-n 4 --dist=load " in command
    assert "--timeout=60" in command
    # Only exit 5 (every module skipped itself, e.g. Linux-only on Windows) is tolerated.
    assert '-ne 0 ] && [ "${status}" -ne 5 ]' in command
    assert stress["env"]["HEADROOM"] in shard["run"]  # same Windows headroom gate
    assert _named_step(job, "Publish pytest evidence")["uses"] == (
        "./.github/actions/publish-pytest-evidence"
    )


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
    assert shard_input["default"] == "4"
    assert shard_input["options"] == ["4", "6", "8", "10"]
    assert benchmark_input["default"] is False
    classify_step = next(
        step for step in jobs["changes"]["steps"] if step.get("name") == "Classify changed paths"
    )
    assert "--windows-shard-benchmark" in classify_step["run"]
    assert "${{ inputs.windows_shard_count || '4' }}" in classify_step["run"]


def test_ci_mark_expressions_never_deselect_sandbox_only_tests() -> None:
    """Sandbox-only tests skip on Windows only; no `-m` expression may drop them anywhere."""
    from tests.sandbox_only import SANDBOX_ONLY_PATHS, is_sandbox_only

    workflows = (REPOSITORY_ROOT / ".github" / "workflows").glob("*.yml")
    mentions = [path.name for path in workflows if "sandbox_only" in path.read_text("utf-8")]
    assert not mentions, f"workflows must not select on sandbox_only: {mentions}"
    compatibility = _named_step(
        _test_workflow()["jobs"]["test"], "Run focused Windows compatibility suite"
    )
    paths = [word for word in shlex.split(compatibility["run"]) if word.startswith("tests/")]
    assert paths
    # A compatibility leg whose paths are all skipped on Windows would pass while testing nothing.
    # A test file is judged by the registry (its Windows exceptions included); a directory
    # overlaps when any registry entry sits inside it or contains it.
    skipped = [
        path
        for path in paths
        if (
            is_sandbox_only(path)
            if path.endswith(".py")
            else any(
                path.startswith(entry) or entry.startswith(path.rstrip("/") + "/")
                for entry in SANDBOX_ONLY_PATHS
            )
        )
    ]
    assert not skipped, f"Windows compatibility paths overlap the Sandbox-only registry: {skipped}"


def test_main_push_tier_keeps_a_nightly_full_run_on_main() -> None:
    """Main pushes run a narrow tier; the nightly full run must exist and outlive them."""
    workflow = _test_workflow()
    classify = _named_step(workflow["jobs"]["changes"], "Classify changed paths")
    group = workflow["concurrency"]["group"]

    def rendered(event: str) -> str:
        return group.replace("${{ github.event_name }}", event).replace(
            "${{ github.ref }}", "refs/heads/main"
        )

    assert workflow[True]["schedule"]
    assert "${{" not in rendered("push")
    assert rendered("push") != rendered("schedule")
    assert classify["env"]["REF"] == "${{ github.ref }}"
    assert '--ref "${REF}"' in classify["run"]


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
        "${{ inputs.windows_shard_count || '4' }}"
    )


def test_coverage_shards_have_dedicated_matrix() -> None:
    jobs = _test_workflow()["jobs"]
    coverage_shards = jobs["coverage-shards"]
    coverage_entries = coverage_shards["strategy"]["matrix"]["include"]

    assert coverage_entries == [
        {
            "name": f"ubuntu-3.14-coverage-{index + 1}-of-{COVERAGE_SHARD_COUNT}",
            "python": "3.14",
            "group": "coverage",
            "shard_index": index,
            "shard_count": COVERAGE_SHARD_COUNT,
        }
        for index in range(COVERAGE_SHARD_COUNT)
    ]
    assert coverage_shards["needs"] == "changes"
    assert coverage_shards["runs-on"] == "ubuntu-latest"
    assert coverage_shards["strategy"]["fail-fast"] is False
    # The coverage shards are the only Ubuntu 3.14 suite, so they must not
    # also run in the plain `test` matrix.
    assert not any(
        entry["os"] == "ubuntu-latest" and entry["python"] == "3.14"
        for entry in build_test_matrix(10)["include"]
    )


def test_coverage_shards_bound_setup_and_pytest_steps() -> None:
    """Coverage shards share the setup limits; the job limit fits setup plus pytest."""
    job = _test_workflow()["jobs"]["coverage-shards"]
    pytest_limit = _named_step(job, "Run duration-balanced coverage shard").get("timeout-minutes")

    assert _setup_step_limits(job) == [5, 5, 6]
    assert pytest_limit == 15
    purge_limit = _named_step(job, "Drop pip cache restored from an older dependency set").get(
        "timeout-minutes"
    )
    assert purge_limit == 3
    # 38 = setup 5 + 5 + 3 + 6 + pytest 15, plus margin for the always-run publish/upload steps.
    assert job["timeout-minutes"] == 38


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
    # The verifier proves exactly the selections the coverage-shards matrix produced.
    assert f"--group coverage --shard-count {COVERAGE_SHARD_COUNT}" in coverage_verifier[
        "run"
    ].replace("\n", " ")
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
    assert rendered_action.count("actions/upload-artifact@") == 4
    assert ".github/scripts/assert_junit.py" in rendered_action
    assert "always() && inputs.publish-shard == 'true'" in rendered_action
    assert "always() && inputs.publish-timing == 'true'" in rendered_action
    assert evidence_steps["test"]["with"]["publish-timing"] == "${{ matrix.mode == 'full' }}"
    assert "publish-timing" not in evidence_steps["coverage-shards"]["with"]


def test_complete_suites_emit_phase_timing_without_sharding() -> None:
    steps = _test_workflow()["jobs"]["test"]["steps"]
    full = next(step for step in steps if step.get("name") == "Run full compatibility suite")
    command = " ".join(full["run"].split())

    assert full["if"] == "matrix.mode == 'full'"
    assert full["env"]["PYTHONPATH"] == ".github/scripts"
    assert "-p ci_pytest_shard" in command
    assert '--ci-timing-output "${{ runner.temp }}/timing/test-timings.json"' in command
    for sharding_option in (
        "--ci-shard-count",
        "--ci-shard-index",
        "--ci-shard-group",
        "--ci-shard-manifest",
        "--ci-timing-model",
    ):
        assert sharding_option not in command


def test_ci_records_queue_and_runner_minutes() -> None:
    workflow = _test_workflow()
    metrics_job = workflow["jobs"]["ci-metrics"]

    assert workflow["permissions"]["actions"] == "read"
    assert metrics_job["needs"] == "ci-required"
    assert metrics_job["if"] == "always() && needs.ci-required.result != 'skipped'"
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


def test_lint_job_uses_quality_only_dependencies() -> None:
    """The fast lint job does not install test or mutation dependencies."""
    workflow = _test_workflow()
    install_step = next(
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if step.get("name") == "Install pinned CI quality tools"
    )

    assert install_step["run"] == 'pip install -e ".[quality]" -c .github/ci/constraints.txt'


def test_actionlint_download_retries_and_still_verifies_its_checksum() -> None:
    """A 504 from the release CDN is retried; the retried archive is still checksum-verified."""
    lint_steps = _test_workflow()["jobs"]["lint"]["steps"]
    install = next(
        step for step in lint_steps if str(step.get("name", "")).startswith("Install actionlint")
    )
    script = " ".join(install["run"].replace("\\\n", " ").split())

    assert "--retry 5 --retry-all-errors --retry-delay 5" in script
    assert "sha256sum --check --strict" in script
    assert script.index("curl ") < script.index("sha256sum") < script.index("tar ")


def _assert_worktree_mounts_disk_and_failure_gating(job: dict) -> None:
    """#2896: free disk before the build, never skip the preflight, gate the evidence steps."""
    names = [item.get("name") for item in job["steps"]]
    step = _named_step(job, "Run required worktree mount cases")
    assert "Free unused runner disk" in names
    free_disk = _named_step(job, "Free unused runner disk")
    assert free_disk["uses"] == "./.github/actions/free-runner-disk"
    assert "if" not in free_disk
    assert names.index(free_disk["name"]) < names.index("Build canonical Sandbox Image")
    deep_tests_text = (REPOSITORY_ROOT / ".github/workflows/deep-tests.yml").read_text(
        encoding="utf-8"
    )
    assert "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT" not in deep_tests_text
    # A failed build leaves only the build step red: the evidence steps key off
    # which steps ran instead of `always()`.
    assert _named_step(job, "Build canonical Sandbox Image")["id"] == "build-image"
    assert step["id"] == "worktree-pytest"
    upload = _named_step(job, "Upload worktree execution evidence")
    assert upload["if"] == "always() && steps.build-image.outcome != 'skipped'"


def test_scheduled_worktree_mounts_requires_real_docker_execution() -> None:
    workflow = _deep_tests_workflow()
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}
    job = workflow["jobs"]["worktree-mounts"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 180
    assert job["env"]["BUILDX_BUILDER"] == "default"
    producer = _named_step(job, "Build canonical Sandbox Image")["run"]
    assert "bash src/booley/data/docker/build.sh" in producer
    assert 'PYTHON="$(command -v python)"\nexport PYTHON' in producer
    assert "set -o pipefail" in producer
    assert not any("setup-buildx-action" in item.get("uses", "") for item in job["steps"])
    assert "if" not in job
    step = _named_step(job, "Run required worktree mount cases")
    assert "if" not in step
    names = [item.get("name") for item in job["steps"]]
    assert names.index("Build canonical Sandbox Image") < names.index(step["name"])
    _assert_worktree_mounts_disk_and_failure_gating(job)
    upload = _named_step(job, "Upload worktree execution evidence")
    assert upload["with"]["if-no-files-found"] == "error"
    assert step["env"]["BOOLEY_REQUIRE_WORKTREE_DOCKER"] == "1"
    assert step["env"]["BOOLEY_WORKTREE_DOCKER_IMAGE"] == "booley-sandbox:latest"
    command = step["run"]
    assert "python -m pytest tests/docker/test_worktree_mount_paths_e2e.py" in command
    assert '--junitxml="${RUNNER_TEMP}/worktree-mounts.xml"' in command
    assert "--timeout=300" in command
    assert " -m " not in command.replace("python -m pytest", "pytest")
    assert " -n " not in command
    evidence = _named_step(job, "Assert worktree execution evidence")
    assert evidence["if"] == (
        "always() && (steps.worktree-pytest.outcome == 'success' "
        "|| steps.worktree-pytest.outcome == 'failure')"
    )
    assert 'assert_junit.py "${RUNNER_TEMP}/worktree-mounts.xml"' in evidence["run"]
    assert "--min-tests 1 --max-skips 0" in evidence["run"]
    for item in [job, *job["steps"]]:
        assert not item.get("continue-on-error", False)
        assert "|| true" not in item.get("run", "")
    for filename in ("test.yml", "full-python-matrix.yml"):
        assert "BOOLEY_REQUIRE_WORKTREE_DOCKER" not in (
            REPOSITORY_ROOT / ".github/workflows" / filename
        ).read_text(encoding="utf-8")


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


def test_scheduled_mutation_campaign_generates_its_copy_list_first() -> None:
    """mutmut's sandbox copy list is generated before anything else runs.

    A hand-kept ``also_copy`` list broke the campaign whenever the tracked
    layout changed (issue #3114). The generator runs before the install, so no
    install byproduct exists yet, and before ``mutmut run``.
    """
    job = _deep_tests_workflow()["jobs"]["mutation"]
    names = [step.get("name") for step in job["steps"]]
    generate = names.index("Generate mutation sandbox copy list")

    assert generate < names.index("Install pinned mutation environment")
    assert generate < names.index("Run bounded mutation campaign")
    assert job["steps"][generate]["run"] == "python .github/scripts/mutmut_also_copy.py"
    assert job["steps"][generate]["id"] == "also-copy"
    # A generator failure is the only red step; results are not read after it.
    assert _named_step(job, "Record mutation results")["if"] == (
        "always() && steps.also-copy.outcome == 'success'"
    )
    assert (REPOSITORY_ROOT / ".github/scripts/mutmut_also_copy.py").is_file()


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
    # The campaign generates also_copy from the tracked layout (issue #3114).
    assert "also_copy" not in _mutmut_config()
    assert _mutmut_config()["only_mutate"] == ["src/booley/harness/setup/*.py"]
    assert _mutmut_config()["pytest_add_cli_args_test_selection"] == ["tests/harness/"]


@pytest.mark.parametrize(
    ("initial", "expected"),
    [
        pytest.param("stats", None, id="stats"),
        pytest.param(
            "booley.harness.setup.x.x_foo__mutmut_1",
            "booley.harness.setup.x.x_foo__mutmut_1",
            id="mutant",
        ),
        pytest.param(None, None, id="unset"),
    ],
)
def test_mutmut_stats_mode_is_hidden_from_child_processes(
    monkeypatch: pytest.MonkeyPatch, initial: str | None, expected: str | None
) -> None:
    """Only mutmut's stats marker is removed; mutant names still reach children."""
    from tests import conftest

    if initial is None:
        monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)
    else:
        monkeypatch.setenv("MUTANT_UNDER_TEST", initial)

    conftest.hide_mutmut_stats_mode_from_children()

    assert os.environ.get("MUTANT_UNDER_TEST") == expected


_STATS_MODE_PROBE = '''\
"""Pytest plugin: record the stats-mode variable a session sees after configuration."""

import os
import pathlib


def pytest_collection_finish(session):
    value = os.environ.get("MUTANT_UNDER_TEST")
    pathlib.Path(os.environ["STATS_MODE_PROBE_OUT"]).write_text(repr(value), encoding="utf-8")
'''


def test_pytest_session_hides_mutmut_stats_mode_from_children(tmp_path: Path) -> None:
    """The real ``pytest_configure`` boundary strips the marker before any test runs.

    The unit cases above call the helper directly; this one fails if the call is
    ever dropped from ``tests/conftest.py``'s configure hook.
    """
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "mutmut_env_probe.py").write_bytes(_STATS_MODE_PROBE.encode("utf-8"))
    probe_out = tmp_path / "probe-out.txt"
    environment = dict(os.environ)
    for name in (
        "BOOLEY_DIR",
        "BOOLEY_PYTEST_EVIDENCE_DIR",
        "BOOLEY_PYTEST_TIMING_FILE",
        "PYTEST_ADDOPTS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        environment.pop(name, None)
    environment["MUTANT_UNDER_TEST"] = "stats"
    environment["STATS_MODE_PROBE_OUT"] = str(probe_out)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(probe_dir), *filter(None, [environment.get("PYTHONPATH")])]
    )

    result = run_bounded_child(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:xdist",
            "-p",
            "no:cacheprovider",
            "-p",
            "mutmut_env_probe",
            "--test-profile",
            "python",
            f"--basetemp={tmp_path / 'child'}",
            "--collect-only",
            "-q",
            "tests/test_pytest_configuration.py::test_mutmut_configuration_lives_only_in_setup_cfg",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        timeout=SUBPROCESS_TIMEOUT_S,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert probe_out.read_text(encoding="utf-8") == "None"


def test_mutmut_pytest_options_show_failing_test_traceback() -> None:
    """A stats-collection failure must print a short traceback and a summary.

    ``--tb=no`` hid the cause of every failed campaign, and mutmut already adds
    ``-x`` to each of its runs, so repeating it is redundant.
    """
    options = _mutmut_config()["pytest_add_cli_args"]

    assert "--tb=short" in options
    assert "-rfE" in options
    assert "--tb=no" not in options
    assert "-x" not in options


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
    assert "--min-tests 1 --max-skips 0" in coverage["run"]
    upload = next(
        step for step in steps if step.get("name") == "Upload coverage release test timing"
    )
    assert "goal-mode" not in "\n".join(step["run"] for step in validations)
    assert "goal-mode" not in upload["with"]["path"]
    # The temp-dir loop prepares exactly the directories the validations use.
    prepare = next(
        step
        for step in steps
        if step.get("name") == "Prepare isolated validation temp directories"
    )
    prepared = re.search(r"for validation in ([^;]+);", prepare["run"])
    assert prepared is not None
    assert set(prepared[1].split()) == {
        step["env"]["VALIDATION_TMP"].rsplit("/", 1)[-1] for step in validations
    }
    assert "native_fst_verilator_test.py" in rendered
    assert "simulator_ground_truth_test.py" in rendered
    assert "cd /validation-tmp/project" in rendered
    cleanup_wrapper = ".github/scripts/run_with_container_cleanup.sh"
    assert all(cleanup_wrapper in step["run"] for step in validations)
    for step in validations:
        tokens = shlex.split(step["run"])
        assert tokens[0] == cleanup_wrapper
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

    result = run_bounded_child(
        [
            str(REPOSITORY_ROOT / ".github/scripts/run_with_container_cleanup.sh"),
            "booley-ci-123-1-fifo",
            "bash",
            "-c",
            "exit 7",
        ],
        cwd=REPOSITORY_ROOT,
        timeout=SUBPROCESS_TIMEOUT_S,
    )

    assert result.returncode == 7
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "ps -aq --filter name=^/booley-ci-123-1-fifo",
        "rm -f container-id",
    ]


def _wait_for_child_pid(path: Path) -> int:
    """Wait for complete readiness, not an empty file created by shell redirection."""
    for _ in range(200):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            text = ""
        if text.isdecimal() and int(text) > 0:
            return int(text)
        time.sleep(0.01)
    pytest.fail("wrapped child did not publish a complete positive PID")


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX signals and process groups")
@pytest.mark.parametrize("publish_delay", [0, 0.1])
def test_image_validation_wrapper_cleans_promptly_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publish_delay: float
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
            'trap "" INT TERM; '
            f': > "$CHILD_PID_PATH.pending"; sleep {publish_delay}; '
            'echo $$ > "$CHILD_PID_PATH.pending" && '
            'mv "$CHILD_PID_PATH.pending" "$CHILD_PID_PATH" && sleep 30',
        ],
        cwd=REPOSITORY_ROOT,
    )
    child_pid = _wait_for_child_pid(child_pid_path)

    process.terminate()
    assert process.wait(timeout=5) == 143
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
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
    assert aggregate["if"] == "always() && needs.changes.result != 'skipped'"
    assert set(aggregate["needs"]) == {"changes", "release-semantic", *conditional}
    rendered_aggregate = "\n".join(str(step) for step in aggregate["steps"])
    assert ".github/scripts/ci_required.py" in rendered_aggregate
    assert "toJSON(needs)" in rendered_aggregate

    metrics = jobs["ci-metrics"]
    assert metrics["needs"] == "ci-required"
    assert metrics["if"] == "always() && needs.ci-required.result != 'skipped'"


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


def test_goal_mode_unit_tests_run_only_when_named(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    """A bare ``pytest tests/`` skips the Goal Mode unit tests; naming them still runs them (#3329)."""
    goal_tests = REPOSITORY_ROOT / "tests" / "goals"
    assert pytestconfig.hook.pytest_ignore_collect(collection_path=goal_tests, config=pytestconfig)
    other_tests = REPOSITORY_ROOT / "tests" / "harness"
    assert not pytestconfig.hook.pytest_ignore_collect(
        collection_path=other_tests, config=pytestconfig
    )

    # Collect-only on one runtime-picked module: no Goal test body runs in CI.
    module = sorted(goal_tests.glob("test_*.py"))[0]
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_XDIST_", "BOOLEY_PYTEST_"))
        and name not in {"PYTEST_ADDOPTS", "BOOLEY_DIR"}
    }
    result = run_bounded_child(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:xdist",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'child'}",
            "--collect-only",
            "-q",
            str(module),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        timeout=SUBPROCESS_TIMEOUT_S,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/goals/" in result.stdout.replace("\\", "/")


def test_goal_mode_source_is_omitted_from_coverage() -> None:
    """Goal Mode source is not measured while its tests are not collected (#3329)."""
    from coverage import Coverage
    from coverage.files import GlobMatcher

    omit = Coverage(config_file=str(REPOSITORY_ROOT / "pyproject.toml")).get_option("run:omit")
    assert isinstance(omit, list)
    omitted = GlobMatcher(omit)

    assert omitted.match(str(REPOSITORY_ROOT / "src/booley/goals/store.py"))
    # Subprocess data from a temporary checkout carries a different root.
    assert omitted.match("/checkout/src/booley/goals/store.py")
    assert not omitted.match(str(REPOSITORY_ROOT / "src/booley/harness/__init__.py"))


def test_booley_never_uses_sqlite() -> None:
    """The SQLite unraisable ignore is safe only while Booley owns no connections."""
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for root in ("src", "tests")
        for path in (REPOSITORY_ROOT / root).rglob("*.py")
        if not path.is_relative_to(REPOSITORY_ROOT / "tests/goals")  # not run in CI (#3329)
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
    """Windows timeouts kill the worker silently, so CI fails tests that drift near one.

    The merge gate warns from 50% and fails above 80% so runner noise does not
    fail unrelated pull requests (#1775); the weekly matrix fails from 50%.
    """
    gate = "${{ runner.os == 'Windows' && '--timeout-headroom=0.8 --timeout-headroom-warn=0.5' || '' }}"
    weekly = "${{ runner.os == 'Windows' && '--timeout-headroom=0.5' || '' }}"
    primary = _test_workflow()["jobs"]["test"]["steps"]
    scheduled = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/full-python-matrix.yml").read_text(encoding="utf-8")
    )["jobs"]["test"]["steps"]

    def pytest_runs(steps: list[dict[str, object]]) -> list[str]:
        return [
            str(step["run"]) for step in steps if str(step.get("run", "")).startswith("pytest ")
        ]

    assert [gate in run for run in pytest_runs(primary)] == [True] * 3
    assert [weekly in run for run in pytest_runs(scheduled)] == [True]
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
    result = run_bounded_child(
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
        timeout=SUBPROCESS_TIMEOUT_S,
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


def test_linux_shard_dispatch_inputs_reach_the_classifier() -> None:
    workflow = _test_workflow()
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    changes = workflow["jobs"]["changes"]
    classify = _named_step(changes, "Classify changed paths")

    assert (inputs["linux_shard_count"]["options"], inputs["linux_shard_count"]["default"]) == (
        ["1", "2", "3"],
        "2",
    )
    assert (inputs["linux_scheduler"]["options"], inputs["linux_scheduler"]["default"]) == (
        ["loadscope", "worksteal"],
        "loadscope",
    )
    assert inputs["linux_shard_benchmark"]["type"] == "boolean"
    assert inputs["linux_shard_benchmark"]["default"] is False
    for expected in (
        "--linux-shard-count \"${{ inputs.linux_shard_count || '2' }}\"",
        "--linux-scheduler \"${{ inputs.linux_scheduler || 'loadscope' }}\"",
        '--linux-shard-benchmark "${{ inputs.linux_shard_benchmark || false }}"',
    ):
        assert expected in classify["run"]
    assert "!inputs.linux_shard_benchmark" in classify["env"]["FORCE_ALL"]
    for output in ("linux_shard_count", "linux_scheduler"):
        assert changes["outputs"][output] == f"${{{{ steps.classify.outputs.{output} }}}}"


def test_linux_scheduler_and_timing_model_reach_only_linux_commands() -> None:
    workflow = _test_workflow()
    test_job = workflow["jobs"]["test"]
    full = _named_step(test_job, "Run full compatibility suite")["run"]
    shard = _named_step(test_job, "Run duration-balanced shard")["run"]
    scheduler_users = {
        (job_name, step.get("name"))
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if _LINUX_SCHEDULER_OUTPUT in step.get("run", "")
    }
    windows_shard = _render_expressions(shard, "Windows", "loadscope")

    assert scheduler_users == {
        ("test", "Run full compatibility suite"),
        ("test", "Run duration-balanced shard"),
    }
    for scheduler in ("loadscope", "worksteal"):
        linux_shard = _render_expressions(shard, "Linux", scheduler)
        assert f"--dist={scheduler} " in _render_expressions(full, "Linux", scheduler)
        assert f"--dist={scheduler} " in linux_shard
        assert "--ci-timing-model .github/ci/linux-test-timings.json" in linux_shard
        assert "windows-test-timings" not in linux_shard
    assert "--dist=worksteal " in windows_shard
    assert "--ci-timing-model .github/ci/windows-test-timings.json" in windows_shard


def test_test_verify_proves_each_linux_group_when_sharded() -> None:
    workflow = _test_workflow()
    verify = workflow["jobs"]["test-verify"]
    steps = verify["steps"]
    names = [step.get("name") for step in steps]
    download = _named_step(verify, "Download Linux shard manifests")
    prove = _named_step(verify, "Prove every Linux sharded test ran exactly once")
    gate = "${{ !cancelled() && needs.changes.outputs.linux_shard_count != '1' }}"
    groups = sorted(
        {
            str(entry["group"])
            for entry in build_test_matrix(10, linux_shard_count=2)["include"]
            if entry["os"] == "ubuntu-latest"
        }
    )
    command = " ".join(prove["run"].split())

    assert verify["needs"] == ["changes", "test"]
    assert names.index(download["name"]) < names.index(prove["name"])
    assert download["if"] == prove["if"] == gate
    assert download["with"]["pattern"] == "shard-linux-*"
    assert download["with"]["path"] == "${{ runner.temp }}/linux-shards"
    assert prove["env"]["LINUX_SHARD_COUNT"] == "${{ needs.changes.outputs.linux_shard_count }}"
    assert groups == ["linux-3.11"]
    assert f'--root "${{RUNNER_TEMP}}/linux-shards" --group {groups[0]}' in command
    assert '--shard-count "${LINUX_SHARD_COUNT}"' in command
    for artifact in ("shard-coverage-0", "coverage-shard-0", "pytest-timing-ubuntu-3.11-full"):
        assert not any(
            fnmatch.fnmatch(artifact, step.get("with", {}).get("pattern", "")) for step in steps
        )
    assert "test-verify" in workflow["jobs"]["ci-required"]["needs"]
    for category in CATEGORIES:
        jobs = required_jobs({category})
        assert ("test" in jobs) == ("test-verify" in jobs)


def test_suite_resolves_failed_tmp_path_retention(pytestconfig: pytest.Config) -> None:
    """The running session keeps `tmp_path` only for failing tests.

    Catches a higher-precedence ini file (`pytest.ini`, `tox.ini`) shadowing the
    `pyproject.toml` setting, which a TOML-only assertion would miss.
    """
    assert pytestconfig.getini("tmp_path_retention_policy") == "failed"


def test_explicit_basetemp_keeps_only_failing_tmp_path(tmp_path: Path) -> None:
    """With an explicit `--basetemp`, a passing test's directory is removed."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_inner.py").write_bytes(
        b"def test_passes(tmp_path):\n"
        b"    (tmp_path / 'out.txt').write_text('x')\n"
        b"\n"
        b"def test_fails(tmp_path):\n"
        b"    (tmp_path / 'out.txt').write_text('x')\n"
        b"    assert False\n"
    )
    basetemp = tmp_path / "inner-basetemp"
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "PYTEST_ADDOPTS" and not name.startswith("PYTEST_XDIST_")
    }
    result = run_bounded_child(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(REPOSITORY_ROOT / "pyproject.toml"),
            "--rootdir",
            str(project),
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            f"--basetemp={basetemp}",
            "test_inner.py",
        ],
        cwd=project,
        env=environment,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed, 1 passed" in output, output
    # pytest's `*current` convenience links are platform-dependent; ignore them.
    kept = sorted(entry.name for entry in basetemp.iterdir() if not entry.is_symlink())
    assert kept == ["test_fails0"], output
    assert (basetemp / "test_fails0" / "out.txt").exists()


def _scoped_git_value(key: str, cwd: Path) -> str:
    """Return ``<scope>\\t<value>`` Git reports for ``key`` (empty when unset).

    Inherits the test's environment. The literal scope prefix lets callers
    tell a command-scope value from the same value in a host global config.
    """
    result = run_bounded_child(
        ["git", "config", "--show-scope", "--get", key],
        cwd=cwd,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    return result.stdout.strip()


def _assert_auto_maintenance_off(cwd: Path) -> None:
    assert _scoped_git_value("maintenance.auto", cwd) == "command\tfalse"
    assert _scoped_git_value("gc.auto", cwd) == "command\t0"


def test_session_git_children_run_without_auto_maintenance(tmp_path: Path) -> None:
    _assert_auto_maintenance_off(tmp_path)


def test_isolated_git_attributes_keeps_auto_maintenance_off(
    tmp_path: Path, isolated_git_attributes: None
) -> None:
    _assert_auto_maintenance_off(tmp_path)


def test_isolated_git_excludes_keeps_auto_maintenance_off_and_strips_other_command_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from tests.conftest import add_git_command_config

    add_git_command_config(monkeypatch, "core.abbrev", "12")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bigfilethreshold'='1m'")
    request.getfixturevalue("isolated_git_excludes")
    _assert_auto_maintenance_off(tmp_path)
    assert _scoped_git_value("core.abbrev", tmp_path) == ""
    assert _scoped_git_value("core.bigfilethreshold", tmp_path) == ""


def test_own_empty_global_config_keeps_auto_maintenance_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_config = tmp_path / "own-global.config"
    own_config.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(own_config))
    _assert_auto_maintenance_off(tmp_path)


def test_added_command_config_overrides_and_keeps_session_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import add_git_command_config

    add_git_command_config(monkeypatch, "core.abbrev", "12")
    add_git_command_config(monkeypatch, "gc.auto", "5")
    assert _scoped_git_value("core.abbrev", tmp_path) == "command\t12"
    assert _scoped_git_value("gc.auto", tmp_path) == "command\t5"
    assert _scoped_git_value("maintenance.auto", tmp_path) == "command\tfalse"


def test_session_git_config_is_idempotent_and_keeps_inherited_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import _disable_git_auto_maintenance_for_children

    # The function writes os.environ directly: register every slot with
    # monkeypatch first so teardown restores what the session had.
    for index in range(3):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", "")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", "")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Inherited")
    _disable_git_auto_maintenance_for_children()
    _disable_git_auto_maintenance_for_children()
    assert os.environ["GIT_CONFIG_COUNT"] == "3"
    assert os.environ["GIT_CONFIG_KEY_0"] == "user.name"
    assert os.environ["GIT_CONFIG_VALUE_0"] == "Inherited"
    assert (os.environ["GIT_CONFIG_KEY_1"], os.environ["GIT_CONFIG_VALUE_1"]) == (
        "maintenance.auto",
        "false",
    )
    assert (os.environ["GIT_CONFIG_KEY_2"], os.environ["GIT_CONFIG_VALUE_2"]) == ("gc.auto", "0")

    # An inherited entry that already has the wanted effective value is not re-added.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "GC.Auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "0")
    _disable_git_auto_maintenance_for_children()
    assert os.environ["GIT_CONFIG_COUNT"] == "2"
    assert os.environ["GIT_CONFIG_KEY_1"] == "maintenance.auto"

    # Git reads an empty count as zero.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "")
    _disable_git_auto_maintenance_for_children()
    assert os.environ["GIT_CONFIG_COUNT"] == "2"
