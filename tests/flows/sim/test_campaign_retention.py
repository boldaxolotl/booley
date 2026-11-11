"""Retention exercised against actual canonical Campaigns and filesystem paths."""

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import booley
from booley.flows.sim.campaign import (
    inspect_retained_campaign,
)
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.runtime.booley_dir import reset_cache
from booley.runtime.execution_records import ExecutionId
from tests.flows.sim.campaign_test_support import (
    _own_attempt_run_directory,
    _retained_invocation,
    coverage_invocation,
)
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.runtime.test_sandbox_layout import project_alias
from tests.subprocess_budget import run_bounded_child

__all__ = ["project_alias"]


def _run_retention_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the maintenance CLI in a subprocess that imports this checkout's Booley."""
    return run_bounded_child(
        [sys.executable, "-m", "booley.flows.sim.campaign_retention", *arguments],
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(Path(booley.__file__).parent.parent)},
    )


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        (Path("artifact"), "layout"),
        (Path("artifacts/other/revision/target/tests/test/run.log"), "role"),
        (Path("artifacts/candidate/target/not-tests/test/run.log"), "layout"),
        (Path("artifacts/candidate/target/tests/test/result.txt"), "file"),
        (Path("artifacts/candidate/target/tests/test/result/wrong.json"), "file"),
        (Path("artifacts/candidate/target/tests/test/trace/wave.txt"), "file"),
        (Path("artifacts/candidate/../tests/test/run.log"), "Unsafe"),
    ],
)
def test_invocation_artifact_layout_rejects_unowned_paths(
    relative: Path,
    message: str,
) -> None:
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _validate_invocation_artifact_layout,
    )

    with pytest.raises(CampaignRetentionError, match=message):
        _validate_invocation_artifact_layout(relative)


def test_referenced_invocation_artifact_must_exist(tmp_path: Path) -> None:
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _referenced_invocation_artifacts,
    )

    document = {
        "artifacts": {"log": "reports/sim/1/artifacts/candidate/target/tests/test/run.log"}
    }

    with pytest.raises(CampaignRetentionError, match="missing or unsafe"):
        _referenced_invocation_artifacts(tmp_path, document)


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


def test_native_pruning_preserves_normalized_campaign_and_records_availability(
    tmp_path, monkeypatch
):
    from booley.flows.sim.campaign_retention import prune_native_payload

    run = coverage_invocation(tmp_path, monkeypatch)
    original = run.campaign_path.read_bytes()
    point_store = run.campaign_path.with_name("coverage-points.jsonl.gz")
    original_points = point_store.read_bytes()
    projection = run.simulation.read_bytes()
    root = run.storage
    (root / "hooks").mkdir()
    (root / "hooks/proof.json").write_text('{"proof": true}')
    prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert not (root / "native").exists()
    assert run.campaign_path.read_bytes() == original
    assert point_store.read_bytes() == original_points
    assert run.simulation.read_bytes() == projection
    assert (root / "hooks/proof.json").is_file()
    decoded = load_coverage_campaign(
        run.campaign_path, DurableTargetIdentity("acme:demo:counter:1#sim_0")
    ).campaign
    assert decoded.rollups[0].covered_points == 1
    availability = json.loads((root / "availability.json").read_text())
    assert availability["status"] == "pruned"
    assert availability["point_store_sha256"] == json.loads(original)["point_store"]["sha256"]
    assert len(availability["artifacts"]) == 3
    prune_native_payload(tmp_path / "reports", 1, "sim_0")


def test_full_pruning_removes_only_selected_invocation_and_keeps_number_reserved(
    tmp_path, monkeypatch
):
    from booley.flows.sim.campaign_retention import prune_invocation

    coverage_invocation(tmp_path, monkeypatch)
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


@pytest.mark.parametrize(
    "artifact_relative",
    [
        "artifacts/candidate/sim_sim_0/tests/smoke/run.log",
        "artifacts/candidate/sim_sim_0/tests/batch/run.log",
        "artifacts/baseline/abc123/sim_sim_0/tests/smoke/run.log",
    ],
)
def test_full_pruning_accepts_report_referenced_invocation_artifacts(tmp_path, artifact_relative):
    from booley.flows.sim.campaign_retention import prune_invocation

    invocation = tmp_path / "reports/sim/1"
    progress = CoverageProgress(invocation, ("sim_0",))
    progress.checkpoint(complete=True)
    target = invocation / "targets/sim_0"
    target.mkdir(parents=True)
    artifact = invocation / artifact_relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("immutable evidence", encoding="utf-8")
    (target / "simulation.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "target": "sim_0",
                "tests": [
                    {
                        "name": "smoke",
                        "artifacts": {"run_log": f"reports/sim/1/{artifact_relative}"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    prune_invocation(tmp_path / "reports", 1)

    assert not invocation.exists()


def test_full_pruning_resumes_from_live_root_prune_journal(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_reports import write_campaign_json
    from booley.flows.sim.campaign_retention import prune_invocation

    coverage_invocation(tmp_path, monkeypatch)
    invocation = tmp_path / "reports/sim/1"
    write_campaign_json(invocation / ".prune.json", {"invocation": 1, "operation": "full"})

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


def test_full_pruning_rejects_changed_progress_lock(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    coverage_invocation(tmp_path, monkeypatch)
    (tmp_path / "reports/sim/1/.progress.lock").write_text("changed", encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match="progress lock content is invalid"):
        prune_invocation(tmp_path / "reports", 1)


def test_pruning_rejects_an_active_invocation(tmp_path, monkeypatch):
    import pytest

    from booley.flows.sim.campaign_reports import campaign_invocation_lock
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
        prune_native_payload,
    )

    run = coverage_invocation(tmp_path, monkeypatch)
    with campaign_invocation_lock(tmp_path / "reports/sim/1"):
        with pytest.raises(CampaignRetentionError, match="invocation 1 is still being produced"):
            prune_native_payload(tmp_path / "reports", 1, "sim_0")
        with pytest.raises(CampaignRetentionError, match="invocation 1 is still being produced"):
            prune_invocation(tmp_path / "reports", 1)
    assert run.campaign_path.is_file()
    assert (run.storage / "native").is_dir()


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


def test_maintenance_cli_translates_active_invocation_contention(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_reports import campaign_invocation_lock

    coverage_invocation(tmp_path, monkeypatch)
    invocation = tmp_path / "reports/sim/1"
    with campaign_invocation_lock(invocation):
        result = _run_retention_cli(
            "--reports-root", str(tmp_path / "reports"), "--invocation", "1", "--full"
        )

    assert result.returncode == 2
    assert "invocation 1 is still being produced; retry after it finishes" in result.stderr
    assert "Errno" not in result.stderr
    assert invocation.is_dir()
    assert not (tmp_path / "reports/sim/.pruned-1").exists()


@pytest.mark.parametrize("defect", ["symlink", "ambiguous_target", "wrong_invocation", "missing"])
def test_native_pruning_validates_every_path_before_deleting(tmp_path, defect, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_native_payload

    run = coverage_invocation(tmp_path, monkeypatch)
    root = run.storage
    native = root / "native"
    if defect == "symlink":
        (native / "link").symlink_to(tmp_path / "outside")
    elif defect == "missing":
        (native / "merged/coverage.dat").unlink()
    elif defect == "ambiguous_target":
        progress = tmp_path / "reports/sim/1/progress.json"
        document = json.loads(progress.read_text())
        document["targets"].append("sim_0")
        progress.write_text(json.dumps(document))
    else:
        document = json.loads(run.campaign_path.read_text())
        document["invocation"]["id"] = 2
        run.campaign_path.write_text(json.dumps(document))
    # The tampered nested Campaign fails the reference digest, not retention's own checks.
    expected = ValueError if defect == "wrong_invocation" else CampaignRetentionError
    with pytest.raises(expected):
        prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert (native / "raw/001-reset@1.dat").is_file()
    assert not (root / "availability.json").exists()


@pytest.mark.parametrize("boundary", ["journal", "delete", "completion"])
def test_native_pruning_is_retryable_at_each_filesystem_boundary(tmp_path, monkeypatch, boundary):
    import shutil

    from booley.flows.sim.campaign_retention import prune_native_payload

    run = coverage_invocation(tmp_path, monkeypatch)
    root = run.storage
    original = run.campaign_path.read_bytes()
    replace_file, rmtree = type(root).replace, shutil.rmtree
    # Booley never recorded this file; it must disappear with the native directory.
    (root / "native/unknown.dat").write_text("unrecorded native file")
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

    def fail_delete(path, *args, **kwargs):
        if boundary == "delete":
            # Model interruption after deleting a recorded native file; the retry
            # must not re-run the missing-payload check.
            (path / "raw/001-reset@1.dat").unlink()
            raise OSError("injected cleanup failure")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(root), "replace", fail_replace)
        patch.setattr(shutil, "rmtree", fail_delete)
        with pytest.raises(OSError):
            prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert run.campaign_path.read_bytes() == original
    if boundary != "journal":
        assert json.loads((root / "availability.json").read_text())["status"] == "pruning"
    prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert not (root / "native").exists()
    assert json.loads((root / "availability.json").read_text())["status"] == "pruned"


def test_full_pruning_can_retry_partial_cleanup(tmp_path, monkeypatch):
    import shutil

    from booley.flows.sim.campaign_retention import prune_invocation

    coverage_invocation(tmp_path, monkeypatch)
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


def test_full_pruning_finishes_journaled_quarantine_without_comparing_contents(tmp_path):
    from booley.flows.sim.campaign_retention import prune_invocation

    quarantine = tmp_path / "reports/sim/.pruned-1"
    quarantine.mkdir(parents=True)
    (quarantine / ".prune.json").write_text("{}")
    (quarantine / "leftover.dat").write_text("half-deleted invocation")

    prune_invocation(tmp_path / "reports", 1)

    assert list(quarantine.iterdir()) == []


def test_full_pruning_accepts_existing_availability_sidecar(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_invocation, prune_native_payload

    run = coverage_invocation(tmp_path, monkeypatch)
    sidecar = prune_native_payload(tmp_path / "reports", 1, "sim_0")
    document = json.loads(sidecar.read_text())
    document["status"] = "invented"
    sidecar.write_text(json.dumps(document))

    prune_invocation(tmp_path / "reports", 1)

    assert not run.campaign_path.exists()
    assert list((tmp_path / "reports/sim/.pruned-1").iterdir()) == []


def test_full_pruning_rejects_unidentified_quarantine(tmp_path):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    orphan = tmp_path / "reports/sim/.pruned-1"
    orphan.mkdir(parents=True)
    (orphan / "valuable").write_text("unrelated")
    with pytest.raises(CampaignRetentionError):
        prune_invocation(tmp_path / "reports", 1)
    assert (orphan / "valuable").read_text() == "unrelated"


def test_full_pruning_detects_legacy_copied_projection_dependency(tmp_path):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    manifest = tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"campaign_id":"01234567-89ab-4def-8123-456789abcdef"}\n')
    projection = tmp_path / "reports/sim/2/targets/sim_0/simulation.json"
    projection.parent.mkdir(parents=True)
    projection.write_text(
        json.dumps(
            {
                "flow": "sim",
                "target": "sim_0",
                "target_identity": "acme:demo:counter:1#sim_0",
                "complete": True,
                "campaign_manifest": str(manifest),
            }
        )
    )

    with pytest.raises(CampaignRetentionError, match=re.escape(str(projection.parents[2]))):
        prune_invocation(tmp_path / "reports", 1)

    assert manifest.is_file()
    assert projection.is_file()


def test_maintenance_cli_requires_exact_selection_and_executes_both_modes(tmp_path, monkeypatch):
    run = coverage_invocation(tmp_path, monkeypatch)
    base = ("--reports-root", str(tmp_path / "reports"), "--invocation", "1")
    rejected = _run_retention_cli(*base)
    assert rejected.returncode == 2
    assert run.campaign_path.is_file()
    native = _run_retention_cli(*base, "--native-target", "sim_0")
    assert native.returncode == 0, native.stderr
    assert run.campaign_path.is_file()
    full = _run_retention_cli(*base, "--full")
    assert full.returncode == 0, full.stderr
    assert not run.campaign_path.exists()


@pytest.mark.parametrize(
    ("defect", "relative", "expected_code"),
    [
        ("changed", "native/raw/001-reset@1.dat", 0),
        ("extra_native", "native/raw/999-extra.dat", 2),
        ("missing", "native/raw/001-reset@1.dat", 0),
        ("extra_invocation", "stray.txt", 2),
    ],
)
def test_full_pruning_refuses_only_unrecognized_content(
    tmp_path, defect, relative, expected_code, monkeypatch
):
    run = coverage_invocation(tmp_path, monkeypatch)
    invocation = tmp_path / "reports/sim/1"
    if defect != "extra_invocation":
        # Native payload files live in the nested Coverage Campaign directory.
        relative = str(run.storage.relative_to(invocation) / relative)
    selected = invocation / relative
    if defect == "changed":
        selected.write_bytes(selected.read_bytes() + b"changed")
    elif defect == "missing":
        selected.unlink()
    else:
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("do not delete", encoding="utf-8")
    result = _run_retention_cli(
        "--reports-root", str(tmp_path / "reports"), "--invocation", "1", "--full"
    )

    assert result.returncode == expected_code, result.stderr
    if expected_code == 0:
        assert not invocation.exists()
    else:
        assert str(Path(relative)) in result.stderr
        assert selected.read_text(encoding="utf-8") == "do not delete"
        assert invocation.is_dir()


def test_maintenance_cli_help_scopes_project_data_to_nonstandard_full_pruning():
    result = _run_retention_cli("--help")
    assert result.returncode == 0
    help_text = " ".join(result.stdout.split()).replace("--reports- root", "--reports-root")
    assert "needed by --full only when an interrupted lane" in help_text
    assert "not used by --native-target" in help_text
    assert "--include-dependents" in help_text


def test_maintenance_cli_rejects_include_dependents_with_native_target(tmp_path):
    result = _run_retention_cli(
        "--reports-root",
        str(tmp_path / "reports"),
        "--invocation",
        "1",
        "--native-target",
        "sim_0",
        "--include-dependents",
    )
    assert result.returncode == 2
    assert "only valid with --full" in result.stderr


def test_project_data_inference_accepts_only_standard_report_roots(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import _infer_project_data
    from booley.runtime.booley_dir import reset_cache

    project_data = tmp_path / "project-data"
    project_data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(project_data))
    reset_cache()

    assert _infer_project_data(project_data / ".runtime/flow-reports/sim/1") == project_data
    assert _infer_project_data(project_data / "flow-reports/sim/1") == project_data
    assert _infer_project_data(tmp_path / "elsewhere/flow-reports/sim/1") is None


def test_full_pruning_rejects_unresolved_completed_target(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    run = coverage_invocation(tmp_path, monkeypatch)
    progress_path = tmp_path / "reports/sim/1/progress.json"
    progress = json.loads(progress_path.read_text())
    progress["targets"].append("missing_completed_target")
    progress["completed_targets"].append("missing_completed_target")
    progress_path.write_text(json.dumps(progress))
    with pytest.raises(CampaignRetentionError, match="completed Target"):
        prune_invocation(tmp_path / "reports", 1)
    assert run.campaign_path.is_file()
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


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"operation": "full-with-dependents", "origin": "x", "members": []}, "fields"),
        (
            {"$schema": "wrong", "operation": "full-with-dependents", "members": []},
            "invalid",
        ),
        (
            {
                "$schema": "booley.simulation-prune-batch/v1",
                "operation": "full-with-dependents",
                "members": {},
            },
            "invalid",
        ),
        (
            {
                "$schema": "booley.simulation-prune-batch/v1",
                "operation": "full-with-dependents",
                "members": [1],
            },
            "member is invalid",
        ),
        (
            {
                "$schema": "booley.simulation-prune-batch/v1",
                "operation": "full-with-dependents",
                "members": ["relative/2"],
            },
            "not canonical",
        ),
    ],
)
def test_prune_batch_journal_rejects_malformed_boundaries(tmp_path, document, message):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _read_prune_batch,
    )

    root = (tmp_path / "reports/sim/1").absolute()
    root.mkdir(parents=True)
    document["origin"] = str(root)
    (root / ".prune-batch.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match=message):
        _read_prune_batch(root)


def test_prune_batch_journal_rejects_ambiguous_and_unordered_members(tmp_path):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _read_prune_batch,
    )

    root = (tmp_path / "reports/sim/1").absolute()
    quarantine = root.with_name(".pruned-1")
    root.mkdir(parents=True)
    quarantine.mkdir()
    document = {
        "$schema": "booley.simulation-prune-batch/v1",
        "operation": "full-with-dependents",
        "origin": str(root),
        "members": [],
    }
    for parent in (root, quarantine):
        (parent / ".prune-batch.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CampaignRetentionError, match="ambiguous"):
        _read_prune_batch(root)

    (quarantine / ".prune-batch.json").unlink()
    document["members"] = [str(root.with_name("3")), str(root.with_name("2"))]
    (root / ".prune-batch.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CampaignRetentionError, match="membership"):
        _read_prune_batch(root)


def test_report_dependency_scan_tolerates_malformed_report_shapes(tmp_path):
    from booley.flows.sim.campaign_retention import _report_references_campaign

    campaign_id = "01234567-89ab-4def-8123-456789abcdef"
    report = tmp_path / "reports/sim/2/report.json"
    report.parent.mkdir(parents=True)
    manifest = tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")

    report.write_text("{}", encoding="utf-8")
    assert not _report_references_campaign(report, manifest, campaign_id)
    report.write_text(json.dumps({"detail": {"campaigns": {"sim_0": []}}}), encoding="utf-8")
    assert not _report_references_campaign(report, manifest, campaign_id)


def test_dependent_discovery_translates_invalid_receipt_registry(tmp_path):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _dependent_invocations,
    )

    root = tmp_path / "reports/sim/1"
    manifest = root / "targets/sim_0/campaign/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"campaign_id": "01234567-89ab-4def-8123-456789abcdef"}),
        encoding="utf-8",
    )
    (manifest.parent / "dependency-receipts").write_text("unsafe", encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match="registry is invalid"):
        _dependent_invocations(root)


def test_dependency_scanners_tolerate_malformed_legacy_artifacts(tmp_path):
    from booley.flows.sim.campaign_retention import (
        _projection_references_origin,
        _report_references_origin,
    )

    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("not json", encoding="utf-8")
    assert not _report_references_origin(report, (manifest,))

    projection = tmp_path / "simulation.json"
    projection.write_text("not json", encoding="utf-8")
    assert not _projection_references_origin(projection, (manifest,))


def test_dependent_preflight_recognizes_terminal_and_journal_states(tmp_path):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _preflight_invocation,
    )

    completed = tmp_path / "reports/sim/1"
    completed.with_name(".pruned-1").mkdir(parents=True)
    _preflight_invocation(completed, project_data=None)

    missing = tmp_path / "reports/sim/2"
    with pytest.raises(CampaignRetentionError, match="does not exist"):
        _preflight_invocation(missing, project_data=None)

    journaled = tmp_path / "reports/sim/3"
    journaled.mkdir()
    (journaled / ".prune.json").write_text(
        json.dumps({"invocation": 3, "operation": "full"}), encoding="utf-8"
    )
    _preflight_invocation(journaled, project_data=None)


def test_remaining_dependencies_reports_members_when_origin_is_missing(tmp_path):
    from booley.flows.sim.campaign_retention import _remaining_dependencies

    root = tmp_path / "reports/sim/1"
    member = tmp_path / "reports/sim/2"
    member.mkdir(parents=True)

    assert _remaining_dependencies(root, (member,)) == (member,)


def test_native_pruning_preserves_complete_source_gap_queries(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_native_payload
    from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
    from booley.flows.sim.coverage_evidence import CoverageEvidenceSession
    from tests.flows.sim.test_coverage_transaction import gap_project

    run = coverage_invocation(tmp_path, monkeypatch, prepare=gap_project)
    prune_native_payload(run.reports, 1, "sim_0")
    loaded = load_coverage_campaign(run.campaign_path)
    assert CoverageEvidenceSession(loaded.campaign, None).query({"view": "zero_point_sources"})[
        "paths"
    ] == ["rtl/unused.sv"]


@pytest.mark.parametrize("mode", ["native", "full"])
def test_pruning_refuses_a_standalone_coverage_target_naming_its_directory(tmp_path, mode):
    """No supported Booley writes a full Campaign in the Target directory: refuse, never prune."""
    from booley.flows.sim.campaign_retention import prune_native_payload
    from tests.mcp_tools.test_coverage_analyst import persist_campaign

    persist_campaign(tmp_path)
    invocation = tmp_path / "reports/sim/12"
    target = invocation / "targets/sim_counter"
    CoverageProgress(invocation, ("sim_counter",)).checkpoint(complete=True, phase="aborted")
    before = {path: path.read_bytes() for path in invocation.rglob("*") if path.is_file()}

    with pytest.raises(CampaignRetentionError) as excinfo:
        if mode == "native":
            prune_native_payload(tmp_path / "reports", 12, "sim_counter")
        else:
            prune_invocation(tmp_path / "reports", 12)

    assert "standalone layout" in str(excinfo.value)
    assert str(target) in str(excinfo.value)
    assert before == {path: path.read_bytes() for path in invocation.rglob("*") if path.is_file()}
    assert not (target / "availability.json").exists()
    assert not (tmp_path / "reports/sim/.pruned-12").exists()


def test_native_pruning_refuses_a_reference_target_without_its_simulation_campaign(
    tmp_path, monkeypatch
):
    """A reference whose Simulation Campaign manifest is gone is not a completed Target."""
    from booley.flows.sim.campaign_retention import prune_native_payload

    run = coverage_invocation(tmp_path, monkeypatch)
    (run.target / "campaign/manifest.json").unlink()

    with pytest.raises(CampaignRetentionError, match="requires a completed Target"):
        prune_native_payload(run.reports, 1, "sim_0")

    assert (run.storage / "native").is_dir()
    assert not (run.storage / "availability.json").exists()


def test_interrupted_collection_owns_only_reserved_declaration_evidence(tmp_path):
    from booley.flows.sim.campaign_retention import _campaign_coverage_files

    root = tmp_path / "coverage-campaign"
    (root / "declarations").mkdir(parents=True)
    (root / "build-evidence").mkdir()
    (root / ".collection-started").write_bytes(b"")
    for name in (
        "declarations/inventory.json",
        "build-evidence/cells.tree.json",
        "build-evidence/tree.meta.json",
        "build-evidence/unrelated.json",
    ):
        (root / name).write_text("{}")
    owned = _campaign_coverage_files(root)
    assert (root / "declarations/inventory.json").absolute() in owned
    assert (root / "build-evidence/cells.tree.json").absolute() in owned
    assert (root / "build-evidence/unrelated.json").absolute() not in owned


def test_image_alias_prunes_only_authenticated_invocation(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
    from tests.conftest import require_symlinks, symlink_or_skip
    from tests.runtime.test_sandbox_layout import configure_project_alias

    require_symlinks(tmp_path)
    coverage_invocation(tmp_path, monkeypatch)
    alias = tmp_path / "booley-project"
    symlink_or_skip(alias, tmp_path / "reports", target_is_directory=True)
    configure_project_alias(monkeypatch, alias, tmp_path / "reports")
    neighbor = tmp_path / "reports/sim/2"
    neighbor.mkdir()
    (neighbor / "keep").write_text("unrelated")
    (tmp_path / "reports/linked").symlink_to(tmp_path / "reports", target_is_directory=True)
    with pytest.raises(CampaignRetentionError, match=r"link|symlink"):
        prune_invocation(alias / "linked", 1)
    assert (tmp_path / "reports/sim/1").exists()
    prune_invocation(alias, 1)
    assert not (tmp_path / "reports/sim/1").exists()
    assert (neighbor / "keep").read_text() == "unrelated"


def test_prune_canonicalizes_explicit_project_data(project_alias, monkeypatch) -> None:
    from contextlib import nullcontext

    from booley.flows.sim import campaign_retention as retention

    alias, data = project_alias
    root = data / "reports/sim/1"
    root.mkdir(parents=True)
    received = []
    monkeypatch.setattr(retention, "_retention_invocation_lock", lambda *_: nullcontext())
    monkeypatch.setattr(retention, "_completed_tombstone", lambda *_: False)
    monkeypatch.setattr(retention, "_read_prune_batch", lambda *_: None)
    monkeypatch.setattr(retention, "_dependent_invocations", lambda *_: ())
    monkeypatch.setattr(
        retention, "_prune_invocation", lambda root, *, project_data: received.append(project_data)
    )
    retention.prune_invocation(data / "reports", 1, project_data=alias)
    assert received == [data]


def test_retention_owns_a_shared_build_compiled_by_a_later_invocation(tmp_path: Path) -> None:
    from booley.flows.sim.campaign.inspection import (
        authenticate_retained_campaign_inventory,
        inspect_retained_campaign,
    )
    from booley.flows.sim.campaign.store import CampaignStore
    from tests.flows.sim.campaign_test_support import _executor, _manifest_for, _request

    manifest = _manifest_for(("alpha", "beta"))
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    items = manifest.document["work_items"]
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    for item, invocation in ((items[0], 1), (items[1], 2)):
        result = _executor(build_root, counters).execute(
            _request(store, manifest, item, project, invocation=invocation)
        )
        store.publish_result(item["work_item_id"], result)  # type: ignore[index]

    status = inspect_retained_campaign(store.manifest_path)
    owned = {*status.retention_files, *authenticate_retained_campaign_inventory(status)}
    produced = {
        path.absolute() for path in (store.root / "build-variants").rglob("*") if path.is_file()
    }
    assert counters["compile"] == 2
    assert produced <= owned


def test_retention_read_survives_transient_sharing_error(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import _read_object
    from tests.sharing_error_support import hold_opens

    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"phase": "complete"}), encoding="utf-8")
    attempts = hold_opens(monkeypatch, {path: 1})
    assert _read_object(path) == {"phase": "complete"}
    assert attempts[path] == 2


def test_retention_read_retries_before_raising_lasting_sharing_error(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import _read_object
    from tests.sharing_error_support import hold_opens

    path = tmp_path / "progress.json"
    path.write_text("{}", encoding="utf-8")
    attempts = hold_opens(monkeypatch, {path: 100})
    with pytest.raises(PermissionError):
        _read_object(path)
    assert attempts[path] > 1


def test_dependency_scan_fails_closed_on_lasting_sharing_error(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        _projection_references_origin,
        _report_references_campaign,
        _report_references_origin,
    )
    from tests.sharing_error_support import hold_opens

    campaign_id = "01234567-89ab-4def-8123-456789abcdef"
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    projection = tmp_path / "simulation.json"
    projection.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"campaign_id": campaign_id}), encoding="utf-8")
    hold_opens(monkeypatch, {report: 100, projection: 100, manifest: 100})
    for scan in (
        lambda: _report_references_campaign(report, manifest, campaign_id),
        lambda: _projection_references_origin(projection, (manifest,)),
        lambda: _report_references_origin(report, (manifest,)),
    ):
        with pytest.raises(CampaignRetentionError, match="cannot read") as refused:
            scan()
        assert isinstance(refused.value.__cause__, PermissionError)


def test_dependency_scan_keeps_unreadable_non_transient_as_not_dependent(tmp_path, monkeypatch):
    import errno

    from booley.flows.sim.campaign_retention import (
        _projection_references_origin,
        _report_references_campaign,
        _report_references_origin,
    )

    campaign_id = "01234567-89ab-4def-8123-456789abcdef"
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"campaign_id": campaign_id}), encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")

    def deny(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "open", deny)
    assert not _report_references_campaign(report, manifest, campaign_id)
    assert not _projection_references_origin(report, (manifest,))
    assert not _report_references_origin(report, (manifest,))


@pytest.mark.parametrize("layout", ["runtime", "flow-reports", "below"])
def test_prune_follows_only_the_booley_dir_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    """#2191: pruning follows a symlinked ``.booley`` entry and refuses a link below it."""
    project = tmp_path / "project"
    project.mkdir()
    external = symlinked_booley_dir(project, tmp_path / "external-booley")
    entry = project / ".booley"
    if layout == "below":
        # Reports live physically elsewhere; ``.runtime`` below the entry is the link.
        real_runtime = tmp_path / "real-runtime"
        real_runtime.mkdir()
        (external / ".runtime").symlink_to(real_runtime, target_is_directory=True)
        reports, _unused, _store = _retained_invocation(
            tmp_path, reports=real_runtime / "flow-reports"
        )
        reports = entry / ".runtime" / "flow-reports"
    else:
        reports_dir = (
            entry / ".runtime" / "flow-reports" if layout == "runtime" else entry / "flow-reports"
        )
        reports, _unused, _store = _retained_invocation(
            tmp_path, reports=reports_dir, booley_dir=entry
        )
    if layout == "flow-reports":
        # The retention CLI runs from the Project; ownership inference reads the cwd.
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        monkeypatch.chdir(project)
        reset_cache()

    if layout == "below":
        with pytest.raises((CampaignRetentionError, SimulationCampaignIntegrityError)):
            prune_invocation(reports, 1)
        assert (reports / "sim/1").is_dir()
        return
    prune_invocation(reports, 1)

    assert (reports / "sim" / ".pruned-1").is_dir()


def _interrupt_latest_attempt(
    tmp_path: Path, reports: Path, store: CampaignStore, lane_id: str | None = None
) -> tuple[Path, dict[str, object]]:
    """Turn the retained Campaign's only item into an interrupted one with an owned run directory.

    Returns the owned run directory and the rewritten attempt document. With ``lane_id`` the
    attempt names that lane execution (a Campaign written by the current release).
    """
    status = inspect_retained_campaign(store.manifest_path)
    work_item_id = status.completed[0]
    (store.work_item_directory(work_item_id) / "result.json").unlink()
    store.summary_path.unlink()
    (store.root.parent / "simulation.json").unlink()
    progress_path = reports / "sim/1/progress.json"
    progress = json.loads(progress_path.read_text())
    progress.update(completed_targets=[], pending_targets=["sim"], complete=False, phase="running")
    progress_path.write_text(json.dumps(progress))
    return _own_attempt_run_directory(tmp_path, store, work_item_id, lane_id)


def test_full_pruning_cleans_interrupted_owned_run_directory(tmp_path: Path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    run_path, _document = _interrupt_latest_attempt(tmp_path, reports, store)

    prune_invocation(reports, 1)

    assert not run_path.exists()
    assert not (reports / "sim/1").exists()


@pytest.mark.parametrize("recovered", [True, False], ids=["recovered", "incomplete"])
def test_full_pruning_recovers_interrupted_lane_before_run_directory_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovered: bool
) -> None:
    """Pruning cancels the lane a crashed Campaign left behind, or keeps everything."""
    from booley.flows.sim.campaign.child_protocol import ChildExecutionRegistry

    reports, project_data, store = _retained_invocation(tmp_path)
    # Unique: real recovery signals every host process carrying this ID (xdist workers too).
    lane = ExecutionId(uuid.uuid4().hex)
    run_path, document = _interrupt_latest_attempt(tmp_path, reports, store, str(lane))
    registry = ChildExecutionRegistry.from_project_data(store.load_manifest(), project_data)
    registry.prepare(
        lane,
        work_item_id=str(document["work_item_id"]),
        attempt_id=str(document["attempt_id"]),
        attempt_ordinal=int(document["attempt_ordinal"]),  # type: ignore[call-overload]
        attempt_directory=store.root,
        parent_execution_id=ExecutionId("a" * 32),
    )
    assert not registry.is_terminal(lane)
    if not recovered:
        monkeypatch.setattr(ChildExecutionRegistry, "cancel", lambda _self, _id: False)

    if recovered:
        prune_invocation(reports, 1)
        assert registry.is_terminal(lane)
        assert not run_path.exists()
        assert not (reports / "sim/1").exists()
    else:
        with pytest.raises(CampaignRetentionError, match="recovery is incomplete"):
            prune_invocation(reports, 1)
        assert run_path.exists()
        assert (reports / "sim/1").exists()


@pytest.mark.usefixtures("mandatory_file_locks")
def test_complete_campaign_pruning_accepts_windows_lock_sentinel(tmp_path: Path) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    (store.root / ".lock").write_bytes(b"\0")

    prune_invocation(reports, 1, project_data=project_data)

    assert list((reports / "sim" / ".pruned-1").iterdir()) == []


@pytest.mark.usefixtures("mandatory_file_locks")
@pytest.mark.parametrize("state", ["terminal-unpublished", "interrupted"])
def test_full_pruning_accepts_authenticated_abandoned_campaign(tmp_path: Path, state: str) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    target = store.root.parent
    store.summary_path.unlink()
    (target / "simulation.json").unlink()
    if state == "interrupted":
        status = inspect_retained_campaign(store.manifest_path)
        result = store.work_item_directory(status.completed[0]) / "result.json"
        result.unlink()
        progress_path = reports / "sim/1/progress.json"
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        progress["completed_targets"] = []
        progress["pending_targets"] = ["sim"]
        progress["phase"] = "aborted"  # terminalized on a catchable exit
        progress_path.write_text(json.dumps(progress), encoding="utf-8")

    prune_invocation(reports, 1)

    assert not (reports / "sim/1").exists()
    assert list((reports / "sim/.pruned-1").iterdir()) == []


def test_interrupted_campaign_without_surviving_resources_needs_no_project_data(
    tmp_path: Path,
) -> None:
    reports, _project_data, store = _retained_invocation(
        tmp_path, reports=tmp_path / "external-reports"
    )
    target = store.root.parent
    store.summary_path.unlink()
    (target / "simulation.json").unlink()
    status = inspect_retained_campaign(store.manifest_path)
    result = store.work_item_directory(status.completed[0]) / "result.json"
    result.unlink()
    progress_path = reports / "sim/1/progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["completed_targets"] = []
    progress["pending_targets"] = ["sim"]
    # A killed producer never terminalizes its progress.
    progress["complete"] = False
    progress["phase"] = "running"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    prune_invocation(reports, 1)

    assert not (reports / "sim/1").exists()
    assert list((reports / "sim/.pruned-1").iterdir()) == []


def test_interrupted_campaign_with_surviving_run_directory_needs_project_data(
    tmp_path: Path,
) -> None:
    reports, _project_data, store = _retained_invocation(
        tmp_path, reports=tmp_path / "external-reports"
    )
    run_path, _document = _interrupt_latest_attempt(tmp_path, reports, store)

    with pytest.raises(CampaignRetentionError, match="Booley Directory is required"):
        prune_invocation(reports, 1)

    assert run_path.exists()


def test_full_pruning_refuses_a_campaign_owned_by_resume(tmp_path: Path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)

    with store.mutation_lock(), pytest.raises(CampaignRetentionError, match="being resumed"):
        prune_invocation(reports, 1)

    assert (reports / "sim/1").is_dir()
    assert not (reports / "sim/.pruned-1").exists()


def test_campaign_mutation_lock_never_recreates_a_missing_root(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign")

    with (
        pytest.raises(SimulationCampaignIntegrityError, match="directory disappeared"),
        store.mutation_lock(),
    ):
        raise AssertionError("unreachable")

    assert not store.root.exists()


@pytest.mark.parametrize("nested", [False, True])
def test_complete_campaign_pruning_rejects_unrecognized_campaign_file(
    tmp_path: Path, nested: bool
) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    parent = store.root
    if nested:
        parent = next(store.root.glob("work-items/*/attempts/*")) / "evidence"
    stray = parent / "stray.txt"
    stray.write_text("do not delete", encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match=r"stray\.txt"):
        prune_invocation(reports, 1, project_data=project_data)

    assert stray.read_text(encoding="utf-8") == "do not delete"
    assert (reports / "sim" / "1").is_dir()


def test_pruning_rejects_complete_results_before_acceptance_projection(
    tmp_path: Path,
) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    projection = store.root.parent / "simulation.json"
    document = json.loads(projection.read_bytes())
    document["complete"] = False
    projection.write_bytes(canonical_json_bytes(document))

    with pytest.raises(CampaignRetentionError, match="acceptance projections"):
        prune_invocation(reports, 1)

    assert store.manifest_path.is_file()
