"""A stalled harness-test child fails its test with elapsed time and partial output (#3415).

Harness tests start their Git and tool children through ``run_bounded_child``.
Plain ``subprocess.run(timeout=)`` raises a bare ``TimeoutExpired`` that names
neither the elapsed time nor the output the child wrote before it stalled.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.harness.setup import test_git_hooks
from tests.subprocess_budget import run_bounded_child

#: Forced bound, short enough to keep the test fast and long enough for the
#: Git -> sh -> Python start on a loaded runner (matches tests/test_subprocess_budget.py).
FORCED_TIMEOUT_S = 10

# Prints a marker, then hangs while holding the output pipes, as a stalled hook
# would. It also stops once the stop file appears, so code that leaves it running
# (a plain ``subprocess.run`` kills only ``git``) does not outlive the test.
_STALLED_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path

    print("HARNESS-GIT-PARTIAL", flush=True)
    stop_file = Path(sys.argv[1])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not stop_file.exists():
        time.sleep(0.05)
    """
)


@pytest.mark.timeout(90)
def test_stalled_harness_git_child_fails_with_elapsed_time_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run_git`` (serving ~60 harness tests) reports a stalled Git child usefully."""

    def run_with_short_bound(argv: list[str], **kwargs: Any):
        return run_bounded_child(argv, **{**kwargs, "timeout": FORCED_TIMEOUT_S})

    # raising=False keeps the test runnable against a tree whose ``_run_git`` has
    # no ``run_bounded_child`` name: it then fails on the bare timeout, as it should.
    monkeypatch.setattr(test_git_hooks, "run_bounded_child", run_with_short_bound, raising=False)

    script = tmp_path / "stalled_child.py"
    script.write_text(_STALLED_CHILD, encoding="utf-8", newline="\n")
    stop_file = tmp_path / "stop"
    # A git alias runs through ``sh``, so the Python process is a grandchild. The
    # marker lives in the script, not in argv, so the command echoed in the
    # failure message cannot satisfy the partial-output assertion.
    python = Path(sys.executable).as_posix()
    alias = f'alias.hang=!"{python}" "{script.as_posix()}" "{stop_file.as_posix()}"'

    try:
        with pytest.raises(pytest.fail.Exception) as raised:
            test_git_hooks._run_git(tmp_path, "-c", alias, "hang")
    finally:
        stop_file.touch()

    message = str(raised.value)
    assert f"timed out after {FORCED_TIMEOUT_S} s" in message
    assert "HARNESS-GIT-PARTIAL" in message
    elapsed = re.search(r"elapsed (\d+\.\d) s", message)
    assert elapsed, message
    assert float(elapsed.group(1)) >= FORCED_TIMEOUT_S
