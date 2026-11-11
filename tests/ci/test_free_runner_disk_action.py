"""The shared ``free-runner-disk`` composite action (#2681, #2896).

``bwave-smoke`` (Tests workflow) and ``worktree-mounts`` (Deep tests workflow)
both build the Sandbox Image on a hosted runner whose ``/`` is nearly full.
The action drops preinstalled toolchains neither job uses and logs free disk,
including the Docker storage filesystem the image-build preflight measures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
_ACTION = _ROOT / ".github/actions/free-runner-disk/action.yml"

# Fakes put on PATH. sudo never runs its argv (it would delete host paths): it
# records the arguments and prints a marker so the test can order the logs
# around the removal. docker answers only the exact `info` query the action
# makes (a mistyped template field fails like the real CLI, which the action's
# `|| true` would otherwise hide) with the directory in DOCKER_ROOT. df prints a
# marker per call so the test sees which filesystems each report measures.
_FAKE_SUDO = '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$SUDO_LOG"\necho SUDO-REMOVAL-MARKER\n'
_FAKE_DOCKER = (
    "#!/usr/bin/env bash\n"
    '[ "$*" = "info --format {{.DockerRootDir}}" ] || exit 1\n'
    'echo "$DOCKER_ROOT"\n'
)
_FAKE_DF = '#!/usr/bin/env bash\necho "DF $*"\n'

_REMOVED = [
    "/usr/local/lib/android",
    "/usr/share/dotnet",
    "/opt/ghc",
    "/usr/local/.ghcup",
    "/opt/hostedtoolcache/CodeQL",
]


@pytest.mark.skipif(sys.platform == "win32", reason="the action's step runs under bash on Linux")
def test_free_runner_disk_logs_docker_storage_around_removal(tmp_path: Path) -> None:
    assert _ACTION.is_file(), "the shared free-runner-disk composite action is missing"
    steps = yaml.safe_load(_ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    assert [step["shell"] for step in steps] == ["bash"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("sudo", _FAKE_SUDO), ("docker", _FAKE_DOCKER), ("df", _FAKE_DF)):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    docker_root = tmp_path / "docker-root"
    docker_root.mkdir()
    sudo_log = tmp_path / "sudo.log"
    result = run_bounded_child(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", steps[0]["run"]],
        env={
            # BASH_ENV would make a non-interactive bash source a host file.
            **{key: value for key, value in os.environ.items() if key != "BASH_ENV"},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_ROOT": str(docker_root),
            "SUDO_LOG": str(sudo_log),
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # `df` measures the whole host before and after the removal, and the Docker
    # root explicitly; `df` prints only the mount, so the `Docker storage:` line
    # ties each report to its path.
    lines = result.stdout.splitlines()
    docker_lines = [i for i, line in enumerate(lines) if line == f"Docker storage: {docker_root}"]
    marker = lines.index("SUDO-REMOVAL-MARKER")
    assert len(docker_lines) == 2
    assert docker_lines[0] < marker < docker_lines[1]
    reports = [line for line in lines if line.startswith("DF ") or line == "SUDO-REMOVAL-MARKER"]
    one_report = ["DF -h", f"DF -h {docker_root}"]
    assert reports == [*one_report, "SUDO-REMOVAL-MARKER", *one_report]
    # setup-python resolves Python from the toolcache: only CodeQL may go, never
    # the toolcache itself, its trailing-slash form, or a glob over its entries.
    removed = sudo_log.read_text(encoding="utf-8").splitlines()
    assert removed == ["rm", "-rf", *_REMOVED]
