"""A stalled nested pytest in the timeout-evidence tests fails with evidence, not a bare timeout."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import test_timeout_evidence


def test_stalled_nested_pytest_fails_with_elapsed_time_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pytest's output capture is suspended outside test items, so this print reaches the child's real stdout.
    (tmp_path / "conftest.py").write_text(
        "import time\n"
        "def pytest_unconfigure(config):\n"
        "    print('STALL-MARKER', flush=True)\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    # Shrink the nested-pytest bound so the stall trips quickly. The unfixed module has no
    # such name, so it still runs its own fixed 30 s bound.
    monkeypatch.setattr(test_timeout_evidence, "SUBPROCESS_TIMEOUT_S", 2, raising=False)
    with pytest.raises(pytest.fail.Exception) as raised:
        test_timeout_evidence._run_pytest_case(  # pyright: ignore[reportPrivateUsage]
            tmp_path, 0, "@pytest.mark.timeout(10)", "pass"
        )
    message = str(raised.value)
    assert "timed out after 2 s" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
