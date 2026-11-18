"""A stalled Vivado wrapper child fails its test with elapsed time and partial output."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from tests.eda.provisioning.policies.test_vivado import _run_wrapper
from tests.time_budget import scaled

_STALL_S = scaled(10)


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell script stands in for the wrapper")
def test_stalled_wrapper_fails_with_elapsed_time_and_partial_output(tmp_path: Path) -> None:
    stalled = tmp_path / "stalled"
    # exec makes sleep the direct child, so a kill of that child alone ends the stall.
    # The child outlives the deadline six times over, so only the deadline ends it.
    stalled.write_text(
        f"#!/bin/sh\necho STALL-MARKER\nexec sleep {math.ceil(6 * _STALL_S)}\n", encoding="utf-8"
    )
    stalled.chmod(0o755)
    with pytest.raises(pytest.fail.Exception) as raised:
        _run_wrapper(stalled, timeout=_STALL_S)
    message = str(raised.value)
    assert f"timed out after {_STALL_S:g} s" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
