"""Behavior of the ``Tests`` workflow's RISC-V tooling steps (ADR 0070).

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
from pathlib import Path
from typing import get_args

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
_CONTEXT = "docker-image://ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "d" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="workflow steps run under bash on the Linux runner"
)

# Fake python: log argv, create any --record file the way the real helpers
# would, print a trivial check script, and fail the compatibility check or the
# resolver when the test asks for it.
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
  *"--name riscv_tooling_compat"*) exit "${COMPAT_STATUS:-0}" ;;
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
  "image inspect"*) echo '[]' ;;
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


def _steps() -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["bwave-smoke"]["steps"]


def _step(name: str) -> dict:
    steps = _steps()
    group = next(step for step in steps if "parallel" in step)["parallel"]
    return next(step for step in (*steps, *group) if step.get("name") == name)


def _expressions_removed(script: str) -> str:
    """Replace ``${{ … }}`` expressions, which GitHub renders before bash runs."""
    return re.sub(r"\$\{\{[^}]*\}\}", "1", script)


def _run(
    tmp_path: Path, step: dict, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (
        ("python", _FAKE_PYTHON),
        ("docker", _FAKE_DOCKER),
        ("timeout", _FAKE_TIMEOUT),
    ):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", "-e", "-c", _expressions_removed(step["run"])],
        cwd=_ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


# --- Resolve step ---------------------------------------------------------


def _resolve_calls(calls: list[str]) -> list[str]:
    """Return the resolver's own invocations, not the ``timeout`` wrapper's."""
    return [call for call in calls if call.startswith(".github/scripts/riscv_tooling.py resolve")]


def test_resolve_step_is_path_gated_and_runs_before_the_lanes() -> None:
    steps = _steps()
    resolve = next(step for step in steps if step.get("id") == "riscv-tooling")
    group = next(index for index, step in enumerate(steps) if "parallel" in step)
    buildx = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("docker/setup-buildx-action@")
    )

    assert resolve["if"] == "needs.changes.outputs.riscv_image == 'true'"
    assert buildx < steps.index(resolve) < group
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
    return _run(
        tmp_path,
        _step("Run RISC-V candidate image contract"),
        {"TICKET_SLUG": "demo", "TOOLING_CONTEXT": _CONTEXT, **env},
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
    assert any("--name riscv_tooling_compat" in call for call in calls)
    assert not _fallbacks(calls)


def test_local_source_builds_the_stage_without_a_compat_check(tmp_path: Path) -> None:
    result, calls = _lane(tmp_path, TOOLING_SOURCE="local")

    assert result.returncode == 0, result.stderr
    (build,) = _substrate_builds(calls)
    assert "riscv-tooling=" not in build
    assert not any("riscv_tooling_compat" in call for call in calls)
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
    # The rejected attempt's evidence leaves the final path's phase records.
    evidence = tmp_path / "runner" / "riscv-image-evidence"
    attempt = {path.name for path in (evidence / "registry-attempt").iterdir()}
    assert "riscv-tool-substrate.json" in attempt
    assert "riscv-tool-substrate-build.raw.jsonl" in attempt
    assert ("riscv-tooling-compat.json" in attempt) == ("COMPAT_STATUS" in env)
    assert not (evidence / "records" / "riscv-tooling-compat.json").exists()
    # The local rebuild writes a fresh record for the final path.
    assert (evidence / "records" / "riscv-tool-substrate.json").exists()


def test_failed_local_rebuild_still_fails_the_lane(tmp_path: Path) -> None:
    result, calls = _lane(
        tmp_path, TOOLING_SOURCE="registry", COMPAT_STATUS="3", LOCAL_BUILD_STATUS="1"
    )

    assert result.returncode != 0
    assert len(_substrate_builds(calls)) == 2
    assert not any("Dockerfile.wheel" in call for call in _builds(calls))


# --- Duration budget ------------------------------------------------------


def _budget(tmp_path: Path, record: str | None, riscv: str = "true") -> tuple[int, str]:
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
            "RISCV_IMAGE": riscv,
            "TOOLING_RECORD": str(path),
            "REGISTRY_BUDGET_SECONDS": "111",
            "LOCAL_BUDGET_SECONDS": "222",
        },
    )
    budget = next(
        (call.split("--budget-seconds ")[1].split()[0] for call in calls if "budget" in call), ""
    )
    return result.returncode, budget


@pytest.mark.parametrize(
    ("source", "budget"),
    [("registry", "111"), ("local", "222"), ("local-compat-fallback", "222")],
)
def test_budget_follows_the_recorded_tooling_source(
    tmp_path: Path, source: str, budget: str
) -> None:
    assert _budget(tmp_path, json.dumps({"schema_version": 1, "source": source})) == (0, budget)
    assert f"`{source}`" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_budget_without_a_source_record_is_the_local_budget(tmp_path: Path) -> None:
    assert _budget(tmp_path, None) == (0, "222")


def test_budget_without_the_riscv_lane_is_the_standard_budget(tmp_path: Path) -> None:
    assert _budget(tmp_path, None, riscv="false") == (0, "600")


def test_budget_rejects_an_unreadable_source_record(tmp_path: Path) -> None:
    status, budget = _budget(tmp_path, "{not json")

    assert status != 0
    assert budget == ""


def test_registry_budget_never_exceeds_the_local_budget() -> None:
    """A hit that is as slow as a local build has lost its reason to exist."""
    env = _step("Enforce duration budget")["env"]

    assert env["REGISTRY_BUDGET_SECONDS"] <= env["LOCAL_BUDGET_SECONDS"]


def test_measurement_arms_agree_across_workflow_classifier_and_evidence() -> None:
    """The dispatch input, its validator, and the evidence schema name the same arms."""
    sys.path.insert(0, str(_ROOT / ".github/scripts"))
    import ci_changes
    import riscv_phase_metrics

    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["riscv_measurement"]["options"]

    assert tuple(dispatch) == ci_changes.RISCV_MEASUREMENT_ARMS
    assert get_args(riscv_phase_metrics.MeasurementArm) == ci_changes.RISCV_MEASUREMENT_ARMS
