"""Exercise the timeout-headroom guard through real pytest and xdist runs.

Every case uses a budget far above the test's runtime and a headroom fraction
chosen so the sleep in the inner test is clearly above or below the limit. The
budget-selection cases inject report durations, so their outcome never depends
on host speed or a timer firing.
"""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.subprocess_budget import run_nested_pytest
from tests.timeout_headroom import suggested_budget

_SLOW_BODY = "time.sleep(0.2)"
# 0.001 of the 60 s CLI budget is 0.06 s, well below the 0.2 s sleep.
_TRIP = "--timeout-headroom=0.001"
_WARN_HALF = "--timeout-headroom-warn=0.5"


# Autoload registers pytest-timeout as "timeout"; -p registers "pytest_timeout".
@pytest.mark.parametrize(("workers", "autoload"), [(0, False), (1, False), (1, True)])
def test_passing_test_over_headroom_fails_with_actionable_message(
    tmp_path: Path, workers: int, autoload: bool
) -> None:
    result = _run_case(tmp_path, [_TRIP, "-n", str(workers)], autoload=autoload)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert _counts(output) == {"passed": 1, "error": 1}, output
    assert "ERROR at teardown of test_case" in output
    assert "test_case.py::test_case used" in output
    assert "of its 60 s timeout" in output
    assert "Raise the test's @pytest.mark.timeout to at least 90 s" in output
    assert "timeout headroom: limit 0.1%, 1 tests checked, highest" in output


def test_guard_is_off_without_the_option(tmp_path: Path) -> None:
    result = _run_case(tmp_path, [])

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}
    assert "timeout headroom" not in result.stdout


@pytest.mark.parametrize(
    ("option", "workers"),
    [("--timeout-headroom=0.5", 0), ("--timeout-headroom=0.5", 1), (_WARN_HALF, 1)],
    ids=["limit-serial", "limit-xdist", "warn-only"],
)
def test_test_within_headroom_passes_and_is_counted(
    tmp_path: Path, option: str, workers: int
) -> None:
    result = _run_case(tmp_path, [option, "-n", str(workers)])

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}
    assert "50.0%, 1 tests checked, highest" in result.stdout
    assert "timeout headroom warning:" not in result.stdout


@pytest.mark.parametrize(
    ("limit", "workers", "github"),
    [(["--timeout-headroom=0.5"], 1, True), (["--timeout-headroom=0.5"], 0, False), ([], 1, True)],
    ids=["warn-and-fail-xdist", "warn-and-fail-serial", "warn-only"],
)
def test_test_in_warning_band_passes_and_is_reported(
    tmp_path: Path, limit: list[str], workers: int, github: bool
) -> None:
    junit = tmp_path / "junit.xml"
    # The 0.2 s sleep is above the 0.06 s warning limit and below the 30 s failure limit.
    options = [*limit, "--timeout-headroom-warn=0.001", "-n", str(workers)]
    result = _run_case(tmp_path, [*options, f"--junitxml={junit}"], github=github)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert _counts(output) == {"passed": 1}, output
    assert "warning above 0.1%, 1 tests checked" in output
    assert ("limit 50.0%, warning above 0.1%" in output) is bool(limit)
    assert "timeout headroom warning: test_case.py::test_case used" in output
    assert "warning above 0.1%). On Windows" in output
    annotation = "::warning title=Timeout headroom::test_case.py::test_case used"
    # GitHub only parses a workflow command at the start of a line.
    assert any(line.startswith(annotation) for line in output.splitlines()) is github
    if github:
        assert "%25; warning above 0.1%25)" in output
    properties = [node.attrib for node in ET.parse(junit).iter("property")]
    assert [p["name"] for p in properties].count("timeout_headroom_warning") == 1
    # The 0.2 s sleep uses about 0.0033 of the 60 s budget (rounded to 4 places).
    ratio = next(p for p in properties if p["name"] == "timeout_headroom_ratio")
    assert 0.003 <= float(ratio["value"]) < 0.5


def test_test_over_failure_limit_is_not_also_a_warning(tmp_path: Path) -> None:
    result = _run_case(tmp_path, [_TRIP, "--timeout-headroom-warn=0.001"])

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "the headroom limit is 0.1%" in output
    assert "timeout headroom warning:" not in output


def test_warning_limit_above_failure_limit_is_a_usage_error(tmp_path: Path) -> None:
    result = _run_case(tmp_path, ["--timeout-headroom=0.5", "--timeout-headroom-warn=0.8"])

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "--timeout-headroom-warn must not exceed --timeout-headroom" in result.stderr


def test_timeout_still_fails_a_test_under_warning_only(tmp_path: Path) -> None:
    marker = "@pytest.mark.timeout(1)"
    result = _run_case(tmp_path, [_WARN_HALF], marker=marker, body="time.sleep(5)")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Timeout" in output


@pytest.mark.parametrize(
    ("call_seconds", "returncode"),
    [
        # 0.001 of the 60 s CLI budget would trip; 0.001 of 600 s (0.6 s) does not.
        pytest.param(0.2, 0, id="marker-overrides-cli-budget"),
        # 0.7 s exceeds 0.001 x 600 s; 3x 0.7 s alone would suggest 30 s.
        pytest.param(0.7, 1, id="suggestion-never-lowers-budget"),
    ],
)
def test_marker_budget_replaces_cli_budget(
    tmp_path: Path, call_seconds: float, returncode: int
) -> None:
    result = _run_case(
        tmp_path, [_TRIP], marker="@pytest.mark.timeout(600)", durations={"call": call_seconds}
    )

    output = result.stdout + result.stderr
    assert result.returncode == returncode, output
    if returncode:
        assert "Raise the test's @pytest.mark.timeout to at least 630 s" in output
    else:
        assert _counts(result.stdout) == {"passed": 1}


def test_disabled_timeout_disables_the_guard(tmp_path: Path) -> None:
    result = _run_case(tmp_path, [_TRIP], marker="@pytest.mark.timeout(0)")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "timeout headroom: limit 0.1%, 0 tests checked" in result.stdout


def test_func_only_budget_ignores_fixture_time(tmp_path: Path) -> None:
    # Counting the 0.2 s fixture against 0.001 x 60 s = 0.06 s would trip.
    fixture = "@pytest.fixture\ndef slow_setup():\n    time.sleep(0.2)\n"
    result = _run_case(
        tmp_path,
        [_TRIP],
        marker=fixture + "@pytest.mark.timeout(60, func_only=True)",
        body="pass",
        arguments="slow_setup",
        durations={"setup": 0.2},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}


@pytest.mark.parametrize(
    ("marker", "body", "arguments", "outcome"),
    [
        pytest.param("", _SLOW_BODY + "\n    assert False", "", "failed", id="failed-test"),
        pytest.param(
            "@pytest.fixture\ndef broken():\n    time.sleep(0.2)\n    raise RuntimeError\n",
            "pass",
            "broken",
            "error",
            id="setup-error",
        ),
    ],
)
def test_failure_is_not_also_reported_as_headroom(
    tmp_path: Path, marker: str, body: str, arguments: str, outcome: str
) -> None:
    result = _run_case(tmp_path, [_TRIP], marker=marker, body=body, arguments=arguments)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert _counts(output) == {outcome: 1}, output
    assert "headroom limit is" not in output


@pytest.mark.parametrize(
    ("marker", "body", "arguments", "phase", "outcome"),
    [
        ("", "pytest.skip('missing prerequisite')", "", "call", "skipped"),
        ("@pytest.mark.xfail(reason='known failure')", "assert False", "", "call", "xfailed"),
        ("", "pytest.xfail('known failure')", "", "call", "xfailed"),
        (
            "@pytest.fixture\ndef missing():\n    pytest.skip('missing prerequisite')\n",
            _SLOW_BODY,
            "missing",
            "setup",
            "skipped",
        ),
    ],
    ids=["skip", "xfail-marker", "xfail-call", "setup-skip"],
)
def test_nonpassing_test_is_not_a_headroom_error(
    tmp_path: Path, marker: str, body: str, arguments: str, phase: str, outcome: str
) -> None:
    result = _run_case(
        tmp_path, [_TRIP], marker=marker, body=body, arguments=arguments, durations={phase: 0.2}
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"1 {outcome}" in output
    assert "headroom limit is" not in output
    assert "0 tests checked" in output


@pytest.mark.parametrize(
    "option",
    [f"--timeout-headroom={value}" for value in ("0", "-0.5", "1.5", "nan", "inf", "half")]
    + ["--timeout-headroom-warn=0"],
)
def test_invalid_fraction_is_a_usage_error(tmp_path: Path, option: str) -> None:
    result = _run_case(tmp_path, [option])

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "--timeout-headroom" in result.stderr


@pytest.mark.parametrize(
    "option", ["--timeout-headroom=0.5", _WARN_HALF], ids=["limit", "warn-only"]
)
def test_guard_requires_pytest_timeout(tmp_path: Path, option: str) -> None:
    result = _run_case(tmp_path, [option], timeout_plugin=False)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    option_name = option.partition("=")[0]
    assert f"{option_name} requires the pytest-timeout plugin" in result.stderr


@pytest.mark.parametrize(
    ("elapsed", "budget"),
    [(0.0, 30), (0.2, 30), (10.0, 30), (10.1, 60), (36.5, 120), (54.4, 180), (149.7, 450)],
)
def test_suggested_budget_is_three_times_elapsed_in_half_minutes(
    elapsed: float, budget: int
) -> None:
    assert suggested_budget(elapsed) == budget


def _counts(output: str) -> dict[str, int]:
    """Parse the outcome counts from pytest's final summary line."""
    summary = output.strip().splitlines()[-1]
    return {
        outcome.rstrip("s") if outcome == "errors" else outcome: int(count)
        for count, outcome in re.findall(r"(\d+) (passed|failed|errors?)\b", summary)
    }


def _run_case(
    tmp_path: Path,
    options: list[str],
    *,
    marker: str = "",
    body: str = _SLOW_BODY,
    arguments: str = "",
    timeout_plugin: bool = True,
    autoload: bool = False,
    durations: dict[str, float] | None = None,
    github: bool = False,
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_default_fixture_loop_scope = function\n", encoding="utf-8"
    )
    (tmp_path / "test_case.py").write_text(
        f"import time\nimport pytest\n{marker}\ndef test_case({arguments}):\n    {body}\n",
        encoding="utf-8",
    )
    _control_durations(tmp_path, durations)
    # A private basetemp keeps the inner run away from the host's shared one.
    plugins = ["-p", "timeout_headroom", "-p", "no:cacheprovider"]
    plugins.append(f"--basetemp={tmp_path / 'inner'}")
    if not autoload:
        plugins = ["-p", "xdist.plugin", *plugins]
        if timeout_plugin:
            plugins = ["-p", "pytest_timeout", *plugins]
    if timeout_plugin:
        plugins.append("--timeout=60")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTEST_") and name != "GITHUB_ACTIONS"
    }
    if github:
        environment["GITHUB_ACTIONS"] = "true"
    environment["PYTHONPATH"] = str(Path(__file__).parent)
    if not autoload:
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Stay well inside this test's own 60 s CI budget (#1202).
    return run_nested_pytest(
        ["test_case.py", "-q", *plugins, *options],
        cwd=tmp_path,
        env=environment,
    )


def _control_durations(tmp_path: Path, durations: dict[str, float] | None) -> None:
    """Set deterministic phase durations before the guard consumes the reports."""
    if durations is not None:
        (tmp_path / "conftest.py").write_text(
            "import pytest\n"
            "@pytest.hookimpl(wrapper=True, trylast=True)\n"
            "def pytest_runtest_makereport(item, call):\n"
            "    report = yield\n"
            f"    report.duration = {durations!r}.get(report.when, 0.0)\n"
            "    return report\n",
            encoding="utf-8",
        )
