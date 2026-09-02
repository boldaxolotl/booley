"""Standalone-module coverage for Simulation's Elaboration Check mode."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.sim import standalone as standalone_mod
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.standalone import _scan_hdl_declarations
from booley.runtime.project_dir import reset_cache


@pytest.fixture(autouse=True)
def _reset_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    if config:
        (project_dir / "booley.toml").write_text(config, encoding="utf-8")
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.init_criteria({"elaborate_standalone": True})
    state.criteria["elaborate_standalone"].met = criterion_met
    state.save()
    env = os.environ.copy()
    env.update(
        BOOLEY_PROJECT_DIR=str(project_dir),
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
            "--elab-only",
        ]
        if standalone:
            args.append("--standalone")
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
) -> None:
    monkeypatch.setattr(SimulateFlow, "_target_handle", lambda *_args: MagicMock())
    monkeypatch.setattr(
        standalone_mod,
        "inspect_target",
        lambda *args, **kwargs: MagicMock(
            rtl_files=tuple(rtl),
            tb_files=tuple(tb or []),
        ),
    )


def _stub_probes(
    monkeypatch: pytest.MonkeyPatch,
    flow: SimulateFlow,
    results: dict[str, SubprocessResult] | None = None,
) -> list[list[str]]:
    captured: list[list[str]] = []
    by_module = results or {}

    def execute(command: list[str]) -> SubprocessResult:
        captured.append(command)
        flag = "-s" if command[0] == "iverilog" else "--top-module"
        module = command[command.index(flag) + 1]
        return by_module.get(module, SubprocessResult(returncode=0))

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
    @pytest.fixture(autouse=True)
    def _pin_iverilog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            SimulateFlow,
            "_resolve_standalone_frontend",
            lambda self: "iverilog",
        )

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

        outcome = flow._run_standalone_check(["sim_dut"])

        assert outcome.passed
        assert flow.state.criteria["elaborate_standalone"].met is True
        assert outcome.detail["modules_checked"] == 2
        by_module = {command[command.index("-s") + 1]: command for command in commands}
        assert [arg for arg in by_module["alu"] if arg.endswith(".sv")] == ["rtl/alu.sv"]
        assert [arg for arg in by_module["top"] if arg.endswith(".sv")] == ["rtl/top.sv"]
        assert all(not arg.startswith("-P") for arg in by_module["alu"])

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

        outcome = flow._run_standalone_check(["sim_dut"])

        assert not outcome.passed
        assert not outcome.eda_tool_failed
        assert flow.state.criteria["elaborate_standalone"].met is False
        assert outcome.detail["failures"][0]["module"] == "top"
        assert "top (rtl/top.sv)" in "\n".join(outcome.lines)
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
        _stub_sources(monkeypatch, ["rtl/alu.sv"])
        flow = _make_flow(tmp_path, criterion_met=True)
        flow._record_eda_tool("sim_dut", "verilator")
        _stub_probes(
            monkeypatch,
            flow,
            {"alu": SubprocessResult(returncode=1, stderr="rtl/alu.sv:1: syntax error\n")},
        )

        outcome = flow._run_standalone_check(["sim_dut"])

        assert outcome.eda_tool_failed
        assert outcome.detail["unparsed"][0]["module"] == "alu"
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
        _stub_sources(monkeypatch, ["rtl/gapped.sv", "rtl/broken.sv"])
        flow = _make_flow(tmp_path, criterion_met=True)
        flow._record_eda_tool("sim_dut", "verilator")
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

        outcome = flow._run_standalone_check(["sim_dut"])

        assert [item["module"] for item in outcome.detail["failures"]] == ["broken"]
        assert [item["module"] for item in outcome.detail["unparsed"]] == ["gapped"]
        assert flow.state.criteria["elaborate_standalone"].met is False


class TestStandaloneFrontend:
    def test_auto_prefers_verilator_and_falls_back_to_iverilog(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = _make_flow(tmp_path)
        monkeypatch.setattr(standalone_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert flow._resolve_standalone_frontend() == "verilator"
        monkeypatch.setattr(standalone_mod.shutil, "which", lambda name: None)
        assert flow._resolve_standalone_frontend() == "iverilog"

    def test_sim_config_pin_wins_and_unknown_value_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(standalone_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        pinned = _make_flow(
            tmp_path,
            config='[flows.sim]\nstandalone_frontend = "iverilog"\n',
        )
        assert pinned._resolve_standalone_frontend() == "iverilog"

        (tmp_path / ".booley_project" / "booley.toml").write_text(
            '[flows.sim]\nstandalone_frontend = "vcs"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="standalone_frontend"):
            pinned._resolve_standalone_frontend()

    def test_probe_command_shapes(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path)

        verilator = flow._standalone_compile_command(
            "alu",
            "rtl/alu.sv",
            ["rtl/pkg.sv"],
            "verilator",
        )
        iverilog = flow._standalone_compile_command(
            "alu",
            "rtl/alu.sv",
            ["rtl/pkg.sv"],
            "iverilog",
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
        assert iverilog[:4] == ["iverilog", "-g2012", "-o", os.devnull]
        assert iverilog[-2:] == ["rtl/pkg.sv", "rtl/alu.sv"]

    @pytest.mark.parametrize(
        ("target_tools", "frontend", "primary_ok", "credible"),
        [
            ({"sim": "verilator"}, "iverilog", True, True),
            ({"sim": "verilator"}, "verilator", True, False),
            ({"sim": "icarus"}, "iverilog", True, False),
            ({"sim": "verilator"}, "iverilog", False, False),
        ],
    )
    def test_parse_gap_credibility(
        self,
        tmp_path: Path,
        target_tools: dict[str, str],
        frontend: str,
        primary_ok: bool,
        credible: bool,
    ) -> None:
        flow = _make_flow(tmp_path)
        for target, tool in target_tools.items():
            flow._record_eda_tool(target, tool)

        assert flow._parse_gap_is_credible(frontend, primary_ok) is credible
