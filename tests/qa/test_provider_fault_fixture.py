"""Exercise real fixture processes without provider credentials or network calls."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "qa/scenarios/taxi/probes/provider-fault/codex_fixture.py"
)


@pytest.mark.parametrize(
    "mode,text",
    [
        ("subscription", "usage limit"),
        ("transient", "response stalled mid-stream"),
        ("crash", "ordinary provider failure"),
    ],
)
def test_provider_fault_retains_attempt_without_prompt(tmp_path, mode, text):
    (tmp_path / "mode").write_text(mode)
    result = subprocess.run(
        [sys.executable, str(FIXTURE), "exec", "--json", "-"],
        input="private prompt marker",
        text=True,
        capture_output=True,
        env={**os.environ, "QA_PROVIDER_FIXTURE_ROOT": str(tmp_path)},
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert text in result.stderr
    assert not result.stdout
    assert (tmp_path / "attempt-0000.txt").read_text() == mode + "\n"
    assert "private prompt marker" not in result.stderr
