"""Retention refuses malformed selections and inconsistent invocation trees."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim import record_retention
from booley.flows.sim.record import ProjectionTrust
from booley.flows.sim.record_retention import SimulationRecordRetentionError, prune_invocation


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("invocation", [0, -1, True, "1"])
def test_pruning_refuses_an_invocation_that_is_not_a_positive_number(
    tmp_path: Path, invocation: object
) -> None:
    with pytest.raises(SimulationRecordRetentionError, match="exact positive invocation"):
        prune_invocation(tmp_path / "reports", invocation)  # type: ignore[arg-type]


def test_reading_a_json_list_instead_of_an_object_is_refused(tmp_path: Path) -> None:
    document = tmp_path / "progress.json"
    _write_json(document, [])

    with pytest.raises(SimulationRecordRetentionError, match="Expected an object"):
        record_retention._read_object(document)


@pytest.mark.parametrize("selector", ["", ".", "..", "latest", 7])
def test_a_target_selector_must_be_exact(tmp_path: Path, selector: object) -> None:
    with pytest.raises(SimulationRecordRetentionError, match="exact Target selector"):
        record_retention._target(tmp_path, selector)  # type: ignore[arg-type]


def test_a_target_selector_must_name_one_target_of_the_invocation(tmp_path: Path) -> None:
    _write_json(tmp_path / "progress.json", {"flow": "sim", "targets": ["a"]})

    with pytest.raises(SimulationRecordRetentionError, match="does not resolve exactly"):
        record_retention._target(tmp_path, "b")


def test_pruning_state_with_both_the_invocation_and_its_quarantine_is_ambiguous(
    tmp_path: Path,
) -> None:
    root = tmp_path / "reports" / "sim" / "1"
    root.mkdir(parents=True)
    root.with_name(".pruned-1").mkdir()

    with pytest.raises(SimulationRecordRetentionError, match="Ambiguous invocation pruning"):
        record_retention._prune_invocation(root)


def test_pruning_an_invocation_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "reports" / "sim" / "1"
    root.parent.mkdir(parents=True)

    with pytest.raises(SimulationRecordRetentionError, match="Invocation does not exist"):
        record_retention._prune_invocation(root)


@pytest.mark.parametrize(
    ("progress", "message"),
    [
        ({"targets": []}, "no exact Target manifest"),
        ({"targets": [""]}, "ambiguous Targets"),
        (
            {"targets": ["a"], "completed_targets": [], "pending_targets": ["a"]},
            "unresolved Target",
        ),
    ],
)
def test_invocation_targets_must_be_exact_and_accounted_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, progress: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(record_retention, "validate_progress_shape", lambda _progress: None)
    full = {"completed_targets": [], "pending_targets": [], **progress}
    _write_json(tmp_path / "progress.json", full)
    (tmp_path / "targets" / "unlisted").mkdir(parents=True)

    with pytest.raises(SimulationRecordRetentionError, match=message):
        record_retention._validate_invocation_targets(tmp_path)


def test_a_progress_lock_with_foreign_content_is_refused(tmp_path: Path) -> None:
    _write_json(tmp_path / "progress.json", {"targets": []})
    (tmp_path / ".progress.lock").write_bytes(b"not a lock sentinel")

    with pytest.raises(SimulationRecordRetentionError, match="progress lock content is invalid"):
        record_retention._validate_invocation_inventory(tmp_path)


_STATUS: Any = SimpleNamespace(
    target_selector="sim_0",
    target_identity="acme:demo:counter:1#sim_0",
    simulation_record_id="id-1",
)
_IDENTITY = {"target": "sim_0", "target_identity": "acme:demo:counter:1#sim_0"}


@pytest.mark.parametrize(
    ("trust", "document", "message"),
    [
        (ProjectionTrust.AUTHENTICATED, {"target": "other"}, "identity disagrees"),
        (
            ProjectionTrust.TARGET_CONSISTENT_LEGACY,
            {**_IDENTITY, "passed": "yes"},
            "legacy Simulation verdict projection is invalid",
        ),
        (
            ProjectionTrust.AUTHENTICATED,
            {**_IDENTITY, "simulation_record_id": "id-2"},
            "owner disagrees",
        ),
    ],
)
def test_a_projection_that_disagrees_with_its_simulation_record_is_refused(
    tmp_path: Path, trust: ProjectionTrust, document: dict[str, object], message: str
) -> None:
    projection: Any = SimpleNamespace(trust=trust, document=document)

    with pytest.raises(SimulationRecordRetentionError, match=message):
        record_retention._validate_projection_identity(tmp_path, _STATUS, projection)
