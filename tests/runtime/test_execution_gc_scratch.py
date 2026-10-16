"""Execution retention must release rendezvous metadata without losing scratch proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.execution_test_support import runtime_env
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core import temporary_workspace as tw
from booley.core.lease_shutdown import ShutdownGroup
from booley.runtime.execution_records import (
    ExecutionId,
    execution_paths,
    gc_terminal_executions,
    read_json,
    write_attachment_heartbeat,
)

pytestmark = pytest.mark.skipif(
    not scratch_process_ownership_supported(), reason="Linux scratch identity"
)


@pytest.fixture
def completed_execution(tmp_path):
    state, registry = tmp_path / "state", tmp_path / "scratch"
    state.mkdir()
    registry.mkdir()
    paths = execution_paths(ExecutionId("d" * 32), booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    snapshot, receipt = tmp_path / "running.json", tmp_path / "root"
    native = "import shutil,sys; shutil.copyfile(sys.argv[1],sys.argv[2])"
    endpoint = (
        "from pathlib import Path\n"
        "import sys\n"
        "from booley.core import temporary_workspace as tw\n"
        "tw._delete_owned = lambda *args, **kwargs: False\n"
        f"with tw.temporary_workspace(parent=Path({str(registry)!r})) as raw:\n"
        f"    Path({str(receipt)!r}).write_text(raw)\n"
        f"    tw.run_guarded([sys.executable, '-c', {native!r}, "
        f"str(Path(raw) / tw._MARKER), {str(snapshot)!r}], timeout=15)\n"
    )
    _run_supervisor(state, paths.root.name, endpoint)
    assert read_json(paths.record)["tree_terminal"] is True
    root = Path(receipt.read_text())
    # Replay the authentic running marker: publication after reaping was interrupted.
    tw._write_marker(root, json.loads(snapshot.read_text()))
    return state, paths, root, next(paths.root.glob(".booley-shutdown-*"))


def _run_supervisor(state: Path, execution_id: str, endpoint: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--attachment-timeout-seconds",
            "60",
            "--",
            sys.executable,
            "-c",
            endpoint,
        ],
        env=runtime_env(state),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("publication_failure", [False, True])
def test_actual_terminal_gc_preserves_recoverable_scratch_proof(
    completed_execution, monkeypatch, publication_failure
):
    state, paths, root, group = completed_execution
    now = paths.record.stat().st_mtime + 10
    if publication_failure:

        def fail_publication(*_) -> None:
            raise OSError("full")

        with monkeypatch.context() as patch:
            patch.setattr(tw, "_write_marker", fail_publication)
            assert gc_terminal_executions(state, now=now, retention_seconds=1) == []
        assert paths.record.exists() and group.exists()
        marker, _ = tw._validated_marker(root)
        assert any(value["state"] == "running" for value in marker["launches"].values())
    assert gc_terminal_executions(state, now=now, retention_seconds=1) == [paths.root.name]
    assert not paths.root.exists()
    marker, _ = tw._validated_marker(root)
    assert marker["launches"] and all(
        value == {"state": "complete"} for value in marker["launches"].values()
    )
    assert tw._delete_owned(root, require_dead=True)
    assert not root.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "live",
        "live_member",
        "nonterminal",
        "corrupt",
        "null",
        "array",
        "scalar",
        "unknown",
        "symlink",
        "marker",
    ],
)
def test_terminal_gc_retains_uncertain_shutdown_groups(completed_execution, damage):
    state, paths, root, group_path = completed_execution
    group = ShutdownGroup(group_path)
    if damage in {"live", "live_member", "nonterminal"}:
        with group.locked() as data:
            if damage == "live":
                data["owner"] = tw.process_identity(os.getpid())
            elif damage == "live_member":
                member = next(iter(data["members"].values()))
                member["identity"] = tw.process_identity(os.getpid())
                member["ancestors"] = [member["identity"]]
            else:
                data["terminal"] = False
    elif damage == "corrupt":
        (group_path / "state.json").write_text("invalid")
    elif damage in {"null", "array", "scalar"}:
        (group_path / "state.json").write_text(
            {"null": "null", "array": "[{}]", "scalar": "1"}[damage]
        )
    elif damage == "unknown":
        (group_path / "foreign").write_text("preserve")
    elif damage == "marker":
        (root / tw._MARKER).unlink()
    else:
        original = group_path.with_name("retained-original")
        group_path.rename(original)
        group_path.symlink_to(original, target_is_directory=True)
    assert (
        gc_terminal_executions(state, now=paths.record.stat().st_mtime + 10, retention_seconds=1)
        == []
    )
    assert paths.record.exists() and root.exists() and group_path.exists()


def test_terminal_gc_removes_metadata_when_referenced_scratch_is_already_gone(completed_execution):
    state, paths, root, _ = completed_execution
    assert tw._delete_owned(root, require_dead=True)
    assert gc_terminal_executions(
        state, now=paths.record.stat().st_mtime + 10, retention_seconds=1
    ) == [paths.root.name]
    assert not paths.root.exists()


def test_terminal_gc_preserves_unrecognized_execution_payload(completed_execution):
    state, paths, root, group = completed_execution
    payload = paths.root / "foreign"
    payload.mkdir()
    (payload / "sentinel").write_text("preserve")
    assert (
        gc_terminal_executions(state, now=paths.record.stat().st_mtime + 10, retention_seconds=1)
        == []
    )
    assert paths.record.exists() and root.exists() and group.exists()
    assert (payload / "sentinel").read_text() == "preserve"


def test_terminal_gc_never_unlinks_replaced_metadata(completed_execution, monkeypatch):
    state, paths, _, group = completed_execution
    remove = ShutdownGroup._remove_metadata
    original = group.with_name("retained-original")

    def substitute(self, identity):
        self.path.rename(original)
        self.path.mkdir(mode=0o700)
        (self.path / "foreign").write_text("preserve")
        return remove(self, identity)

    monkeypatch.setattr(ShutdownGroup, "_remove_metadata", substitute)
    assert (
        gc_terminal_executions(state, now=paths.record.stat().st_mtime + 10, retention_seconds=1)
        == []
    )
    assert paths.record.exists() and (original / "state.json").exists()
    assert (group / "foreign").read_text() == "preserve"
