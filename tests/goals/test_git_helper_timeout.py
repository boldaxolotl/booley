"""The Goal tests' ``git()`` helper reports a stalled git child usefully.

Issue #3115: ``subprocess.run(timeout=...)`` kills only the direct child, so a
descendant holding the output pipes made the helper wait far past its limit
(30 s became 80.6 s on a Windows runner) and the failure showed no output.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest

from tests.goals import conftest as goal_support

# Prints, then hangs while holding the pipes, as a stalled hook or helper would.
# It also stops once the stop file appears, so code that leaves it running (a
# plain ``subprocess.run`` kills only ``git``) does not outlive the test.
_STALLED_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path

    print("GIT-HELPER-PARTIAL", flush=True)
    stop_file = Path(sys.argv[1])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not stop_file.exists():
        time.sleep(0.05)
    """
)


@pytest.mark.timeout(90)
def test_git_helper_timeout_reports_elapsed_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "stalled_child.py"
    script.write_text(_STALLED_CHILD, encoding="utf-8")
    stop_file = tmp_path / "stop"
    # A git alias runs through ``sh``, so the Python process is a grandchild.
    python = Path(sys.executable).as_posix()
    alias = f'alias.hang=!"{python}" "{script.as_posix()}" "{stop_file.as_posix()}"'
    # 15 s is a 15x margin over the sub-second git -> sh -> Python start.
    monkeypatch.setattr(goal_support, "GIT_TIMEOUT_S", 15)

    try:
        with pytest.raises(pytest.fail.Exception) as raised:
            goal_support.git(tmp_path, "-c", alias, "hang")
    finally:
        stop_file.touch()

    message = str(raised.value)
    assert "GIT-HELPER-PARTIAL" in message
    assert re.search(r"elapsed \d+\.\d s", message), message
