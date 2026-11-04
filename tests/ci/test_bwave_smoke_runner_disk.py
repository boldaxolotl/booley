"""Disk handling of the ``bwave-smoke`` job in the ``Tests`` workflow (#2681).

The job builds several images and runs a dozen validations concurrently on one
hosted runner, so its `/` filesystem can fill up mid-group. These tests pin the
steps that free preinstalled toolchains before any image build and log free
space and scratch sizes around the validation group, so an out-of-disk failure
is both less likely and attributable.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).parents[2]

_FREE_STEP = "Free unused runner disk"
_BEFORE_STEP = "Log free disk before image validations"
_AFTER_STEP = "Log disk use after image validations"

# Fakes put on PATH: sudo and timeout run the rest of their argv (timeout drops
# its duration, so no GNU timeout is needed); docker always fails to prove the
# report never changes the job result.
_FAKE_SUDO = '#!/usr/bin/env bash\nexec "$@"\n'
_FAKE_TIMEOUT = '#!/usr/bin/env bash\nshift\nexec "$@"\n'
_FAKE_DOCKER = "#!/usr/bin/env bash\necho 'docker unavailable' >&2\nexit 1\n"


def _steps() -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["bwave-smoke"]["steps"]


def _index(steps: list[dict], name: str) -> int:
    matches = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert matches, f"bwave-smoke has no step named {name!r}"
    return matches[0]


def _builds_image(step: dict) -> bool:
    return (
        str(step.get("name", "")).startswith("Build ")
        or str(step.get("uses", "")).startswith("docker/build-push-action@")
        or "docker buildx build" in str(step.get("run", ""))
    )


def test_runner_disk_is_freed_before_image_builds() -> None:
    steps = _steps()
    free = _index(steps, _FREE_STEP)
    step = steps[free]
    script = step["run"]

    assert "if" not in step, "the disk-freeing step must run on every lane"
    builds = [i for i, other in enumerate(steps) if _builds_image(other)]
    assert builds, "expected image-building steps in bwave-smoke"
    assert free < min(builds)
    assert free < _index(steps, "Start test-phase duration clock")

    removal = script.index("sudo rm -rf")
    assert script.count("df -h") >= 2
    assert script.index("df -h") < removal < script.rindex("df -h")
    # setup-python resolves Python from the toolcache: only CodeQL may go.
    assert not re.search(r"/opt/hostedtoolcache\s*(\\|$)", script, re.MULTILINE)


def test_free_disk_is_logged_around_image_validations() -> None:
    steps = _steps()
    group = next(i for i, step in enumerate(steps) if "parallel" in step)
    before, after = steps[group - 1], steps[group + 1]

    assert before.get("name") == _BEFORE_STEP
    assert "df -h /" in before["run"]
    assert after.get("name") == _AFTER_STEP
    assert after.get("if") == "always()"
    assert "df -h /" in after["run"]
    assert "du -sh" in after["run"]
    assert '"${RUNNER_TEMP}"/bwave-smoke/*' in after["run"]


def _run_report(tmp_path: Path, runner_temp: Path) -> subprocess.CompletedProcess[str]:
    steps = _steps()
    script = re.sub(r"\$\{\{[^}]*\}\}", "1", steps[_index(steps, _AFTER_STEP)]["run"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("sudo", _FAKE_SUDO), ("timeout", _FAKE_TIMEOUT), ("docker", _FAKE_DOCKER)):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
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

    # The group may never have started: no scratch directory, still exit 0.
    second = tmp_path / "second"
    second.mkdir()
    empty_runner_temp = tmp_path / "empty-runner"
    empty_runner_temp.mkdir()
    result = _run_report(second, empty_runner_temp)
    assert result.returncode == 0, result.stderr
