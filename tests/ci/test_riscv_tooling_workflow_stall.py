"""A stalled workflow step in the RISC-V workflow tests fails with evidence, not a bare timeout."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.ci import test_riscv_tooling_workflow

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="workflow steps run under bash on the Linux runner"
)


def test_stalled_workflow_step_fails_with_elapsed_time_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the step bound so the stall trips quickly. The unfixed module has no such
    # name, so it still runs its own fixed 60 s bound.
    monkeypatch.setattr(test_riscv_tooling_workflow, "SUBPROCESS_TIMEOUT_S", 2, raising=False)
    # exec makes sleep the direct child, so the unfixed direct-child kill also ends the stall.
    stalled = {"run": "echo STALL-MARKER\nexec sleep 120\n"}
    with pytest.raises(pytest.fail.Exception) as raised:
        test_riscv_tooling_workflow._run(tmp_path, stalled, {})  # pyright: ignore[reportPrivateUsage]
    message = str(raised.value)
    assert "timed out after 2 s" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
