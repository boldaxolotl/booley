"""Retention exercised against actual canonical Campaigns and filesystem paths."""

import json
from dataclasses import replace

import pytest

from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    prepare_coverage_invocation,
)
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.coverage_transaction import run_coverage_target
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution


def campaign(tmp_path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    invocation = tmp_path / "reports/sim/1"
    plan = replace(prepared.plan.targets[0], invocation_dir=invocation)
    progress = CoverageProgress(invocation, ("sim_0",))
    outcome = run_coverage_target(plan, NativeExecution(), progress)
    progress.checkpoint(complete=True)
    assert outcome.exit_code == 0
    return outcome


def test_coverage_progress_stamps_run_identity_and_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_RUN_ID", "sim-run-1")
    invocation = tmp_path / "reports/sim/1"
    CoverageProgress(invocation, ("sim_0",)).checkpoint()
    progress = json.loads((invocation / "progress.json").read_text())
    assert progress["run_id"] == "sim-run-1"
    assert progress["timestamp"].endswith("Z")


def test_coverage_progress_rejects_inconsistent_terminal_phase(tmp_path):
    progress = CoverageProgress(tmp_path / "reports/sim/1", ("sim_0",))

    with pytest.raises(ValueError, match="complete and phase disagree"):
        progress.checkpoint(phase="complete")


def test_coverage_infrastructure_failure_remains_pending_in_terminal_progress(tmp_path):
    class Unavailable(NativeExecution):
        def build(self, request):
            raise FileNotFoundError("Verilator disappeared")

    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    invocation = tmp_path / "reports/sim/1"
    plan = replace(prepared.plan.targets[0], invocation_dir=invocation)
    progress = CoverageProgress(invocation, ("sim_0",))
    progress.checkpoint()

    outcome = run_coverage_target(plan, Unavailable(), progress)
    progress.checkpoint(complete=True, phase="aborted")

    document = json.loads((invocation / "progress.json").read_text())
    assert outcome.abort_remaining is True
    assert document["phase"] == "aborted"
    assert document["completed_targets"] == []
    assert document["pending_targets"] == ["sim_0"]


def test_native_pruning_preserves_normalized_campaign_and_records_availability(tmp_path):
    from booley.flows.sim.campaign_retention import prune_native_payload

    outcome = campaign(tmp_path)
    original = outcome.campaign_path.read_bytes()
    point_store = outcome.campaign_path.with_name("coverage-points.jsonl.gz")
    original_points = point_store.read_bytes()
    projection = outcome.simulation_path.read_bytes()
    root = outcome.campaign_path.parent
    (root / "hooks").mkdir()
    (root / "hooks/proof.json").write_text('{"proof": true}')
    prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert not (root / "native").exists()
    assert outcome.campaign_path.read_bytes() == original
    assert point_store.read_bytes() == original_points
    assert outcome.simulation_path.read_bytes() == projection
    assert (root / "hooks/proof.json").is_file()
    decoded = load_coverage_campaign(
        outcome.campaign_path, DurableTargetIdentity("acme:demo:counter:1#sim_0")
    ).campaign
    assert decoded.rollups[0].covered_points == 1
    availability = json.loads((root / "availability.json").read_text())
    assert availability["status"] == "pruned"
    assert availability["point_store_sha256"] == json.loads(original)["point_store"]["sha256"]
    assert len(availability["artifacts"]) == 3
    prune_native_payload(tmp_path / "reports", 1, "sim_0")


def test_full_pruning_removes_only_selected_invocation_and_keeps_number_reserved(tmp_path):
    from booley.flows.sim.campaign_retention import prune_invocation

    campaign(tmp_path)
    neighbor = tmp_path / "reports/sim/2"
    neighbor.mkdir()
    (neighbor / "keep").write_text("unrelated")
    prune_invocation(tmp_path / "reports", 1)
    assert not (tmp_path / "reports/sim/1").exists()
    assert (neighbor / "keep").read_text() == "unrelated"
    assert list((tmp_path / "reports/sim/.pruned-1").iterdir()) == []
    prune_invocation(tmp_path / "reports", 1)


@pytest.mark.parametrize("phase", ["aborted", "superseded"])
def test_full_pruning_accepts_terminal_partial_progress(tmp_path, phase):
    from booley.flows.sim.campaign_retention import prune_invocation

    invocation = tmp_path / "reports/sim/1"
    progress = CoverageProgress(invocation, ("sim_0",))
    progress.checkpoint(complete=True, phase=phase)
    prune_invocation(tmp_path / "reports", 1)
    assert not invocation.exists()
    assert list((tmp_path / "reports/sim/.pruned-1").iterdir()) == []


def test_full_pruning_accepts_abandoned_nonterminal_progress(tmp_path):
    """A producer killed before terminalizing progress leaves an abandoned invocation."""
    from booley.flows.sim.campaign_retention import prune_invocation

    invocation = tmp_path / "reports/sim/1"
    CoverageProgress(invocation, ("sim_0",)).checkpoint()

    prune_invocation(tmp_path / "reports", 1)

    assert not invocation.exists()
    assert list((tmp_path / "reports/sim/.pruned-1").iterdir()) == []


def test_full_pruning_rejects_contradictory_progress(tmp_path):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    invocation = tmp_path / "reports/sim/1"
    CoverageProgress(invocation, ("sim_0",)).checkpoint()
    progress_path = invocation / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["complete"] = True
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match="complete and phase disagree"):
        prune_invocation(tmp_path / "reports", 1)

    assert invocation.is_dir()


def test_full_pruning_rejects_changed_progress_lock(tmp_path):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    campaign(tmp_path)
    (tmp_path / "reports/sim/1/.progress.lock").write_text("changed", encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match="progress lock content is invalid"):
        prune_invocation(tmp_path / "reports", 1)


def test_pruning_rejects_an_active_invocation(tmp_path):
    import pytest

    from booley.flows.sim.campaign_reports import campaign_invocation_lock
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
        prune_native_payload,
    )

    outcome = campaign(tmp_path)
    with campaign_invocation_lock(tmp_path / "reports/sim/1"):
        with pytest.raises(CampaignRetentionError, match="invocation 1 is still being produced"):
            prune_native_payload(tmp_path / "reports", 1, "sim_0")
        with pytest.raises(CampaignRetentionError, match="invocation 1 is still being produced"):
            prune_invocation(tmp_path / "reports", 1)
    assert outcome.campaign_path.is_file()
    assert (outcome.campaign_path.parent / "native").is_dir()


@pytest.mark.usefixtures("mandatory_file_locks")
def test_full_pruning_accepts_an_empty_abandoned_reservation(tmp_path):
    from booley.flows.sim.campaign_reports import campaign_invocation_lock
    from booley.flows.sim.campaign_retention import prune_invocation

    invocation = tmp_path / "reports/sim/1"
    invocation.parent.mkdir(parents=True)
    with campaign_invocation_lock(invocation):
        invocation.mkdir()

    prune_invocation(tmp_path / "reports", 1)

    assert not invocation.exists()
    assert list((tmp_path / "reports/sim/.pruned-1").iterdir()) == []


@pytest.mark.usefixtures("mandatory_file_locks")
def test_native_pruning_points_empty_abandoned_reservation_at_full_pruning(tmp_path):
    from booley.flows.sim.campaign_reports import campaign_invocation_lock
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_native_payload,
    )

    invocation = tmp_path / "reports/sim/1"
    invocation.parent.mkdir(parents=True)
    with campaign_invocation_lock(invocation):
        invocation.mkdir()

    with pytest.raises(CampaignRetentionError, match="use --full"):
        prune_native_payload(tmp_path / "reports", 1, "sim_0")

    assert invocation.is_dir()


def test_maintenance_cli_translates_active_invocation_contention(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import booley
    from booley.flows.sim.campaign_reports import campaign_invocation_lock

    campaign(tmp_path)
    invocation = tmp_path / "reports/sim/1"
    command = [
        sys.executable,
        "-m",
        "booley.flows.sim.campaign_retention",
        "--reports-root",
        str(tmp_path / "reports"),
        "--invocation",
        "1",
        "--full",
    ]
    with campaign_invocation_lock(invocation):
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONPATH": str(Path(booley.__file__).parent.parent)},
            check=False,
        )

    assert result.returncode == 2
    assert "invocation 1 is still being produced; retry after it finishes" in result.stderr
    assert "Errno" not in result.stderr
    assert invocation.is_dir()
    assert not (tmp_path / "reports/sim/.pruned-1").exists()


@pytest.mark.parametrize(
    "defect", ["symlink", "unknown", "changed", "ambiguous_target", "wrong_invocation"]
)
def test_native_pruning_validates_every_path_before_deleting(tmp_path, defect):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_native_payload

    outcome = campaign(tmp_path)
    root = outcome.campaign_path.parent
    native = root / "native"
    if defect == "symlink":
        (native / "link").symlink_to(tmp_path / "outside")
    elif defect == "unknown":
        (native / "unknown.dat").write_text("do not delete")
    elif defect == "changed":
        (native / "merged/coverage.dat").write_text("changed after collection")
    elif defect == "ambiguous_target":
        progress = tmp_path / "reports/sim/1/progress.json"
        document = json.loads(progress.read_text())
        document["targets"].append("sim_0")
        progress.write_text(json.dumps(document))
    else:
        document = json.loads(outcome.campaign_path.read_text())
        document["invocation"]["id"] = 2
        outcome.campaign_path.write_text(json.dumps(document))
    with pytest.raises(CampaignRetentionError):
        prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert (native / "raw/001-reset.dat").is_file()
    assert not (root / "availability.json").exists()


@pytest.mark.parametrize("boundary", ["journal", "rename", "delete", "completion"])
def test_native_pruning_is_retryable_at_each_filesystem_boundary(tmp_path, monkeypatch, boundary):
    import shutil

    from booley.flows.sim.campaign_retention import prune_native_payload

    outcome = campaign(tmp_path)
    root = outcome.campaign_path.parent
    original = outcome.campaign_path.read_bytes()
    replace_file, rename, rmtree = type(root).replace, type(root).rename, shutil.rmtree
    writes = 0

    def fail_replace(source, destination):
        nonlocal writes
        if destination.name == "availability.json":
            writes += 1
            if (boundary == "journal" and writes == 1) or (
                boundary == "completion" and writes == 2
            ):
                raise OSError("injected journal failure")
        return replace_file(source, destination)

    def fail_rename(source, destination):
        if boundary == "rename":
            raise OSError("injected rename failure")
        return rename(source, destination)

    def fail_delete(path, *args, **kwargs):
        if boundary == "delete":
            # Model interruption after deleting a subset of quarantined files.
            next(path.rglob("*.dat")).unlink()
            raise OSError("injected cleanup failure")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(root), "replace", fail_replace)
        patch.setattr(type(root), "rename", fail_rename)
        patch.setattr(shutil, "rmtree", fail_delete)
        with pytest.raises(OSError):
            prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert outcome.campaign_path.read_bytes() == original
    if boundary != "journal":
        assert json.loads((root / "availability.json").read_text())["status"] == "pruning"
    prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert not (root / "native").exists()
    assert not (root / ".native-pruned").exists()
    assert json.loads((root / "availability.json").read_text())["status"] == "pruned"


def test_full_pruning_can_retry_partial_cleanup(tmp_path, monkeypatch):
    import shutil

    from booley.flows.sim.campaign_retention import prune_invocation

    campaign(tmp_path)
    with monkeypatch.context() as patch:

        def interrupted(path):
            next(path.rglob("*.dat")).unlink()
            raise OSError("cleanup interrupted")

        patch.setattr(shutil, "rmtree", interrupted)
        with pytest.raises(OSError):
            prune_invocation(tmp_path / "reports", 1)
    assert not (tmp_path / "reports/sim/1").exists()
    prune_invocation(tmp_path / "reports", 1)
    assert list((tmp_path / "reports/sim/.pruned-1").iterdir()) == []


def test_full_pruning_rejects_unidentified_quarantine(tmp_path):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    orphan = tmp_path / "reports/sim/.pruned-1"
    orphan.mkdir(parents=True)
    (orphan / "valuable").write_text("unrelated")
    with pytest.raises(CampaignRetentionError):
        prune_invocation(tmp_path / "reports", 1)
    assert (orphan / "valuable").read_text() == "unrelated"


@pytest.mark.parametrize("defect", ["missing", "unidentified_quarantine", "changed_sidecar"])
def test_native_pruning_does_not_adopt_unexplained_retention_state(tmp_path, defect):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_native_payload

    outcome = campaign(tmp_path)
    root = outcome.campaign_path.parent
    if defect == "missing":
        (root / "native/raw/001-reset.dat").unlink()
    elif defect == "unidentified_quarantine":
        (root / "native").rename(root / ".native-pruned")
    else:
        prune_native_payload(tmp_path / "reports", 1, "sim_0")
        path = root / "availability.json"
        content = json.loads(path.read_text())
        content["status"] = "invented"
        path.write_text(json.dumps(content))
    with pytest.raises(CampaignRetentionError):
        prune_native_payload(tmp_path / "reports", 1, "sim_0")


def test_maintenance_cli_requires_exact_selection_and_executes_both_modes(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import booley

    outcome = campaign(tmp_path)
    command = [
        sys.executable,
        "-m",
        "booley.flows.sim.campaign_retention",
        "--reports-root",
        str(tmp_path / "reports"),
        "--invocation",
        "1",
    ]
    environment = {**os.environ, "PYTHONPATH": str(Path(booley.__file__).parent.parent)}
    rejected = subprocess.run(
        command, capture_output=True, text=True, timeout=15, env=environment, check=False
    )
    assert rejected.returncode == 2
    assert outcome.campaign_path.is_file()
    native = subprocess.run(
        [*command, "--native-target", "sim_0"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
        check=False,
    )
    assert native.returncode == 0, native.stderr
    assert outcome.campaign_path.is_file()
    full = subprocess.run(
        [*command, "--full"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
        check=False,
    )
    assert full.returncode == 0, full.stderr
    assert not outcome.campaign_path.exists()


@pytest.mark.parametrize(
    ("defect", "relative", "expected_code"),
    [
        ("changed", "targets/sim_0/native/raw/001-reset.dat", 0),
        ("extra_native", "targets/sim_0/native/raw/999-extra.dat", 2),
        ("missing", "targets/sim_0/native/raw/001-reset.dat", 0),
        ("extra_invocation", "stray.txt", 2),
    ],
)
def test_full_pruning_refuses_only_unrecognized_content(tmp_path, defect, relative, expected_code):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import booley

    campaign(tmp_path)
    invocation = tmp_path / "reports/sim/1"
    selected = invocation / relative
    if defect == "changed":
        selected.write_bytes(selected.read_bytes() + b"changed")
    elif defect == "missing":
        selected.unlink()
    else:
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("do not delete", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "booley.flows.sim.campaign_retention",
            "--reports-root",
            str(tmp_path / "reports"),
            "--invocation",
            "1",
            "--full",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(Path(booley.__file__).parent.parent)},
        check=False,
    )

    assert result.returncode == expected_code, result.stderr
    if expected_code == 0:
        assert not invocation.exists()
    else:
        assert str(Path(relative)) in result.stderr
        assert selected.read_text(encoding="utf-8") == "do not delete"
        assert invocation.is_dir()


def test_maintenance_cli_help_scopes_project_data_to_nonstandard_full_pruning():
    import os
    import subprocess
    import sys
    from pathlib import Path

    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[3] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "booley.flows.sim.campaign_retention", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
        check=False,
    )
    assert result.returncode == 0
    help_text = " ".join(result.stdout.split()).replace("--reports- root", "--reports-root")
    assert "for --full when --reports-root is outside" in help_text
    assert "not required for --native-target" in help_text


def test_project_data_inference_accepts_only_standard_report_roots(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import _infer_project_data
    from booley.runtime.project_dir import reset_cache

    project_data = tmp_path / "project-data"
    project_data.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_data))
    reset_cache()

    assert _infer_project_data(project_data / ".runtime/flow-reports/sim/1") == project_data
    assert _infer_project_data(project_data / "flow-reports/sim/1") == project_data
    assert _infer_project_data(tmp_path / "elsewhere/flow-reports/sim/1") is None


def test_full_pruning_rejects_unresolved_completed_target(tmp_path):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    outcome = campaign(tmp_path)
    progress_path = tmp_path / "reports/sim/1/progress.json"
    progress = json.loads(progress_path.read_text())
    progress["targets"].append("missing_completed_target")
    progress["completed_targets"].append("missing_completed_target")
    progress_path.write_text(json.dumps(progress))
    with pytest.raises(CampaignRetentionError, match="completed Target"):
        prune_invocation(tmp_path / "reports", 1)
    assert outcome.campaign_path.is_file()
    assert not (tmp_path / "reports/sim/.pruned-1").exists()


@pytest.mark.parametrize(
    ("content", "valid"), [(None, False), (b"", True), (b"\0", True), (b"x", False)]
)
def test_lock_sentinel_accepts_only_platform_sentinels(tmp_path, content, valid):
    from booley.flows.sim.campaign_retention import _valid_lock_sentinel

    lock = tmp_path / ".lock"
    if content is not None:
        lock.write_bytes(content)

    assert _valid_lock_sentinel(lock) is valid


def test_lock_sentinel_read_denial_is_an_error_without_mandatory_locks(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.flows.sim import campaign_retention

    lock = tmp_path / ".lock"
    lock.write_bytes(b"")
    monkeypatch.setattr(campaign_retention, "_MANDATORY_FILE_LOCKS", False)

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError):
        campaign_retention._valid_lock_sentinel(lock)
