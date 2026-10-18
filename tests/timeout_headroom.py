"""Fail passing tests that consume too much of their pytest-timeout budget.

On Windows, pytest-timeout can only use its thread method. When a budget
expires, that method ends the whole worker with ``os._exit(1)``, so xdist
reports an unattributed ``node down: Not properly terminated`` and the merge
queue drops the batch (#1202). A test whose normal runtime drifts close to its
budget therefore fails rarely, late, and without a name.

``--timeout-headroom=FRACTION`` turns that drift into an ordinary, named
failure before the kill: a test that passes but spends more than FRACTION of
its effective budget fails at teardown. ``--timeout-headroom-warn=FRACTION``
reports a lower band without failing, so runner noise does not fail unrelated
pull requests (#1775) while drift stays visible: as a JUnit property, in the
terminal summary, and as a GitHub Actions warning annotation.

Size a budget at three times the slowest observed Windows CI duration, rounded
up to a 30-second multiple (:func:`suggested_budget`). CI warns above 0.5 and
fails above 0.8: 1.5x slowdown before a warning, 2.4x before a failure, and 3x
before the worker is killed.
"""

from __future__ import annotations

import argparse
import math
import os
from collections.abc import Generator

import pytest

_BUDGET_MULTIPLIER = 3
_BUDGET_GRANULARITY_S = 30
_ELAPSED = pytest.StashKey[float]()
_PHASE_NOT_PASSED = pytest.StashKey[bool]()
# Recorded on each teardown report; JUnit keeps it as a <property>.
_RATIO_PROPERTY = "timeout_headroom_ratio"
_WARNING_PROPERTY = "timeout_headroom_warning"


def timeout_plugin_loaded(config: pytest.Config) -> bool:
    """Return whether pytest-timeout is active under either registration name.

    Its entry point registers it as ``timeout``; ``-p pytest_timeout`` registers
    it under the module name instead.
    """
    manager = config.pluginmanager
    return manager.hasplugin("timeout") or manager.hasplugin("pytest_timeout")


def suggested_budget(elapsed: float) -> int:
    """Return the timeout budget, in seconds, that this sizing rule assigns."""
    multiples = math.ceil(_BUDGET_MULTIPLIER * elapsed / _BUDGET_GRANULARITY_S)
    return max(1, multiples) * _BUDGET_GRANULARITY_S


def _headroom_fraction(value: str) -> float:
    """Parse the CLI limit; NaN and infinities fail the range check."""
    try:
        fraction = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from None
    if not 0 < fraction <= 1:
        raise argparse.ArgumentTypeError(f"must be in (0, 1], got {value!r}")
    return fraction


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the opt-in headroom limits."""
    group = parser.getgroup("timeout")
    group.addoption(
        "--timeout-headroom",
        type=_headroom_fraction,
        default=None,
        metavar="FRACTION",
        help="Fail a passing test that uses more than FRACTION of its pytest-timeout budget.",
    )
    group.addoption(
        "--timeout-headroom-warn",
        type=_headroom_fraction,
        default=None,
        metavar="FRACTION",
        help="Report, without failing, a passing test that uses more than FRACTION of its "
        "budget. Requires --timeout-headroom at or above FRACTION.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Refuse limits that cannot be enforced, and report what was enforced."""
    fraction = config.getoption("timeout_headroom")
    warn = config.getoption("timeout_headroom_warn")
    if warn is not None and (fraction is None or warn > fraction):
        raise pytest.UsageError(
            "--timeout-headroom-warn requires --timeout-headroom at or above it"
        )
    if fraction is None:
        return
    if not timeout_plugin_loaded(config):
        raise pytest.UsageError("--timeout-headroom requires the pytest-timeout plugin")
    # xdist workers measure; the controller (or a serial run) summarizes.
    if not hasattr(config, "workerinput"):
        summary = _HeadroomSummary(fraction, warn)
        config.pluginmanager.register(summary, "timeout-headroom-summary")


class _HeadroomSummary:
    """Collect per-test headroom ratios so CI logs prove the guard ran."""

    def __init__(self, fraction: float, warn: float | None) -> None:
        self.fraction = fraction
        self.warn = warn
        self.checked = 0
        self.highest: tuple[float, str] | None = None
        self.warnings: list[str] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        properties = dict(report.user_properties)
        ratio = properties.get(_RATIO_PROPERTY)
        if report.when != "teardown" or ratio is None:
            return
        self.checked += 1
        if self.highest is None or ratio > self.highest[0]:
            self.highest = (ratio, report.nodeid)
        if _WARNING_PROPERTY in properties:
            self.warnings.append(properties[_WARNING_PROPERTY])

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        line = f"timeout headroom: limit {self.fraction:.1%}"
        if self.warn is not None:
            line += f", warning above {self.warn:.1%}"
        line += f", {self.checked} tests checked"
        if self.highest is not None:
            line += f", highest {self.highest[0]:.1%} ({self.highest[1]})"
        terminalreporter.write_line(line)
        annotate = os.environ.get("GITHUB_ACTIONS") == "true"
        for message in self.warnings:
            terminalreporter.write_line(f"timeout headroom warning: {message}")
            if annotate:
                terminalreporter.write_line(_github_warning(message))


def _github_warning(message: str) -> str:
    """Render a one-line message as a GitHub Actions warning annotation."""
    # Workflow commands treat '%' as an escape character.
    return f"::warning title=Timeout headroom::{message.replace('%', '%25')}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Accumulate phase durations and judge the total once teardown finishes."""
    del call  # hook signature; the report carries the phase and duration
    report = yield
    fraction = item.config.getoption("timeout_headroom")
    if fraction is None:
        return report
    item.stash[_PHASE_NOT_PASSED] = item.stash.get(_PHASE_NOT_PASSED, False) or not report.passed
    budget_scope = _budget_scope(item)
    if budget_scope is None:
        return report
    budget, func_only = budget_scope
    if not func_only or report.when == "call":
        item.stash[_ELAPSED] = item.stash.get(_ELAPSED, 0.0) + report.duration
    # Only passing tests are eligible; failures, skips, and xfails retain their outcome.
    if report.when == "teardown" and not item.stash[_PHASE_NOT_PASSED]:
        elapsed = item.stash.get(_ELAPSED, 0.0)
        report.user_properties.append((_RATIO_PROPERTY, round(elapsed / budget, 4)))
        warn = item.config.getoption("timeout_headroom_warn")
        if elapsed > fraction * budget:
            report.outcome = "failed"
            report.longrepr = _overrun(
                item.nodeid, elapsed, budget, f"the headroom limit is {fraction:.1%}"
            )
        elif warn is not None and elapsed > warn * budget:
            message = _overrun(item.nodeid, elapsed, budget, f"warning above {warn:.1%}")
            report.user_properties.append((_WARNING_PROPERTY, message))
    return report


def _budget_scope(item: pytest.Item) -> tuple[float, bool] | None:
    """Return the effective budget and whether it covers only the call phase."""
    # pytest-timeout has no public resolver for an item's effective settings.
    # The pinned version's private helper applies markers, CLI, ini, and env
    # exactly as the timer does, so the guard cannot disagree with the kill.
    from pytest_timeout import _get_item_settings

    settings = _get_item_settings(item)
    if not settings.timeout or settings.timeout <= 0:
        return None
    return float(settings.timeout), bool(settings.func_only)


def _overrun(nodeid: str, elapsed: float, budget: float, limit: str) -> str:
    """Explain a headroom overrun and name the budget the sizing rule assigns."""
    # Never suggest a budget at or below the current one (possible when the
    # limit is under one third).
    target = max(suggested_budget(elapsed), math.floor(budget) + _BUDGET_GRANULARITY_S)
    return (
        f"{nodeid} used {elapsed:.1f} s of its {budget:g} s timeout "
        f"({elapsed / budget:.1%}; {limit}). On Windows an "
        "expired budget kills the xdist worker without a traceback (#1202). Raise the "
        f"test's @pytest.mark.timeout to at least {target} s "
        "(3x its slowest Windows CI duration), or make the test faster."
    )
