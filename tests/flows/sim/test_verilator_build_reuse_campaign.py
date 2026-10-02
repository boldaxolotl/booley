"""Verilator build reuse through the real ``booley flow sim`` path.

``SimulateFlow.execute`` runs every ordinary Simulation as a Simulation
Campaign: ``OrdinaryHdlSerialExecutor`` drives
``SimulationExecution.ordinary_group`` and ``PreparedOrdinaryGroup.compile``,
not ``SimulationExecution.run``. These tests pin build reuse on that path, so a
second identical ``sim`` call is served by the retained image and its durable
``build-execution.json`` records why.

The fake Verilator toolchain is shared with ``test_verilator_build_reuse_engine``;
the production ``verilator_identity`` and read-closure checks run unpatched.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.flows.sim import build_reuse
from booley.flows.sim.build_session import simulation_build_slot
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from tests.flows.sim.test_verilator_build_reuse_engine import (
    _edit_keeping_mtime,
    _select,
    _write_core,
    _write_fake_verilator,
    _write_project,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src"


@pytest.fixture
def flow_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake-Verilator Project that ``SimulateFlow`` can run in-Sandbox."""
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    verilator_bin = _write_fake_verilator(tmp_path)
    path = os.pathsep.join((str(verilator_bin), str(tmp_path / "toolbin"), os.environ["PATH"]))
    monkeypatch.setenv("PATH", path)
    for name in ("VERILATOR_ROOT", "MAKEFLAGS", "MFLAGS", "EDALIZE_LAUNCHER"):
        monkeypatch.delenv(name, raising=False)
    # The adapter runs as ``python -m``; it must import this checkout's source.
    python_path = os.pathsep.join(
        part for part in (str(_SRC_ROOT), os.environ.get("PYTHONPATH", "")) if part
    )
    monkeypatch.setenv("PYTHONPATH", python_path)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    # The fake installation is written moments before the first build; the
    # production race guard would (correctly) refuse to trust its files.
    monkeypatch.setattr(build_reuse, "RACE_MARGIN_NS", 0)
    project = _write_project(tmp_path)
    (project / ".booley_project" / "tests.toml").write_text(
        '[sim]\ntests = ["first", "second", "third"]\nselect = "+test={name}"\n',
        encoding="utf-8",
    )
    return project


def _flow_sim(project: Path, report_root: Path, test: str) -> dict[str, object]:
    """Run ``booley flow sim --target sim --test <test>``; return its build record.

    Shared-variant builds (immutable Pre-Sim access) and private per-test builds
    (``legacy-per-test``) both record ``evidence/build-execution.json``.
    """
    before = _report_runs(report_root)
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=project,
            report_dir=report_root,
            test=(test,),
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    (run,) = _report_runs(report_root) - before
    (document,) = run.glob("targets/sim/campaign/**/evidence/build-execution.json")
    return json.loads(document.read_text(encoding="utf-8"))["build"]


def _report_runs(report_root: Path) -> set[Path]:
    """Numbered per-invocation report directories (lock files excluded)."""
    return {path for path in report_root.glob("sim/*") if path.is_dir()}


def _generations(project: Path) -> list[Path]:
    return sorted((simulation_build_slot(_select(project)) / "g").iterdir())


def _current_generation(project: Path) -> str:
    slot = simulation_build_slot(_select(project))
    return str(json.loads((slot / "current.json").read_text(encoding="utf-8"))["generation"])


def _assert_built(build: dict[str, object], decision: str) -> None:
    assert build["ran"] is True
    assert str(build["cache_decision"]).startswith(f"{decision};"), build["cache_decision"]


def _assert_hit(build: dict[str, object]) -> None:
    assert build["ran"] is False
    assert str(build["cache_decision"]).startswith("hit;"), build["cache_decision"]


def _configure(project: Path, *lines: str) -> None:
    (project / ".booley_project" / "booley.toml").write_text(
        "[flows.sim]\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )


@pytest.mark.parametrize("second_test", ["first", "second"], ids=["same-test", "other-test"])
def test_second_identical_flow_sim_reuses_retained_image(
    flow_project: Path, tmp_path: Path, second_test: str
) -> None:
    reports = tmp_path / "reports"
    _assert_built(_flow_sim(flow_project, reports, "first"), "missing provenance")
    _assert_hit(_flow_sim(flow_project, reports, second_test))
    # The unused candidate generation of the hit is discarded.
    assert len(_generations(flow_project)) == 1


def _edit_rtl(project: Path) -> None:
    _edit_keeping_mtime(
        project / "rtl" / "tb.sv", '`include "defs.svh"\nmodule tb; // NEW\nendmodule\n'
    )


def _edit_include(project: Path) -> None:
    _edit_keeping_mtime(project / "inc" / "defs.svh", "// DEFS v2\n")


def _edit_flag(project: Path) -> None:
    _write_core(project, options="--timing, -DEXTRA_FLAG")


@pytest.mark.parametrize(
    "edit", [_edit_rtl, _edit_include, _edit_flag], ids=["rtl", "include", "flag"]
)
def test_flow_sim_input_edit_misses_then_hits(
    flow_project: Path, tmp_path: Path, edit: Callable[[Path], None]
) -> None:
    reports = tmp_path / "reports"
    _flow_sim(flow_project, reports, "first")
    edit(flow_project)
    _assert_built(_flow_sim(flow_project, reports, "first"), "changed source, recipe, or tool")
    _assert_hit(_flow_sim(flow_project, reports, "first"))


_RUN_CWD_FIRMWARE = 'pre_run_commands = ["printf fw > \\"$BOOLEY_RUN_CWD/firmware.hex\\""]'


@pytest.mark.parametrize(
    "access", [(), ('pre_sim_build_access = "legacy-per-test"',)], ids=["immutable", "legacy"]
)
def test_flow_sim_pre_sim_firmware_in_run_cwd_keeps_reuse(
    flow_project: Path, tmp_path: Path, access: tuple[str, ...]
) -> None:
    """A Pre-Sim firmware command writing only into the run directory still hits."""
    _configure(flow_project, *access, _RUN_CWD_FIRMWARE)
    reports = tmp_path / "reports"
    _assert_built(_flow_sim(flow_project, reports, "first"), "missing provenance")
    retained = _current_generation(flow_project)
    _assert_hit(_flow_sim(flow_project, reports, "second"))
    # The hit discarded its candidate; only the first, retained build exists.
    assert [path.name for path in _generations(flow_project)] == [retained]


@pytest.mark.parametrize(
    "effect",
    [
        'printf fw > \\"$BOOLEY_BUILD_ROOT/firmware.hex\\"',
        'printf fw > \\"$BOOLEY_BUILD_ROOT/.booley-adapter-x.json\\"',
        'chmod u+x \\"$BOOLEY_BUILD_ROOT\\"/*.vc',
        'mkdir \\"$BOOLEY_BUILD_ROOT/empty\\"',
    ],
    ids=["file", "reserved-name", "chmod-only", "empty-dir"],
)
def test_flow_sim_legacy_pre_sim_change_to_generation_forces_build(
    flow_project: Path, tmp_path: Path, effect: str
) -> None:
    """A per-test Pre-Sim Command that changes the candidate generation never hits.

    Only test ``second`` runs the effect, so a reusable image is already
    retained when it runs: the change alone must force the second build, and
    the image it built (with the hook's effect) is never retained for a hit.
    """
    _configure(
        flow_project,
        'pre_sim_build_access = "legacy-per-test"',
        f'pre_run_commands = ["[ \\"$BOOLEY_TEST_NAME\\" != second ] || {effect}"]',
    )
    reports = tmp_path / "reports"
    _assert_built(_flow_sim(flow_project, reports, "first"), "missing provenance")
    retained = _current_generation(flow_project)
    _assert_built(_flow_sim(flow_project, reports, "second"), "reuse unsupported")
    assert len(_generations(flow_project)) == 2
    assert _current_generation(flow_project) == retained
    _assert_hit(_flow_sim(flow_project, reports, "third"))
    assert len(_generations(flow_project)) == 2
