"""``booley measure --validate`` reads the working tree's ``metrics.toml`` and measures nothing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from booley.harness import booley as cli
from tests.measurement.metrics_project_support import EXAMPLE_METRICS, write_metrics_project


def _measure(monkeypatch: pytest.MonkeyPatch, project: Path, *arguments: str) -> int:
    """Run the real entry point as ``booley -C <project> measure <arguments>`` in a Sandbox."""
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(project), "measure", *arguments])
    return cli._dispatch_main()


def _listing(project: Path) -> list[str]:
    """Every path in the Project's Booley Directory, so a test can show nothing was written."""
    data = project / ".booley"
    return sorted(str(path.relative_to(data)) for path in data.rglob("*"))


def test_validate_valid_file_exits_zero_and_runs_no_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path, EXAMPLE_METRICS)  # not a Git repository: no commit exists
    before = _listing(tmp_path)
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    out = capsys.readouterr().out
    assert "metrics.toml is valid: 6 tables" in out
    assert "[sim_fifo.sim]" in out
    assert _listing(tmp_path) == before


def test_validate_lists_every_load_error_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(
        tmp_path,
        "[nope.sim]\ntests_failed_max = 0\n[sim_core.sim]\nbogus_max = 1\n",
    )
    assert _measure(monkeypatch, tmp_path, "--validate") == 2
    err = capsys.readouterr().err
    assert "[nope.sim]" in err
    assert "[sim_core.sim] bogus_max: " in err
    assert "metrics.toml has 2 load errors" in err


def test_validate_reports_a_table_free_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path, "[defaults.sim]\ntests_failed_max = 0\n")
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    assert "declares no `[<target>.<flow>]` table" in capsys.readouterr().out


def test_measure_without_validate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path, EXAMPLE_METRICS)
    assert _measure(monkeypatch, tmp_path) == 2
    assert "measurement is not available yet" in capsys.readouterr().err


def test_validate_without_metrics_file_declares_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path)
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    out = capsys.readouterr().out
    assert "no metrics.toml" in out
    assert "declares nothing" in out
