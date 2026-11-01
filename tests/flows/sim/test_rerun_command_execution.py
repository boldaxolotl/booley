"""A printed rerun command, executed, runs the same Test Run again (ADR 0072).

Each case fails one Test Run on the direct Simulation path with a fake
simulator, takes the rerun command from the report, executes it as a new
invocation, and compares what reached the simulator: the same arguments, the
same Test Variant environment, the same Test Run name.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.base import SubprocessResult
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EXIT_FAILURE
from tests.flows.sim.test_flow import (
    _FLOW_ENABLED,
    _canned_build_session,  # noqa: F401 -- autouse fixture
    _make_flow,
)

_VARIANTS = {
    "stress": {
        "default": "short",
        "short": ["+iters=10"],
        "soak": {"args": ["+iters=900"], "env": {"MODE": "soak"}},
    }
}
_NAMED = {"tests": ["smoke", "stress"], "select": "+test={name}", "variants": _VARIANTS}
_CORE = (
    "CAPI=2:\nname: {vlnv}\ntargets:\n  lite:\n    flow: sim\n"
    "    flow_options: {{tool: {tool}}}\n    toplevel: alu_tb\n"
)


def _invoke(root: Path, target: str, argv: list[str], section: dict | None, tool: str, vlnv: str):
    """Run one invocation that fails every launch; return its report and launches."""
    root.mkdir()
    (root / "lite.core").write_text(_CORE.format(vlnv=vlnv, tool=tool), encoding="utf-8")
    if "#" in target:
        # A second core with a Target of the same name: only the qualified
        # selector names this one, so the rerun command must keep it.
        (root / "other.core").write_text(
            _CORE.format(vlnv="acme:lib:other:1", tool=tool), encoding="utf-8"
        )
    flow = _make_flow(root, config=target, extra_args=argv, seed_core=False)
    launches: list[tuple[list[str], str | None]] = []

    def execute(command):
        script = command[-1]
        words = script.rsplit(" && ", maxsplit=1)[-1].split()
        plusargs = [word.removeprefix("--plusarg=") for word in words if "--plusarg=" in word]
        mode = "soak" if "export MODE=soak" in script else None
        launches.append((plusargs, mode))
        return SubprocessResult(returncode=1, stdout="[SIM_RESULT] FAILED\n", duration_s=0.1)

    sections = normalize_tests_toml({"lite": section}) if section is not None else {}
    names = {"lite": list(section["tests"])} if section and "tests" in section else {}
    with (
        patch(
            "booley.flows.target_test_plan_loading.load_test_configuration", return_value=sections
        ),
        patch("booley.flows.sim.flow._get_test_names", return_value=names),
        patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
        patch.object(flow, "_execute", side_effect=execute),
        flow.publication_resources,
    ):
        result = flow._run()
    assert result.exit_code == EXIT_FAILURE, result.report_text
    (report_path,) = (root / "reports/sim/1/targets").glob("*/simulation.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return result, [row["name"] for row in report["test_runs"]], launches


@pytest.mark.parametrize(
    ("target", "argv", "section", "tool", "vlnv", "test_run", "command"),
    [
        pytest.param(
            "lite",
            ["--test", "stress+soak@7"],
            _NAMED,
            "verilator",
            "::lite:0",
            "stress+soak@7",
            "booley flow sim --target lite --test=stress+soak@7",
            id="named",
        ),
        pytest.param(
            "lite",
            ["--seed", "9"],
            None,
            "verilator",
            "::lite:0",
            "<default>@9",
            "booley flow sim --target lite --seed 9",
            id="unnamed",
        ),
        pytest.param(
            "lite",
            ["--test", "stress+soak"],
            _NAMED,
            "icarus",
            "::lite:0",
            "stress+soak",
            "booley flow sim --target lite --test=stress+soak",
            id="unseeded",
        ),
        pytest.param(
            "lite",
            [],
            None,
            "icarus",
            "::lite:0",
            "<default>",
            "booley flow sim --target lite",
            id="unnamed-unseeded",
        ),
        pytest.param(
            "acme:lib:lite:1#lite",
            ["--test", "stress+soak@2147483647"],
            _NAMED,
            "verilator",
            "acme:lib:lite:1",
            "stress+soak@2147483647",
            # The catalog's own selector of this Target: qualified, so quoted.
            "booley flow sim --target 'lite#lite' --test=stress+soak@2147483647",
            id="vlnv-qualified",
        ),
    ],
)
def test_executing_the_rerun_command_reproduces_the_test_run(
    tmp_path: Path, target, argv, section, tool, vlnv, test_run, command
):
    first, first_runs, first_launches = _invoke(
        tmp_path / "first", target, argv, section, tool, vlnv
    )
    assert first_runs == [test_run]
    lines = first.report_text.splitlines()
    rerun = lines[lines.index("  Rerun a failed Test Run with the same seed:") + 1].strip()
    assert rerun == command

    words = shlex.split(rerun)
    assert words[:4] == ["booley", "flow", "sim", "--target"]
    again, again_runs, again_launches = _invoke(
        tmp_path / "again", words[4], words[5:], section, tool, vlnv
    )

    assert again_runs == first_runs
    assert again_launches == first_launches and len(first_launches) == 1
    assert rerun in again.report_text
