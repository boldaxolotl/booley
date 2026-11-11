"""A printed rerun command, executed, runs the same Test Run again (ADR 0072).

Each case fails one Test Run on the Simulation Campaign path with a fake
simulator, takes the rerun command from the report, executes it as a new
invocation through the production CLI parser, and compares what reached the
simulator: the same arguments, the same Test Variant environment, the same
Test Run name.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.builtin_cli import parse_request
from booley.flows.sim.request import SimRequest
from booley.runtime.endpoint_execution import EXIT_FAILURE
from tests.flows.sim.campaign_boundary import Launch, run_campaign_sim
from tests.flows.sim.test_flow import (
    SimulateFlow,
    _canned_build_session,  # noqa: F401 -- autouse fixture
)

_NAMED_TOML = (
    '[lite]\ntests = ["smoke", "stress"]\nselect = "+test={name}"\n'
    '[lite.variants.stress]\ndefault = "short"\nshort = ["+iters=10"]\n'
    'soak = { args = ["+iters=900"], env = { MODE = "soak" } }\n'
)
_CORE = (
    "CAPI=2:\nname: {vlnv}\ntargets:\n  lite:\n    flow: sim\n"
    "    flow_options: {{tool: {tool}}}\n    toplevel: alu_tb\n"
)


def _request_fields(argv: list[str]) -> dict[str, Any]:
    """What the production CLI parser makes of *argv*, as ``SimRequest`` keywords."""
    request = parse_request(SimulateFlow(), argv)
    defaults = SimRequest()
    return {
        field.name: getattr(request, field.name)
        for field in dataclasses.fields(request)
        if field.name not in {"work_dir", "report_dir"}
        and getattr(request, field.name) != getattr(defaults, field.name)
    }


def _invoke(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    tool: str,
    vlnv: str,
    *,
    registry: tuple[dict[str, list[str]], str] = ({"lite": ["smoke", "stress"]}, _NAMED_TOML),
    extra_files: dict[str, str] | None = None,
    build: SubprocessResult | None = None,
    exit_code: int = EXIT_FAILURE,
):
    """Run one campaign invocation that fails every launch; return its outcome and launches.

    *registry* is the catalog handed to the harness and the ``tests.toml`` text
    written over it (a Target without registered Tests has an empty catalog).
    """
    root.mkdir()
    catalog, registry_toml = registry
    files = {"lite.core": _CORE.format(vlnv=vlnv, tool=tool), ".booley/tests.toml": registry_toml}
    files.update(extra_files or {})
    if any("#" in word for word in argv):
        # A second core with a Target of the same name: only the qualified
        # selector names this one, so the rerun command must keep it.
        files["other.core"] = _CORE.format(vlnv="acme:lib:other:1", tool=tool)

    def fail(_launch: Launch) -> SubprocessResult:
        return SubprocessResult(returncode=1, stdout="[SIM_RESULT] FAILED\n", duration_s=0.1)

    run = run_campaign_sim(
        root,
        monkeypatch,
        catalog,
        fail,
        files=files,
        build=build,
        **_request_fields(argv),
    )
    assert run.result.exit_code == exit_code, run.result.outcome.report_text
    (report_path,) = (root / "reports/sim/1/targets").glob("*/simulation.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    launches = [
        (list(launch.work.plusargs), "soak" if "export MODE=soak" in launch.script else None)
        for launch in run.launches
    ]
    return run.result.outcome, [row["name"] for row in report["test_runs"]], launches


@pytest.mark.parametrize(
    ("target", "argv", "tool", "vlnv", "test_run", "command"),
    [
        pytest.param(
            "lite",
            ["--test", "stress+soak@7"],
            "verilator",
            "::lite:0",
            "stress+soak@7",
            "booley flow sim --target lite --test=stress+soak@7",
            id="named",
        ),
        pytest.param(
            "lite",
            ["--test", "stress+soak"],
            "icarus",
            "::lite:0",
            "stress+soak",
            "booley flow sim --target lite --test=stress+soak",
            id="unseeded",
        ),
        pytest.param(
            "acme:lib:lite:1#lite",
            ["--test", "stress+soak@2147483647"],
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target, argv, tool, vlnv, test_run, command
):
    first, first_runs, first_launches = _invoke(
        tmp_path / "first", monkeypatch, ["--target", target, *argv], tool, vlnv
    )
    assert first_runs == [test_run]
    lines = first.report_text.splitlines()
    rerun = lines[lines.index("  Rerun a failed Test Run with the same seed:") + 1].strip()
    assert rerun == command

    words = shlex.split(rerun)
    assert words[:3] == ["booley", "flow", "sim"]
    again, again_runs, again_launches = _invoke(
        tmp_path / "again", monkeypatch, words[3:], tool, vlnv
    )

    assert again_runs == first_runs
    assert again_launches == first_launches and len(first_launches) == 1
    assert rerun in again.report_text


_UNNAMED = ({"lite": []}, "")


def _rerun_line(report_text: str) -> str | None:
    """The rerun command the report prints, or ``None`` when it prints none."""
    lines = report_text.splitlines()
    header = "  Rerun a failed Test Run with the same seed:"
    return lines[lines.index(header) + 1].strip() if header in lines else None


@pytest.mark.parametrize(
    ("argv", "tool", "vlnv", "target", "test_run", "command"),
    [
        pytest.param(
            ["--seed", "9"],
            "verilator",
            "::lite:0",
            "lite",
            "<default>@9",
            "booley flow sim --target lite --seed 9",
            id="seeded",
        ),
        pytest.param(
            [],
            "verilator",
            "::lite:0",
            "lite",
            "<default>@1",
            "booley flow sim --target lite --seed 1",
            id="default-seed",
        ),
        pytest.param(
            [],
            "icarus",
            "::lite:0",
            "lite",
            "<default>",
            "booley flow sim --target lite",
            id="unseeded",
        ),
        pytest.param(
            ["--seed", "9"],
            "verilator",
            "acme:lib:lite:1",
            "acme:lib:lite:1#lite",
            "<default>@9",
            "booley flow sim --target 'lite#lite' --seed 9",
            id="vlnv-qualified",
        ),
    ],
)
def test_executing_the_rerun_command_of_the_unnamed_invocation_reproduces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv, tool, vlnv, target, test_run, command
):
    """A Target without registered Tests fails as its unnamed invocation, never as ``--test``."""
    first, first_runs, first_launches = _invoke(
        tmp_path / "first", monkeypatch, ["--target", target, *argv], tool, vlnv, registry=_UNNAMED
    )
    assert first_runs == [test_run]
    rerun = _rerun_line(first.report_text)
    assert rerun == command

    words = shlex.split(rerun)
    assert words[:3] == ["booley", "flow", "sim"]
    again, again_runs, again_launches = _invoke(
        tmp_path / "again", monkeypatch, words[3:], tool, vlnv, registry=_UNNAMED
    )

    assert again_runs == first_runs
    assert again_launches == first_launches and len(first_launches) == 1
    assert rerun in again.report_text


_FAILING_PRE_SIM = {".booley/booley.toml": '[flows.sim]\npre_run_commands = ["exit 3"]\n'}


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
@pytest.mark.parametrize("stage", ["pre-sim", "build"])
def test_an_unnamed_invocation_that_fails_before_the_simulator_is_named_by_its_test_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, access: str
):
    """Pre-sim and build failures bind to the unnamed run in both build access modes."""
    sim_table = f"pre_sim_build_access = '{access}'\n"
    config = '[flows.sim]\nrun_cwd = "."\n' + sim_table
    if stage == "pre-sim":
        config += 'pre_run_commands = ["exit 3"]\n'
    outcome, runs, launches = _invoke(
        tmp_path / "run",
        monkeypatch,
        ["--target", "lite", "--seed", "9"],
        "verilator",
        "::lite:0",
        registry=_UNNAMED,
        extra_files={".booley/booley.toml": config},
        build=SubprocessResult(returncode=1, stdout="build broke\n") if stage == "build" else None,
        exit_code=2 if stage == "build" else EXIT_FAILURE,
    )
    assert runs == ["<default>@9"]
    assert launches == []
    assert _rerun_line(outcome.report_text) in (None, "booley flow sim --target lite --seed 9")
