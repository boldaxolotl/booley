"""Disk handling of the ``bwave-smoke`` job in the ``Tests`` workflow (#2681).

The job builds several images and runs a dozen validations concurrently on one
hosted runner, so its `/` filesystem can fill up mid-group. This test runs the step that logs
free space and scratch sizes after the validation group, so an out-of-disk
failure is attributable even when docker itself fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]

_AFTER_STEP = "Log disk use after image validations"

# Fakes put on PATH: sudo and timeout run the rest of their argv (timeout drops
# its duration, so no GNU timeout is needed); docker always fails to prove the
# report never changes the job result.
_FAKE_SUDO = '#!/usr/bin/env bash\nexec "$@"\n'
_FAKE_TIMEOUT = '#!/usr/bin/env bash\nshift\nexec "$@"\n'
_FAKE_DOCKER = "#!/usr/bin/env bash\necho 'docker unavailable' >&2\nexit 1\n"


def _steps(job: str = "bwave-smoke") -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    return workflow["jobs"][job]["steps"]


def _index(steps: list[dict], name: str) -> int:
    matches = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert matches, f"job has no step named {name!r}"
    return matches[0]


def _run_report(tmp_path: Path, runner_temp: Path) -> subprocess.CompletedProcess[str]:
    steps = _steps()
    script = steps[_index(steps, _AFTER_STEP)]["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("sudo", _FAKE_SUDO), ("timeout", _FAKE_TIMEOUT), ("docker", _FAKE_DOCKER)):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    return run_bounded_child(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={
            # BASH_ENV would make a non-interactive bash source a host file.
            **{key: value for key, value in os.environ.items() if key != "BASH_ENV"},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
        },
        check=False,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run under bash on Linux")
def test_disk_report_after_validations_never_fails(tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner"
    (runner_temp / "bwave-smoke/verilator").mkdir(parents=True)
    (runner_temp / "bwave-smoke/verilator/scratch.bin").write_bytes(b"x" * 4096)
    (runner_temp / "bwave-smoke/openroad").mkdir()

    first = tmp_path / "first"
    first.mkdir()
    result = _run_report(first, runner_temp)
    assert result.returncode == 0, result.stderr
    assert "bwave-smoke/verilator" in result.stdout
    assert "bwave-smoke/openroad" in result.stdout
    assert "No such file" not in result.stderr

    # The group may never have started: no scratch directory, still exit 0 and
    # no du call on an empty list.
    third = tmp_path / "third"
    third.mkdir()
    empty_runner_temp = tmp_path / "empty-runner"
    empty_runner_temp.mkdir()
    result = _run_report(third, empty_runner_temp)
    assert result.returncode == 0, result.stderr
    assert "bwave-smoke" not in result.stdout
    assert "du:" not in result.stderr
