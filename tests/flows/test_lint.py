"""Tests for LintFlow вЂ” warning parsing, dedup, scope, reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.flows.lint import (
    LintConfigResult,
    LintFlow,
    LintWarning,
    deduplicate_warnings,
    filter_by_scope,
    parse_verible_warnings,
    parse_warnings,
)
from booley.mcp_tools.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS


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
    from booley import fusesoc_registry

    def _lenient(target_arg, project_root):
        return [c.strip() for c in (target_arg or "").split(",") if c.strip()]

    monkeypatch.setattr(fusesoc_registry, "resolve_target_selection", _lenient)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_VERILATOR_OUTPUT = """\
%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: Signal is not used: 'foo'
                       ... In instance 'design_top.mod_a_inst'
%Warning-UNDRIVEN: rtl/mod_a.sv:100:3: Signal is not driven: 'bar'
%Warning-WIDTH: rtl/mod_c.sv:55:10: Operator ASSIGN expects 32 bits on the LHS
"""

SAMPLE_STDERR_MIXED = """\
Compiling mod_b.sv
%Warning-UNUSEDSIGNAL: rtl/mod_b.sv:88:7: Signal is not used: 'debug_flag'
%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: Signal is not used: 'foo'
"""


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal state file and set env vars."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "test-lint"
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "test-lint")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


@pytest.fixture()
def lint_flow() -> LintFlow:
    return LintFlow()


def _stub_resolved(eda_tool: str | None = "verilator") -> object:
    """Minimal ResolvedTarget for tests that patch _prepare_lint_command.

    ``_prepare_lint_command`` now returns (make command, ResolvedTarget) so the
    caller can report the actual EDA tool and file coverage; mocked call sites
    pair the command with this stub. Empty toplevel/files skip the coverage
    checks.
    """
    from booley import fusesoc_registry

    return fusesoc_registry.ResolvedTarget(
        name="stub",
        vlnv="::stub_demo:0",
        toplevel="",
        eda_tool=eda_tool,
        files=(),
        parameters={},
        build_root=Path("build"),
        edam_path=Path("build/stub_demo_0.eda.yml"),
    )


# ---------------------------------------------------------------------------
# FuseSoC lint Target resolution (ADR 0022) — replaces the Booley-built EDAM
# ---------------------------------------------------------------------------

# A minimal CAPI2 .core whose `lint` Target carries the -Wall option that
# Booley's deleted _build_lint_edam used to inject. --lint-only is added by the
# lint flow itself, so the Target only needs the extra verilator option.
_LINT_CORE_TEXT = """\
CAPI=2:
name: ::lint_demo:0
description: lint slice fixture
filesets:
  rtl:
    files:
      - rtl/top.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
targets:
  default:
    filesets: [rtl]
  lite:
    default_tool: verilator
    flow: lint
    flow_options:
      tool: verilator
      verilator_options: [-Wall]
    filesets: [rtl]
    toplevel: top
"""


class TestLintResolution:
    """`_prepare_lint_command` now drives `make` over FuseSoC's resolved build dir."""

    def _flow(self, tmp_path: Path, state_file: Path) -> LintFlow:
        (tmp_path / "rtl").mkdir(exist_ok=True)
        (tmp_path / "rtl" / "top.sv").write_text("module top; endmodule\n")
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "lite"])
        flow.read_state()
        return flow

    def test_prepare_drives_make_over_resolved_build_root(
        self,
        tmp_path: Path,
        state_file: Path,
    ):
        """Booley resolves the Target through FuseSoC, then `make -C <relpath>`."""
        from booley import fusesoc_registry

        flow = self._flow(tmp_path, state_file)
        # FuseSoC lays the build dir at <build_root>/<name>/<target>/; parse_edam
        # points ResolvedTarget.build_root there. Emulate that for the mock.
        resolved_build = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "lint"
            / "lite"
            / "lint_demo_0"
            / "lite"
        )
        fake = fusesoc_registry.ResolvedTarget(
            name="lite",
            vlnv="::lint_demo:0",
            toplevel="top",
            eda_tool="verilator",
            files=(),
            parameters={},
            build_root=resolved_build,
            edam_path=resolved_build / "lint_demo_0.eda.yml",
        )
        captured = {}

        def fake_resolve(target, *, project_root, build_root, **kw):
            captured.update(
                target=target,
                project_root=project_root,
                build_root=build_root,
            )
            return fake

        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=fake_resolve,
        ):
            cmd, resolved = flow._prepare_lint_command("lite")

        # The ResolvedTarget rides along for EDA-tool/coverage reporting.
        assert resolved is fake
        # Target name and project root are forwarded; build_root is the
        # per-(EDA tool, config) Edalize dir.
        assert captured["target"] == "lite"
        assert Path(captured["project_root"]) == Path(flow.args.work_dir)
        assert captured["build_root"] == (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "lint" / "lite"
        )
        # Drives make over the resolved build dir via a relocatable relpath.
        assert cmd == [
            "make",
            "-C",
            ".booley_project/.runtime/edalize/lint/lite/lint_demo_0/lite",
        ]

    def test_setup_failure_propagates(self, tmp_path: Path, state_file: Path):
        """A FuseSoC resolution failure surfaces (caller records a Flow error)."""
        from booley import fusesoc_registry

        flow = self._flow(tmp_path, state_file)
        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fusesoc_registry.TargetResolutionError("boom"),
            ),
            pytest.raises(fusesoc_registry.TargetResolutionError, match="boom"),
        ):
            flow._prepare_lint_command("lite")

    def test_real_fusesoc_lint_setup(self, tmp_path: Path, state_file: Path):
        """End-to-end: a real `fusesoc run --setup` leaves a makeable lint dir.

        Mirrors the registry e2e (test_fusesoc_registry) but exercises the lint
        Target through the Flow's own seam, proving -Wall/--lint-only land in
        the resolved .vc and the build dir is relocatable.
        """
        pytest.importorskip("fusesoc")
        pytest.importorskip("edalize")
        import shutil
        import sys

        from booley import fusesoc_registry

        work_dir = tmp_path / "proj"
        (work_dir / "rtl").mkdir(parents=True)
        (work_dir / "rtl" / "top.sv").write_text(
            "module top; endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "lint_demo.core").write_text(_LINT_CORE_TEXT, encoding="utf-8")

        flow = LintFlow()
        flow.parse_args(["--work-dir", str(work_dir), "--target", "lite"])
        flow.read_state()

        # Prefer the console script; otherwise invoke the importable module
        # (eda-libs ships no console script — same fallback as the registry e2e).
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
            cmd, _resolved = flow._prepare_lint_command("lite")

        assert cmd[0] == "make" and cmd[1] == "-C"
        make_dir = (work_dir / cmd[2]).resolve()
        assert (make_dir / "Makefile").exists()
        vc = next(make_dir.glob("*.vc")).read_text(encoding="utf-8")
        assert "--lint-only" in vc
        assert "-Wall" in vc
        # Relocatable: no absolute project/build paths baked into the .vc.
        assert str(work_dir) not in vc


# ---------------------------------------------------------------------------
# Warning parsing
# ---------------------------------------------------------------------------


class TestWarningParsing:
    def test_parse_basic_warnings(self):
        warnings = parse_warnings(SAMPLE_VERILATOR_OUTPUT, "lite")
        assert len(warnings) == 3
        assert warnings[0].rule == "UNUSEDSIGNAL"
        assert warnings[0].file == "rtl/mod_a.sv"
        assert warnings[0].line == 42
        assert warnings[0].col == 5
        assert "'foo'" in warnings[0].message
        assert warnings[0].target == "lite"

    def test_parse_width_warning(self):
        warnings = parse_warnings(SAMPLE_VERILATOR_OUTPUT, "full")
        width_warnings = [w for w in warnings if w.rule == "WIDTH"]
        assert len(width_warnings) == 1
        assert width_warnings[0].file == "rtl/mod_c.sv"
        assert width_warnings[0].line == 55

    def test_parse_empty_output(self):
        warnings = parse_warnings("", "lite")
        assert warnings == []

    def test_parse_no_warnings(self):
        warnings = parse_warnings("Compiling module...\nDone.\n", "lite")
        assert warnings == []

    def test_parse_mixed_stderr(self):
        """Warnings embedded among non-warning lines."""
        warnings = parse_warnings(SAMPLE_STDERR_MIXED, "combo")
        assert len(warnings) == 2
        assert warnings[0].rule == "UNUSEDSIGNAL"
        assert warnings[0].file == "rtl/mod_b.sv"

    def test_parse_preserves_config(self):
        warnings = parse_warnings(SAMPLE_VERILATOR_OUTPUT, "my_cfg")
        assert all(w.target == "my_cfg" for w in warnings)

    def test_dedup_key_property(self):
        w = LintWarning("RULE", "file.sv", 10, 3, "msg", "lite")
        assert w.dedup_key == ("RULE", "file.sv", 10)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_dedup_same_warning_different_configs(self):
        w1 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg", "lite")
        w2 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg", "full")
        w3 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg", "combo")
        result = deduplicate_warnings([w1, w2, w3])
        assert len(result) == 1
        assert result[0].target == "lite"  # first occurrence kept

    def test_dedup_different_warnings(self):
        w1 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg1", "lite")
        w2 = LintWarning("WIDTH", "mod_c.sv", 55, 10, "msg2", "lite")
        result = deduplicate_warnings([w1, w2])
        assert len(result) == 2

    def test_dedup_same_rule_different_lines(self):
        w1 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg1", "lite")
        w2 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 99, 5, "msg2", "lite")
        result = deduplicate_warnings([w1, w2])
        assert len(result) == 2

    def test_dedup_empty(self):
        assert deduplicate_warnings([]) == []

    def test_dedup_across_configs_preserves_order(self):
        """First occurrence from each dedup group is kept."""
        w_lite = LintWarning("A", "f.sv", 1, 1, "m", "lite")
        w_full = LintWarning("B", "g.sv", 2, 1, "m", "full")
        w_lite_dup = LintWarning("A", "f.sv", 1, 1, "m", "combo")
        result = deduplicate_warnings([w_lite, w_full, w_lite_dup])
        assert len(result) == 2
        assert result[0].rule == "A"
        assert result[1].rule == "B"


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


class TestScopeFiltering:
    def test_scope_matches_substring(self):
        w1 = LintWarning("A", "rtl/mod_a.sv", 1, 1, "m", "lite")
        w2 = LintWarning("B", "rtl/mod_c.sv", 2, 1, "m", "lite")
        result = filter_by_scope([w1, w2], "mod_a")
        assert len(result) == 1
        assert result[0].file == "rtl/mod_a.sv"

    def test_scope_multiple_paths(self):
        w1 = LintWarning("A", "rtl/mod_a.sv", 1, 1, "m", "lite")
        w2 = LintWarning("B", "rtl/mod_c.sv", 2, 1, "m", "lite")
        w3 = LintWarning("C", "rtl/design_top.sv", 3, 1, "m", "lite")
        result = filter_by_scope([w1, w2, w3], "mod_a,mod_c")
        assert len(result) == 2

    def test_scope_empty_returns_all(self):
        w1 = LintWarning("A", "f.sv", 1, 1, "m", "lite")
        result = filter_by_scope([w1], "")
        assert len(result) == 1

    def test_scope_no_match(self):
        w1 = LintWarning("A", "rtl/mod_a.sv", 1, 1, "m", "lite")
        result = filter_by_scope([w1], "nonexistent")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# LintFlow CLI args
# ---------------------------------------------------------------------------


class TestLintFlowArgs:
    def test_default_args(self, state_file: Path):
        flow = LintFlow()
        args = flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        assert args.scope == ""
        assert args.dry_run is False
        assert args.timeout == 120000

    def test_scope_arg(self, state_file: Path):
        flow = LintFlow()
        args = flow.parse_args(
            [
                "--target",
                "lite",
                "--scope",
                "mod_a.sv,mod_c.sv",
            ]
        )
        assert args.scope == "mod_a.sv,mod_c.sv"


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_shows_fusesoc_setup_without_resolving(
        self,
        tmp_path: Path,
        state_file: Path,
        capsys,
    ):
        """Dry-run previews ``fusesoc run --setup`` + ``make`` without resolving.

        The preview is sourced from a cheap ``.core`` YAML read; patching
        ``resolve_target`` to fail proves dry-run never invokes fusesoc.
        """
        from booley import fusesoc_registry

        (tmp_path / "lint.core").write_text(
            "CAPI=2:\nname: ::lint_demo:0\ntargets:\n  lite:\n    flow: lint\n"
            "    flow_options:\n      tool: verilator\n",
            encoding="utf-8",
        )
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "lite", "--dry-run"])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=AssertionError("dry-run must not resolve (run fusesoc)"),
        ):
            result = flow._run()
        assert result.exit_code == 0
        assert "Dry run" in result.report_text
        data = json.loads(capsys.readouterr().out)
        cmd = data["lite"]
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        assert "run --build-root" in script and "--setup" in script
        assert "--target lite" in script
        assert "lint_demo" in script  # the resolved vlnv from the .core
        assert "make -C" in script

    def test_dry_run_unknown_target_reports_error_inline(
        self,
        tmp_path: Path,
        state_file: Path,
        capsys,
    ):
        """A config with no ``.core`` Target yields a clean ERROR, not a crash."""
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "lite", "--dry-run"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["lite"][0].startswith("ERROR: lint dry-run:")


# ---------------------------------------------------------------------------
# Per-config criterion setting
# ---------------------------------------------------------------------------


class TestCriterionSetting:
    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_sets_criterion_per_config(self, mock_cmd, mock_exec, state_file: Path):
        """Criterion lint_clean_<config> is set per config."""
        # No warnings => clean
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=1.0,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
            ]
        )
        flow.read_state()
        flow._run()

        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lite") is True
        assert st.is_met("lint_clean_full") is True

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_criterion_false_when_warnings(self, mock_cmd, mock_exec, state_file: Path):
        """Criterion is False when config has warnings."""
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:1:1: unused\n",
            stderr="",
            timed_out=False,
            duration_s=1.0,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        flow.read_state()
        flow._run()

        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lite") is False

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_hard_error_is_not_clean_pass(self, mock_cmd, mock_exec, state_file: Path, capsys):
        """QA-7: a non-zero rc (Verilator ``%Error``) must fail the gate.

        ``%Error`` lines aren't ``%Warning`` lines, so parse_warnings finds
        zero warnings — before the fix that scored as a clean PASS with the
        criterion satisfied, green-lighting RTL that doesn't even elaborate.

        Verilator ran and rejected the design, so the verdict is a design FAIL
        rather than a Flow ERROR, matching `elaborate` on the same source
        (F-29). The gate must be unsatisfied either way.
        """
        mock_exec.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr=(
                "%Error: src/core/biriscv_alu.v:184:39: Can't find definition "
                "of variable: 'missing_operand_zzz'\n"
                "%Error: Exiting due to 1 error(s)\n"
            ),
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        captured = capsys.readouterr()
        assert "RESULT: FAIL" in captured.out
        assert "RESULT: PASS" not in captured.out
        assert "missing_operand_zzz" in captured.out
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lite") is False


class TestWarningsOnlyFatalExit:
    """Verilator's default warnings-are-fatal exit must not defeat the knob.

    A warnings-only run exits rc=2 with the location-less "%Error: Exiting
    due to N warning(s)" epilogue as its only %Error line. Grading that a
    hard failure made [flows.lint].warnings_as_errors=false inert on the
    builtin path (C910 re-port finding; the adapter-era QA-5 lesson).
    """

    _WARNINGS_ONLY_OUTPUT = (
        "%Warning-IMPLICIT: rtl/ct_l2c_wb.v:251:25: Signal definition not "
        "found, creating implicitly: 'rfifo_full'\n"
        "%Error: Exiting due to 44 warning(s)\n"
    )

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_non_blocking_knob_passes_warnings_only_fatal_exit(
        self, mock_cmd, mock_exec, state_file: Path, capsys
    ):
        mock_exec.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr=self._WARNINGS_ONLY_OUTPUT,
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        with patch(
            "booley.flows.lint._lint_warnings_as_errors",
            return_value=False,
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "non-blocking" in captured.out
        # The criterion still records the truth: warnings mean not clean.
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lite") is False

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_default_knob_still_fails_warnings_only_run(
        self, mock_cmd, mock_exec, state_file: Path
    ):
        mock_exec.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr=self._WARNINGS_ONLY_OUTPUT,
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_real_errors_still_fail_regardless_of_knob(
        self, mock_cmd, mock_exec, state_file: Path
    ):
        # A real located %Error precedes the epilogue — stays a design FAIL
        # even with the non-blocking knob (it only covers warnings).
        mock_exec.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr=(
                "%Error: rtl/x.v:1:1: Can't find definition of variable: 'zzz'\n"
                "%Error: Exiting due to 1 error(s)\n"
            ),
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        with patch(
            "booley.flows.lint._lint_warnings_as_errors",
            return_value=False,
        ):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE


class TestErrorVsFailTaxonomy:
    """F-29: exit 2 means the linter could not run; exit 1 means the RTL failed.

    The same undeclared identifier used to grade ERROR under `lint` and FAIL
    under `elaborate`. Now a compiler diagnostic is a design FAIL in both, and
    exit 2 is reserved for a linter that never reached a verdict.
    """

    def test_missing_linter_binary_is_an_eda_tool_error(self):
        from booley.flows.lint import _errored_verdict

        result = LintConfigResult(target="lint_style", returncode=127)
        result.error = "verible-verilog-lint is not installed"
        result.error_is_eda_tool_failure = True

        exit_code, summary = _errored_verdict([result])
        assert exit_code == EXIT_ERROR
        assert "RESULT: ERROR" in summary

    def test_rejected_design_is_a_failure(self):
        from booley.flows.lint import _errored_verdict

        result = LintConfigResult(target="lite", returncode=2)
        result.error = "%Error: can't find definition of variable 'zzz'"

        exit_code, summary = _errored_verdict([result])
        assert exit_code == EXIT_FAILURE
        assert "RESULT: FAIL" in summary

    def test_a_single_eda_tool_failure_decides_a_mixed_run(self):
        """An unusable linter makes the other Targets' verdicts untrustworthy."""
        from booley.flows.lint import _errored_verdict

        design = LintConfigResult(target="lite", returncode=2)
        design.error = "%Error: syntax"
        broken = LintConfigResult(target="lint_style", returncode=127)
        broken.error = "not installed"
        broken.error_is_eda_tool_failure = True

        exit_code, _summary = _errored_verdict([design, broken])
        assert exit_code == EXIT_ERROR

    def test_timeout_is_an_eda_tool_error_not_a_design_failure(self, tmp_path: Path):
        """A timeout reached no verdict about the RTL at all."""
        with (
            patch.object(LintFlow, "_execute") as mock_exec,
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["make", "-C", "x"], _stub_resolved("verilator")),
            ),
        ):
            mock_exec.return_value = MagicMock(
                returncode=-1, stdout="", stderr="", timed_out=True, duration_s=99.0
            )
            flow = LintFlow()
            flow.parse_args(["--target", "lite"])
            flow.read_state()
            result = flow._run()
        assert result.exit_code == EXIT_ERROR

    def test_hard_fail_error_carries_run_log_pointer(self, tmp_path: Path, state_file: Path):
        """The classified hard-fail error cites only the FIRST error line; the
        full linter output is already persisted as run.log right before the
        classification, so the error must point at it (benchmark finding:
        agents shelled out to recover the rest of the diagnostics)."""
        with (
            patch.object(LintFlow, "_execute") as mock_exec,
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["make", "-C", "x"], _stub_resolved("verilator")),
            ),
        ):
            mock_exec.return_value = MagicMock(
                returncode=2,
                stdout="",
                stderr=(
                    "%Error: rtl/x.v:1:1: Can't find definition of variable: 'zzz'\n"
                    "%Error: rtl/x.v:9:5: second diagnostic the first line omits\n"
                ),
                timed_out=False,
                duration_s=0.5,
            )
            flow = LintFlow()
            flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
            flow.read_state()
            cr = flow._run_lint_target("lite")

        pointer = ".booley_project/.runtime/edalize/lint/lite/run.log"
        assert cr.error.startswith("%Error: rtl/x.v:1:1")
        assert cr.error.endswith(f"(full log: {pointer})")
        # The pointer is honest: THIS invocation wrote that log, in full.
        log_text = (tmp_path / pointer).read_text(encoding="utf-8")
        assert "second diagnostic" in log_text

    def test_previous_runs_log_is_erased_before_the_run(self, tmp_path: Path, state_file: Path):
        """F-26: lint's run.log is only written at the END of a run, so it is
        claimed (truncated to a run header) at the start — a tail during the
        run must never show the previous run's findings."""
        pointer = tmp_path / ".booley_project/.runtime/edalize/lint/lite/run.log"
        pointer.parent.mkdir(parents=True)
        pointer.write_text("%Warning: stale finding from an older run\n", encoding="utf-8")

        flow = LintFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        seen: dict[str, str] = {}

        def _prepare(target: str):
            # Read the log back mid-run: by prepare time it must be claimed.
            seen["mid_run"] = pointer.read_text(encoding="utf-8")
            return ["make", "-C", "x"], _stub_resolved("verilator")

        with (
            patch.object(LintFlow, "_execute") as mock_exec,
            patch.object(LintFlow, "_prepare_lint_command", side_effect=_prepare),
        ):
            mock_exec.return_value = MagicMock(
                returncode=0, stdout="", stderr="", timed_out=False, duration_s=0.1
            )
            flow._run_lint_target("lite")

        assert "stale finding" not in seen["mid_run"]
        assert seen["mid_run"].startswith("[BOOLEY RUN_LOG] ")
        assert "flow=lint target=lite" in seen["mid_run"]

    def test_hard_fail_pointer_omitted_when_log_write_failed(
        self, tmp_path: Path, state_file: Path
    ):
        """No run.log on disk → no pointer appended (never cite a file that
        does not exist)."""
        flow = LintFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with (
            patch.object(LintFlow, "_execute") as mock_exec,
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["make", "-C", "x"], _stub_resolved("verilator")),
            ),
            patch("booley.flows.lint.write_run_log", side_effect=OSError("disk full")),
        ):
            mock_exec.return_value = MagicMock(
                returncode=2,
                stdout="",
                stderr="%Error: rtl/x.v:1:1: Can't find definition of variable: 'zzz'\n",
                timed_out=False,
                duration_s=0.5,
            )
            cr = flow._run_lint_target("lite")

        assert "full log:" not in cr.error


# ---------------------------------------------------------------------------
# Full run (mocked subprocess)
# ---------------------------------------------------------------------------


class TestFullRun:
    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_clean_run(self, mock_cmd, mock_exec, state_file: Path, capsys):
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        flow.read_state()
        result = flow._run()
        assert result.exit_code == 0
        captured = capsys.readouterr()
        assert "RESULT: PASS" in captured.out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_warnings_exit_1(self, mock_cmd, mock_exec, state_file: Path, capsys):
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout=SAMPLE_VERILATOR_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=1.0,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        flow.read_state()
        result = flow._run()
        assert result.exit_code == 1
        captured = capsys.readouterr()
        assert "RESULT: WARN" in captured.out

    def test_summary_points_at_report_on_warn(self):
        """WARN summary references lint_report.json so agents stay in the Flow."""
        warnings = [LintWarning("UNUSEDSIGNAL", "f.sv", 1, 5, "unused 'x'", "lite")]
        report_path = Path("/runtime/flow-reports/lint_report.json")
        exit_code, summary = LintFlow._build_summary(warnings, report_path)
        assert exit_code == EXIT_FAILURE
        assert str(report_path) in summary
        assert "rule/file:line/message" in summary

    def test_summary_no_pointer_without_report(self):
        """No report dir (Interactive Mode) вЂ” summary stays count-only."""
        warnings = [LintWarning("UNUSEDSIGNAL", "f.sv", 1, 5, "unused 'x'", "lite")]
        exit_code, summary = LintFlow._build_summary(warnings, None)
        assert exit_code == EXIT_FAILURE
        assert "lint_report.json" not in summary

    def test_summary_pass_has_no_pointer(self):
        """Clean run never references the report."""
        exit_code, summary = LintFlow._build_summary([], Path("x.json"))
        assert exit_code == EXIT_SUCCESS
        assert summary == "RESULT: PASS"

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_multi_config_dedup(self, mock_cmd, mock_exec, state_file: Path, capsys):
        """Same warning from two configs counts once."""
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:42:5: unused 'x'\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
            ]
        )
        flow.read_state()
        result = flow._run()
        captured = capsys.readouterr()
        assert "1 unique in-scope warning" in captured.out
        assert result.detail.get("total_warnings") == 1

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_scope_filtering_in_run(self, mock_cmd, mock_exec, state_file: Path, capsys):
        """--scope filters warnings to matching files."""
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout=(
                "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: unused\n"
                "%Warning-WIDTH: rtl/mod_c.sv:10:1: width mismatch\n"
            ),
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--scope",
                "mod_a",
            ]
        )
        flow.read_state()
        flow._run()
        captured = capsys.readouterr()
        # Only 1 warning in scope
        assert "1 unique in-scope warning" in captured.out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_scope_filtering_sets_clean_criterion(
        self, mock_cmd, mock_exec, state_file: Path, capsys
    ):
        """Out-of-scope baseline warnings must not keep a scoped ticket dirty."""
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="%Warning-WIDTH: rtl/baseline.sv:10:1: width mismatch\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite", "--scope", "rtl/ticket.sv"])
        flow.read_state()

        result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert "0 unique in-scope warnings" in capsys.readouterr().out
        state = DevelopmentState.load(state_file)
        assert state.is_met("lint_clean_lite") is True


# ---------------------------------------------------------------------------
# Structured report
# ---------------------------------------------------------------------------


class TestStructuredReport:
    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_report_written(self, mock_cmd, mock_exec, state_file: Path, tmp_path: Path):
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:1:1: unused\n",
            stderr="",
            timed_out=False,
            duration_s=1.0,
        )
        report_dir = tmp_path / "reports"
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--report-dir",
                str(report_dir),
            ]
        )
        flow.read_state()
        flow._run()

        report_path = report_dir / "lint_report.json"
        assert report_path.exists()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["flow"] == "lint"
        assert data["targets"] == ["lite"]
        assert data["total_warnings"] == 1
        assert data["passed"] is False
        assert len(data["warnings"]) == 1
        assert data["warnings"][0]["rule"] == "UNUSEDSIGNAL"
        # The parsed warnings carry rule/file:line/message; the linter's raw
        # output (banner, include resolution, the lines around a diagnostic)
        # only exists in run.log, so the report has to name it.
        assert data["target_results"][0]["log"].endswith("run.log")
        assert data["artifacts"]["report"].endswith("lint_report.json")
        assert data["artifacts"]["log_lite"].endswith("run.log")

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_report_clean(self, mock_cmd, mock_exec, state_file: Path, tmp_path: Path):
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        report_dir = tmp_path / "reports"
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
                "--report-dir",
                str(report_dir),
            ]
        )
        flow.read_state()
        flow._run()

        data = json.loads((report_dir / "lint_report.json").read_text(encoding="utf-8"))
        assert data["passed"] is True
        assert data["total_warnings"] == 0
        assert data["targets"] == ["lite", "full"]


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


class TestConfigDiscovery:
    def test_split_comma_separated(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full,combo",
            ]
        )
        assert flow._get_targets() == ["lite", "full", "combo"]

    def test_single_config(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        assert flow._get_targets() == ["lite"]

    def test_empty_target_returns_nothing(self, state_file: Path, monkeypatch):
        """ADR 0030: empty --target no longer sweeps every Target — with no
        [flows.lint].default_target configured it returns [] and the caller refuses."""
        from booley.flows import lint as lint_mod

        monkeypatch.setattr(lint_mod, "resolve_flow_default_target", lambda _flow, _wd: "")
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "",
            ]
        )
        assert flow._get_targets() == []

    def test_empty_target_falls_back_to_configured(self, state_file: Path, monkeypatch):
        """ADR 0030: empty --target falls back to [flows.lint].default_target."""
        from booley.flows import lint as lint_mod

        monkeypatch.setattr(lint_mod, "resolve_flow_default_target", lambda _flow, _wd: "cfg_lint")
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "",
            ]
        )
        assert flow._get_targets() == ["cfg_lint"]

    def test_run_consults_config_fallback_before_refusing(self, monkeypatch, tmp_path):
        """Interactive Mode, no --target, [flows.lint].default_target configured: _run
        must apply the config fallback BEFORE _validate_interactive_args, or the
        documented fallback is unreachable from the CLI ('lint: --target is
        required' despite a configured Target)."""
        from booley.flows import lint as lint_mod

        monkeypatch.setattr(lint_mod, "resolve_flow_default_target", lambda _flow, _wd: "cfg_lint")
        flow = LintFlow()
        flow.parse_args(["--target", "", "--work-dir", str(tmp_path)])

        class _PastValidationError(Exception):
            pass

        # _get_targets runs only after the validation gate — reaching it proves
        # the fallback satisfied the gate.
        monkeypatch.setattr(
            flow, "_get_targets", lambda: (_ for _ in ()).throw(_PastValidationError())
        )
        with pytest.raises(_PastValidationError):
            flow._run()


# ---------------------------------------------------------------------------
# Verible Targets (ADR 0033) — EDA-tool-keyed parsing + verdict semantics
# ---------------------------------------------------------------------------

SAMPLE_VERIBLE_OUTPUT = """\
rtl/top.sv:4:11: Interface names must use lower_snake_case naming convention. [interface-name-style]
rtl/top.sv:10:1-5: Remove trailing spaces. [no-trailing-spaces]
rtl/other.sv:2:3: Explicitly define a storage type for every parameter. [explicit-parameter-storage-type]
"""

SAMPLE_VERIBLE_PARSE_ERROR = """\
rtl/top.sv:3:1: syntax error at token "endmodule"
"""

# Two Verible lint Targets so cross-target dedup is exercised on the Verible
# parser path; the cheap .core read is what routes parsing to it.
_VERIBLE_CORE_TEXT = """\
CAPI=2:
name: ::style_demo:0
filesets:
  rtl:
    files:
      - rtl/top.sv: {file_type: systemVerilogSource}
targets:
  default:
    filesets: [rtl]
  lint_style:
    flow: lint
    flow_options:
      tool: verible
    filesets: [rtl]
    toplevel: top
  lint_style_alt:
    flow: lint
    flow_options:
      tool: verible
    filesets: [rtl]
    toplevel: top
"""


class TestVeribleParsing:
    def test_parse_basic_findings(self):
        warnings = parse_verible_warnings(SAMPLE_VERIBLE_OUTPUT, "lint_style")
        assert len(warnings) == 3
        assert warnings[0].rule == "interface-name-style"
        assert warnings[0].file == "rtl/top.sv"
        assert warnings[0].line == 4
        assert warnings[0].col == 11
        assert warnings[0].target == "lint_style"

    def test_parse_column_range_keeps_leading_col(self):
        warnings = parse_verible_warnings(SAMPLE_VERIBLE_OUTPUT, "lint_style")
        ranged = [w for w in warnings if w.rule == "no-trailing-spaces"]
        assert len(ranged) == 1
        assert ranged[0].line == 10
        assert ranged[0].col == 1

    def test_parse_error_lines_are_not_findings(self):
        """A syntax-error line has no [rule] — it must not count as a warning
        (it is the rc!=0 / EDA-tool-error signal, QA-7)."""
        assert parse_verible_warnings(SAMPLE_VERIBLE_PARSE_ERROR, "lint_style") == []

    def test_dedup_key_shared_with_verilator_shape(self):
        w = parse_verible_warnings(SAMPLE_VERIBLE_OUTPUT, "t")[0]
        assert w.dedup_key == ("interface-name-style", "rtl/top.sv", 4)


class TestVeribleTargets:
    """Full-run semantics for Targets whose flow_options.eda_tool is verible."""

    def _flow(self, tmp_path: Path, targets: str, extra: list[str] | None = None) -> LintFlow:
        (tmp_path / "style_demo.core").write_text(_VERIBLE_CORE_TEXT, encoding="utf-8")
        flow = LintFlow()
        flow.parse_args(
            ["--work-dir", str(tmp_path), "--target", targets, *(extra or [])],
        )
        flow.read_state()
        return flow

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_findings_with_rc0_are_warn_not_error(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """--parse_fatal without --lint_fatal: findings arrive with rc 0 and
        must score WARN with the criterion unmet (ADR 0033 decision 5)."""
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout=SAMPLE_VERIBLE_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "RESULT: WARN" in capsys.readouterr().out
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lint_style") is False

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_clean_run_is_pass(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: PASS" in capsys.readouterr().out
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lint_style") is True

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_parse_error_is_error_never_pass(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """The QA-7 trap: a parse failure yields zero findings; without the
        rc!=0 branch it would score as a clean PASS.

        The linter ran and rejected the source, so this is a design FAIL, not
        a Flow ERROR — the same grading `elaborate` gives the identical source
        (F-29). What must never happen is a PASS."""
        mock_exec.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr=SAMPLE_VERIBLE_PARSE_ERROR,
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        out = capsys.readouterr().out
        assert "RESULT: FAIL" in out
        assert "RESULT: PASS" not in out
        assert "syntax error" in out
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lint_style") is False

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_stale_image_names_rebuild_fix(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """An image predating the Verible binary must name cause + fix
        (ADR 0033 decision 8), not a generic spawn failure."""
        mock_exec.return_value = MagicMock(
            returncode=127,
            stdout="",
            stderr="make: verible-verilog-lint: No such file or directory\n",
            timed_out=False,
            duration_s=0.1,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_ERROR
        out = capsys.readouterr().out
        assert "rebuild the image" in out.lower()
        assert "predates Verible support" in out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_multi_target_cross_dedup(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """The (rule, file, line) dedup key works unchanged for Verible rules."""
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="rtl/top.sv:4:11: Interface names. [interface-name-style]\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style,lint_style_alt")
        result = flow._run()
        assert "1 unique in-scope warning" in capsys.readouterr().out
        assert result.detail.get("total_warnings") == 1

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_report_carries_verible_rule_names(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
    ):
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout=SAMPLE_VERIBLE_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        report_dir = tmp_path / "reports"
        flow = self._flow(tmp_path, "lint_style", ["--report-dir", str(report_dir)])
        flow._run()
        data = json.loads(
            (report_dir / "lint_report.json").read_text(encoding="utf-8"),
        )
        assert data["passed"] is False
        assert data["total_warnings"] == 3
        rules = {w["rule"] for w in data["warnings"]}
        assert "interface-name-style" in rules

    def test_dry_run_shape_matches_verilator_path(
        self,
        tmp_path: Path,
        state_file: Path,
        capsys,
    ):
        """A3: a Verible Target's --dry-run shows the same
        ``fusesoc run --setup && make`` preview shape, never resolving."""
        from booley import fusesoc_registry

        (tmp_path / "style_demo.core").write_text(_VERIBLE_CORE_TEXT, encoding="utf-8")
        flow = LintFlow()
        flow.parse_args(
            ["--work-dir", str(tmp_path), "--target", "lint_style", "--dry-run"],
        )
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=AssertionError("dry-run must not resolve (run fusesoc)"),
        ):
            result = flow._run()
        assert result.exit_code == 0
        data = json.loads(capsys.readouterr().out)
        cmd = data["lint_style"]
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        assert "run --build-root" in script and "--setup" in script
        assert "--target lint_style" in script
        assert "style_demo" in script
        assert "make -C" in script


# ---------------------------------------------------------------------------
# Execution selection (ADR 0037) — the backend/venue split
# ---------------------------------------------------------------------------


def _write_lint_venue_config(tmp_path: Path, body: str) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    (project_dir / "booley.toml").write_text(body, encoding="utf-8")


class TestExecutionSelection:
    def test_command_runs_in_session_runtime(self, state_file: Path, tmp_path: Path):
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "lite"])
        flow.read_state()
        assert flow._resolve_job_class() is None

    def test_combined_backend_is_a_hard_migration(self, state_file: Path, tmp_path: Path):
        _write_lint_venue_config(tmp_path, '[flows.lint]\nbackend = "builtin-sandbox"\n')
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "lite"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "retired" in result.report_text
        assert "Session Runtime" in result.report_text

    def test_verible_missing_message_names_runtime(self):
        from booley.flows.lint import _verible_missing_msg

        assert "Session Runtime" in _verible_missing_msg()


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_conversion(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--timeout",
                "60000",
            ]
        )
        assert flow._get_timeout() == 60

    def test_default_timeout(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        assert flow._get_timeout() == 120

    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    @patch.object(LintFlow, "_execute")
    def test_timeout_reported_as_error(self, mock_exec, mock_cmd, state_file: Path, capsys):
        mock_exec.return_value = MagicMock(
            returncode=-1,
            stdout="",
            stderr="",
            timed_out=True,
            duration_s=120.0,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        flow.read_state()
        flow._run()
        captured = capsys.readouterr()
        assert "ERROR" in captured.out


# ---------------------------------------------------------------------------
# Observability: EDA tool identity, file coverage, flow check, per-run reports
# ---------------------------------------------------------------------------


class TestLintObservability:
    """The lint output must say WHAT ran and what it covered.

    The linter comes from the resolved Target, so without these signals a run
    can silently use the wrong EDA tool, lint a fileset that excludes the design's
    own toplevel, or clobber the previous run's report.
    """

    _PROC_CLEAN: ClassVar[dict] = {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "duration_s": 0.5,
    }

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_console_and_report_name_the_linter(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        mock_exec.return_value = MagicMock(**self._PROC_CLEAN)
        report_dir = tmp_path / "reports"
        flow = LintFlow()
        flow.parse_args(["--target", "lite", "--report-dir", str(report_dir)])
        flow.read_state()
        result = flow._run()
        out = capsys.readouterr().out
        assert "[verible]" in out
        assert result.detail["eda_tools"] == {"lite": "verible"}
        assert "linter: verible" in result.display_lines
        data = json.loads((report_dir / "lint_report.json").read_text(encoding="utf-8"))
        assert data["eda_tools"] == {"lite": "verible"}
        assert data["target_results"][0]["eda_tool"] == "verible"

    @patch.object(LintFlow, "_execute")
    def test_toplevel_excluded_from_fileset_hard_fails(
        self,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """A Target whose fileset excludes its own toplevel hard-fails.

        The run would lint nothing real and pass vacuously (a false green on
        the ``lint_clean`` gate), so this is an ERROR — no verdict about the
        design was reached — and the lint make never runs. Upgraded from the
        earlier loud-WARN, matching the ADR 0026 doctor hard-fail spirit.
        """
        from booley import fusesoc_registry
        from booley.fusesoc_registry import ResolvedFile

        (tmp_path / "other.sv").write_text("module other; endmodule\n", encoding="utf-8")
        resolved = fusesoc_registry.ResolvedTarget(
            name="style",
            vlnv="::demo:0",
            toplevel="design_top",  # declared by NO linted source
            eda_tool="verible",
            files=(ResolvedFile(name="other.sv", file_type="systemVerilogSource"),),
            parameters={},
            build_root=tmp_path,
            edam_path=tmp_path / "demo.eda.yml",
        )
        mock_exec.return_value = MagicMock(**self._PROC_CLEAN)
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        with patch.object(
            LintFlow,
            "_prepare_lint_command",
            return_value=(["make", "-C", "x"], resolved),
        ):
            result = flow._run()
        out = capsys.readouterr().out
        assert result.exit_code == EXIT_ERROR
        assert "toplevel 'design_top' is not declared by any fileset file" in out
        assert "vacuous" in out
        # The lint make never ran — its verdict would be untrustworthy.
        mock_exec.assert_not_called()
        # The gate records the truth: not clean.
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lite") is False

    @patch.object(LintFlow, "_execute")
    def test_toplevel_in_fileset_stays_silent(
        self,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        from booley import fusesoc_registry
        from booley.fusesoc_registry import ResolvedFile

        (tmp_path / "top.sv").write_text("module design_top; endmodule\n", encoding="utf-8")
        resolved = fusesoc_registry.ResolvedTarget(
            name="style",
            vlnv="::demo:0",
            toplevel="design_top",
            eda_tool="verilator",
            files=(ResolvedFile(name="top.sv", file_type="systemVerilogSource"),),
            parameters={},
            build_root=tmp_path,
            edam_path=tmp_path / "demo.eda.yml",
        )
        mock_exec.return_value = MagicMock(**self._PROC_CLEAN)
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        with patch.object(
            LintFlow,
            "_prepare_lint_command",
            return_value=(["make", "-C", "x"], resolved),
        ):
            flow._run()
        out = capsys.readouterr().out
        assert "toplevel" not in out
        assert "1 files" in out  # coverage count still printed

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow, "_prepare_lint_command", return_value=(["make", "-C", "x"], _stub_resolved())
    )
    def test_non_lint_flow_target_warns(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """[flows.lint].default_target naming a sim Target must not lint silently."""
        (tmp_path / "sim_demo.core").write_text(
            "CAPI=2:\n"
            "name: ::sim_demo:0\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - rtl/top.sv: {file_type: systemVerilogSource}\n"
            "targets:\n"
            "  default:\n"
            "    filesets: [rtl]\n"
            "  smoke_sim:\n"
            "    flow: sim\n"
            "    flow_options:\n"
            "      tool: verilator\n"
            "    filesets: [rtl]\n"
            "    toplevel: top\n",
            encoding="utf-8",
        )
        mock_exec.return_value = MagicMock(**self._PROC_CLEAN)
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "smoke_sim"])
        flow.read_state()
        flow._run()
        out = capsys.readouterr().out
        assert "declares flow 'sim', not" in out

    def test_summary_warnings_not_errors_exits_zero(self):
        """[flows.lint].warnings_as_errors=false: WARN text, exit 0."""
        warnings = [LintWarning("UNUSEDSIGNAL", "f.sv", 1, 5, "unused 'x'", "lite")]
        exit_code, summary = LintFlow._build_summary(warnings, None, warnings_as_errors=False)
        assert exit_code == EXIT_SUCCESS
        assert "RESULT: WARN" in summary
        assert "non-blocking" in summary

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_warnings_as_errors_false_in_run(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ):
        """End-to-end: the knob turns a warnings-only run into exit 0, while
        the lint_clean criterion still records the warning truthfully."""
        from booley.flows import lint as lint_mod

        monkeypatch.setattr(lint_mod, "_lint_warnings_as_errors", lambda _wd: False)
        mock_exec.return_value = MagicMock(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:1:1: unused\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: WARN" in capsys.readouterr().out
        st = DevelopmentState.load(state_file)
        assert st.is_met("lint_clean_lite") is False

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_consecutive_runs_keep_per_run_reports(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """Run N's report must survive run N+1 (numbered invocation copies)."""
        mock_exec.return_value = MagicMock(**self._PROC_CLEAN)
        report_dir = tmp_path / "reports"
        for _ in range(2):
            flow = LintFlow()
            flow.parse_args(["--target", "lite", "--report-dir", str(report_dir)])
            flow.read_state()
            flow._run()
        capsys.readouterr()
        assert (report_dir / "lint_report.json").exists()  # stable latest
        assert (report_dir / "lint" / "1" / "lint_report.json").exists()
        assert (report_dir / "lint" / "2" / "lint_report.json").exists()
