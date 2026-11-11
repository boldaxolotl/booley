"""Standalone-module coverage for Simulation's Elaboration Check mode."""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.sim import standalone as standalone_mod
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.standalone import _scan_hdl_declarations
from booley.runtime.booley_dir import reset_cache


@pytest.fixture(autouse=True)
def _reset_booley_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _make_flow(
    tmp_path: Path,
    *,
    config: str = "",
    criterion_met: bool = False,
    standalone: bool = True,
) -> SimulateFlow:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir(exist_ok=True)
    if config:
        (booley_dir / "booley.toml").write_text(config, encoding="utf-8")
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.init_criteria({"elaborate_standalone": True})
    state.criteria["elaborate_standalone"].met = criterion_met
    state.save()
    env = os.environ.copy()
    env.update(
        BOOLEY_DIR=str(booley_dir),
        BOOLEY_SLUG="standalone-test",
        BOOLEY_STATE_FILE=str(state_file),
    )
    flow = SimulateFlow()
    with patch.dict(os.environ, env):
        args = [
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
            "--target",
            "sim_dut",
            "--mode",
            "elab-only-standalone" if standalone else "elab-only",
        ]
        flow.parse_args(args)
    flow.read_state()
    return flow


def _write_sources(tmp_path: Path, files: dict[str, str]) -> None:
    for relative, contents in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


def _stub_sources(
    monkeypatch: pytest.MonkeyPatch,
    rtl: list[str],
    *,
    tb: list[str] | None = None,
    eda_tool: str | None = "icarus",
    targets: dict[str, tuple[Any, ...]] | None = None,
) -> None:
    """Stub Target resolution: ``targets`` maps name -> (rtl files, simulator[, options]).

    The optional third element is ``{"flow_options": {...}, "tool_options": {...}}``.

    Without ``targets`` a single ``sim_dut`` Target simulating with *eda_tool*
    covers *rtl*. The simulator is never a ``MagicMock``: the probe selection
    reads it as a string.
    """
    per_target = targets if targets is not None else {"sim_dut": (rtl, eda_tool)}
    monkeypatch.setattr(
        SimulateFlow, "_target_handle", lambda _self, name: SimpleNamespace(identity=name)
    )

    def inspect(handle: SimpleNamespace) -> SimpleNamespace:
        files, tool, *rest = per_target[handle.identity]
        options = rest[0] if rest else {}
        return SimpleNamespace(
            rtl_files=tuple(files),
            tb_files=tuple(tb or []),
            eda_tool=tool,
            flow_options=options.get("flow_options", {}),
            tool_options=options.get("tool_options", {}),
        )

    monkeypatch.setattr(
        standalone_mod.TargetCatalog,
        "build",
        classmethod(lambda _cls, _root: MagicMock(inspect=MagicMock(side_effect=inspect))),
    )


def _stub_which(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    """Make ``shutil.which("verilator")`` report Verilator present or absent."""
    monkeypatch.setattr(
        standalone_mod.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if present else None,
    )


def _stub_probes(
    monkeypatch: pytest.MonkeyPatch,
    flow: SimulateFlow,
    results: dict[str | tuple[str, str], SubprocessResult] | None = None,
) -> list[list[str]]:
    """Capture probe argv; results key by ``(module, compiler)`` or bare module."""
    captured: list[list[str]] = []
    by_key = results or {}

    def execute(command: list[str], *, timeout: int) -> SubprocessResult:
        assert timeout > 0
        captured.append(command)
        flag = "-s" if command[0] == "iverilog" else "--top-module"
        module = command[command.index(flag) + 1]
        result = by_key.get((module, command[0])) or by_key.get(module)
        return result or SubprocessResult(returncode=0)

    monkeypatch.setattr(flow, "_execute", execute)
    return captured


class TestDeclarationScan:
    def test_finds_modules_and_shared_declarations(self) -> None:
        text = (
            "package alu_pkg; endpackage\n"
            "interface bus_if; endinterface\n"
            "module alu; endmodule\n"
            "macromodule legacy_top; endmodule\n"
            "module automatic worker; endmodule\n"
        )

        modules, has_shared = _scan_hdl_declarations(text)

        assert modules == ["alu", "legacy_top", "worker"]
        assert has_shared is True

    def test_ignores_commented_declarations(self) -> None:
        modules, has_shared = _scan_hdl_declarations(
            "// module dead_line;\n"
            "/* module dead_block; endmodule */\n"
            "module live_one; endmodule\n"
        )

        assert modules == ["live_one"]
        assert has_shared is False


class TestStandaloneSweep:
    def test_declared_criterion_does_not_implicitly_enable_sweep(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path, standalone=False)

        assert flow.state.has_criterion("elaborate_standalone")
        assert flow._standalone_requested() is False

    def test_success_probes_each_declaring_file_and_records_pass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_sources(
            tmp_path,
            {
                "rtl/alu.sv": "module alu; endmodule\n",
                "rtl/top.sv": "module top; endmodule\n",
            },
        )
        _stub_sources(monkeypatch, ["rtl/alu.sv", "rtl/top.sv"])
        flow = _make_flow(tmp_path)
        flow._record_eda_tool("sim_dut", "icarus")
        commands = _stub_probes(monkeypatch, flow)

        with flow.publication_resources:
            outcome = flow._run_standalone_check(["sim_dut"])

        assert outcome.passed
        assert flow.state.criteria["elaborate_standalone"].met is True
        assert flow.state.criteria["elaborate_standalone"].detail["mode"] == (
            "elab_only_standalone"
        )
        assert outcome.detail["mode"] == "elab_only_standalone"
        assert outcome.detail["modules_checked"] == 2
        assert outcome.detail["frontend"] == "iverilog"
        assert outcome.detail["probes"] == [
            {
                "frontend": "iverilog",
                "dialect_flags": [],
                "fallback": False,
                "modules": [
                    {"module": "alu", "file": "rtl/alu.sv"},
                    {"module": "top", "file": "rtl/top.sv"},
                ],
            }
        ]
        by_module = {command[command.index("-s") + 1]: command for command in commands}
        assert [arg for arg in by_module["alu"] if arg.endswith(".sv")] == ["rtl/alu.sv"]
        assert [arg for arg in by_module["top"] if arg.endswith(".sv")] == ["rtl/top.sv"]
        assert all(not arg.startswith("-P") for arg in by_module["alu"])

    def test_complete_sweep_shares_one_deadline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.flows.sim import standalone as standalone_module

        flow = _make_flow(tmp_path)
        monkeypatch.setattr(flow, "_effective_timeout_ms", lambda: 2_500)
        monkeypatch.setattr(
            flow,
            "_effective_build_timeout_ms",
            MagicMock(side_effect=AssertionError("standalone sweep must use run timeout")),
        )
        monotonic_values = iter((0.0, 0.0, 1.2))
        monkeypatch.setattr(standalone_module, "monotonic", lambda: next(monotonic_values))
        observed_timeouts: list[int] = []

        def execute(_command: list[str], *, timeout: int) -> SubprocessResult:
            observed_timeouts.append(timeout)
            return SubprocessResult(returncode=0)

        monkeypatch.setattr(flow, "_execute", execute)

        failures, unparsed, _logs, error = flow._run_standalone_probes(
            (
                standalone_mod._StandaloneProbe("one", "one.sv", "iverilog", (), False, ()),
                standalone_mod._StandaloneProbe("two", "two.sv", "iverilog", (), False, ()),
            ),
            primary_ok=True,
        )

        assert not failures and not unparsed and not error
        assert observed_timeouts == [3, 2]

    def test_complete_sweep_stops_when_shared_deadline_expires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = _make_flow(tmp_path)
        monkeypatch.setattr(flow, "_effective_timeout_ms", lambda: 1_000)
        monotonic_values = iter((0.0, 1.0))
        monkeypatch.setattr(
            standalone_mod,
            "monotonic",
            lambda: next(monotonic_values),
        )
        probe = MagicMock(side_effect=AssertionError("expired sweep must not start a probe"))
        monkeypatch.setattr(flow, "_run_standalone_probe", probe)

        failures, unparsed, logs, error = flow._run_standalone_probes(
            (standalone_mod._StandaloneProbe("late", "late.sv", "iverilog", (), False, ()),),
            primary_ok=True,
        )

        assert not failures and not unparsed and not logs
        assert error == "iverilog standalone sweep timed out after 1s"
        probe.assert_not_called()

    def test_prepare_reuses_frozen_standalone_recipe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = _make_flow(tmp_path)
        recipe = standalone_mod._StandalonePlanRecipe(
            probes=(
                standalone_mod._StandaloneProbe(
                    "top", "rtl/top.sv", "iverilog", (), False, ("rtl/pkg.sv",)
                ),
            ),
            shared=("rtl/pkg.sv",),
            commands=(("iverilog", "rtl/top.sv"),),
        )
        flow._standalone_plan_recipe = recipe
        monkeypatch.setattr(flow, "_open_run_log", MagicMock())

        prepared = flow._prepare_standalone_check(["sim_dut"])

        assert prepared is recipe
        assert recipe.modules == (("top", "rtl/top.sv"),)
        assert recipe.frontend_label == "iverilog"

    def test_plan_reports_source_scope_resolution_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = _make_flow(tmp_path)
        monkeypatch.setattr(
            flow,
            "_standalone_rtl_scope",
            MagicMock(side_effect=OSError("catalog unavailable")),
        )

        with pytest.raises(ValueError, match="could not resolve RTL source scope"):
            flow._plan_standalone_check(["sim_dut"])

    def test_design_failure_names_module_and_records_fail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_sources(tmp_path, {"rtl/top.sv": "module top; endmodule\n"})
        _stub_sources(monkeypatch, ["rtl/top.sv"])
        flow = _make_flow(tmp_path, criterion_met=True)
        flow._record_eda_tool("sim_dut", "icarus")
        _stub_probes(
            monkeypatch,
            flow,
            {
                "top": SubprocessResult(
                    returncode=2,
                    stderr="rtl/top.sv:2: error: Unknown module type: missing_sub\n",
                )
            },
        )

        with flow.publication_resources:
            outcome = flow._run_standalone_check(["sim_dut"])

        assert not outcome.passed
        assert not outcome.eda_tool_failed
        assert flow.state.criteria["elaborate_standalone"].met is False
        assert outcome.detail["failures"][0]["module"] == "top"
        assert "top (rtl/top.sv) via iverilog" in "\n".join(outcome.lines)
        assert outcome.detail["failures"][0]["frontend"] == "iverilog"
        assert outcome.detail["failures"][0]["fallback"] is False
        log = tmp_path / outcome.detail["log"]
        assert "$ iverilog" in log.read_text(encoding="utf-8")

    def test_shared_files_precede_declaring_file_and_tb_is_excluded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_sources(
            tmp_path,
            {
                "rtl/alu_pkg.sv": "package alu_pkg; endpackage\n",
                "rtl/bus_if.sv": "interface bus_if; endinterface\n",
                "rtl/alu.sv": "import alu_pkg::*;\nmodule alu; endmodule\n",
                "tb/tb_alu.sv": "module tb_alu; endmodule\n",
            },
        )
        _stub_sources(
            monkeypatch,
            ["rtl/alu_pkg.sv", "rtl/bus_if.sv", "rtl/alu.sv"],
            tb=["tb/tb_alu.sv"],
        )
        flow = _make_flow(tmp_path)
        flow._record_eda_tool("sim_dut", "icarus")
        commands = _stub_probes(monkeypatch, flow)

        with flow.publication_resources:
            outcome = flow._run_standalone_check(["sim_dut"])

        assert outcome.passed
        assert len(commands) == 1
        sources = [arg for arg in commands[0] if arg.endswith(".sv")]
        assert sources[-1] == "rtl/alu.sv"
        assert set(sources[:-1]) == {"rtl/alu_pkg.sv", "rtl/bus_if.sv"}
        assert "tb/tb_alu.sv" not in sources

    @pytest.mark.parametrize(
        ("sources", "probe", "message"),
        [
            ([], None, "vacuous"),
            (
                ["rtl/alu.sv"],
                SubprocessResult(returncode=-1, stderr="spawn failed"),
                "could not run",
            ),
        ],
    )
    def test_infrastructure_error_preserves_prior_criterion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sources: list[str],
        probe: SubprocessResult | None,
        message: str,
    ) -> None:
        if sources:
            _write_sources(tmp_path, {sources[0]: "module alu; endmodule\n"})
        _stub_sources(monkeypatch, sources)
        flow = _make_flow(tmp_path, criterion_met=True)
        flow._record_eda_tool("sim_dut", "icarus")
        _stub_probes(monkeypatch, flow, {"alu": probe} if probe else None)

        with flow.publication_resources:
            outcome = flow._run_standalone_check(["sim_dut"])

        assert outcome.eda_tool_failed
        assert message in "\n".join(outcome.lines)
        assert flow.state.criteria["elaborate_standalone"].met is True

    def test_parse_gap_is_no_verdict_and_preserves_prior_criterion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
        _stub_sources(monkeypatch, ["rtl/alu.sv"], eda_tool="xcelium")
        _stub_which(monkeypatch, True)
        flow = _make_flow(tmp_path, criterion_met=True)
        flow._record_eda_tool("sim_dut", "xcelium")
        _stub_probes(
            monkeypatch,
            flow,
            {"alu": SubprocessResult(returncode=1, stderr="rtl/alu.sv:1: syntax error\n")},
        )

        with flow.publication_resources:
            outcome = flow._run_standalone_check(["sim_dut"])

        assert outcome.eda_tool_failed
        assert outcome.detail["unparsed"][0]["module"] == "alu"
        assert outcome.detail["unparsed"][0]["fallback"] is True
        assert "not a design defect" in "\n".join(outcome.lines)
        assert flow.state.criteria["elaborate_standalone"].met is True

    def test_real_failure_and_parse_gap_are_both_retained(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_sources(
            tmp_path,
            {
                "rtl/gapped.sv": "module gapped; endmodule\n",
                "rtl/broken.sv": "module broken; endmodule\n",
            },
        )
        _stub_sources(monkeypatch, ["rtl/gapped.sv", "rtl/broken.sv"], eda_tool="xcelium")
        _stub_which(monkeypatch, True)
        flow = _make_flow(tmp_path, criterion_met=True)
        flow._record_eda_tool("sim_dut", "xcelium")
        _stub_probes(
            monkeypatch,
            flow,
            {
                "gapped": SubprocessResult(
                    returncode=1,
                    stderr="rtl/gapped.sv:1: syntax error\n",
                ),
                "broken": SubprocessResult(
                    returncode=1,
                    stderr="rtl/broken.sv:1: error: Unknown module type: missing_sub\n",
                ),
            },
        )

        with flow.publication_resources:
            outcome = flow._run_standalone_check(["sim_dut"])

        assert [item["module"] for item in outcome.detail["failures"]] == ["broken"]
        assert [item["module"] for item in outcome.detail["unparsed"]] == ["gapped"]
        assert flow.state.criteria["elaborate_standalone"].met is False
        assert "ungraded" in "\n".join(outcome.lines)


class TestStandaloneFrontend:
    def test_probe_command_shapes(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path)

        verilator = flow._standalone_compile_command(
            "alu",
            "rtl/alu.sv",
            ["rtl/pkg.sv"],
            "verilator",
            (),
        )
        iverilog = flow._standalone_compile_command(
            "alu",
            "rtl/alu.sv",
            ["rtl/pkg.sv"],
            "iverilog",
            ("-g2005",),
        )

        assert verilator == [
            "verilator",
            "--lint-only",
            "-Wno-fatal",
            "--top-module",
            "alu",
            "rtl/pkg.sv",
            "rtl/alu.sv",
        ]
        assert iverilog == [
            "iverilog",
            "-g2005",
            "-o",
            os.devnull,
            "-s",
            "alu",
            "rtl/pkg.sv",
            "rtl/alu.sv",
        ]


def _run_sweep(flow: SimulateFlow, targets: list[str], *, primary_ok: bool = True):
    """Run the sweep over *targets* and return its outcome."""
    with flow.publication_resources:
        return flow._run_standalone_check(targets, primary_ok=primary_ok)


_SYNTAX = SubprocessResult(returncode=1, stderr="rtl/alu.sv:1: syntax error\n")
_UNKNOWN = SubprocessResult(
    returncode=1, stderr="rtl/alu.sv:1: error: Unknown module type: missing_sub\n"
)


class TestProbeFrontendSelection:
    """ADR 0089: each module is probed with the simulator of the Targets using it."""

    @pytest.mark.parametrize(
        ("eda_tool", "compiler", "verilator_on_path"),
        [
            pytest.param("icarus", "iverilog", True, id="icarus-verilator_on_path"),
            pytest.param(
                "verilator", "verilator", False, id="verilator-verilator_absent_from_path"
            ),
        ],
    )
    def test_target_simulator_selects_probe_frontend(
        self, tmp_path, monkeypatch, eda_tool, compiler, verilator_on_path
    ) -> None:
        _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
        _stub_sources(monkeypatch, ["rtl/alu.sv"], eda_tool=eda_tool)
        _stub_which(monkeypatch, verilator_on_path)
        flow = _make_flow(tmp_path)
        commands = _stub_probes(monkeypatch, flow)

        outcome = _run_sweep(flow, ["sim_dut"])

        assert outcome.passed
        assert [command[0] for command in commands] == [compiler]

    @staticmethod
    def _two_simulator_project(tmp_path, monkeypatch, *, shared_packages=False):
        files = {"rtl/alu.sv": "module alu; endmodule\n"}
        ice_rtl, ver_rtl = ["rtl/alu.sv"], ["rtl/alu.sv"]
        if shared_packages:
            files["rtl/pkg_i.sv"] = "package pkg_i; endpackage\n"
            files["rtl/pkg_v.sv"] = "package pkg_v; endpackage\n"
            ice_rtl.insert(0, "rtl/pkg_i.sv")
            ver_rtl.insert(0, "rtl/pkg_v.sv")
        _write_sources(tmp_path, files)
        _stub_sources(
            monkeypatch,
            [],
            targets={"ice": (ice_rtl, "icarus"), "ver": (ver_rtl, "verilator")},
        )
        _stub_which(monkeypatch, True)
        return _make_flow(tmp_path)

    def test_module_shared_by_icarus_and_verilator_targets_is_probed_by_both(
        self, tmp_path, monkeypatch
    ) -> None:
        flow = self._two_simulator_project(tmp_path, monkeypatch)
        commands = _stub_probes(monkeypatch, flow)

        outcome = _run_sweep(flow, ["ice", "ver"])

        assert outcome.passed
        assert sorted(command[0] for command in commands) == ["iverilog", "verilator"]
        assert outcome.detail["modules_checked"] == 1
        assert outcome.detail["frontend"] == "iverilog+verilator"
        assert "iverilog+verilator" in outcome.lines[0]
        # A mixed sweep invents no run-level tool.
        assert flow._eda_tool is None

    @pytest.mark.parametrize("failing", ["iverilog", "verilator"])
    def test_failure_under_either_frontend_fails_and_names_it(
        self, tmp_path, monkeypatch, failing
    ) -> None:
        flow = self._two_simulator_project(tmp_path, monkeypatch)
        _stub_probes(monkeypatch, flow, {("alu", failing): _UNKNOWN})

        outcome = _run_sweep(flow, ["ice", "ver"])

        assert not outcome.passed
        assert not outcome.eda_tool_failed
        assert flow.state.criteria["elaborate_standalone"].met is False
        assert [f["frontend"] for f in outcome.detail["failures"]] == [failing]
        assert f"via {failing}" in "\n".join(outcome.lines)

    def test_module_failing_under_both_frontends_is_counted_once(
        self, tmp_path, monkeypatch
    ) -> None:
        flow = self._two_simulator_project(tmp_path, monkeypatch)
        _stub_probes(monkeypatch, flow, {"alu": _UNKNOWN})

        outcome = _run_sweep(flow, ["ice", "ver"])

        assert not outcome.passed
        assert len(outcome.detail["failures"]) == 2
        assert "standalone: 1/1 modules FAIL" in outcome.display

    def test_probe_only_includes_shared_files_of_targets_using_its_frontend(
        self, tmp_path, monkeypatch
    ) -> None:
        flow = self._two_simulator_project(tmp_path, monkeypatch, shared_packages=True)
        commands = _stub_probes(monkeypatch, flow)

        outcome = _run_sweep(flow, ["ice", "ver"])

        assert outcome.passed
        by_compiler = {command[0]: command for command in commands}
        assert "rtl/pkg_i.sv" in by_compiler["iverilog"]
        assert "rtl/pkg_v.sv" not in by_compiler["iverilog"]
        assert "rtl/pkg_v.sv" in by_compiler["verilator"]
        assert "rtl/pkg_i.sv" not in by_compiler["verilator"]
        # The detail's shared list stays the union.
        assert outcome.detail["shared_files"] == ["rtl/pkg_i.sv", "rtl/pkg_v.sv"]

    @pytest.mark.parametrize(
        ("eda_tool", "verilator_on_path"),
        [
            pytest.param("icarus", True, id="icarus-verilator_on_path"),
            pytest.param("verilator", False, id="verilator-verilator_absent_from_path"),
        ],
    )
    def test_single_simulator_parse_error_is_a_finding(
        self, tmp_path, monkeypatch, eda_tool, verilator_on_path
    ) -> None:
        _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
        _stub_sources(monkeypatch, ["rtl/alu.sv"], eda_tool=eda_tool)
        _stub_which(monkeypatch, verilator_on_path)
        flow = _make_flow(tmp_path)
        _stub_probes(monkeypatch, flow, {"alu": _SYNTAX})

        outcome = _run_sweep(flow, ["sim_dut"])

        assert not outcome.eda_tool_failed
        assert [f["module"] for f in outcome.detail["failures"]] == ["alu"]
        assert "unparsed" not in outcome.detail

    @pytest.mark.parametrize(
        ("verilator_on_path", "primary_ok", "compiler"),
        [
            pytest.param(True, True, "verilator", id="verilator_on_path"),
            pytest.param(False, True, "iverilog", id="verilator_absent_from_path"),
            pytest.param(True, False, "verilator", id="primary_failed"),
        ],
    )
    def test_commercial_only_module_uses_fallback_and_grades_parse_gap(
        self, tmp_path, monkeypatch, verilator_on_path, primary_ok, compiler
    ) -> None:
        _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
        _stub_sources(monkeypatch, ["rtl/alu.sv"], eda_tool="xcelium")
        _stub_which(monkeypatch, verilator_on_path)
        flow = _make_flow(tmp_path, criterion_met=True)
        commands = _stub_probes(monkeypatch, flow, {"alu": _SYNTAX})

        outcome = _run_sweep(flow, ["sim_dut"], primary_ok=primary_ok)

        assert [command[0] for command in commands] == [compiler]
        # A fallback probe keeps its own defaults, never a Target's flags.
        assert commands[0][1] == ("-g2012" if compiler == "iverilog" else "--lint-only")
        if primary_ok:
            assert outcome.eda_tool_failed
            assert outcome.detail["unparsed"][0]["fallback"] is True
            assert flow.state.criteria["elaborate_standalone"].met is True
        else:
            # The Target build itself failed: a syntax error is a finding.
            assert not outcome.eda_tool_failed
            assert outcome.detail["failures"][0]["fallback"] is True

    def test_fallback_matching_a_native_frontend_runs_once_and_is_graded(
        self, tmp_path, monkeypatch
    ) -> None:
        _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
        _stub_sources(
            monkeypatch,
            [],
            targets={"ver": (["rtl/alu.sv"], "verilator"), "xcel": (["rtl/alu.sv"], "xcelium")},
        )
        _stub_which(monkeypatch, True)
        flow = _make_flow(tmp_path)
        commands = _stub_probes(monkeypatch, flow, {"alu": _SYNTAX})

        outcome = _run_sweep(flow, ["ver", "xcel"])

        assert [command[0] for command in commands] == ["verilator"]
        assert not outcome.eda_tool_failed
        assert outcome.detail["failures"][0]["fallback"] is False

    def test_dry_run_plan_shows_per_module_frontend(self, tmp_path, monkeypatch) -> None:
        flow = self._two_simulator_project(tmp_path, monkeypatch, shared_packages=True)

        unit, error = flow._planned_standalone_unit(["ice", "ver"])

        assert error is None
        assert unit is not None
        assert unit.eda_tool == "iverilog+verilator"
        assert unit.recipe["frontends"] == ["iverilog", "verilator"]
        sources = unit.recipe["module_sources"]
        assert {(entry["module"], entry["frontend"], entry["fallback"]) for entry in sources} == {
            ("alu", "iverilog", False),
            ("alu", "verilator", False),
        }
        assert len(unit.commands) == len(sources) == 2
        assert {command.argv[0] for command in unit.commands} == {"iverilog", "verilator"}

    def test_run_log_records_probe_frontend_and_fallback(self, tmp_path, monkeypatch) -> None:
        _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
        _stub_sources(monkeypatch, ["rtl/alu.sv"], eda_tool="xcelium")
        _stub_which(monkeypatch, True)
        flow = _make_flow(tmp_path)
        _stub_probes(monkeypatch, flow)

        outcome = _run_sweep(flow, ["sim_dut"])

        log = (tmp_path / outcome.detail["log"]).read_text(encoding="utf-8")
        assert (
            "# standalone probe: alu (rtl/alu.sv) via verilator (default dialect) (fallback)"
            in log
        )
        assert outcome.detail["probes"] == [
            {
                "frontend": "verilator",
                "dialect_flags": [],
                "fallback": True,
                "modules": [{"module": "alu", "file": "rtl/alu.sv"}],
            }
        ]


# SystemVerilog keywords used as plain Verilog-2005 identifiers: valid under
# the Verilog-2005 generation, a syntax error under SystemVerilog.
_KEYWORD_IDENTIFIER_RTL = (
    "module kw(input wire [7:0] bit, output wire [7:0] byte);\n"
    "  wire [7:0] int = bit;\n"
    "  assign byte = int;\n"
    "endmodule\n"
)


def _stub_dialect_compilers(
    monkeypatch: pytest.MonkeyPatch, flow: SimulateFlow
) -> list[list[str]]:
    """Emulate Icarus/Verilator on keyword-identifier RTL; capture each argv.

    Measured behavior: the RTL is a syntax error unless the command selects the
    Verilog-2005 generation (Icarus: no ``-g2012``; Verilator: an explicit
    ``--default-language 1364-2005``).
    """
    captured: list[list[str]] = []

    def execute(command: list[str], *, timeout: int) -> SubprocessResult:
        captured.append(command)
        if command[0] == "iverilog":
            sv_only = "-g2012" in command
        else:
            sv_only = not any(
                first == "--default-language" and second == "1364-2005"
                for first, second in itertools.pairwise(command)
            )
        if sv_only:
            return SubprocessResult(returncode=1, stderr="rtl/kw.v:1: syntax error\n")
        return SubprocessResult(returncode=0)

    monkeypatch.setattr(flow, "_execute", execute)
    return captured


def _dialect_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: dict[str, tuple[Any, ...]],
    extra_files: dict[str, str] | None = None,
) -> SimulateFlow:
    """A project whose ``rtl/kw.v`` keyword-identifier module is shared by *targets*."""
    _write_sources(tmp_path, {"rtl/kw.v": _KEYWORD_IDENTIFIER_RTL, **(extra_files or {})})
    _stub_sources(monkeypatch, [], targets=targets)
    _stub_which(monkeypatch, True)
    return _make_flow(tmp_path)


class TestDialectFlagExtraction:
    """Which authored compiler options count as language-dialect flags."""

    @pytest.mark.parametrize(
        ("frontend", "options", "expected"),
        [
            pytest.param("iverilog", ["-g2005", "-Wall", "-DX=1"], ("-g2005",), id="icarus"),
            pytest.param("iverilog", ["-g", "2012"], ("-g", "2012"), id="icarus_bare_g"),
            pytest.param("iverilog", ["-g2005-sv -g2012"], ("-g2005-sv", "-g2012"), id="order"),
            pytest.param("iverilog", ["-g'unterminated"], ("-g'unterminated",), id="no_shlex"),
            pytest.param("iverilog", "-g2005", (), id="not_a_list"),
            pytest.param("verilator", ["-DX", "+libext+.v", "+incdir+i", "-Wall"], (), id="none"),
            pytest.param(
                "verilator",
                ["-language 1800-2012", "+systemverilogext+sv", "-y", "lib"],
                ("-language", "1800-2012", "+systemverilogext+sv"),
                id="verilator_single_dash",
            ),
            pytest.param("verilator", ["--language"], ("--language",), id="dangling_value"),
            pytest.param("verilator", ["-g2012"], (), id="verilator_ignores_icarus_flag"),
            pytest.param(
                "iverilog", ["--language", "1364-2005"], (), id="icarus_ignores_verilator"
            ),
        ],
    )
    def test_only_dialect_flags_are_kept(self, frontend, options, expected) -> None:
        assert standalone_mod._dialect_flags(frontend, options) == expected

    def test_modern_options_precede_legacy_options(self) -> None:
        inspection = SimpleNamespace(
            eda_tool="icarus",
            flow_options={"iverilog_options": ["-g2005"]},
            tool_options={"iverilog_options": ["-g2012"], "other": 1},
        )

        assert standalone_mod._target_dialect(inspection) == standalone_mod._ProbeDialect(
            "iverilog", ("-g2005", "-g2012")
        )

    def test_undrivable_simulator_has_no_dialect(self) -> None:
        inspection = SimpleNamespace(eda_tool="xcelium", flow_options={}, tool_options={})

        assert standalone_mod._target_dialect(inspection) is None


class TestProbeDialectFlags:
    """Issue #3392: the probe uses each Target's own language-dialect flags."""

    @pytest.mark.parametrize(
        ("options", "argv_prefix", "passes"),
        [
            pytest.param(
                {"flow_options": {"iverilog_options": ["-g2005", "-Wall"]}},
                ["iverilog", "-g2005"],
                True,
                id="verilog2005",
            ),
            pytest.param(
                {"flow_options": {"iverilog_options": ["-Wall"]}},
                ["iverilog"],
                True,
                id="no_generation_flag",
            ),
            pytest.param(
                {"tool_options": {"iverilog_options": ["-g2005"]}},
                ["iverilog", "-g2005"],
                True,
                id="legacy_tool_options",
            ),
            pytest.param(
                {"flow_options": {"iverilog_options": ["-g2012"]}},
                ["iverilog", "-g2012"],
                False,
                id="systemverilog",
            ),
        ],
    )
    def test_icarus_probe_uses_target_generation_flags(
        self, tmp_path, monkeypatch, options, argv_prefix, passes
    ) -> None:
        flow = _dialect_project(tmp_path, monkeypatch, {"ice": (["rtl/kw.v"], "icarus", options)})
        commands = _stub_dialect_compilers(monkeypatch, flow)

        outcome = _run_sweep(flow, ["ice"])

        assert outcome.passed is passes
        assert commands == [[*argv_prefix, "-o", os.devnull, "-s", "kw", "rtl/kw.v"]]
        if not passes:
            assert outcome.detail["failures"][0]["dialect_flags"] == ["-g2012"]

    @pytest.mark.parametrize(
        "verilator_options",
        [
            pytest.param(
                ["--default-language 1364-2005", "+1364-2005ext+v", "--timing"],
                id="one_entry",
            ),
            pytest.param(
                ["--default-language", "1364-2005", "+1364-2005ext+v", "--timing"],
                id="split_entries",
            ),
        ],
    )
    def test_verilator_probe_honors_target_language_flags(
        self, tmp_path, monkeypatch, verilator_options
    ) -> None:
        options = {"flow_options": {"verilator_options": verilator_options}}
        flow = _dialect_project(
            tmp_path, monkeypatch, {"ver": (["rtl/kw.v"], "verilator", options)}
        )
        commands = _stub_dialect_compilers(monkeypatch, flow)

        outcome = _run_sweep(flow, ["ver"])

        assert outcome.passed
        assert commands == [
            [
                "verilator",
                "--lint-only",
                "-Wno-fatal",
                "--default-language",
                "1364-2005",
                "+1364-2005ext+v",
                "--top-module",
                "kw",
                "rtl/kw.v",
            ]
        ]

    def test_target_defines_and_include_paths_never_reach_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """Guard, not the red test: defines/includes are not dialect flags."""
        icarus = {
            "flow_options": {
                "iverilog_options": ["-g2012", "-DFOO=1", "-I", "inc", "-Iinc2", "-y", "lib"]
            }
        }
        verilator = {
            "flow_options": {
                "verilator_options": [
                    "-DFOO",
                    "+define+BAR=1",
                    "+incdir+inc",
                    "-Iinc",
                    "+libext+.v",
                ]
            }
        }
        flow = _dialect_project(
            tmp_path,
            monkeypatch,
            {
                "ice": (["rtl/kw.v"], "icarus", icarus),
                "ver": (["rtl/kw.v"], "verilator", verilator),
            },
        )
        commands = _stub_dialect_compilers(monkeypatch, flow)

        _run_sweep(flow, ["ice", "ver"])

        assert sorted(commands) == [
            ["iverilog", "-g2012", "-o", os.devnull, "-s", "kw", "rtl/kw.v"],
            ["verilator", "--lint-only", "-Wno-fatal", "--top-module", "kw", "rtl/kw.v"],
        ]

    def test_module_shared_by_different_dialects_probes_each_set(
        self, tmp_path, monkeypatch
    ) -> None:
        def icarus(*flags: str) -> dict[str, Any]:
            return {"flow_options": {"iverilog_options": list(flags)}}

        flow = _dialect_project(
            tmp_path,
            monkeypatch,
            {
                "v2005": (["rtl/kw.v"], "icarus", icarus("-g2005")),
                "sv_a": (["rtl/pkg.sv", "rtl/kw.v"], "icarus", icarus("-g2012")),
                "sv_b": (["rtl/kw.v"], "icarus", icarus("-g2012")),
            },
            extra_files={"rtl/pkg.sv": "package pkg; endpackage\n"},
        )
        commands = _stub_dialect_compilers(monkeypatch, flow)

        outcome = _run_sweep(flow, ["v2005", "sv_a", "sv_b"])

        assert sorted(commands) == [
            ["iverilog", "-g2005", "-o", os.devnull, "-s", "kw", "rtl/kw.v"],
            ["iverilog", "-g2012", "-o", os.devnull, "-s", "kw", "rtl/pkg.sv", "rtl/kw.v"],
        ]
        assert not outcome.passed
        assert "standalone: 1/1 modules FAIL" in outcome.display
        assert [(p["frontend"], p["dialect_flags"]) for p in outcome.detail["probes"]] == [
            ("iverilog", ["-g2005"]),
            ("iverilog", ["-g2012"]),
        ]

    def test_dialect_flags_named_in_plan_and_report(self, tmp_path, monkeypatch) -> None:
        options = {"flow_options": {"iverilog_options": ["-g2012"]}}
        flow = _dialect_project(
            tmp_path,
            monkeypatch,
            {
                "sv": (["rtl/kw.v"], "icarus", options),
                "ver": (["rtl/kw.v"], "verilator"),
            },
        )

        unit, error = flow._planned_standalone_unit(["sv", "ver"])

        assert error is None
        assert unit is not None
        sources = unit.recipe["module_sources"]
        assert {(e["frontend"], tuple(e["dialect_flags"])) for e in sources} == {
            ("iverilog", ("-g2012",)),
            ("verilator", ()),
        }
        assert {command.argv[0]: command.argv[1] for command in unit.commands}["iverilog"] == (
            "-g2012"
        )

        _stub_dialect_compilers(monkeypatch, flow)
        outcome = _run_sweep(flow, ["sv", "ver"])

        console = "\n".join(outcome.lines)
        assert "kw (rtl/kw.v) via iverilog -g2012:" in console
        log = (tmp_path / outcome.detail["log"]).read_text(encoding="utf-8")
        assert "# standalone probe: kw (rtl/kw.v) via iverilog -g2012\n" in log
        assert "# standalone probe: kw (rtl/kw.v) via verilator (default dialect)\n" in log


@pytest.mark.parametrize(
    ("kind", "expected_exit"),
    [("pass", 0), ("design", 1), ("tool", 2)],
)
def test_optional_standalone_exit_codes_for_single_simulator_project(
    tmp_path, monkeypatch, kind, expected_exit
) -> None:
    """Icarus-only project with Verilator on PATH: verdict, exit code, key unchanged."""
    _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
    _stub_sources(monkeypatch, ["rtl/alu.sv"])
    _stub_which(monkeypatch, True)
    # Start met so the tool-error case proves prior state is kept, not reset.
    flow = _make_flow(tmp_path, criterion_met=True)
    results = {
        "pass": SubprocessResult(returncode=0),
        "design": _SYNTAX,
        "tool": SubprocessResult(returncode=-1, stderr="spawn failed"),
    }
    _stub_probes(monkeypatch, flow, {"alu": results[kind]})

    with flow.publication_resources:
        exit_code, outcome = flow._run_optional_standalone(["sim_dut"], [])

    assert exit_code == expected_exit
    assert outcome is not None
    if kind == "tool":
        assert flow.state.criteria["elaborate_standalone"].met is True
    else:
        assert flow.state.criteria["elaborate_standalone"].met is (kind == "pass")


@pytest.mark.parametrize("met", [True, False])
def test_memory_only_module_sweep_final_headline(tmp_path, monkeypatch, met):
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.flows.report_headline import final_report_omits_headline

    _write_sources(tmp_path, {"rtl/alu.sv": "module alu; endmodule\n"})
    _stub_sources(monkeypatch, ["rtl/alu.sv"])
    flow = _make_flow(tmp_path)
    flow.args.state_file = None
    flow.args.target = ""
    flow.read_state()
    flow._record_eda_tool("sim_dut", "icarus")
    process = SubprocessResult(
        returncode=0 if met else 1,
        stderr="" if met else "error: Unknown module type: missing_sub",
    )
    _stub_probes(monkeypatch, flow, {"alu": process})
    with flow.publication_resources:
        outcome = flow._run_standalone_check(["sim_dut"])
    result = EndpointOutcome(exit_code=0 if outcome.passed else 1, detail=outcome.detail)
    final_report_omits_headline(flow, result)
    assert flow.state.criteria["elaborate_standalone"].met is met
    assert flow.state._file_path is None


def test_standalone_frontend_identity_is_scalar(tmp_path, monkeypatch):
    import json

    from booley.runtime.endpoint_execution import EndpointOutcome

    _write_sources(tmp_path, {"rtl/top.sv": "module top; endmodule\n"})
    _stub_sources(monkeypatch, ["rtl/top.sv"])
    flow = _make_flow(tmp_path)
    _stub_probes(monkeypatch, flow)
    with flow.publication_resources:
        outcome = flow._run_standalone_check(["sim_dut"])
    assert outcome.passed
    path = flow.write_report(EndpointOutcome())
    report = json.loads(path.read_bytes())
    assert report.get("eda_tool") == "iverilog"
    assert "eda_tools" not in report
