"""Real EDA coverage and streaming regressions for newly scaffolded SV projects."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.bwave.native_binary import require_exact_binary

from booley.dev_support.test_profiles import native_test_binary, native_test_problem
from booley.flows.sim.coverage_reference import resolve_coverage_campaign_reference
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.harness.setup.scaffold import ScaffoldChoices, scaffold_files
from booley.runtime.booley_dir import reset_cache

_TOOLS = ("verilator", "verilator_coverage", "make", "g++", "fusesoc")
pytestmark = [
    pytest.mark.skipif(
        any(shutil.which(tool) is None for tool in _TOOLS),
        reason="real scaffold smoke requires Verilator, coverage, make, g++, FuseSoC",
    ),
    pytest.mark.timeout(180),
]


@pytest.fixture
def scaffold_native_binary(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    binary = native_test_binary(root)
    if not binary.is_file():
        pytest.skip("real scaffold smoke requires this checkout's native B-Wave debug build")
    assert native_test_problem(root) is None
    monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(binary))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr(
        "booley.runtime.paths.native_bwave_binary", lambda: require_exact_binary(binary)
    )
    monkeypatch.setattr(
        "booley.bwave.cli.native_bwave_binary", lambda: require_exact_binary(binary)
    )
    return binary


def _write_project(root: Path, *, stealth: bool = False) -> None:
    choices = ScaffoldChoices(
        "wave_counter", "verilator", "sv", "verilator", False, None, stealth=stealth
    )
    for name, content in scaffold_files(choices).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, timeout=10, capture_output=True, check=True)
    if stealth:
        # The layout `booley init` leaves behind: the Booley Directory is hidden
        # from the host repository and FuseSoC skips worktree copies.
        (root / ".booley/FUSESOC_IGNORE").touch()
        (root / ".git/info/exclude").write_text("/.booley\n/.booley-projected-*.core\n")
    for args in (
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "scaffold",
        ],
    ):
        subprocess.run(["git", *args], cwd=root, timeout=10, capture_output=True, check=True)


def _assert_coverage(reports: Path, detail: dict) -> None:
    ref = detail["targets"]["sim"]["coverage_campaign"]
    assert ref["path_base"] == "report_invocation"
    path = reports / "sim/1" / ref["path"]
    resolved = resolve_coverage_campaign_reference(path)
    campaign = resolved.loaded.campaign
    artifact_root = resolved.campaign_path.parent
    assert campaign.collection["status"] == "complete"
    assert campaign.coverage_window["mode"] == "whole_run"
    assert {run.test for run in campaign.runs} == {"reset", "count"}
    assert all(run.simulation_verdict == "pass" for run in campaign.runs)
    raw = [a for a in campaign.artifacts if a.kind == "raw_native"]
    assert len(raw) == 2
    assert all((artifact_root / a.path).stat().st_size > 0 for a in raw)
    hooks = [a for a in campaign.artifacts if a.kind == "coverage_hook_evidence"]
    assert len(hooks) == 2
    for artifact in hooks:
        evidence = json.loads((artifact_root / artifact.path).read_text())
        assert evidence["events"] == [{"hook": "write", "sequence": 1, "success": True}]


@pytest.mark.parametrize(
    "coverage,trace", [(False, False), (False, True), (True, False), (True, True)]
)
def test_real_scaffold_simulation_modes(
    tmp_path, monkeypatch, scaffold_native_binary, coverage, trace
):
    if trace and os.name != "posix":
        pytest.skip("real scaffold trace smoke requires POSIX FIFOs")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    _write_project(tmp_path)
    reports = tmp_path / "reports"
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=tmp_path,
            coverage=coverage,
            trace=trace,
            timeout_ms=60_000,
            report_dir=reports,
        )
    )
    assert result.exit_code == 0, result.outcome
    if coverage:
        _assert_coverage(reports, result.outcome.detail)
    else:
        logs = "\n".join(path.read_text() for path in reports.rglob("*run.log"))
        for index in (0, 1):
            assert f"tb_wave_counter: test_id={index} finished with 0 error(s)" in logs
    if trace:
        _assert_trace(tmp_path, reports, scaffold_native_binary)


def _assert_trace(tmp_path, reports, scaffold_native_binary):
    assert os.name == "posix", "streaming smoke requires POSIX FIFOs"
    assert not list(tmp_path.rglob("dump.vcd")), "Flow must honor its per-attempt FIFO"
    # Campaign publication retains evidence copies as well as producing stores.
    stores = list((reports / "sim/1").rglob("*.fst"))
    assert stores, "current invocation must retain a queryable Trace Artifact"
    for store in stores:
        query = subprocess.run(
            [str(scaffold_native_binary), "list", str(store), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert json.loads(query.stdout)["data"]["signal_count"] > 0


def test_real_stealth_scaffold_simulation(tmp_path, monkeypatch, scaffold_native_binary):
    """The relocated cores under .booley/cores/ are found and simulated."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    _write_project(tmp_path, stealth=True)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10
    ).stdout.split()
    assert not [path for path in tracked if path.endswith(".core")]
    reports = tmp_path / "reports"
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=tmp_path,
            trace=True,
            timeout_ms=60_000,
            report_dir=reports,
        )
    )
    assert result.exit_code == 0, result.outcome
    logs = "\n".join(path.read_text() for path in reports.rglob("*run.log"))
    for index in (0, 1):
        assert f"tb_wave_counter: test_id={index} finished with 0 error(s)" in logs
