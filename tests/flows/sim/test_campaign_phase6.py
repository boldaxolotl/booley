"""Release-gate integrity, scale, and retention checks for Simulation Campaigns."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import tracemalloc
from pathlib import Path

import pytest

from booley.flows.sim.campaign.codec import (
    RECORD_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
from booley.runtime.execution_records import ExecutionId, atomic_write_json, execution_paths
from tests.flows.sim.test_campaign_phase3_adversarial import _completed
from tests.flows.sim.test_campaign_phase3_integrity import _manifest_for


@pytest.mark.parametrize(
    "defect", ["corrupt", "truncate", "replace", "symlink", "hardlink", "resize"]
)
def test_terminal_authority_defect_matrix_fails_closed(
    tmp_path: Path, defect: str
) -> None:
    (tmp_path / ".booley_project").mkdir()
    completed = _completed(tmp_path)
    result = completed.store.work_item_directory(completed.item_id) / "result.json"
    original = result.with_name("original-result.json")
    result.rename(original)
    if defect == "corrupt":
        result.write_bytes(b"{}\n")
    elif defect == "truncate":
        result.write_bytes(original.read_bytes()[:23])
    elif defect == "replace":
        replacement = json.loads(original.read_bytes())
        replacement["attempt_id"] = "0" * 32
        result.write_bytes(canonical_json_bytes(replacement))
    elif defect == "symlink":
        result.symlink_to(original.name)
    elif defect == "hardlink":
        os.link(original, result)
    else:
        result.write_bytes(b"x" * (RECORD_MAX_BYTES + 1))

    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.scan()


def test_maximum_synthetic_campaign_scan_is_bounded_and_deterministic(
    tmp_path: Path,
) -> None:
    manifest = _manifest_for(tuple(f"case_{index:04d}" for index in range(1_000)))
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)

    tracemalloc.start()
    started = time.monotonic()
    first = store.regenerate_summary()
    elapsed = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    first_bytes = store.summary_path.read_bytes()
    second = store.regenerate_summary()

    assert first == second
    assert store.summary_path.read_bytes() == first_bytes
    assert len(first["pending"]) == 1_000
    assert elapsed < 10
    assert peak < 96 * 1024 * 1024


def _retained_invocation(tmp_path: Path) -> tuple[Path, Path, CampaignStore]:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".booley_project").mkdir()
    complete = _completed(source)
    project_data = tmp_path / "project-data"
    reports = project_data / ".runtime" / "flow-reports"
    invocation = reports / "sim" / "1"
    target = invocation / "targets" / "sim"
    target.mkdir(parents=True)
    shutil.move(str(complete.store.root), target / "campaign")
    store = CampaignStore(target / "campaign")
    store.regenerate_summary()
    (target / "simulation.json").write_bytes(
        canonical_json_bytes(
            {
                "complete": True,
                "target": "sim",
                "campaign_manifest": str(store.manifest_path),
                "campaign_summary": str(store.summary_path),
            }
        )
    )
    (invocation / "progress.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "targets": ["sim"],
                "completed_targets": ["sim"],
                "pending_targets": [],
            }
        ),
        encoding="utf-8",
    )
    return reports, project_data, store


def _publish_retired_child(project_data: Path, store: CampaignStore) -> ExecutionId:
    execution_id = ExecutionId("1" * 32)
    entry = {
        "$schema": "booley.simulation-campaign-child-entry/v1",
        "child_execution_id": execution_id,
    }
    entry_raw = canonical_json_bytes(entry)
    terminal = {
        "state": "terminal",
        "tree_terminal": True,
        "terminal_cause": "completed",
    }
    terminal_raw = canonical_json_bytes(terminal)
    retirement = {
        "$schema": "booley.simulation-campaign-child-retirement/v1",
        "child_execution_id": execution_id,
        "entry_sha256": "sha256:" + hashlib.sha256(entry_raw).hexdigest(),
        "execution_terminal_sha256": "sha256:"
        + hashlib.sha256(terminal_raw).hexdigest(),
        "lease_id": None,
        "token_absent": True,
        "terminal_cause": "completed",
    }
    retirement_raw = canonical_json_bytes(retirement)
    project_children = project_data / ".runtime" / "campaign-child-executions"
    campaign_children = store.root / "child-executions"
    for root in (project_children, campaign_children):
        (root / "entries").mkdir(parents=True)
        (root / "retired").mkdir(parents=True)
        (root / "entries" / f"{execution_id}.json").write_bytes(entry_raw)
        (root / "retired" / f"{execution_id}.json").write_bytes(retirement_raw)
    atomic_write_json(execution_paths(execution_id, project_dir=project_data).record, terminal)
    return execution_id


def test_complete_campaign_pruning_releases_exact_project_child_pair(
    tmp_path: Path,
) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)

    prune_invocation(reports, 1)

    project_children = project_data / ".runtime" / "campaign-child-executions"
    assert not (project_children / "entries" / f"{execution_id}.json").exists()
    assert not (project_children / "retired" / f"{execution_id}.json").exists()
    assert list((reports / "sim" / ".pruned-1").iterdir()) == []


@pytest.mark.parametrize("defect", ["substituted", "missing"])
def test_pruning_rejects_invalid_project_child_pair(
    tmp_path: Path, defect: str
) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)
    project_entry = (
        project_data
        / ".runtime/campaign-child-executions/entries"
        / f"{execution_id}.json"
    )
    if defect == "substituted":
        project_entry.write_bytes(b"{}\n")
    else:
        project_entry.unlink()

    with pytest.raises(CampaignRetentionError, match=r"disagrees|disappeared"):
        prune_invocation(reports, 1)

    assert (reports / "sim" / "1").is_dir()
    assert project_entry.is_file() is (defect == "substituted")


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
