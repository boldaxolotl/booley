"""Behavior of the ``Tests`` workflow's ``riscv-image`` job (ADR 0070).

The step scripts run for real under bash. ``docker``, ``python``, and
``timeout`` are fakes on ``PATH`` that log every call, so these tests pin the
source selection, the compatibility fallback, and the budget routing without a
registry or a Docker daemon.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

_ROOT = Path(__file__).parents[2]
_CONTEXT = "docker-image://ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "d" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="workflow steps run under bash on the Linux runner"
)

# Fake python: log argv, create any --record file the way the real helpers
# would, print a trivial check script, and fail the resolver when requested.
_FAKE_PYTHON = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_LOG}"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == --record ]]; then
    mkdir -p "$(dirname "${args[i + 1]}")"
    touch "${args[i + 1]}"
  fi
done
case "$*" in
  *"riscv_tooling.py checks"*) echo true ;;
  *"riscv_tooling.py resolve"*) exit "${RESOLVE_STATUS:-0}" ;;
esac
exit 0
"""

# Fake docker: log argv, fail a build that overrides the tooling stage when
# asked, and fail a local build when asked.
_FAKE_DOCKER = r"""#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "${FAKE_LOG}"
case "$*" in
  "buildx build"*"riscv-tooling=docker-image://"*) exit "${REGISTRY_BUILD_STATUS:-0}" ;;
  "buildx build"*"Dockerfile.riscv"*) exit "${LOCAL_BUILD_STATUS:-0}" ;;
  "buildx build"*"Dockerfile.wheel"*) exit "${WHEEL_BUILD_STATUS:-0}" ;;
  "run "*"--entrypoint bash booley-riscv-substrate:ci"*)
    echo compatibility-output; exit "${COMPAT_STATUS:-0}" ;;
esac
exit 0
"""

# Fake timeout: report a timeout without running the command when asked.
_FAKE_TIMEOUT = r"""#!/usr/bin/env bash
printf 'timeout %s\n' "$*" >> "${FAKE_LOG}"
if [[ -n "${TIMEOUT_STATUS:-}" ]]; then exit "${TIMEOUT_STATUS}"; fi
shift
exec "$@"
"""

_FAKE_SUDO = r"""#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "${FAKE_LOG}"
if [[ "$1" == chmod ]]; then shift; exec chmod "$@"; fi
"""


def _steps() -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["riscv-image"]["steps"]


def _step(name: str) -> dict:
    return next(step for step in _steps() if step.get("name") == name)


def _expressions_removed(script: str) -> str:
    """Replace ``${{ … }}`` expressions, which GitHub renders before bash runs."""
    return re.sub(r"\$\{\{[^}]*\}\}", "1", script)


def _run(
    tmp_path: Path, step: dict, env: dict[str, str], *, cwd: Path = _ROOT
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (
        ("python", _FAKE_PYTHON),
        ("docker", _FAKE_DOCKER),
        ("timeout", _FAKE_TIMEOUT),
        ("sudo", _FAKE_SUDO),
    ):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    result = run_bounded_child(
        ["bash", "-e", "-c", _expressions_removed(step["run"])],
        cwd=cwd,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            **env,
        },
        check=False,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_demo_chowns_and_mounts_only_its_readiness_subdirectory(tmp_path: Path) -> None:
    result, calls = _run(
        tmp_path,
        {"run": 'bash .github/scripts/run_picorv32_ci_demo.sh "${TEST_WORKSPACE}" booley-r2'},
        {"TEST_WORKSPACE": str(tmp_path)},
    )
    directory = tmp_path / "runner/riscv-image-evidence/goal-readiness"
    assert result.returncode == 0, result.stderr
    assert [call for call in calls if call.startswith("sudo chown")] == [
        f"sudo chown -R 1000:1000 demo {directory}"
    ]
    assert any(f"src={directory},dst=/evidence" in call for call in calls)


def test_readiness_ownership_and_upload_access_are_restored_before_upload(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "runner/riscv-image-evidence/goal-readiness"
    directory.mkdir(parents=True)
    evidence = directory / "goal-readiness.json"
    evidence.write_text("{}")
    evidence.chmod(0o600)
    step = _step("Restore RISC-V demo checkout ownership")
    assert "always()" in step["if"]
    steps = _steps()
    assert steps.index(step) < steps.index(_step("Upload candidate RISC-V evidence"))
    result, calls = _run(tmp_path, step, {})
    assert result.returncode == 0, result.stderr
    assert f"sudo chown -R {os.getuid()}:{os.getgid()} {directory}" in calls
    assert evidence.stat().st_mode & 0o004


# --- Resolve step ---------------------------------------------------------


def _resolve_calls(calls: list[str]) -> list[str]:
    """Return the resolver's own invocations, not the ``timeout`` wrapper's."""
    return [call for call in calls if call.startswith(".github/scripts/riscv_tooling.py resolve")]


def test_resolve_step_runs_before_any_image_build() -> None:
    """Resolving first lets the tooling pull overlap the standard substrate build."""
    steps = _steps()
    resolve = next(step for step in steps if step.get("id") == "riscv-tooling")
    python = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    builds = [
        index
        for index, step in enumerate(steps)
        if str(step.get("name", "")).startswith("Build ") or "buildx build" in step.get("run", "")
    ]

    assert python < steps.index(resolve) < min(builds)
    assert "uses" not in resolve


def test_resolve_step_asks_the_registry_with_a_timeout(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, _step("Resolve RISC-V tooling source"), {})

    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("timeout 120 python .github/scripts/riscv_tooling.py resolve")
    assert len(_resolve_calls(calls)) == 1
    assert "--force-local" not in calls[1]


def test_cold_arm_never_asks_the_registry(tmp_path: Path) -> None:
    result, calls = _run(
        tmp_path, _step("Resolve RISC-V tooling source"), {"MEASUREMENT_ARM": "cold"}
    )

    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("timeout") for call in calls)
    assert _resolve_calls(calls)[0].endswith("--force-local cold")


def test_registry_timeout_builds_locally(tmp_path: Path) -> None:
    result, calls = _run(
        tmp_path, _step("Resolve RISC-V tooling source"), {"TIMEOUT_STATUS": "124"}
    )

    assert result.returncode == 0, result.stderr
    assert "::warning::RISC-V tooling lookup timed out" in result.stdout
    assert _resolve_calls(calls)[-1].endswith("--force-local registry-timeout")


def test_resolver_integrity_failure_fails_the_step(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, _step("Resolve RISC-V tooling source"), {"RESOLVE_STATUS": "1"})

    assert result.returncode == 1
    assert not any("--force-local" in call for call in calls)


# --- RISC-V lane ----------------------------------------------------------


def _lane(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    scripts = tmp_path / ".github/scripts"
    scripts.mkdir(parents=True)
    for name, status in (
        ("verify_riscv_image_contract.sh", "IMAGE_CONTRACT_STATUS"),
        ("run_picorv32_ci_demo.sh", "DEMO_STATUS"),
    ):
        fake = scripts / name
        fake.write_text(
            f'#!/usr/bin/env bash\nprintf "{name} %s\\n" "$*" >> "${{FAKE_LOG}}"\n'
            f'exit "${{{status}:-0}}"\n'
        )
        fake.chmod(0o755)
    return _run(
        tmp_path,
        _step("Run RISC-V candidate image contract"),
        {"TOOLING_CONTEXT": _CONTEXT, **env},
        cwd=tmp_path,
    )


def _builds(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("docker buildx build") and "riscv" in call]


def _substrate_builds(calls: list[str]) -> list[str]:
    return [call for call in _builds(calls) if "Dockerfile.riscv" in call]


def _fallbacks(calls: list[str]) -> list[str]:
    return [call for call in calls if "riscv_tooling.py fallback" in call]


def test_registry_hit_overrides_the_stage_and_checks_compatibility(tmp_path: Path) -> None:
    result, calls = _lane(tmp_path, TOOLING_SOURCE="registry")

    assert result.returncode == 0, result.stderr
    (build,) = _substrate_builds(calls)
    assert f"--build-context riscv-tooling={_CONTEXT}" in build
    assert any("--entrypoint bash booley-riscv-substrate:ci" in call for call in calls)
    assert not _fallbacks(calls)


def test_local_source_builds_the_stage_without_a_compat_check(tmp_path: Path) -> None:
    result, calls = _lane(tmp_path, TOOLING_SOURCE="local")

    assert result.returncode == 0, result.stderr
    (build,) = _substrate_builds(calls)
    assert "riscv-tooling=" not in build
    assert not any("--entrypoint bash booley-riscv-substrate:ci" in call for call in calls)
    assert not _fallbacks(calls)


@pytest.mark.parametrize(
    ("env", "detail"),
    [
        ({"COMPAT_STATUS": "3"}, "compatibility check exited 3"),
        ({"REGISTRY_BUILD_STATUS": "1"}, "composed candidate build exited 1"),
    ],
    ids=["compat-check", "composed-build"],
)
def test_incompatible_published_tooling_falls_back_to_a_local_build(
    tmp_path: Path, env: dict[str, str], detail: str
) -> None:
    """A stale registry artifact costs time, never the pull request."""
    result, calls = _lane(tmp_path, TOOLING_SOURCE="registry", **env)

    assert result.returncode == 0, result.stderr
    first, second = _substrate_builds(calls)
    assert "riscv-tooling=" in first
    assert "riscv-tooling=" not in second
    (fallback,) = _fallbacks(calls)
    assert fallback.endswith(f"--detail {detail}")
    assert f"::warning::Published RISC-V tooling {_CONTEXT} failed: {detail}" in result.stdout
    evidence = tmp_path / "runner" / "riscv-image-evidence"
    assert not (evidence / "registry-attempt").exists()
    log = evidence / "tooling-compat.log"
    assert log.exists() == ("COMPAT_STATUS" in env)
    if log.exists():
        assert log.read_text() == "compatibility-output\n"


def test_failed_local_rebuild_still_fails_the_lane(tmp_path: Path) -> None:
    result, calls = _lane(
        tmp_path, TOOLING_SOURCE="registry", COMPAT_STATUS="3", LOCAL_BUILD_STATUS="1"
    )

    assert result.returncode != 0
    assert len(_substrate_builds(calls)) == 2
    assert not any("Dockerfile.wheel" in call for call in _builds(calls))


# --- Duration budget ------------------------------------------------------


def _budget(tmp_path: Path, record: str | None) -> tuple[int, str, str]:
    """Return the exit code and the (warn, budget) thresholds the step passed on."""
    step = _step("Enforce duration budget")
    path = tmp_path / "runner" / "riscv-image-evidence" / "tooling-source.json"
    if record is not None:
        path.parent.mkdir(parents=True)
        path.write_text(record, encoding="utf-8")
    result, calls = _run(
        tmp_path,
        step,
        {
            **{name: str(value) for name, value in step["env"].items()},
            "TOOLING_RECORD": str(path),
            "REGISTRY_WARN_SECONDS": "100",
            "REGISTRY_BUDGET_SECONDS": "111",
            "LOCAL_WARN_SECONDS": "200",
            "LOCAL_BUDGET_SECONDS": "222",
        },
    )
    call = next((call for call in calls if "budget" in call), "")
    warn = call.split("--warn-seconds ")[1].split()[0] if "--warn-seconds " in call else ""
    budget = call.split("--budget-seconds ")[1].split()[0] if "--budget-seconds " in call else ""
    return result.returncode, warn, budget


@pytest.mark.parametrize(
    ("source", "warn", "budget"),
    [
        ("registry", "100", "111"),
        ("local", "200", "222"),
        ("local-compat-fallback", "200", "222"),
    ],
)
def test_budget_follows_the_recorded_tooling_source(
    tmp_path: Path, source: str, warn: str, budget: str
) -> None:
    record = json.dumps({"schema_version": 1, "source": source})
    assert _budget(tmp_path, record) == (0, warn, budget)
    assert f"`{source}`" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_budget_without_a_source_record_is_the_local_budget(tmp_path: Path) -> None:
    assert _budget(tmp_path, None) == (0, "200", "222")


def test_budget_rejects_an_unreadable_source_record(tmp_path: Path) -> None:
    status, warn, budget = _budget(tmp_path, "{not json")

    assert status != 0
    assert (warn, budget) == ("", "")


def test_registry_budget_never_exceeds_the_local_budget() -> None:
    """A hit that is as slow as a local build has lost its reason to exist."""
    env = _step("Enforce duration budget")["env"]

    assert env["REGISTRY_WARN_SECONDS"] <= env["LOCAL_WARN_SECONDS"]
    assert env["REGISTRY_BUDGET_SECONDS"] <= env["LOCAL_BUDGET_SECONDS"]


def test_every_lane_warns_at_or_below_its_budget() -> None:
    """The script rejects a warn threshold above its fail threshold; catch it at config time."""
    env = _step("Enforce duration budget")["env"]

    for lane in ("REGISTRY", "LOCAL"):
        assert 0 < env[f"{lane}_WARN_SECONDS"] <= env[f"{lane}_BUDGET_SECONDS"]


@pytest.mark.parametrize(
    "failure,started",
    [
        ("LOCAL_BUILD_STATUS", False),
        ("WHEEL_BUILD_STATUS", False),
        ("IMAGE_CONTRACT_STATUS", False),
        ("DEMO_STATUS", True),
        (None, True),
    ],
)
def test_readiness_upload_marker_tracks_demo_attempt(
    tmp_path: Path, failure: str | None, started: bool
) -> None:
    env = {"TOOLING_SOURCE": "local", "TOOLING_CONTEXT": ""}
    if failure:
        env[failure] = "1"
    result, calls = _lane(tmp_path, **env)
    assert result.returncode == (1 if failure else 0), result.stderr
    marker = tmp_path / "runner/riscv-image-evidence/goal-readiness-started"
    assert marker.exists() == started
    collected, _ = _run(tmp_path, _step("Read Goal readiness attempt marker"), {})
    assert collected.returncode == 0, collected.stderr
    output = tmp_path / "github-output"
    assert (output.exists() and "started=true" in output.read_text()) == started
    assert any(call.startswith("run_picorv32_ci_demo.sh ") for call in calls) == started


# --- Tooling prefetch and export ------------------------------------------


def _wait_for_exit(pid: int) -> None:
    """Wait (bounded) for a backgrounded fake to finish writing its log."""
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"background process {pid} did not exit")


def test_prefetch_pulls_the_published_digest_in_the_background(tmp_path: Path) -> None:
    step = _step("Prefetch published RISC-V tooling")
    steps = _steps()

    assert step["if"] == "steps.riscv-tooling.outputs.source == 'registry'"
    assert steps.index(_step("Resolve RISC-V tooling source")) < steps.index(step)
    assert steps.index(step) < steps.index(_step("Build candidate from published stable base"))
    result, _ = _run(tmp_path, step, {"TOOLING_CONTEXT": _CONTEXT})
    assert result.returncode == 0, result.stderr
    _wait_for_exit(int((tmp_path / "runner/riscv-tooling-prefetch.pid").read_text()))
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert calls == [f"docker pull --quiet {_CONTEXT.removeprefix('docker-image://')}"]


def _run_prefetch_wait_step(tmp_path: Path, pid: int | None) -> subprocess.CompletedProcess[str]:
    """Run the wait step with a one-second bound (fake ``seq``, no-op ``sleep``).

    ``pid`` is the prefetch process to wait on; ``None`` means the prefetch
    never wrote its pid file.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("seq", "#!/usr/bin/env bash\necho 1\n"), ("sleep", "#!/bin/sh\n")):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    evidence = tmp_path / "runner/riscv-image-evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "tooling-prefetch.log").write_text("pulled\n", encoding="utf-8")
    if pid is not None:
        (tmp_path / "runner/riscv-tooling-prefetch.pid").write_text(f"{pid}\n", encoding="utf-8")
    step = _step("Wait for RISC-V tooling prefetch")
    result, _ = _run(tmp_path, step, {name: str(value) for name, value in step["env"].items()})
    return result


def test_wait_returns_once_the_prefetch_has_exited(tmp_path: Path) -> None:
    # The test needs this exited child's PID, so it drives the Popen itself.
    done = subprocess.Popen(["true"])
    done.wait(timeout=10)

    result = _run_prefetch_wait_step(tmp_path, done.pid)

    assert result.returncode == 0, result.stderr
    assert "pulled" in result.stdout
    assert "::warning::" not in result.stdout


def test_wait_gives_up_on_a_stuck_prefetch_without_failing(tmp_path: Path) -> None:
    """The prefetch is an optimization; a stuck pull must never fail the lane."""
    # The test drives this Popen itself (kill, wait).
    stuck = subprocess.Popen(["sleep", "30"])
    try:
        result = _run_prefetch_wait_step(tmp_path, stuck.pid)
    finally:
        stuck.kill()
        stuck.wait(timeout=10)

    assert result.returncode == 0, result.stderr
    assert "::warning::RISC-V tooling prefetch still running" in result.stdout


def test_wait_without_a_started_prefetch_does_not_fail(tmp_path: Path) -> None:
    result = _run_prefetch_wait_step(tmp_path, None)

    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stdout


def test_wait_runs_on_the_lane_clock_before_the_build() -> None:
    steps = _steps()
    wait = _step("Wait for RISC-V tooling prefetch")

    assert wait["if"] == "steps.riscv-tooling.outputs.source == 'registry'"
    assert steps.index(_step("Start test-phase duration clock")) < steps.index(wait)
    assert steps.index(wait) < steps.index(_step("Run RISC-V candidate image contract"))


@pytest.mark.parametrize("source", ["registry", "local"])
def test_substrate_exports_zstd_without_a_gzip_load(tmp_path: Path, source: str) -> None:
    """BuildKit gzips single-threaded; zstd level 1 exports the tooling layer in seconds."""
    result, calls = _lane(tmp_path, TOOLING_SOURCE=source)

    assert result.returncode == 0, result.stderr
    (build,) = _substrate_builds(calls)
    assert (
        "--output type=image,name=booley-riscv-substrate:ci,compression=zstd,compression-level=1"
        in build
    )
    assert "--load" not in build
    assert "force-compression" not in build  # inherited standard layers keep their blobs


def test_lane_host_python_can_audit_zstd_layers() -> None:
    """image_contract.py reads zstd layer tars only on Python >= 3.14."""
    setup = next(
        step for step in _steps() if str(step.get("uses", "")).startswith("actions/setup-python@")
    )

    assert setup["with"]["python-version"] == "3.14"


def test_budget_is_reported_under_the_lane_label() -> None:
    step = _step("Enforce duration budget")

    assert "--label riscv-image" in step["run"]
    assert step["if"] == (
        "always() && steps.runtime-base.outputs.build != 'true' && "
        "steps.duration-clock.outcome == 'success'"
    )
