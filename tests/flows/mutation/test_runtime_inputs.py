"""The Mutation Flow exposes Target-declared ``copyto`` runtime inputs (issue #2061).

A testbench that opens a data file reads it from the configured ``run_cwd``.
FuseSoC ``copyto`` only places the file in the build root, so the Mutation
Flow must stage it the way the Simulation Flow does: before each run, removed
afterwards, with a missing declared ``$readmemh`` input read as infrastructure.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import booley
from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.flows.mutation.flow import run_tool
from booley.flows.mutation.memory_claims import CampaignClaims
from booley.flows.sim.backends import cocotb_results as cr
from booley.fusesoc.fusesoc_registry import ResolvedFile
from booley.runtime.endpoint_execution import EXIT_ERROR, EXIT_SUCCESS, EndpointOutcome
from tests.flows.mutation.test_flow import (
    _SCOPE,
    _fake_proc,
    _make_endpoint,
    _patch_resolve_target,
    _prepare_scope_files,
)
from tests.flows.sim.launched_work import launched_work
from tests.flows.sim.simulation_record_test_support import as_if_the_sandbox_restarted

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stand-in V<top> is a POSIX shell script")

_PAYLOAD = "deadbeef\n"
_PASSING_BINARY = """#!/bin/sh
if [ "$(cat vectors.hex 2>/dev/null)" = "deadbeef" ]; then
  echo "[SIM_RESULT] PASSED"
  exit 0
fi
echo "ERROR: Can't open file vectors.hex"
exit 1
"""
# Reports a $readmemh failure for a file the binary itself removed, then hangs
# like a real simulator spinning on uninitialised memory.
_MISSING_READMEMH_BINARY = """#!/bin/sh
rm -f vectors.hex
echo '$readmemh: Unable to open vectors.hex for reading'
sleep 30
"""
_ADAPTERS = ("verilator", "icarus", "cocotb")


_LAUNCH_MARK = "booley.flows.sim.backends."


def _copyto_run(
    tmp_path: Path,
    monkeypatch,
    adapter: str = "verilator",
    binary: str = _PASSING_BINARY,
    *,
    launch: Any = None,
    staged_before: str | None = None,
    staged_inode: list[int] | None = None,
) -> tuple[EndpointOutcome, Path]:
    """Run a whole one-Mutant campaign whose Target ``copyto``s ``vectors.hex`` (issue #2061).

    The adapter's run half is real and starts the stand-in ``Vtb`` of *binary* in the
    configured ``run_cwd`` (``run``); *launch(cmd, original)* may replace a launch of the
    run half.  Returns the campaign result and the ``run`` directory.
    """
    # The real run half is a child `python -m booley...` started in the checkout.
    monkeypatch.setenv("PYTHONPATH", str(Path(booley.__file__).resolve().parents[1]))
    tool = "icarus" if adapter == "icarus" else "verilator"
    _patch_resolve_target(
        monkeypatch, eda_tool=tool, cocotb_module="tb_test" if adapter == "cocotb" else None
    )
    fake_prepare = _flow_attr("prepare_simulation_build")

    def _prepare(handle, *, build_root, **kwargs):
        # Like FuseSoC, `copyto` places the file in the build root beside the binary.
        prepared = fake_prepare(handle, build_root=build_root, **kwargs)
        root = Path(build_root)
        (root / "vectors.hex").write_text(_PAYLOAD, encoding="utf-8")
        (root / "Vtb").write_text(binary, encoding="utf-8")
        (root / "Vtb").chmod(0o755)
        resolved = SimpleNamespace(
            **{**vars(prepared.resolved), "files": (ResolvedFile("vectors.hex", "user"),)}
        )
        return replace(prepared, resolved=resolved, make_argv=(sys.executable, "-c", ""))

    monkeypatch.setattr("booley.flows.mutation.flow.prepare_simulation_build", _prepare)
    (tmp_path / ".booley").mkdir(exist_ok=True)
    (tmp_path / ".booley" / "booley.toml").write_text(
        '[project]\nname = "demo"\n\n[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    if staged_before is not None:
        (run_dir / "vectors.hex").write_text(staged_before, encoding="utf-8")
        if staged_inode is not None:
            staged_inode.append((run_dir / "vectors.hex").stat().st_ino)
    if launch is not None:
        original = run_tool

        def _run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and _LAUNCH_MARK in " ".join(cmd):
                return launch(cmd)
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr("booley.flows.mutation.flow.run_tool", _run)
    _prepare_scope_files(tmp_path, [_SCOPE])
    endpoint = _make_endpoint(
        tmp_path, monkeypatch, target="default", scope=_SCOPE, count=1, min_detected=0, tb_top="tb"
    )
    return endpoint.run_with_claims(CampaignClaims()), run_dir


def _flow_attr(name: str) -> Any:
    from booley.flows.mutation import flow

    return getattr(flow, name)


def _overlays(root: Path) -> list[Path]:
    """Overlay directories any run left behind under the checkout."""
    return list(root.rglob(".booley-runtime-inputs"))


def _ledger_records(root: Path) -> list[Path]:
    """Staged-input records any run left in the checkout's Booley Directory."""
    ledger = resolve_checkout_booley_dir(root) / ".runtime/staged-runtime-inputs"
    return list(ledger.glob("*.json"))


def test_a_run_stages_the_copyto_input_for_baseline_and_mutant(
    tmp_path: Path, monkeypatch
) -> None:
    """Both the baseline and the Mutant see ``vectors.hex``; nothing is left behind."""
    result, run_dir = _copyto_run(tmp_path, monkeypatch)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [row["status"] for row in result.detail["classified"]] == ["not_detected"]
    assert list(run_dir.iterdir()) == []
    assert _overlays(tmp_path) == []
    assert _ledger_records(tmp_path) == []


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_pinned_runs_stage_inputs_for_every_adapter(
    tmp_path: Path, monkeypatch, adapter: str
) -> None:
    seen: list[tuple[tuple[str, ...], str | None]] = []
    run_dir = tmp_path / "run"
    passing_line = cr.format_results_line(
        cr.results_payload(
            cr.CocotbResults(state=cr.STATE_OK, tests=(cr.CocotbTest("t", "tb", cr.VERDICT_PASS),))
        )
    )

    def _launch(cmd):
        staged = run_dir / "vectors.hex"
        text = staged.read_text(encoding="utf-8") if staged.is_file() else None
        seen.append((launched_work(cmd).runtime_inputs, text))
        return _fake_proc(rc=0, stdout=passing_line if adapter == "cocotb" else "passed")

    result, _ = _copyto_run(tmp_path, monkeypatch, adapter, launch=_launch)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert seen == [(("vectors.hex",), _PAYLOAD)] * 2  # the baseline and the Mutant
    assert list(run_dir.iterdir()) == []
    assert _overlays(tmp_path) == []


def test_staged_input_is_removed_after_timeout(tmp_path: Path, monkeypatch) -> None:
    visible: list[bool] = []
    run_dir = tmp_path / "run"

    def _hang(cmd):
        visible.append((run_dir / "vectors.hex").is_file())
        raise subprocess.TimeoutExpired(cmd, 1)

    result, _ = _copyto_run(tmp_path, monkeypatch, launch=_hang)

    assert result.exit_code == EXIT_ERROR
    assert "RESULT: ERROR (baseline:" in result.report_text
    assert visible == [True]
    assert list(run_dir.iterdir()) == []


def test_declared_unstaged_readmemh_input_is_inconclusive(tmp_path: Path, monkeypatch) -> None:
    result, _ = _copyto_run(tmp_path, monkeypatch, binary=_MISSING_READMEMH_BINARY)

    assert result.exit_code == EXIT_ERROR
    assert "declared runtime input 'vectors.hex' was not staged" in result.report_text


def test_conflicting_destination_is_inconclusive_setup_failure(
    tmp_path: Path, monkeypatch
) -> None:
    launches: list[object] = []
    result, run_dir = _copyto_run(
        tmp_path,
        monkeypatch,
        staged_before="project data\n",
        launch=lambda cmd: launches.append(cmd) or _fake_proc(),
    )

    assert result.exit_code == EXIT_ERROR
    assert (
        "Simulation runtime input setup failed: "
        "run input conflicts with an existing path: vectors.hex"
    ) in result.report_text
    assert (run_dir / "vectors.hex").read_text(encoding="utf-8") == "project data\n"
    assert launches == []


def test_identical_existing_destination_is_left_untouched(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    inode: list[int] = []
    created: list[int] = []

    def _stat_and_pass(_cmd):
        inode.append((run_dir / "vectors.hex").stat().st_ino)
        return _fake_proc(rc=0, stdout="passed")

    # Create the destination before the campaign; the launch sees the same file.
    result, _ = _copyto_run(
        tmp_path,
        monkeypatch,
        staged_before=_PAYLOAD,
        staged_inode=created,
        launch=_stat_and_pass,
    )

    staged = run_dir / "vectors.hex"
    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert staged.is_file() and not staged.is_symlink()
    assert staged.read_text(encoding="utf-8") == _PAYLOAD
    assert set(inode) == {created[0]} == {staged.stat().st_ino}


@pytest.mark.timeout(120)
@pytest.mark.parametrize("restarted", [False, True])
def test_mutant_killed_by_sigterm_leaves_no_link_for_the_next_run(
    tmp_path: Path, monkeypatch, restarted: bool
) -> None:
    """A Mutant run killed in its process group does not break the next run's staging."""
    launches: list[object] = []

    def _die_on_the_mutant(cmd):
        launches.append(cmd)
        if len(launches) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return _fake_proc(rc=0, stdout="passed")

    pid = os.fork()
    if pid == 0:
        try:
            _copyto_run(tmp_path, monkeypatch, launch=_die_on_the_mutant)
        finally:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGTERM
    run_dir = tmp_path / "run"
    assert (run_dir / "vectors.hex").is_symlink()
    for overlay in _overlays(tmp_path):
        shutil.rmtree(overlay)
    assert not (run_dir / "vectors.hex").exists()
    if restarted:
        as_if_the_sandbox_restarted(resolve_checkout_booley_dir(tmp_path))

    result, _ = _copyto_run(tmp_path, monkeypatch)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert list(run_dir.iterdir()) == []
    assert _ledger_records(tmp_path) == []
