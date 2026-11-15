"""Simulation baseline requests keep working after a Sandbox mount point change.

The criterion tests of issue 4124 live here, not under ``tests/goals``, because
the broad run never collects that directory (``tests/conftest.py``).
"""

# ruff: noqa: F811  (the Goal fixtures are imported, then named as test arguments)
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.booley_dir import reset_cache
from booley.goals.sim_baseline_store import (
    SYNTH_BASELINE,
    baseline_observations,
    read_document,
    sim_goals,
    write_document,
)
from booley.mcp.sim_baseline_execution import reap_owned_checkout, run_baseline
from booley.runtime import job_records
from booley.runtime.timefmt import utc_now_rfc3339
from tests.goals.conftest import enter_goals, git, layout  # noqa: F401  (fixtures)
from tests.goals.test_sim_baseline import (
    _execute,
    _schedule,
    _version_booley_directory,
    baseline,  # noqa: F401  (fixture)
)
from tests.mount_change_support import respell, simulate_mount_change


def _dead_job(request: dict, pid: int) -> None:
    job_records.write_record(
        job_records.JobRecord(
            request["job_id"],
            "sim_baseline",
            utc_now_rfc3339(),
            60,
            pid=pid,
            status=job_records.STATUS_FAILED,
        ),
        Path(request["jobs_root"]),
    )


def test_interrupted_request_under_former_mount_reaps_its_scratch_checkout(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, raw = _schedule(baseline)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    owned = baseline.control / f".baseline-wt-{process.pid}-{baseline.record.base_sha[:7]}"
    git(baseline.main, "worktree", "add", "--detach", "-q", str(owned), baseline.record.base_sha)
    write_document(
        path,
        raw
        | {
            "owned_checkout": str(owned),
            "scratch_parent": str(owned.parent),
            "owned_paired_checkout": None,
        },
    )
    _dead_job(raw, process.pid)
    former = simulate_mount_change(monkeypatch, baseline.control, tmp_path / "former-mount")
    respell(path, baseline.control, former)

    _schedule(baseline)

    assert not owned.exists()
    assert str(owned) not in git(baseline.main, "worktree", "list", "--porcelain")
    rewritten = read_document(path)
    assert rewritten["status"] == "unavailable"
    assert json.loads(path.read_bytes())["booley_dir"] == str(baseline.control)
    for key in ("booley_dir", "jobs_root", "scratch_parent"):
        assert str(baseline.control) in rewritten[key]
        assert former not in rewritten[key]
    assert rewritten["owned_checkout"] == str(owned)


def test_interrupted_paired_scratch_checkout_under_former_mount_is_cleaned_up(
    layout: SimpleNamespace,
    tmp_path: Path,
    isolated_git_excludes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _version_booley_directory(layout, isolated_git_excludes, tmp_path)
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])
    path, request = _schedule(mode)

    class KilledError(Exception):
        """Stands in for SIGKILL: the supervisor's own cleanup never runs."""

    def execute(checkout, target, runs, output):
        raise KilledError

    monkeypatch.delenv("BOOLEY_DIR")
    reset_cache()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                "booley.flows.baseline_worktree._cleanup_baseline_worktree", lambda *args: None
            )
            with pytest.raises(KilledError):
                run_baseline(path, request, execute=execute)
    finally:
        monkeypatch.setenv("BOOLEY_DIR", str(layout.control))
        reset_cache()
    stored = read_document(path)
    scratch, paired = Path(stored["owned_checkout"]), Path(stored["owned_paired_checkout"])
    assert scratch.exists()
    assert paired.exists()

    former = simulate_mount_change(monkeypatch, layout.control, tmp_path / "former-mount")
    respell(path, layout.control, former)
    assert json.loads(path.read_bytes())["source_repository"].startswith(former)

    reap_owned_checkout(read_document(path), os.getpid())

    assert not scratch.exists()
    assert not paired.exists()
    assert ".baseline-wt-" not in git(layout.control, "worktree", "list", "--porcelain")
    assert ".baseline-wt-" not in git(layout.worktree, "worktree", "list", "--porcelain")


def test_request_naming_another_checkout_under_former_mount_still_fails_ownership(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, raw = _schedule(baseline)
    owned = baseline.control / f".baseline-wt-{os.getpid()}-x"
    owned.mkdir()
    elsewhere = baseline.control / "elsewhere"
    elsewhere.mkdir()
    write_document(
        path,
        raw | {"owned_checkout": str(owned), "scratch_parent": str(elsewhere)},
    )
    former = simulate_mount_change(monkeypatch, baseline.control, tmp_path / "former-mount")
    respell(path, baseline.control, former)

    with pytest.raises(ValueError, match="scratch ownership differs"):
        reap_owned_checkout(read_document(path), os.getpid())

    assert owned.exists()


def test_pending_request_under_former_mount_runs_under_current_mount(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, _request = _schedule(baseline)
    former = simulate_mount_change(monkeypatch, baseline.control, tmp_path / "former-mount")
    respell(path, baseline.control, former)

    rows = run_baseline(path, read_document(path, None), execute=_execute)

    assert rows[0]["grade"] == "pass"
    assert not (tmp_path / "former-mount").exists()
    assert (baseline.control / ".runtime").exists()


def test_synthesis_child_request_under_former_mount_reads_current_paths(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    former = simulate_mount_change(monkeypatch, baseline.control, tmp_path / "former-mount")
    path = tmp_path / "synthesis-request.json"
    write_document(
        path,
        {
            "schema": SYNTH_BASELINE.schema,
            "checkout": f"{former}/scratch/checkout",
            "output": f"{former}/scratch/output",
            "owned_paired_checkout": None,
        },
    )

    document = read_document(path, None)

    assert document["checkout"] == str(baseline.control / "scratch" / "checkout")
    assert document["output"] == str(baseline.control / "scratch" / "output")
    assert document["owned_paired_checkout"] is None


def test_unreadable_mount_history_reports_unavailable_naming_the_file(
    baseline: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, _request = _schedule(baseline)
    former = simulate_mount_change(monkeypatch, baseline.control, tmp_path / "former-mount")
    respell(path, baseline.control, former)
    history = baseline.control / ".runtime" / "sandbox-mounts.json"
    history.write_bytes(b"{not json")

    observed = baseline_observations(
        baseline.control, baseline.record, sim_goals(baseline.record)[0]
    )

    assert observed["status"] == "unavailable"
    assert str(history) in observed["reason"]
