"""A stalled git child of the wheel-build fixtures fails with evidence, not a bare timeout."""

from __future__ import annotations

import math
import os

import pytest
from tests.dev_support.test_build_development_wheel import init_git, isolated_git  # noqa: F401
from tests.time_budget import scaled

_STALL_S = scaled(10)


@pytest.mark.skipif(os.name == "nt", reason="a shell script stands in for git")
def test_stalled_git_fixture_fails_with_elapsed_time_and_partial_output(tmp_path, monkeypatch):
    """The fixture's first ``git init`` hangs after printing a marker."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    # The child outlives the deadline six times over, so only the deadline ends it.
    sleep_s = math.ceil(6 * _STALL_S)
    fake_git.write_bytes(f"#!/bin/sh\necho STALL-MARKER\nexec sleep {sleep_s}\n".encode())
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(pytest.fail.Exception) as failure:
        init_git(repo, timeout=_STALL_S)

    message = str(failure.value)
    assert f"timed out after {_STALL_S:g} s" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
