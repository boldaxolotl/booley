"""A nested pytest session that stalls dumps every thread's stack into its stderr."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_thread_leak_guard import _run_inner  # pyright: ignore[reportPrivateUsage]


def test_stalled_nested_test_dumps_every_thread_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 1 s dump, for this test only, fires while the nested test sleeps; the hang
    # guard would fail this test instead of returning.
    monkeypatch.setattr("tests.subprocess_budget.STALL_DUMP_S", 1, raising=False)
    module = """
        import time

        def test_blocks():
            time.sleep(3)
    """
    result = _run_inner(tmp_path, module)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Timeout (0:00:01)!" in result.stderr
    assert "in test_blocks" in result.stderr
