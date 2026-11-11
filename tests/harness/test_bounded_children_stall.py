"""A stalled harness-test child fails its test with elapsed time and partial output (#3415).

Harness tests start their Git and tool children through ``run_bounded_child``.
Plain ``subprocess.run(timeout=)`` raises a bare ``TimeoutExpired`` that names
neither the elapsed time nor the output the child wrote before it stalled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.harness.setup import test_git_hooks
from tests.subprocess_budget import run_bounded_child

#: Forced bound, short enough to keep the test fast and long enough for the
#: shell to print its marker on a loaded runner (matches tests/test_subprocess_budget.py).
FORCED_TIMEOUT_S = 10


def test_stalled_harness_git_child_fails_with_elapsed_time_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run_git`` (serving ~60 harness tests) reports a stalled Git child usefully."""

    def run_with_short_bound(argv: list[str], **kwargs: Any):
        return run_bounded_child(argv, **{**kwargs, "timeout": FORCED_TIMEOUT_S})

    # raising=False: before the fix the module has no such name and the patch is a no-op.
    monkeypatch.setattr(test_git_hooks, "run_bounded_child", run_with_short_bound, raising=False)

    # The alias prints a marker built at run time (absent from argv, so the echoed
    # command in the failure message cannot satisfy the assertion), then stalls
    # for up to 60 s. The loop ends once Git (its parent) is gone, so an unbounded
    # ``subprocess.run`` that kills only Git leaves no orphan behind.
    stall_alias = (
        "alias.stall=!printf 'STALL-%s\\n' MARKER; "
        "i=0; while kill -0 $PPID 2>/dev/null && [ $i -lt 300 ]; do sleep 0.2; i=$((i+1)); done"
    )
    with pytest.raises(pytest.fail.Exception) as raised:
        test_git_hooks._run_git(tmp_path, "-c", stall_alias, "stall")

    message = str(raised.value)
    assert f"timed out after {FORCED_TIMEOUT_S} s" in message
    assert "elapsed " in message
    assert "STALL-MARKER" in message
