from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/ci_duration_budget.py"
SPEC = importlib.util.spec_from_file_location("ci_duration_budget", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ci_duration_budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_duration_budget)


def test_duration_within_budget_passes() -> None:
    result = ci_duration_budget.evaluate("bwave-smoke", started_at=100, now=1_179, budget=1_080)

    assert result.elapsed_seconds == 1_079
    assert result.passed is True
    assert "PASS" in ci_duration_budget.summary_line(result)


def test_duration_over_budget_fails() -> None:
    result = ci_duration_budget.evaluate("bwave-smoke", started_at=100, now=1_181, budget=1_080)

    assert result.elapsed_seconds == 1_081
    assert result.passed is False
    assert "FAIL" in ci_duration_budget.summary_line(result)


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


def test_cli_under_warn_passes_without_annotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary = _run_cli(monkeypatch, tmp_path, elapsed=630, warn=640, budget=660)

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out
    assert "::warning" not in out
    assert "::warning" not in summary.read_text(encoding="utf-8")


def test_cli_between_warn_and_budget_warns_and_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary = _run_cli(monkeypatch, tmp_path, elapsed=650, warn=640, budget=660)

    out = capsys.readouterr().out
    text = summary.read_text(encoding="utf-8")
    assert code == 0
    assert (
        "::warning title=Duration budget::bwave-smoke took 650s, "
        "over the 640s warn threshold (fail at 660s)"
    ) in out.splitlines()
    assert "WARN" in text
    assert "+10s" in text
    assert "::warning" not in text


def test_cli_over_budget_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary = _run_cli(monkeypatch, tmp_path, elapsed=661, warn=640, budget=660)

    assert code == 1
    assert "FAIL" in capsys.readouterr().out
    assert "FAIL" in summary.read_text(encoding="utf-8")


def test_cli_rejects_warn_above_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="warn"):
        _run_cli(monkeypatch, tmp_path, elapsed=600, warn=661, budget=660)

    assert not (tmp_path / "summary.md").exists()


@pytest.mark.parametrize(
    ("elapsed", "warn", "budget", "code", "verdict"),
    [
        (640, 640, 660, 0, "PASS"),  # exactly at warn: still silent
        (660, 640, 660, 0, "WARN"),  # exactly at budget: warns, does not fail
        (650, 660, 660, 0, "PASS"),  # warn == budget is accepted (no band)
    ],
)
def test_cli_thresholds_are_inclusive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    elapsed: int,
    warn: int,
    budget: int,
    code: int,
    verdict: str,
) -> None:
    assert _run_cli(monkeypatch, tmp_path, elapsed=elapsed, warn=warn, budget=budget)[0] == code
    assert verdict in capsys.readouterr().out
