"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

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

from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child, run_nested_pytest
from tests.time_budget import hang_guard_s, scaled

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


# Six coverage shards keep a shard plus the combine step off the queue's critical path.
COVERAGE_SHARD_COUNT = 6


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
    "tests/bwave/test_contract.py::test_reset_object_validates_against_published_schema",
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
    "tests/bwave/test_scope_relative_globs.py::test_gui_resolves_relative_glob[all-match]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_resolves_relative_glob[one-missing]",
    "tests/bwave/test_scope_relative_globs.py::test_glob_total_miss_suggests_anchored_form[u_core.*]",
    "tests/bwave/test_scope_relative_globs.py::test_glob_total_miss_suggests_anchored_form[tb.*]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_total_miss_suggests_anchored_form",
    "tests/bwave/test_scope_relative_globs.py::test_anchored_globs_select_as_before[TOP.tb.dut.*]",
    "tests/bwave/test_scope_relative_globs.py::test_anchored_globs_select_as_before[*dut.*]",
    "tests/bwave/test_scope_relative_globs.py::test_bare_name_suffix_match_unchanged",
    "tests/bwave/test_scope_relative_globs.py::test_gui_keeps_anchored_and_bare_matching[TOP.tb.dut.*-expected0]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_keeps_anchored_and_bare_matching[*dut.*-expected1]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_keeps_anchored_and_bare_matching[bus-expected2]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_refuses_bit_slice[False]",
    "tests/bwave/test_scope_relative_globs.py::test_gui_refuses_bit_slice[True]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --before hs --count-find *req* 1 --before 25 --count-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --after hs --first-find *req* 1 --after 25 --first-2]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --before hs-find *req* 1 --before 25-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --after hs-find *req* 1 --after 25-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[sample *req* 1 -s *data* --before hs --count-sample *req* 1 -s *data* --before 25 --count-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[sample *req* 1 -s *data* --after hs --first-sample *req* 1 -s *data* --after 25 --first-2]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[sample *req* 1 -s *data* --after hs-sample *req* 1 -s *data* --after 25-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --before=hs --count-find *req* 1 --before=25 --count-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --time=hs:he-find *req* 1 --time=25:35-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 -ths:he-find *req* 1 -t25:35-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[value --at=hs -s *req*-value --at=25 -s *req*-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[diff -s *data* hs he-diff -s *data* 25 35-0]",
    "tests/bwave/test_sessions.py::test_marker_matches_its_cycle_form[find *req* 1 --before hs --async --count-find *req* 1 --before 265 --async --count-0]",
    "tests/bwave/test_sessions.py::test_async_marker_bound_on_a_clockless_trace_is_refused",
}


def test_native_bwave_marker_selects_only_real_binary_tests() -> None:
    """The native integration job owns every test that executes B-Wave."""
    result = run_nested_pytest(
        ["--collect-only", "-q", "-m", "native_bwave", "tests/bwave"],
        cwd=REPOSITORY_ROOT,
        env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PYTEST_XDIST_")
        },
    )

    assert result.returncode == 0, result.stderr
    selected = {line for line in result.stdout.splitlines() if line.startswith("tests/")}
    assert selected == _EXPECTED_NATIVE_BWAVE_NODES


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


def test_image_validations_cannot_pass_without_running() -> None:
    """No validation continues on error; the coverage release suite fails on a skipped test."""
    workflow = _test_workflow()
    steps = workflow["jobs"]["bwave-smoke"]["steps"]
    group = next(step for step in steps if "parallel" in step)
    validations = [
        step
        for step in group["parallel"]
        if step.get("name") != "Run RISC-V candidate image contract"
    ]

    assert all(not step.get("continue-on-error", False) for step in validations)
    coverage = next(step for step in validations if step["name"].startswith("Run Coverage"))
    assert "assert_junit.py" in coverage["run"]
    assert "--min-tests 1 --max-skips 0" in coverage["run"]


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
    assert calls == ["ps -aq --filter name=^/booley-ci-123-1-fifo", "rm -f container-id"]


def _wait_for_child_pid(path: Path) -> int:
    """Wait for complete readiness, not an empty file created by shell redirection."""
    deadline = time.monotonic() + hang_guard_s()
    while time.monotonic() < deadline:
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
    assert process.wait(timeout=scaled(5)) == 143
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls[-2:] == ["ps -aq --filter name=^/booley-ci-123-1-fifo", "rm -f container-id"]


def test_change_aware_jobs_feed_an_always_running_aggregate() -> None:
    """Conditional jobs never leave the stable required check unresolved."""
    workflow = _test_workflow()
    jobs = workflow["jobs"]
    conditional = set(jobs) - {
        "changes",
        "release-semantic",
        "test-ratio",
        "ci-required",
        "ci-metrics",
    }

    aggregate = jobs["ci-required"]
    assert aggregate["if"] == "always() && needs.changes.result != 'skipped'"
    assert set(aggregate["needs"]) == {"changes", "release-semantic", "test-ratio", *conditional}
    rendered_aggregate = "\n".join(str(step) for step in aggregate["steps"])
    assert ".github/scripts/ci_required.py" in rendered_aggregate
    assert "toJSON(needs)" in rendered_aggregate


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
        if not path.is_relative_to(REPOSITORY_ROOT / "tests/goals")  # not run in CI (#3329)
        if re.search(r"^\s*(import|from)\s+sqlite3\b", path.read_text(encoding="utf-8"), re.M)
    ]

    assert offenders == []


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
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTEST_") and name != "BOOLEY_TEST_TIME_SCALE"
    }
    if environment_timeout is not None:
        environment["PYTEST_TIMEOUT"] = environment_timeout
    result = run_nested_pytest(
        [
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "tests/test_timeout_headroom.py::test_guard_is_off_without_the_option",
            *options,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    if expected == "0.0s":
        assert "\ntimeout:" not in result.stdout
    else:
        assert f"\ntimeout: {expected}\n" in result.stdout


def _collect_with_time_scale(scale: str, *options: str) -> subprocess.CompletedProcess[str]:
    """Start a nested collect-only session under *scale*, ignoring the outer environment."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTEST_") and name != "BOOLEY_TEST_TIME_SCALE"
    }
    environment["BOOLEY_TEST_TIME_SCALE"] = scale
    return run_nested_pytest(
        [
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "tests/test_timeout_headroom.py::test_guard_is_off_without_the_option",
            *options,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )


@pytest.mark.parametrize(
    ("scale", "options", "returncode", "expected"),
    [
        pytest.param(
            "abc",
            [],
            4,
            "BOOLEY_TEST_TIME_SCALE='abc' is not a positive finite number",
            id="invalid-scale",
        ),
        pytest.param(
            "2",
            ["--timeout=60"],
            4,
            "--timeout=60 is below twice the hang guard (60 s",
            id="timeout-below-hang-guard",
        ),
        pytest.param(
            "0.01",
            ["--timeout=30"],
            4,
            "--timeout=30 is below",
            id="fractional-scale-keeps-60s-floor",
        ),
        pytest.param(
            "2", ["--help", "--timeout=60"], 0, "--timeout-headroom", id="help-is-never-refused"
        ),
    ],
)
def test_session_refuses_an_unsafe_time_budget(
    scale: str, options: list[str], returncode: int, expected: str
) -> None:
    """A session whose per-test limit cannot outlast the hang guard fails at startup."""
    result = _collect_with_time_scale(scale, *options)

    assert result.returncode == returncode, result.stdout + result.stderr
    assert expected in result.stdout + result.stderr


def test_scaled_session_default_timeout() -> None:
    """The default per-test limit rises to the minimum instead of refusing itself."""
    result = _collect_with_time_scale("3")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "\ntimeout: 180s\n" in result.stdout


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
    result = run_nested_pytest(
        [
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
        ["git", "config", "--show-scope", "--get", key], cwd=cwd, timeout=SUBPROCESS_TIMEOUT_S
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


def test_issuance_journal_store_resolves_under_the_test_tmp_path(tmp_path: Path) -> None:
    from booley.runtime import issuance_invalidation

    journal = issuance_invalidation.journal_store().path(str((tmp_path / "project").resolve()))
    assert journal.is_relative_to(tmp_path)
    assert not journal.is_relative_to(Path.home() / ".config")


def test_requested_fixture_xdg_config_home_overrides_the_default(
    tmp_path: Path, isolated_git_attributes: None
) -> None:
    from booley.core.user_paths import config_dir

    assert config_dir() == tmp_path / "isolated-xdg" / "booley"


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
