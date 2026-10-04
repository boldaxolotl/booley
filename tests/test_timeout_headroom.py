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
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.timeout_headroom import suggested_budget

_SLOW_BODY = "time.sleep(0.2)"
# 0.001 of the 60 s CLI budget is 0.06 s, well below the 0.2 s sleep.
_TRIP = "--timeout-headroom=0.001"


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


@pytest.mark.parametrize("workers", [0, 1])
def test_test_within_headroom_passes_and_is_counted(tmp_path: Path, workers: int) -> None:
    result = _run_case(tmp_path, ["--timeout-headroom=0.5", "-n", str(workers)])

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}
    assert "timeout headroom: limit 50.0%, 1 tests checked, highest" in result.stdout


def test_ratio_is_recorded_in_junit(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    result = _run_case(tmp_path, ["--timeout-headroom=0.5", f"--junitxml={junit}"])

    assert result.returncode == 0, result.stdout + result.stderr
    properties = {
        node.get("name"): float(node.get("value", "nan"))
        for node in ET.parse(junit).iter("property")
    }
    # The 0.2 s sleep uses about 0.0033 of the 60 s budget (rounded to 4 places).
    assert 0.003 <= properties["timeout_headroom_ratio"] < 0.5


def test_marker_budget_overrides_cli_budget(tmp_path: Path) -> None:
    # 0.001 of the 60 s CLI budget would trip; 0.001 of 600 s (0.6 s) does not.
    result = _run_case(
        tmp_path, [_TRIP], marker="@pytest.mark.timeout(600)", durations={"call": 0.2}
    )

    assert result.returncode == 0, result.stdout + result.stderr
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


def test_suggestion_never_lowers_the_current_budget(tmp_path: Path) -> None:
    # 0.7 s exceeds 0.001 x 600 s; 3x 0.7 s alone would suggest 30 s.
    result = _run_case(
        tmp_path, [_TRIP], marker="@pytest.mark.timeout(600)", durations={"call": 0.7}
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Raise the test's @pytest.mark.timeout to at least 630 s" in output


def test_failed_test_is_not_reported_twice(tmp_path: Path) -> None:
    result = _run_case(tmp_path, [_TRIP], body=_SLOW_BODY + "\n    assert False")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert _counts(output) == {"failed": 1}, output
    assert "headroom limit is" not in output


def test_setup_error_is_not_reported_twice(tmp_path: Path) -> None:
    fixture = "@pytest.fixture\ndef broken():\n    time.sleep(0.2)\n    raise RuntimeError\n"
    result = _run_case(tmp_path, [_TRIP], marker=fixture, body="pass", arguments="broken")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert _counts(output) == {"error": 1}, output
    assert "headroom limit is" not in output


@pytest.mark.parametrize(
    ("marker", "body", "outcome"),
    [
        ("", "pytest.skip('missing prerequisite')", "skipped"),
        ("@pytest.mark.xfail(reason='known failure')", "assert False", "xfailed"),
        ("", "pytest.xfail('known failure')", "xfailed"),
    ],
)
def test_nonpassing_test_is_not_a_headroom_error(
    tmp_path: Path, marker: str, body: str, outcome: str
) -> None:
    result = _run_case(tmp_path, [_TRIP], marker=marker, body=body, durations={"call": 0.2})

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"1 {outcome}" in output
    assert "headroom limit is" not in output
    assert "0 tests checked" in output


def test_setup_skip_is_not_a_headroom_error(tmp_path: Path) -> None:
    fixture = "@pytest.fixture\ndef missing():\n    pytest.skip('missing prerequisite')\n"
    result = _run_case(
        tmp_path, [_TRIP], marker=fixture, arguments="missing", durations={"setup": 0.2}
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 skipped" in output
    assert "headroom limit is" not in output
    assert "0 tests checked" in output


@pytest.mark.parametrize("value", ["0", "-0.5", "1.5", "nan", "inf", "half"])
def test_invalid_fraction_is_a_usage_error(tmp_path: Path, value: str) -> None:
    result = _run_case(tmp_path, [f"--timeout-headroom={value}"])

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "--timeout-headroom" in result.stderr


def test_guard_requires_pytest_timeout(tmp_path: Path) -> None:
    result = _run_case(tmp_path, ["--timeout-headroom=0.5"], timeout_plugin=False)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "requires the pytest-timeout plugin" in result.stderr


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
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
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
        name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")
    }
    environment["PYTHONPATH"] = str(Path(__file__).parent)
    if not autoload:
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Stay well inside this test's own 60 s CI budget (#1202).
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_case.py", "-q", *plugins, *options],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
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
