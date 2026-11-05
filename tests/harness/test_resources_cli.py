"""`booley resources` and its writer share one Job resource history per Project."""

from __future__ import annotations

import argparse
import json

import pytest

from booley.flows import job_resources
from booley.harness import resources_cli
from booley.runtime import resource_history
from tests.booley_checkouts_support import LAYOUTS, RUNTIMES, linked_worktree

SAMPLE = {
    "key": {"target": "core", "phase": "sim", "flags": [], "test_run": ""},
    "solo": False,
    "container_peak_delta_mb": None,
    "peak_memory_mb": 10.0,
    "wall_s": 1.0,
}


@pytest.mark.parametrize("runtime", RUNTIMES)
@pytest.mark.parametrize("layout", LAYOUTS)
def test_history_is_written_and_read_in_main_from_a_worktree(
    tmp_path, monkeypatch, capsys, layout, runtime
):
    """Resource history is Common Booley State: a worktree neither forks nor hides it."""
    main, worktree = linked_worktree(tmp_path, monkeypatch, layout=layout, runtime=runtime)
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(job_resources, "history_records", lambda *_args: [SAMPLE])

    job_resources.record_command("sim", None, (), None, None)  # type: ignore[arg-type]

    assert len(resource_history.load_records(main / ".booley")) == 1
    assert not resource_history.history_path(worktree / ".booley").exists()
    assert resources_cli.run(argparse.Namespace(json=True), worktree) == 0
    assert [summary["runs"] for summary in json.loads(capsys.readouterr().out)] == [1]
