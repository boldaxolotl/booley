"""Portable scratch must delete child-free contexts and retain unknown native use."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core import temporary_workspace as tw


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("force_portable", [False, True])
def test_child_free_normal_and_exception_scratch_is_removed(
    monkeypatch: pytest.MonkeyPatch, fail: bool, force_portable: bool
) -> None:
    if force_portable:
        monkeypatch.setattr(tw.sys, "platform", "unsupported-test-platform")
    root = None
    try:
        with tw.temporary_workspace() as directory:
            root = Path(directory)
            assert root.is_dir()
            if not scratch_process_ownership_supported():
                assert json.loads((root / tw._MARKER).read_text())["owner"] is None
                assert tw.active_guard_fds() == ()
            if fail:
                raise RuntimeError("synthetic operation failed")
    except RuntimeError:
        assert fail
    tw.sweep_stale_workspaces()
    assert root is not None and not root.exists()


def test_portable_native_use_retains_payload_after_exit_and_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tw.sys, "platform", "unsupported-test-platform")
    with tw.temporary_workspace() as directory:
        root = Path(directory)
        payload = root / "session.jsonl"
        payload.write_text("synthetic native session", encoding="utf-8")
        result = tw.run_guarded([sys.executable, "-c", "pass"], timeout=5, check=True)
        assert result.returncode == 0
    tw.sweep_stale_workspaces()
    assert payload.read_text(encoding="utf-8") == "synthetic native session"
    marker = json.loads((root / tw._MARKER).read_text())
    assert marker["owner"] is None and marker["launches"] == {}


def test_default_portable_registry_is_reused_for_child_free_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tw.sys, "platform", "unsupported-test-platform")
    monkeypatch.delenv(tw.ROOT_ENV, raising=False)
    monkeypatch.setattr(tw, "_PORTABLE_REGISTRY", None)
    monkeypatch.setattr(tw.tempfile, "tempdir", str(tmp_path))
    parents = []
    for _ in range(3):
        with tw.temporary_workspace() as directory:
            root = Path(directory)
            parents.append(root.parent)
        assert not root.exists()
    assert len(set(parents)) == 1
    assert parents[0].parent == tmp_path
    assert list(parents[0].iterdir()) == []
    parents[0].rmdir()
