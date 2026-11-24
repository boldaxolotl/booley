"""A stalled workflow step in the RISC-V workflow tests fails with evidence, not a bare timeout."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from tests.ci import test_riscv_tooling_workflow
from tests.time_budget import scaled

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="workflow steps run under bash on the Linux runner"
)


_STALL_S = scaled(2)


def test_stalled_workflow_step_fails_with_elapsed_time_and_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the step bound so the stall trips quickly. The unfixed module has no such
    # name, so it still runs its own fixed 60 s bound.
    monkeypatch.setattr(
        test_riscv_tooling_workflow, "SUBPROCESS_TIMEOUT_S", _STALL_S, raising=False
    )
    # exec makes sleep the direct child, so the unfixed direct-child kill also ends the stall.
    sleep_s = math.ceil(60 * _STALL_S)
    stalled = {"run": f"echo STALL-MARKER\nexec sleep {sleep_s}\n"}
    with pytest.raises(pytest.fail.Exception) as raised:
        test_riscv_tooling_workflow._run(tmp_path, stalled, {})  # pyright: ignore[reportPrivateUsage]
    message = str(raised.value)
    assert f"timed out after {_STALL_S:g} s" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
