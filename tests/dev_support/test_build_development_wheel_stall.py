"""A stalled git child of the wheel-build fixtures fails with evidence, not a bare timeout."""

from __future__ import annotations

import os

import pytest
from tests.dev_support.test_build_development_wheel import init_git, isolated_git  # noqa: F401


@pytest.mark.skipif(os.name == "nt", reason="a shell script stands in for git")
def test_stalled_git_fixture_fails_with_elapsed_time_and_partial_output(tmp_path, monkeypatch):
    """The fixture's first ``git init`` hangs after printing a marker."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_bytes(b"#!/bin/sh\necho STALL-MARKER\nexec sleep 60\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(pytest.fail.Exception) as failure:
        init_git(repo)

    message = str(failure.value)
    assert "timed out after" in message
    assert "elapsed" in message
    assert "STALL-MARKER" in message
