"""Tests for ElaborateFlow — elab-only checks, dry-run, multi-config, gist."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.elab.flow import ElaborateFlow, _extract_error_gist
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS


@pytest.fixture(autouse=True)
def _adr0039_lenient_selection(monkeypatch):
    """Pre-0039 pass-through target selection for these layer-focused tests.

    ADR 0039 made resolve_target_selection validate every token against a
    real .core surface. These unit tests mock the build/run layers and name
    ad-hoc targets (lite/full/...) without authoring cores, so give them the
    old pass-through selection — the .core-mandatory behavior itself is
    pinned in test_fusesoc_registry.py (test_no_core_rejects_any_token) and
    the .core-authoring integration tests.
    """
    from booley.fusesoc import fusesoc_registry

    def _lenient(target_arg, project_root):
        return [c.strip() for c in (target_arg or "").split(",") if c.strip()]

    monkeypatch.setattr(fusesoc_registry, "resolve_target_selection", _lenient)


def _env(state_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = "test-ticket"
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


def _make_flow(
    tmp_path: Path,
    *,
    target: str = "default",
    tb_top: str = "tb",  # accepted for call-site compat; tb_top left the surface (ADR 0021)
    extra_args: list[str] | None = None,
) -> ElaborateFlow:
    state_file = tmp_path / "state.json"
    DevelopmentState.load(state_file).save()
    report_dir = tmp_path / "reports"
    argv = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--target",
        target,
    ]
    if extra_args:
        argv.extend(extra_args)
    flow = ElaborateFlow()
    with patch.dict(os.environ, _env(state_file)):
        flow.parse_args(argv)
    flow.read_state()
    return flow


def test_human_display_caps_targets_at_three():
    results = [
        {"target": f"config_{index}", "passed": True, "error_gist": ""} for index in range(10)
    ]

    lines = ElaborateFlow._build_display_lines(results)

    assert lines == [
        "10/10 targets",
        "  + config_0",
        "  + config_1",
        "  + config_2",
        "... and 7 more targets",
    ]


# ---------------------------------------------------------------------------
# Error gist extraction
# ---------------------------------------------------------------------------


class TestErrorGist:
    def test_verilator_error_pattern(self):
        out = "junk\n%Error: top.sv:42: syntax error, unexpected ENDMODULE\nmore"
        assert "syntax error" in _extract_error_gist(out)

    def test_generic_error_prefix(self):
        out = "starting elab\nerror: undeclared identifier `foo`\nbye"
        assert "undeclared identifier" in _extract_error_gist(out)

    def test_icarus_file_line_error(self):
        out = "top.sv:17: error: cannot find module bar\n"
        assert "cannot find" in _extract_error_gist(out)

    def test_empty_output(self):
        assert _extract_error_gist("") == ""

    def test_fallback_last_line(self):
        out = "noise\ncompilation aborted with 3 errors"
        assert "compilation aborted" in _extract_error_gist(out)

    def test_truncates_long(self):
        out = "error: " + "x" * 500
        assert len(_extract_error_gist(out)) <= 80


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


class TestArgs:
    def test_default_timeout(self, tmp_path):
        flow = _make_flow(tmp_path)
        assert flow._get_timeout() == 300

    def test_custom_timeout(self, tmp_path):
        flow = _make_flow(tmp_path, extra_args=["--timeout", "60000"])
        assert flow._get_timeout() == 60


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------

# A minimal CAPI2 .core whose `sim` Target carries everything the deleted
# Booley elab EDAM hand-assembled: the --timing option set in flow_options, and
# the custom main + VCD-dump as compiled fileset sources (decision 4 — Booley no
# longer generates them; --exe wires the cppSource main). Elaborate resolves the
# sim Target and runs the default `make` (build-only, no `run`).
_SIM_CORE_TEXT = """\
CAPI=2:
name: ::elab_demo:0
description: elaborate slice fixture
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_counter.sv: {file_type: systemVerilogSource}
      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}
    tags: [tb]
  tb_cpp:
    files:
      - sim/tb_counter__main.cpp: {file_type: cppSource}
    tags: [tb]
targets:
  default:
    filesets: [rtl]
  sim:
    default_tool: verilator
    flow: sim
    flow_options:
      tool: verilator
      verilator_options: [--timing, --timescale, 1ns/1ns, "+1800-2009ext+sv", --trace, -Wno-fatal]
    filesets: [rtl, tb, tb_cpp]
    toplevel: tb_counter
"""


class TestElabResolution:
    """`_prepare_elab_command` drives a build-only `make` over FuseSoC's dir."""

    def test_prepare_drives_make_over_resolved_build_root(self, tmp_path):
        """Booley resolves the sim Target, then `make -C <relpath>` (build-only)."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, target="sim")
        resolved_build = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "elab"
            / "sim"
            / "elab_demo_0"
            / "sim"
        )
        fake = fusesoc_registry.ResolvedTarget(
            name="sim",
            vlnv="::elab_demo:0",
            toplevel="tb_counter",
            eda_tool="verilator",
            files=(),
            parameters={},
            build_root=resolved_build,
            edam_path=resolved_build / "elab_demo_0.eda.yml",
        )
        captured = {}

        def fake_resolve(target, *, project_root, build_root, **kw):
            captured.update(target=target, build_root=build_root)
            return fake

        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fake_resolve,
            ),
            patch("booley.flows.elab.flow.validate_top_parameter_intent") as guard,
        ):
            cmd = flow._prepare_elab_command("sim")
        guard.assert_called_once()

        assert captured["target"] == "sim"
        assert captured["build_root"] == (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "elab" / "sim"
        )
        assert cmd == [
            "make",
            "-C",
            ".booley_project/.runtime/edalize/elab/sim/elab_demo_0/sim",
        ]

    def test_vivado_elaboration_stops_at_synthesis_target(self, tmp_path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, target="sim")
        build = tmp_path / "build" / "sim"
        resolved = fusesoc_registry.ResolvedTarget(
            name="sim",
            vlnv="::demo:0",
            toplevel="top",
            eda_tool="vivado",
            files=(),
            parameters={},
            build_root=build,
            edam_path=build / "demo.eda.yml",
        )
        with (
            patch.object(fusesoc_registry, "resolve_target", return_value=resolved),
            patch("booley.flows.elab.flow.validate_top_parameter_intent"),
        ):
            command = flow._prepare_elab_command("sim")

        assert command[-1] == "synth"

    def test_setup_failure_propagates(self, tmp_path):
        """A FuseSoC resolution failure surfaces (caller records it as FAIL)."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, target="sim")
        import pytest

        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fusesoc_registry.TargetResolutionError("boom"),
            ),
            pytest.raises(fusesoc_registry.TargetResolutionError, match="boom"),
        ):
            flow._prepare_elab_command("sim")


class TestWarningsNonFatal:
    """`_ensure_warnings_nonfatal` demotes Verilator fatal warnings (QA-5).

    Verilator exits non-zero on *any* warning by default, so a benign style
    warning (e.g. NORETURN) would abort elaboration and make ``elab_pass``
    unreachable for an otherwise-clean core — while ``lint`` only WARNs on the
    same finding. Elaborate appends ``-Wno-fatal`` to the resolved ``.vc`` so
    warnings are non-fatal (lint-parity) while genuine ``%Error`` still fails.
    """

    @staticmethod
    def _resolved(build_root, eda_tool="verilator"):
        from booley.fusesoc import fusesoc_registry

        return fusesoc_registry.ResolvedTarget(
            name="sim",
            vlnv="::demo:0",
            toplevel="tb",
            eda_tool=eda_tool,
            files=(),
            parameters={},
            build_root=build_root,
            edam_path=build_root / "demo.eda.yml",
        )

    def test_appends_wno_fatal_to_verilator_vc(self, tmp_path):
        vc = tmp_path / "demo.vc"
        vc.write_text("--timing\n--trace\n", encoding="utf-8")
        ElaborateFlow._ensure_warnings_nonfatal(self._resolved(tmp_path))
        text = vc.read_text(encoding="utf-8")
        assert "-Wno-fatal" in text
        # Original options preserved; the flag is appended, not replacing.
        assert "--timing" in text and "--trace" in text

    def test_idempotent_when_already_present(self, tmp_path):
        vc = tmp_path / "demo.vc"
        vc.write_text("--timing\n-Wno-fatal\n", encoding="utf-8")
        ElaborateFlow._ensure_warnings_nonfatal(self._resolved(tmp_path))
        # No duplicate appended.
        assert vc.read_text(encoding="utf-8").count("-Wno-fatal") == 1

    def test_non_verilator_eda_tool_untouched(self, tmp_path):
        # Icarus/iverilog has no fatal-on-warning default — leave its files alone.
        vc = tmp_path / "demo.vc"
        vc.write_text("--timing\n", encoding="utf-8")
        ElaborateFlow._ensure_warnings_nonfatal(self._resolved(tmp_path, eda_tool="icarus"))
        assert "-Wno-fatal" not in vc.read_text(encoding="utf-8")

    def test_missing_vc_is_noop(self, tmp_path):
        # Best-effort: no .vc in the build dir must not raise.
        ElaborateFlow._ensure_warnings_nonfatal(self._resolved(tmp_path))

    def test_prepare_elab_command_injects_wno_fatal(self, tmp_path):
        """End-to-end through `_prepare_elab_command`: the resolved .vc gains it."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, target="sim")
        build_dir = tmp_path / "resolved"
        build_dir.mkdir()
        (build_dir / "demo.vc").write_text("--timing\n", encoding="utf-8")
        resolved = self._resolved(build_dir)

        with patch.object(
            fusesoc_registry,
            "resolve_target",
            return_value=resolved,
        ):
            flow._prepare_elab_command("sim")

        assert "-Wno-fatal" in (build_dir / "demo.vc").read_text(encoding="utf-8")

    def test_real_fusesoc_elab_setup(self, tmp_path):
        """End-to-end: a real `fusesoc run --setup` leaves a makeable build dir.

        Proves the --timing option set and the custom --exe main land in the
        resolved .vc and the build dir is relocatable — the sim Target built-only
        is exactly elaboration.
        """
        import pytest

        pytest.importorskip("fusesoc")
        pytest.importorskip("edalize")
        import shutil
        import sys

        from booley.fusesoc import fusesoc_registry

        work_dir = tmp_path / "proj"
        (work_dir / "rtl").mkdir(parents=True)
        (work_dir / "tb").mkdir(parents=True)
        (work_dir / "sim").mkdir(parents=True)
        (work_dir / "rtl" / "counter.sv").write_text(
            "module counter(input logic clk); endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "tb" / "tb_counter.sv").write_text(
            "module tb_counter; counter dut(.clk(1'b0)); initial $finish; endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "sim" / "booley_vcd_dump.sv").write_text(
            "module booley_vcd_dump;\n"
            '  initial if ($test$plusargs("trace")) $dumpvars(0);\n'
            "endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "sim" / "tb_counter__main.cpp").write_text(
            '#include "verilated.h"\n#include "Vtb_counter.h"\n'
            "double sc_time_stamp() { return 0; }\n"
            "int main(int argc, char** argv, char**) { return 0; }\n",
            encoding="utf-8",
        )
        (work_dir / "elab_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")

        flow = _make_flow(work_dir, target="sim")

        if shutil.which("fusesoc"):
            fusesoc_cmd = list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        else:
            fusesoc_cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

        orig_resolve = fusesoc_registry.resolve_target
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: orig_resolve(
                *a,
                **{**k, "fusesoc_cmd": fusesoc_cmd},
            ),
        ):
            cmd = flow._prepare_elab_command("sim")

        assert cmd[0] == "make" and cmd[1] == "-C"
        make_dir = (work_dir / cmd[2]).resolve()
        assert (make_dir / "Makefile").exists()
        vc = next(make_dir.glob("*.vc")).read_text(encoding="utf-8")
        assert "--timing" in vc and "--trace" in vc and "-Wno-fatal" in vc
        # Custom main wired via --exe; relocatable (no absolute project paths).
        assert "--exe" in vc
        assert "tb_counter__main.cpp" in vc
        assert str(work_dir) not in vc


# ---------------------------------------------------------------------------
# _run paths
# ---------------------------------------------------------------------------


class TestRun:
    def test_empty_config_returns_error(self, tmp_path):
        flow = _make_flow(tmp_path, target="")
        result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "--target" in result.report_text

    def test_dry_run_shows_fusesoc_setup_without_resolving(self, tmp_path, capsys):
        """Dry-run previews ``fusesoc run --setup`` + ``make`` without resolving.

        The preview is sourced from a cheap ``.core`` YAML read; patching
        ``resolve_target`` to fail proves dry-run never invokes fusesoc.
        """
        from booley.fusesoc import fusesoc_registry

        (tmp_path / "elab.core").write_text(
            "CAPI=2:\nname: ::elab_demo:0\ntargets:\n  sim:\n    flow: sim\n"
            "    flow_options:\n      tool: verilator\n",
            encoding="utf-8",
        )
        flow = _make_flow(tmp_path, target="sim", extra_args=["--dry-run"])
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=AssertionError("dry-run must not resolve (run fusesoc)"),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(capsys.readouterr().out.split("\n\n")[0])
        cmd = data["sim"]
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        assert "run --build-root" in script and "--setup" in script
        assert "--target sim" in script
        assert "elab_demo" in script  # the resolved vlnv from the .core
        assert "make -C" in script

    def test_dry_run_unknown_target_reports_error_inline(self, tmp_path, capsys):
        """A config with no ``.core`` Target yields a clean ERROR, not a crash."""
        flow = _make_flow(tmp_path, target="default", extra_args=["--dry-run"])
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(capsys.readouterr().out.split("\n\n")[0])
        assert data["default"][0].startswith("ERROR: elab dry-run:")

    def test_single_config_pass(self, tmp_path):
        flow = _make_flow(tmp_path)
        flow.state.init_criteria({"elab_pass_default": True})
        ok = SubprocessResult(returncode=0, stdout="OK", stderr="")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=ok),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "PASS" in result.report_text
        assert flow.state.criteria["elab_pass_default"].met is True
        assert "_source_fingerprint" in flow.state.criteria["elab_pass_default"].detail
        report = json.loads(
            (tmp_path / "reports" / "elab_default.json").read_text(),
        )
        assert report["passed"] is True

    def test_single_config_fail(self, tmp_path):
        flow = _make_flow(tmp_path)
        bad = SubprocessResult(
            returncode=1,
            stdout="",
            stderr="%Error: top.sv:5: bad syntax",
        )
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "FAIL" in result.report_text
        # error_gist should propagate to detail
        gist = result.detail["targets"][0]["error_gist"]
        assert "bad syntax" in gist

    def test_setup_failure_is_reported_as_eda_tool_error(self, tmp_path, capsys):
        """An EDAM/configure failure surfaces as an ERROR, not a crash.

        It reached no verdict about the RTL, so claiming the design failed to
        elaborate would be a lie — exit 2, matching `lint` on an unusable
        toolchain (F-29).
        """
        flow = _make_flow(tmp_path)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", side_effect=RuntimeError("boom")
        ):
            result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "RESULT: ERROR" in capsys.readouterr().out
        assert "boom" in result.detail["targets"][0]["error_gist"]

    def test_rejected_design_is_a_failure_not_an_eda_tool_error(self, tmp_path):
        """A compiler that ran and rejected the RTL is exit 1, like lint."""
        flow = _make_flow(tmp_path)
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make"]),
            patch.object(ElaborateFlow, "_execute_boundary") as mock_exec,
        ):
            mock_exec.return_value = MagicMock(
                returncode=2,
                stdout="",
                stderr="%Error: rtl/top.sv:3: Can't find definition of 'zzz'\n",
                timed_out=False,
                duration_s=0.4,
            )
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE

    def test_multi_config_mixed(self, tmp_path):
        flow = _make_flow(tmp_path, target="a,b")
        outputs = [
            SubprocessResult(returncode=0, stdout="ok", stderr=""),
            SubprocessResult(returncode=1, stdout="", stderr="error: nope"),
        ]
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", side_effect=outputs),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert len(result.detail["targets"]) == 2
        assert result.detail["targets"][0]["passed"] is True
        assert result.detail["targets"][1]["passed"] is False


# ---------------------------------------------------------------------------
# Elaborate follows the Simulation Flow's enablement
# ---------------------------------------------------------------------------


class TestFollowedSelection:
    def test_job_class_is_heavy(self, tmp_path: Path):
        from booley.runtime import job_slots

        assert _make_flow(tmp_path)._resolve_job_class() == job_slots.CLASS_HEAVY


def test_run_refuses_an_explicit_empty_target(tmp_path):
    flow = _make_flow(tmp_path, target="")
    result = flow._run()
    assert result.exit_code == EXIT_ERROR
    assert "--target is required" in result.report_text


# ---------------------------------------------------------------------------
# run.log parity + build-context observability (simulate/lint parity)
# ---------------------------------------------------------------------------


class TestRunLogAndBuildContext:
    """elaborate used to truncate silently to 2000 chars and persist NOTHING —
    after MCP stdout truncation the rest of the compiler output was gone
    forever (benchmark finding). It now persists run.log on pass AND fail,
    marks the truncation explicitly, and names the generated build config
    (compile command + fileset) that was invisible in reports."""

    @staticmethod
    def _log_path(tmp_path: Path) -> Path:
        return (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "elab" / "default" / "run.log"
        )

    def test_previous_runs_log_is_erased_before_the_run(self, tmp_path):
        """F-26: run.log only lands at the END of a run, so it is claimed
        (truncated to a run header) at the start — a tail during a long
        elaboration must never show the previous run's output."""
        log = self._log_path(tmp_path)
        log.parent.mkdir(parents=True)
        log.write_text("%Error: stale error from an older run\n", encoding="utf-8")
        flow = _make_flow(tmp_path)
        mid_run: dict[str, str] = {}

        def _prepare(_self, _target):
            mid_run["text"] = log.read_text(encoding="utf-8")
            return ["make", "-C", "x"]

        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", _prepare),
            patch.object(
                flow, "_execute", return_value=SubprocessResult(returncode=0, stdout="ok\n")
            ),
        ):
            flow._run()

        assert "stale error" not in mid_run["text"]
        assert mid_run["text"].startswith("[BOOLEY RUN_LOG] ")
        assert "flow=elab target=default" in mid_run["text"]

    def test_run_log_persisted_on_pass(self, tmp_path):
        flow = _make_flow(tmp_path)
        ok = SubprocessResult(returncode=0, stdout="compile ok\n", stderr="")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=ok),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        # The run header (F-26) rides above the output it identifies.
        text = self._log_path(tmp_path).read_text(encoding="utf-8")
        assert text.startswith("[BOOLEY RUN_LOG] ")
        assert text.endswith("compile ok\n")
        report = json.loads((tmp_path / "reports" / "elab_default.json").read_text())
        assert report["log"] == ".booley_project/.runtime/edalize/elab/default/run.log"

    def test_run_log_persisted_on_fail_and_cited(self, tmp_path, capsys):
        flow = _make_flow(tmp_path)
        bad = SubprocessResult(returncode=1, stdout="", stderr="%Error: top.sv:5: bad syntax\n")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "%Error" in self._log_path(tmp_path).read_text(encoding="utf-8")
        # The failure card cites the durable log this invocation wrote.
        assert "log: .booley_project/.runtime/edalize/elab/default/run.log" in (result.report_text)

    def test_truncation_is_explicit_and_names_full_log(self, tmp_path):
        """A >2000-char failure tail must say it was truncated and where the
        full output lives — silent clipping read as the whole story."""
        flow = _make_flow(tmp_path)
        chatty = "\n".join(f"%Error: line-{i:04d}" for i in range(300))  # ~5KB
        bad = SubprocessResult(returncode=1, stdout=chatty, stderr="")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()
        assert (
            "... (truncated to last 2000 chars, full log: "
            ".booley_project/.runtime/edalize/elab/default/run.log)"
        ) in result.report_text
        # The durable copy is complete even though the excerpt is clipped.
        assert "line-0000" in self._log_path(tmp_path).read_text(encoding="utf-8")

    def test_tail_cap_scales_with_mcp_budget(self, tmp_path, monkeypatch):
        """A raised BOOLEY_MCP_MAX_STDOUT_BYTES budget widens the failure tail
        proportionally (2000 chars per 12KB of budget)."""
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "24000")
        flow = _make_flow(tmp_path)
        chatty = "\n".join(f"%Error: line-{i:04d}" for i in range(300))  # ~5KB
        bad = SubprocessResult(returncode=1, stdout=chatty, stderr="")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()
        assert "... (truncated to last 4000 chars" in result.report_text
        report = json.loads((tmp_path / "reports" / "elab_default.json").read_text())
        assert len(report["error_output"]) == 4000

    def test_short_failure_has_no_truncation_marker(self, tmp_path):
        flow = _make_flow(tmp_path)
        bad = SubprocessResult(returncode=1, stdout="", stderr="%Error: nope\n")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()
        assert "truncated to last" not in result.report_text

    def test_setup_failure_persists_message_as_run_log(self, tmp_path):
        """Even a setup failure leaves a durable run.log (the message IS the
        full output) so the report's log key never points at a stale file."""
        flow = _make_flow(tmp_path)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", side_effect=RuntimeError("boom")
        ):
            flow._run()
        assert "elab setup failed: boom" in self._log_path(tmp_path).read_text(encoding="utf-8")
        report = json.loads((tmp_path / "reports" / "elab_default.json").read_text())
        assert report["log"] == ".booley_project/.runtime/edalize/elab/default/run.log"

    def test_report_and_failure_card_carry_build_context(self, tmp_path):
        """With an authored .core, the report gains compile_command + fileset
        and the failure card names both compactly (the invisible half of most
        compile failures: the make line and the fileset)."""
        (tmp_path / "elab_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, target="sim")
        bad = SubprocessResult(returncode=1, stdout="", stderr="%Error: top.sv:5: bad syntax\n")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()

        report = json.loads((tmp_path / "reports" / "elab_sim.json").read_text())
        assert "--setup" in report["compile_command"]
        assert "make -C" in report["compile_command"]
        assert report["fileset"]["rtl"] == ["rtl/counter.sv"]
        assert "tb/tb_counter.sv" in report["fileset"]["tb"]

        assert "build: " in result.report_text
        fileset = report["fileset"]
        total = len(fileset["rtl"]) + len(fileset["tb"])
        assert f"fileset: {total} files ({len(fileset['tb'])} tb)" in result.report_text

    def test_unauthored_target_omits_context_keys(self, tmp_path):
        """Best-effort contract: no .core → no compile_command/fileset keys,
        never a failed Flow."""
        flow = _make_flow(tmp_path)
        bad = SubprocessResult(returncode=1, stdout="", stderr="%Error: nope\n")
        with (
            patch.object(ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]),
            patch.object(flow, "_execute", return_value=bad),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        report = json.loads((tmp_path / "reports" / "elab_default.json").read_text())
        assert "compile_command" not in report
        assert "fileset" not in report


# ---------------------------------------------------------------------------
# Standalone-elaboration sweep (`elaborate_standalone`, ADR 0042)
# ---------------------------------------------------------------------------


class TestScanDeclarations:
    """Lexical module/package/interface scanning behind the standalone sweep."""

    def test_finds_modules_and_shared(self):
        from booley.flows.elab.flow import _scan_hdl_declarations

        text = (
            "package alu_pkg;\n  typedef logic [3:0] op_t;\nendpackage\n"
            "module alu(input logic clk);\nendmodule\n"
            "macromodule legacy_top;\nendmodule\n"
            "module automatic worker;\nendmodule\n"
        )
        modules, has_shared = _scan_hdl_declarations(text)
        assert modules == ["alu", "legacy_top", "worker"]
        assert has_shared is True

    def test_interface_marks_shared(self):
        from booley.flows.elab.flow import _scan_hdl_declarations

        modules, has_shared = _scan_hdl_declarations("interface bus_if;\nendinterface\n")
        assert modules == []
        assert has_shared is True

    def test_commented_declarations_ignored(self):
        """A `module old_impl` inside comments must not become a phantom probe."""
        from booley.flows.elab.flow import _scan_hdl_declarations

        text = (
            "// module dead_line;\n"
            "/*\nmodule dead_block;\nendmodule\n*/\n"
            "module live_one;\nendmodule\n"
        )
        modules, has_shared = _scan_hdl_declarations(text)
        assert modules == ["live_one"]
        assert has_shared is False


class TestStandalone:
    """The `elaborate_standalone` sweep: per-module iverilog probes over the
    Targets' RTL source scope, criterion-driven (ticket opt-in) or --standalone.
    """

    @staticmethod
    def _project(tmp_path: Path, files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    @staticmethod
    def _stub_sources(monkeypatch, rtl: list[str], tb: list[str] | None = None) -> None:
        """Fix the RTL/TB partition the sweep resolves (fake-registry stub)."""
        from booley.fusesoc import fusesoc_registry

        sources = fusesoc_registry.CoreSources(
            rtl_source_files=tuple(rtl),
            tb_files=tuple(tb or []),
        )
        monkeypatch.setattr(
            fusesoc_registry,
            "target_source_files",
            lambda *a, **k: sources,
        )

    @pytest.fixture(autouse=True)
    def _pin_probe_frontend(self, monkeypatch):
        """Pin the probe frontend so these cases assert one command shape.

        The knob defaults to `auto`, which picks verilator when it is on PATH
        (F-25) — leaving these assertions at the mercy of the host's install.
        The auto/verilator paths have their own cases below.
        """
        monkeypatch.setattr(
            ElaborateFlow,
            "_resolve_standalone_frontend",
            lambda self: "iverilog",
        )

    @staticmethod
    def _fake_execute(flow, probe_results: dict[str, SubprocessResult] | None = None):
        """Patch the Flow's `_execute`: `make` passes; the probes answer per-module.

        Returns the list of captured probe argvs (either frontend). Unlisted
        modules pass.
        """
        captured: list[list[str]] = []
        results = probe_results or {}

        def _execute(cmd: list[str]) -> SubprocessResult:
            if cmd[0] in ("iverilog", "verilator"):
                captured.append(cmd)
                flag = "-s" if cmd[0] == "iverilog" else "--top-module"
                module = cmd[cmd.index(flag) + 1]
                return results.get(module, SubprocessResult(returncode=0))
            return SubprocessResult(returncode=0, stdout="make ok")

        flow._execute = _execute
        return captured

    def _flow_with_criterion(self, tmp_path: Path, *, declare: bool = True) -> ElaborateFlow:
        flow = _make_flow(tmp_path)
        criteria = {"elab_pass_default": True}
        if declare:
            criteria["elaborate_standalone"] = True
        flow.state.init_criteria(criteria)
        return flow

    def test_pass_marks_criterion_met_and_probes_declaring_file_only(self, tmp_path, monkeypatch):
        """Auto-trigger from the declared criterion; each module compiles from
        its declaring file only (no sibling files on the command line)."""
        self._project(
            tmp_path,
            {
                "rtl/alu.sv": "module alu(input logic clk);\nendmodule\n",
                "rtl/top.sv": "module top;\nendmodule\n",
            },
        )
        self._stub_sources(monkeypatch, ["rtl/alu.sv", "rtl/top.sv"])
        flow = self._flow_with_criterion(tmp_path)
        captured = self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert flow.state.criteria["elaborate_standalone"].met is True
        assert result.detail["standalone"]["modules_checked"] == 2
        # One probe per module: -g2012, -s <module>, ONLY the declaring file,
        # defaults (no -P overrides), no worktree litter (-o null device).
        by_module = {cmd[cmd.index("-s") + 1]: cmd for cmd in captured}
        assert set(by_module) == {"alu", "top"}
        alu = by_module["alu"]
        assert "-g2012" in alu and "-o" in alu
        assert not any(a.startswith("-P") for a in alu)
        assert [a for a in alu if a.endswith(".sv")] == ["rtl/alu.sv"]
        assert [a for a in by_module["top"] if a.endswith(".sv")] == ["rtl/top.sv"]

    def test_cross_file_dependency_fails_with_module_file_and_stderr(self, tmp_path, monkeypatch):
        """A module leaning on a sibling file's identifier is a design FAIL:
        criterion unmet, exit 1, report names module/file with compiler stderr."""
        self._project(
            tmp_path,
            {
                "rtl/alu.sv": "module alu;\nendmodule\n",
                "rtl/top.sv": "module top;\n  assign x = shared_net;\nendmodule\n",
            },
        )
        self._stub_sources(monkeypatch, ["rtl/alu.sv", "rtl/top.sv"])
        flow = self._flow_with_criterion(tmp_path)
        stderr = "rtl/top.sv:2: error: Unable to bind wire/reg/memory `shared_net'\n"
        self._fake_execute(
            flow,
            {"top": SubprocessResult(returncode=2, stderr=stderr)},
        )
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert flow.state.criteria["elaborate_standalone"].met is False
        failures = result.detail["standalone"]["failures"]
        assert failures == [
            {
                "module": "top",
                "file": "rtl/top.sv",
                "error_gist": failures[0]["error_gist"],
            }
        ]
        assert "shared_net" in failures[0]["error_gist"]
        # Console names the module, its declaring file, and the stderr.
        assert "top (rtl/top.sv):" in result.report_text
        assert "Unable to bind" in result.report_text
        # Full per-module compiler output persisted as the standalone run.log.
        log = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "elab"
            / "standalone-sweep"
            / "run.log"
        )
        assert "$ iverilog" in log.read_text(encoding="utf-8")
        assert result.detail["standalone"]["log"] == (
            ".booley_project/.runtime/edalize/elab/standalone-sweep/run.log"
        )

    def test_package_and_interface_files_ride_along_on_every_probe(self, tmp_path, monkeypatch):
        """Shared package/interface files are auto-included so `import pkg::*`
        never scores as a cross-file finding; they are not probed themselves."""
        self._project(
            tmp_path,
            {
                "rtl/alu_pkg.sv": "package alu_pkg;\nendpackage\n",
                "rtl/bus_if.sv": "interface bus_if;\nendinterface\n",
                "rtl/alu.sv": "import alu_pkg::*;\nmodule alu;\nendmodule\n",
            },
        )
        self._stub_sources(monkeypatch, ["rtl/alu_pkg.sv", "rtl/bus_if.sv", "rtl/alu.sv"])
        flow = self._flow_with_criterion(tmp_path)
        captured = self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        # Only the module is probed — package/interface files carry no modules.
        assert len(captured) == 1
        cmd = captured[0]
        assert cmd[cmd.index("-s") + 1] == "alu"
        sv_args = [a for a in cmd if a.endswith(".sv")]
        # Shared prerequisites FIRST, declaring file last: both frontends
        # resolve `import pkg::*` during the parse, so a package listed after
        # its importer reads as "not found" — a fabricated finding.
        assert sv_args[-1] == "rtl/alu.sv"
        assert set(sv_args[:-1]) == {"rtl/alu_pkg.sv", "rtl/bus_if.sv"}
        assert result.detail["standalone"]["shared_files"] == [
            "rtl/alu_pkg.sv",
            "rtl/bus_if.sv",
        ]

    def test_tb_and_out_of_scope_files_are_exempt(self, tmp_path, monkeypatch):
        """Only the RTL source scope is probed: TB fileset modules and files
        outside the declared filesets (vendor/) are never compiled."""
        self._project(
            tmp_path,
            {
                "rtl/alu.sv": "module alu;\nendmodule\n",
                "tb/tb_alu.sv": "module tb_alu;\nendmodule\n",
                "vendor/prim.sv": "module vendor_prim;\nendmodule\n",
            },
        )
        self._stub_sources(monkeypatch, ["rtl/alu.sv"], tb=["tb/tb_alu.sv"])
        flow = self._flow_with_criterion(tmp_path)
        captured = self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        probed = {cmd[cmd.index("-s") + 1] for cmd in captured}
        assert probed == {"alu"}
        assert result.detail["standalone"]["modules_checked"] == 1

    def test_not_run_without_criterion_or_flag(self, tmp_path, monkeypatch):
        """No declared criterion, no --standalone: the sweep never runs and no
        criterion key is fabricated."""
        self._project(tmp_path, {"rtl/alu.sv": "module alu;\nendmodule\n"})
        self._stub_sources(monkeypatch, ["rtl/alu.sv"])
        flow = self._flow_with_criterion(tmp_path, declare=False)
        captured = self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert captured == []
        assert "standalone" not in result.detail
        assert "elaborate_standalone" not in flow.state.criteria

    def test_flag_triggers_without_declared_criterion(self, tmp_path, monkeypatch):
        """--standalone runs the sweep in Interactive/human mode; the criterion
        is recorded (auto-created optional) so the result is inspectable."""
        self._project(tmp_path, {"rtl/alu.sv": "module alu;\nendmodule\n"})
        self._stub_sources(monkeypatch, ["rtl/alu.sv"])
        flow = _make_flow(tmp_path, extra_args=["--standalone"])
        captured = self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert len(captured) == 1
        assert flow.state.criteria["elaborate_standalone"].met is True
        assert result.detail["standalone"]["modules_checked"] == 1
        assert "standalone: 1 modules OK" in result.display_lines

    def test_unrunnable_iverilog_is_an_eda_tool_error(self, tmp_path, monkeypatch):
        """A spawn failure (missing iverilog) reached no verdict: exit 2,
        criterion unmet — never a fabricated design FAIL."""
        self._project(tmp_path, {"rtl/alu.sv": "module alu;\nendmodule\n"})
        self._stub_sources(monkeypatch, ["rtl/alu.sv"])
        flow = self._flow_with_criterion(tmp_path)
        self._fake_execute(flow, {"alu": SubprocessResult(returncode=-1)})
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert flow.state.criteria["elaborate_standalone"].met is False
        assert "iverilog could not run" in result.report_text

    def test_empty_module_scope_is_an_eda_tool_error_not_a_vacuous_pass(
        self, tmp_path, monkeypatch
    ):
        """Zero discovered modules must not mark the criterion met — the same
        false-pass family as the vacuous-lint toplevel check."""
        self._stub_sources(monkeypatch, [])
        flow = self._flow_with_criterion(tmp_path)
        self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert flow.state.criteria["elaborate_standalone"].met is False
        assert "vacuous" in result.report_text

    def test_scope_resolution_failure_is_an_eda_tool_error(self, tmp_path, monkeypatch):
        """An unresolvable Target scope grades ERROR (no verdict), not a crash."""
        from booley.fusesoc import fusesoc_registry

        def _boom(*a, **k):
            raise fusesoc_registry.FuseSocError("no .core")

        monkeypatch.setattr(fusesoc_registry, "target_source_files", _boom)
        flow = self._flow_with_criterion(tmp_path)
        self._fake_execute(flow)
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert "could not resolve RTL source scope" in result.report_text
        assert flow.state.criteria["elaborate_standalone"].met is False

    def test_real_iverilog_end_to_end(self, tmp_path, monkeypatch):
        """With a real iverilog on PATH: the clean module passes, the module
        with a cross-file dependency fails, and the package import survives."""
        import shutil

        if shutil.which("iverilog") is None:
            pytest.skip("iverilog not installed")
        self._project(
            tmp_path,
            {
                "rtl/alu_pkg.sv": "package alu_pkg;\n  typedef logic [3:0] op_t;\nendpackage\n",
                "rtl/alu.sv": (
                    "import alu_pkg::*;\nmodule alu(input op_t op, output logic ok);\n"
                    "  assign ok = |op;\nendmodule\n"
                ),
                "rtl/broken.sv": (
                    "module broken(output logic y);\n"
                    "  assign y = net_declared_elsewhere;\nendmodule\n"
                ),
            },
        )
        self._stub_sources(monkeypatch, ["rtl/alu_pkg.sv", "rtl/alu.sv", "rtl/broken.sv"])
        flow = self._flow_with_criterion(tmp_path)
        # Only the per-target make is faked; iverilog probes run for real.
        real_execute = flow._execute

        def _execute(cmd):
            if cmd[0] == "make":
                return SubprocessResult(returncode=0, stdout="make ok")
            return real_execute(cmd)

        flow._execute = _execute
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        failures = result.detail["standalone"]["failures"]
        assert [f["module"] for f in failures] == ["broken"]

    def test_frontend_parse_gap_is_inconclusive_not_a_design_fail(self, tmp_path, monkeypatch):
        """F-25: the probe frontend choking on SystemVerilog the per-Target
        elaborate just compiled is a capability gap — ERROR (no verdict), never
        a design FAIL, and the message names the way out."""
        self._project(tmp_path, {"rtl/alu.sv": "module alu;\nendmodule\n"})
        self._stub_sources(monkeypatch, ["rtl/alu.sv"])
        flow = self._flow_with_criterion(tmp_path)
        self._fake_execute(
            flow,
            {
                "alu": SubprocessResult(
                    returncode=1,
                    stderr="rtl/fp_wire.sv:27: syntax error\n",
                )
            },
        )
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert flow.state.criteria["elaborate_standalone"].met is False
        detail = result.detail["standalone"]
        assert detail["frontend"] == "iverilog"
        assert [u["module"] for u in detail["unparsed"]] == ["alu"]
        assert "failures" not in detail
        assert "cannot parse 1/1 module" in result.report_text
        assert "not a design defect" in result.report_text
        assert 'standalone_frontend = "verilator"' in result.report_text

    def test_syntax_error_stays_a_failure_when_the_per_target_leg_failed(
        self, tmp_path, monkeypatch
    ):
        """The gap downgrade is gated on the design's own frontend accepting the
        sources: genuinely malformed RTL keeps scoring as a design FAIL."""
        self._project(tmp_path, {"rtl/alu.sv": "module alu;\nendmodule\n"})
        self._stub_sources(monkeypatch, ["rtl/alu.sv"])
        flow = self._flow_with_criterion(tmp_path)

        def _execute(cmd):
            if cmd[0] == "iverilog":
                return SubprocessResult(returncode=1, stderr="rtl/alu.sv:1: syntax error\n")
            return SubprocessResult(returncode=1, stdout="%Error: syntax error")

        flow._execute = _execute
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        detail = result.detail["standalone"]
        assert [f["module"] for f in detail["failures"]] == ["alu"]
        assert "unparsed" not in detail

    def test_a_parse_gap_does_not_erase_a_real_failure(self, tmp_path, monkeypatch):
        """One ungraded module must not swallow the verdict on the others: the
        sweep names the genuinely failing module AND the ungraded one, instead
        of reporting the whole run as 'capability gap, no verdict reached'."""
        self._project(
            tmp_path,
            {
                "rtl/gapped.sv": "module gapped;\nendmodule\n",
                "rtl/broken.sv": "module broken;\nendmodule\n",
            },
        )
        self._stub_sources(monkeypatch, ["rtl/gapped.sv", "rtl/broken.sv"])
        flow = self._flow_with_criterion(tmp_path)
        self._fake_execute(
            flow,
            {
                "gapped": SubprocessResult(returncode=1, stderr="rtl/gapped.sv:9: syntax error\n"),
                "broken": SubprocessResult(
                    returncode=1,
                    stderr="rtl/broken.sv:3: error: Unknown module type: missing_sub\n",
                ),
            },
        )
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        detail = result.detail["standalone"]
        assert [f["module"] for f in detail["failures"]] == ["broken"]
        assert [u["module"] for u in detail["unparsed"]] == ["gapped"]
        # Both facts reach the console, not just the one that returned first.
        assert "broken (rtl/broken.sv)" in result.report_text
        assert "gapped (rtl/gapped.sv)" in result.report_text
        assert "1 module(s) ungraded" in result.report_text
        assert flow.state.criteria["elaborate_standalone"].met is False


class TestStandaloneFrontend:
    """`[flows.elab].standalone_frontend` — which compiler probes (F-25)."""

    @staticmethod
    def _flow(tmp_path: Path) -> ElaborateFlow:
        return _make_flow(tmp_path)

    def test_auto_prefers_verilator_when_installed(self, tmp_path, monkeypatch):
        """Default `auto` probes with the frontend the design itself elaborates
        under, so the probe cannot reject SystemVerilog the design compiles."""
        import booley.flows.elab.flow as elab_mod

        monkeypatch.setattr(elab_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert self._flow(tmp_path)._resolve_standalone_frontend() == "verilator"

    def test_auto_falls_back_to_iverilog(self, tmp_path, monkeypatch):
        import booley.flows.elab.flow as elab_mod

        monkeypatch.setattr(elab_mod.shutil, "which", lambda name: None)
        assert self._flow(tmp_path)._resolve_standalone_frontend() == "iverilog"

    def test_explicit_pin_wins_over_path(self, tmp_path, monkeypatch):
        import booley.flows.elab.flow as elab_mod

        monkeypatch.setattr(elab_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        _asic_project(tmp_path, '[flows.elab]\nstandalone_frontend = "iverilog"\n')
        assert self._flow(tmp_path)._resolve_standalone_frontend() == "iverilog"

    def test_unknown_frontend_is_a_loud_config_error(self, tmp_path):
        _asic_project(tmp_path, '[flows.elab]\nstandalone_frontend = "vcs"\n')
        with pytest.raises(ValueError, match="standalone_frontend"):
            self._flow(tmp_path)._resolve_standalone_frontend()

    def test_bad_knob_grades_as_an_eda_tool_error_not_a_crash(self, tmp_path, monkeypatch):
        """A wrong value must surface as ERROR with the valid choices, not as
        an unhandled exception mid-sweep."""
        from booley.fusesoc import fusesoc_registry

        monkeypatch.setattr(
            fusesoc_registry,
            "target_source_files",
            lambda *a, **k: fusesoc_registry.CoreSources(rtl_source_files=(), tb_files=()),
        )
        _asic_project(tmp_path, '[flows.elab]\nstandalone_frontend = "vcs"\n')
        flow = _make_flow(tmp_path, extra_args=["--standalone"])
        flow._execute = lambda cmd: SubprocessResult(returncode=0, stdout="make ok")
        with patch.object(
            ElaborateFlow, "_prepare_elab_command", return_value=["make", "-C", "x"]
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert "standalone_frontend" in result.report_text

    def test_verilator_probe_command_shape(self, tmp_path):
        """--lint-only elaborates the hierarchy without emitting C++;
        -Wno-fatal keeps style warnings from scoring as findings."""
        cmd = self._flow(tmp_path)._standalone_compile_command(
            "alu", "rtl/alu.sv", ["rtl/pkg.sv", "rtl/alu.sv"], "verilator"
        )
        assert cmd == [
            "verilator",
            "--lint-only",
            "-Wno-fatal",
            "--top-module",
            "alu",
            "rtl/pkg.sv",
            "rtl/alu.sv",
        ]

    def test_iverilog_probe_command_shape(self, tmp_path):
        cmd = self._flow(tmp_path)._standalone_compile_command(
            "alu", "rtl/alu.sv", ["rtl/pkg.sv"], "iverilog"
        )
        assert cmd[:4] == ["iverilog", "-g2012", "-o", os.devnull]
        # Prerequisites before the declaring file — imports resolve at parse time.
        assert cmd[-2:] == ["rtl/pkg.sv", "rtl/alu.sv"]


class TestParseGapCredibility:
    """When "the probe frontend just can't read this" is an arguable claim.

    The escape hatch says one frontend rejected what *another* accepted. With
    `standalone_frontend = auto` the probe is usually the very Verilator the
    Targets built with, and then the claim is about a single compiler — which
    makes it incoherent, and makes the error the real standalone defect
    (a missing +define+/include only the Target's command line supplies).
    """

    @staticmethod
    def _flow(tmp_path: Path, target_eda_tools: dict[str, str]) -> ElaborateFlow:
        flow = _make_flow(tmp_path)
        for target, eda_tool in target_eda_tools.items():
            flow._record_eda_tool(target, eda_tool)
        return flow

    def test_a_failing_primary_leg_is_never_excused(self, tmp_path):
        flow = self._flow(tmp_path, {"sim_x": "yosys"})
        assert flow._parse_gap_is_credible("iverilog", primary_ok=False) is False

    def test_different_frontends_keep_the_hatch(self, tmp_path):
        flow = self._flow(tmp_path, {"sim_x": "verilator"})
        assert flow._parse_gap_is_credible("iverilog", primary_ok=True) is True

    def test_same_frontend_closes_the_hatch(self, tmp_path):
        flow = self._flow(tmp_path, {"sim_x": "verilator"})
        assert flow._parse_gap_is_credible("verilator", primary_ok=True) is False

    def test_icarus_is_the_iverilog_probe_under_another_name(self, tmp_path):
        """Edalize spells it `icarus`; it is the same binary the probe runs."""
        flow = self._flow(tmp_path, {"sim_x": "icarus"})
        assert flow._parse_gap_is_credible("iverilog", primary_ok=True) is False

    def test_any_matching_target_closes_the_hatch(self, tmp_path):
        flow = self._flow(tmp_path, {"sim_x": "icarus", "asic_y": "yosys"})
        assert flow._parse_gap_is_credible("iverilog", primary_ok=True) is False

    def test_unrelated_eda_tools_keep_the_hatch(self, tmp_path):
        flow = self._flow(tmp_path, {"asic_y": "yosys", "sim_z": "xcelium"})
        assert flow._parse_gap_is_credible("verilator", primary_ok=True) is True

    def test_same_frontend_syntax_error_is_a_real_finding(self, tmp_path, monkeypatch):
        """End to end: verilator built the Target and verilator rejects the
        module standalone -> a FAIL, not an excused capability gap."""
        from booley.fusesoc import fusesoc_registry

        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl/alu.sv").write_text("module alu;\nendmodule\n", encoding="utf-8")
        monkeypatch.setattr(
            fusesoc_registry,
            "target_source_files",
            lambda *a, **k: fusesoc_registry.CoreSources(
                rtl_source_files=("rtl/alu.sv",), tb_files=()
            ),
        )
        monkeypatch.setattr(
            ElaborateFlow, "_resolve_standalone_frontend", lambda self: "verilator"
        )
        flow = _make_flow(tmp_path, extra_args=["--standalone"])

        def _prepare(self, target):
            self._record_eda_tool(target, "verilator")
            return ["make", "-C", "x"]

        monkeypatch.setattr(ElaborateFlow, "_prepare_elab_command", _prepare)
        flow._execute = lambda cmd: (
            SubprocessResult(returncode=1, stderr="%Error: rtl/alu.sv:1: syntax error\n")
            if cmd[0] == "verilator"
            else SubprocessResult(returncode=0, stdout="make ok")
        )
        result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        detail = result.detail["standalone"]
        assert [f["module"] for f in detail["failures"]] == ["alu"]
        assert "unparsed" not in detail


def _asic_project(tmp_path: Path, body: str) -> None:
    """Author a booley.toml carrying an [flows.synth] section."""
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    (project_dir / "booley.toml").write_text(body, encoding="utf-8")


def _yosys_resolved(
    build_root: Path,
    *,
    eda_tool: str = "yosys",
    flow_options: dict | None = None,
):
    """A resolved ASIC Target: two SV sources, one header, one vlogdefine."""
    from booley.fusesoc import fusesoc_registry

    files = (
        fusesoc_registry.ResolvedFile(name="rtl/pkg.sv", file_type="systemVerilogSource"),
        fusesoc_registry.ResolvedFile(name="rtl/dut.sv", file_type="systemVerilogSource"),
        fusesoc_registry.ResolvedFile(
            name="rtl/inc/defs.svh",
            file_type="systemVerilogSource",
            is_include=True,
        ),
    )
    return fusesoc_registry.ResolvedTarget(
        name="asic_dut",
        vlnv="::demo:0",
        toplevel="dut",
        eda_tool=eda_tool,
        flow_options=flow_options or {"tool": eda_tool},
        files=files,
        parameters={
            "NO_ASSERTIONS": {"paramtype": "vlogdefine", "default": True},
            "N": {"paramtype": "vlogparam", "default": 4},
        },
        build_root=build_root,
        edam_path=build_root / "demo.eda.yml",
    )


class TestAsicFrontendParity:
    """`elaborate` honors the ASIC Target's frontend (ravenoc F-31).

    Edalize's Yosys flow reads RTL with a generic `read_verilog`, which dies on
    a package import (`syntax error, unexpected TOK_IMPORT`) — so *every*
    SystemVerilog ASIC Target was un-elaboratable, on either frontend. Booley
    now reads the design the way `asic_synthesize` will.
    """

    def test_slang_target_runs_booleys_own_yosys_script(self, tmp_path):
        resolved = _yosys_resolved(
            tmp_path / "build",
            flow_options={
                "tool": "yosys",  # upstream FuseSoC/Edalize schema field
                "frontend": "slang",
                "slang_options": ["--single-unit"],
            },
        )
        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", resolved)

        assert cmd is not None
        assert cmd[:2] == ["yosys", "-p"]
        script = cmd[2]
        assert script.startswith("read_slang --top dut ")
        assert "--single-unit" in script
        assert "-D NO_ASSERTIONS" in script
        assert "-G N=4" in script
        # The include header is an -I dir, not a source to elaborate.
        assert "defs.svh" not in script
        assert "-I build/rtl/inc" in script
        assert "hierarchy -check -top dut" in script

    def test_sv2v_target_transpiles_before_reading(self, tmp_path):
        """The regression this finding is really about: on the DEFAULT frontend
        an SV ASIC Target must go through sv2v, not straight into read_verilog.
        """
        resolved = _yosys_resolved(
            tmp_path / "build",
            flow_options={"tool": "yosys", "frontend": "sv2v"},
        )
        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", resolved)

        assert cmd is not None
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        # Stage 1: sv2v over the raw SV sources, with includes + defines.
        assert script.startswith("sv2v ")
        assert "-Ibuild/rtl/inc" in script
        assert "-DNO_ASSERTIONS" in script
        assert "build/rtl/pkg.sv" in script and "build/rtl/dut.sv" in script
        assert "-w build/sv2v_converted.v" in script
        # Stage 2: Yosys reads the TRANSPILED file, never the raw .sv.
        yosys_stage = script[script.index("yosys -p") :]
        assert "sv2v_converted.v" in yosys_stage
        assert "rtl/dut.sv" not in yosys_stage
        assert "chparam -set N 4 dut" in yosys_stage
        assert "hierarchy -libdir ./ -check -top dut" in yosys_stage
        # sv2v runs first and gates Yosys.
        assert script.index("sv2v ") < script.index("yosys -p")

    def test_unset_frontend_defaults_to_the_sv2v_chain(self, tmp_path):
        """No [flows.synth] section at all — the common case, and the
        one the finding reproduced on."""
        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", _yosys_resolved(tmp_path / "build"))
        assert cmd is not None
        assert cmd[:2] == ["sh", "-c"]
        assert "sv2v " in cmd[2]

    def test_sv2v_failure_names_the_stage(self, tmp_path):
        """A transpile failure must not read as a Yosys diagnostic."""
        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", _yosys_resolved(tmp_path / "build"))
        script = cmd[2]
        assert "the sv2v transpile FAILED" in script
        assert "come from sv2v" in script and "not from Yosys" in script
        # Named as a real elaboration failure, not an EDA-tool problem.
        assert "real elaboration failure" in script
        # And the rc is preserved so the verdict stays a design FAIL.
        assert "exit $rc" in script

    def test_asic_target_is_marked_for_local_execution(self, tmp_path):
        """Neither `yosys -p` nor the sh -c chain is a Boundary-Command-Contract
        command, so they must never be routed to the host."""
        flow = _make_flow(tmp_path, target="asic_dut")
        flow._asic_elab_command("asic_dut", _yosys_resolved(tmp_path / "build"))
        assert "asic_dut" in flow._asic_targets()

    def test_sim_target_is_never_diverted(self, tmp_path):
        """A verilator Target keeps make-driving Edalize."""
        flow = _make_flow(tmp_path, target="sim")
        resolved = _yosys_resolved(tmp_path / "b", eda_tool="verilator")
        assert flow._asic_elab_command("sim", resolved) is None

    def test_toplevel_less_target_is_never_diverted(self, tmp_path):
        """No elaboration root means no `--top`; the Edalize path reports that
        gap in its own vocabulary."""
        import dataclasses

        flow = _make_flow(tmp_path, target="asic_dut")
        resolved = dataclasses.replace(_yosys_resolved(tmp_path / "b"), toplevel="")
        assert flow._asic_elab_command("asic_dut", resolved) is None

    def test_prepare_returns_the_asic_command_end_to_end(self, tmp_path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, target="asic_dut")
        resolved = _yosys_resolved(
            tmp_path / "build",
            flow_options={"tool": "yosys", "frontend": "slang"},
        )
        with patch.object(fusesoc_registry, "resolve_target", return_value=resolved):
            cmd = flow._prepare_elab_command("asic_dut")
        assert cmd[0] == "yosys"

    def test_asic_target_uses_the_local_executor(self, tmp_path):
        """Wiring check: the diverted command goes through `_execute`, not the
        Session Runtime boundary executor."""
        flow = _make_flow(tmp_path, target="asic_dut")
        flow._asic_targets().add("asic_dut")
        with (
            patch.object(
                ElaborateFlow, "_prepare_elab_command", return_value=["yosys", "-p", "x"]
            ),
            patch.object(
                ElaborateFlow,
                "_execute",
                return_value=SubprocessResult(returncode=0, stdout="ok"),
            ) as local,
            patch.object(ElaborateFlow, "_execute_boundary") as boundary,
        ):
            flow._elaborate_targets(["asic_dut"])
        assert local.called
        assert not boundary.called

    @staticmethod
    def _stub_eda_tools(tmp_path, *, sv2v_rc: int) -> dict[str, str]:
        """PATH with stub sv2v/yosys so the composed shell can be run for real."""
        bindir = tmp_path / "stubbin"
        bindir.mkdir()
        (bindir / "sv2v").write_text(
            f"#!/bin/sh\necho 'sv2v: Parse error near import' >&2\nexit {sv2v_rc}\n",
            encoding="utf-8",
        )
        (bindir / "yosys").write_text("#!/bin/sh\necho YOSYS_RAN\nexit 0\n", encoding="utf-8")
        for name in ("sv2v", "yosys"):
            (bindir / name).chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        return env

    def test_composed_shell_chains_the_two_stages(self, tmp_path):
        """Run the generated `sh -c` for real (stub binaries): a clean transpile
        hands off to Yosys."""
        import subprocess

        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", _yosys_resolved(tmp_path / "build"))
        proc = subprocess.run(
            cmd,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=self._stub_eda_tools(tmp_path, sv2v_rc=0),
        )
        assert proc.returncode == 0
        assert "YOSYS_RAN" in proc.stdout

    def test_composed_shell_stops_and_reports_on_a_failed_transpile(self, tmp_path):
        """A failing transpile keeps its rc, never reaches Yosys, and says so."""
        import subprocess

        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", _yosys_resolved(tmp_path / "build"))
        proc = subprocess.run(
            cmd,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=self._stub_eda_tools(tmp_path, sv2v_rc=3),
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 3  # rc preserved -> a design FAIL, not a Flow error
        assert "YOSYS_RAN" not in combined  # Yosys never ran
        assert "sv2v: Parse error near import" in combined  # sv2v's own stderr survives
        assert "the sv2v transpile FAILED" in combined  # ...and the stage is named

    def test_real_sv2v_yosys_elaborates_a_package_import(self, tmp_path, monkeypatch):
        """End-to-end with real sv2v + Yosys: an SV ASIC Target whose sources
        use `package`/`import` elaborates cleanly on the DEFAULT frontend.

        This is the finding's exact reproducer — it used to die with
        `ERROR: syntax error, unexpected TOK_IMPORT`.
        """
        import shutil as _shutil
        import subprocess

        if _shutil.which("sv2v") is None or _shutil.which("yosys") is None:
            pytest.skip("sv2v and/or yosys not installed")

        build = tmp_path / "build"
        (build / "rtl" / "inc").mkdir(parents=True)
        (build / "rtl" / "pkg.sv").write_text(
            "package demo_pkg;\n  typedef logic [3:0] nibble_t;\nendpackage\n",
            encoding="utf-8",
        )
        (build / "rtl" / "dut.sv").write_text(
            "import demo_pkg::*;\n"
            "module dut #(parameter int N = 4) (\n"
            "  input logic clk, input nibble_t d, output logic q);\n"
            "  always_ff @(posedge clk) q <= |d;\n"
            "endmodule\n",
            encoding="utf-8",
        )
        (build / "rtl" / "inc" / "defs.svh").write_text("// header\n", encoding="utf-8")

        flow = _make_flow(tmp_path, target="asic_dut")
        cmd = flow._asic_elab_command("asic_dut", _yosys_resolved(build))
        proc = subprocess.run(
            cmd, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=300
        )
        combined = proc.stdout + proc.stderr
        assert "TOK_IMPORT" not in combined, combined
        assert proc.returncode == 0, combined


class TestBuildTreeRetention:
    """Elaborate cleans up its build trees after a PASS (ravenoc F-33).

    An 11-Target sweep left 1.4 GB of verilated object trees behind for what is
    a compile-only check.
    """

    @staticmethod
    def _flow_with_build_dir(tmp_path: Path) -> tuple[ElaborateFlow, Path]:
        flow = _make_flow(tmp_path, target="sim")
        build_dir = tmp_path / "build" / "sim"
        build_dir.mkdir(parents=True)
        (build_dir / "Vtb__ALL.o").write_text("object soup", encoding="utf-8")
        flow._record_build_dir("sim", build_dir)
        return flow, build_dir

    def test_clean_run_discards_the_build_tree(self, tmp_path):
        flow, build_dir = self._flow_with_build_dir(tmp_path)
        flow._discard_build_dir("sim")
        assert not build_dir.exists()

    def test_run_log_survives_the_cleanup(self, tmp_path):
        """Only the inner resolved build dir goes; run.log lives one level up
        in the work root, so failure triage still has the compiler output."""
        flow, _build_dir = self._flow_with_build_dir(tmp_path)
        log = flow._persist_run_log("sim", "compiler said things")
        flow._discard_build_dir("sim")
        assert log is not None
        assert (Path(flow.args.work_dir) / log).exists()

    def test_keep_build_dir_knob_retains_it(self, tmp_path):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            "[flows.elab]\nkeep_build_dir = true\n", encoding="utf-8"
        )
        flow, build_dir = self._flow_with_build_dir(tmp_path)
        flow._discard_build_dir("sim")
        assert build_dir.exists()

    def test_unrecorded_target_is_a_noop(self, tmp_path):
        flow = _make_flow(tmp_path, target="sim")
        flow._discard_build_dir("never-resolved")  # must not raise

    def test_failed_target_keeps_its_build_tree(self, tmp_path):
        """The tree is the evidence — a FAIL never triggers cleanup."""
        flow, build_dir = self._flow_with_build_dir(tmp_path)
        with (
            patch.object(
                ElaborateFlow,
                "_prepare_elab_command",
                return_value=["make", "-C", "x"],
            ),
            patch.object(
                ElaborateFlow,
                "_execute_boundary",
                return_value=SubprocessResult(returncode=1, stdout="%Error: nope"),
            ),
        ):
            flow._elaborate_targets(["sim"])
        assert build_dir.exists()

    def test_passing_target_loses_its_build_tree(self, tmp_path):
        flow, build_dir = self._flow_with_build_dir(tmp_path)
        with (
            patch.object(
                ElaborateFlow,
                "_prepare_elab_command",
                return_value=["make", "-C", "x"],
            ),
            patch.object(
                ElaborateFlow,
                "_execute_boundary",
                return_value=SubprocessResult(returncode=0, stdout="ok"),
            ),
        ):
            flow._elaborate_targets(["sim"])
        assert not build_dir.exists()
