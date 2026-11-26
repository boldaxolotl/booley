from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).parents[2] / ".github/scripts/ci_duration_budget.py"
SPEC = importlib.util.spec_from_file_location("ci_duration_budget", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ci_duration_budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_duration_budget)


@pytest.mark.parametrize(
    ("now", "passed", "verdict"), [(1_179, True, "PASS"), (1_181, False, "FAIL")]
)
def test_duration_against_budget(now: int, passed: bool, verdict: str) -> None:
    result = ci_duration_budget.evaluate("bwave-smoke", started_at=100, now=now, budget=1_080)

    assert result.elapsed_seconds == now - 100
    assert result.passed is passed
    assert verdict in ci_duration_budget.summary_line(result)


@pytest.mark.parametrize(
    ("started_at", "now", "budget", "warn", "message"),
    [
        (0, 100, 10, None, "started_at"),
        (100, 99, 10, None, "precedes"),
        (100, 101, 0, None, "budget"),
        (100, 101, 10, 0, "warn must be positive"),
        (100, 101, 10, 11, "warn must not exceed budget"),
    ],
)
def test_duration_rejects_invalid_boundaries(
    started_at: int, now: int, budget: int, warn: int | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ci_duration_budget.evaluate(
            "bwave-smoke", started_at=started_at, now=now, budget=budget, warn=warn
        )


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, elapsed: int, warn: int, budget: int
) -> tuple[int, Path]:
    """Run ``main()`` for a job that took ``elapsed`` seconds; return exit code and summary path."""
    summary = tmp_path / "summary.md"
    argv = [
        "ci_duration_budget.py",
        "--label",
        "bwave-smoke",
        "--started-at-epoch",
        "1000",
        "--now-epoch",
        str(1000 + elapsed),
        "--warn-seconds",
        str(warn),
        "--budget-seconds",
        str(budget),
        "--summary",
        str(summary),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return ci_duration_budget.main(), summary


def test_cli_rejects_warn_above_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="warn"):
        _run_cli(monkeypatch, tmp_path, elapsed=600, warn=661, budget=660)

    assert not (tmp_path / "summary.md").exists()


_WARNING = (
    "::warning title=Duration budget::bwave-smoke took 650s, "
    "over the 640s warn threshold (fail at 660s)"
)


@pytest.mark.parametrize(
    ("elapsed", "warn", "budget", "code", "verdict", "annotated"),
    [
        (630, 640, 660, 0, "PASS", False),
        (640, 640, 660, 0, "PASS", False),  # exactly at warn: still silent
        (650, 640, 660, 0, "WARN", True),
        (660, 640, 660, 0, "WARN", True),  # exactly at budget: warns, does not fail
        (661, 640, 660, 1, "FAIL", False),
        (650, 660, 660, 0, "PASS", False),  # warn == budget is accepted (no band)
    ],
)
def test_cli_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    elapsed: int,
    warn: int,
    budget: int,
    code: int,
    verdict: str,
    annotated: bool,
) -> None:
    exit_code, summary = _run_cli(monkeypatch, tmp_path, elapsed=elapsed, warn=warn, budget=budget)

    out = capsys.readouterr().out
    text = summary.read_text(encoding="utf-8")
    assert exit_code == code
    assert verdict in out
    assert verdict in text
    assert ("::warning" in out) is annotated
    assert "::warning" not in text
    if elapsed == 650 and warn == 640:
        assert _WARNING in out.splitlines()
        assert "+10s" in text


def test_bwave_smoke_budget_passes_the_slow_runner_run() -> None:
    """Run 43927091139 (#4537): a 2.3x slower runner took 848 s with no test failing."""
    workflow = yaml.safe_load(
        (SCRIPT.parents[1] / "workflows/test.yml").read_text(encoding="utf-8")
    )
    (step,) = [
        step
        for step in workflow["jobs"]["bwave-smoke"]["steps"]
        if step.get("name") == "Enforce duration budget"
    ]
    warn = step["env"]["STANDARD_WARN_SECONDS"]
    budget = step["env"]["STANDARD_BUDGET_SECONDS"]

    result = ci_duration_budget.evaluate(
        "bwave-smoke", started_at=1, now=1 + 848, budget=budget, warn=warn
    )

    assert result.passed
    assert warn < budget
