"""A stalled Vivado wrapper child fails its test with elapsed time and partial output."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.eda.provisioning.policies.test_vivado import _run_wrapper


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell script stands in for the wrapper")
def test_stalled_wrapper_fails_with_elapsed_time_and_partial_output(tmp_path: Path) -> None:
    stalled = tmp_path / "stalled"
    # exec makes sleep the direct child, so a kill of that child alone ends the stall.
    stalled.write_text("#!/bin/sh\necho STALL-MARKER\nexec sleep 60\n", encoding="utf-8")
    stalled.chmod(0o755)
    with pytest.raises(pytest.fail.Exception) as raised:
        _run_wrapper(stalled)
    message = str(raised.value)
    assert "timed out after 10 s" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
