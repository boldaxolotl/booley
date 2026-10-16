"""Host GC must retain Linux shutdown metadata without using Linux-only APIs."""

import json
import os
import sys

import pytest

from booley.runtime.execution_records import execution_paths, gc_terminal_executions


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_foreign_shutdown_metadata_is_retained_on_portable_hosts(tmp_path, monkeypatch, platform):
    paths = execution_paths("e" * 32, booley_dir=tmp_path)
    paths.root.mkdir(parents=True)
    paths.record.write_text(json.dumps({"state": "terminal", "tree_terminal": True}))
    group = paths.root / (".booley-shutdown-" + "a" * 32)
    group.mkdir(mode=0o700)
    (group / "state.json").write_text("preserve")

    def unavailable_uid():
        raise AssertionError("Portable cleanup attempted Linux ownership validation")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", platform)
        patch.setattr(os, "getuid", unavailable_uid, raising=False)
        removed = gc_terminal_executions(
            tmp_path, now=paths.record.stat().st_mtime + 10, retention_seconds=1
        )
    assert removed == []
    assert paths.record.exists() and (group / "state.json").read_text() == "preserve"
