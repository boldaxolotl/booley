"""Tests for the built-in fpga_impl Booley Flow."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.core.boundary import BoundaryError
from booley.dev_support.development_state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.clock_timing import ClockTiming
from booley.flows.fpga.flow import FpgaImplFlow, _vlogdefine_args
from booley.flows.fpga.metrics import FpgaMetrics, _metrics_detail
from booley.flows.implementation_comparison import ImplementationTargetPair
from booley.flows.recipe_evidence import (
    BASELINE_REF_PARAM,
    RECIPE_FINGERPRINT_PARAM,
    RECIPE_SNAPSHOT_PARAM,
)
from booley.fusesoc import fusesoc_registry
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime import job_slots


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


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria({"fpga_impl_ok_default": True})
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    return state_path


def _write_project_config(
    tmp_path: Path,
    *,
    execution_lines: str = "",
) -> None:
    """Write the fixture project config.

    *execution_lines* can inject a retired spelling to exercise the hard
    migration error.
    """
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    (project_dir / "booley.toml").write_text(
        "[sources.rtl]\n"
        'source_dirs = ["rtl"]\n'
        'include_dirs = ["rtl/include"]\n\n'
        "[flows.fpga]\n" + execution_lines,
        encoding="utf-8",
    )
    (project_dir / "configs.toml").write_text(
        '[default]\ntop_module = "dut_top"\ndefines = ["CFG_DEFAULT"]\n',
        encoding="utf-8",
    )
    (tmp_path / "constraints").mkdir()
    (tmp_path / "constraints" / "timing.xdc").write_text("create_clock\n", encoding="utf-8")
    (tmp_path / "rtl" / "include").mkdir(parents=True)
    (tmp_path / "rtl" / "include" / "defs.svh").write_text("`define FOO 1\n", encoding="utf-8")
    (tmp_path / "rtl" / "top.sv").write_text("module dut_top; endmodule\n", encoding="utf-8")
    (tmp_path / "rtl" / "legacy.v").write_text("module legacy; endmodule\n", encoding="utf-8")


def _flow(tmp_path: Path, state_file: Path, *extra_args: str) -> FpgaImplFlow:
    flow = FpgaImplFlow()
    flow.parse_args(
        [
            "--target",
            "default",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
            *extra_args,
        ]
    )
    flow.read_state()
    return flow


def test_relative_ticket_criterion_auto_applies_pinned_baseline(
    tmp_path: Path,
    state_file: Path,
) -> None:
    flow = _flow(tmp_path, state_file)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "rtl.v").write_text("module rtl; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "rtl.v"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    flow.state.init_criteria(
        {"fpga_impl_ok_default": True},
        criterion_params={"fpga_impl_ok_default": {BASELINE_REF_PARAM: base_sha}},
    )

    assert flow._apply_ticket_baseline(["default"]) is None
    assert flow.args.baseline == base_sha


def test_paired_baseline_runs_baseline_target_and_keys_candidate(
    tmp_path: Path,
    state_file: Path,
) -> None:
    flow = _flow(tmp_path, state_file, "--baseline", "v1.0")
    calls: list[str] = []
    metrics = FpgaMetrics(lut_count=10, ff_count=20)

    @contextmanager
    def fake_worktree(_project_root, _ref):
        baseline = tmp_path / ".booley_project" / ".baseline-wt-test"
        baseline.mkdir(parents=True, exist_ok=True)
        yield baseline

    with (
        patch("booley.flows.fpga.flow.baseline_worktree", fake_worktree),
        patch("booley.flows.fpga.flow.git_short_sha", return_value="abc1234"),
        patch.object(
            flow,
            "_run_single_target",
            side_effect=lambda target: calls.append(target) or metrics,
        ),
    ):
        results, short_sha = flow._run_baseline_configs(
            (ImplementationTargetPair("fpga_before", "fpga_after"),)
        )

    assert calls == ["fpga_before"]
    assert short_sha == "abc1234"
    assert list(results) == ["fpga_after"]


def test_changed_fpga_recipe_is_evidence_not_a_rejection(
    tmp_path: Path,
    state_file: Path,
) -> None:
    flow = _flow(tmp_path, state_file)
    baseline_ref = "a" * 40
    baseline_snapshot = {
        "flow": "fpga",
        "target": "default",
        "flow_options": {"part": "old"},
    }
    current_snapshot = {
        "flow": "fpga",
        "target": "default",
        "flow_options": {"part": "new"},
    }
    flow.state.init_criteria(
        {"fpga_impl_ok_default": True},
        criterion_params={
            "fpga_impl_ok_default": {
                "lut_count_increase_at_most": 10,
                BASELINE_REF_PARAM: baseline_ref,
                RECIPE_FINGERPRINT_PARAM: "baseline-recipe",
                RECIPE_SNAPSHOT_PARAM: baseline_snapshot,
            }
        },
    )
    flow._baseline_full_sha = baseline_ref
    base = FpgaMetrics(
        lut_count=100,
        ff_count=50,
        wns_ns=0.2,
        whs_ns=0.1,
        recipe_snapshot=baseline_snapshot,
        recipe_fingerprint="baseline-recipe",
    )
    cur = FpgaMetrics(
        lut_count=105,
        ff_count=52,
        wns_ns=0.2,
        whs_ns=0.1,
        recipe_snapshot=current_snapshot,
        recipe_fingerprint="current-recipe",
    )

    flow._set_config_criterion("default", cur, base, baseline_ref[:12])

    entry = flow.state.criteria["fpga_impl_ok_default"]
    assert entry.met is True
    assert entry.detail["recipe_comparison"]["flow"] == "fpga"
    assert entry.detail["recipe_comparison"]["changes"] == [
        {"path": "flow_options.part", "before": "old", "after": "new"}
    ]


def _collect_flow(work_dir: Path) -> FpgaImplFlow:
    """A minimally-parsed Flow whose ``work_dir`` anchors report relpaths.

    ``_artifact_dirs`` posix-relativizes each directory against
        ``self.args.work_dir``, so a bare ``FpgaImplFlow()`` (no ``parse_args``)
        would raise on ``self.args``. This gives just enough for that method.
    """
    flow = FpgaImplFlow()
    flow.parse_args(["--target", "default", "--work-dir", str(work_dir)])
    return flow


def _patch_fpga_resolve(tmp_path: Path):
    """Patch the design-description resolver the builtin path now uses.

    ADR 0022 (decision 4) replaced ``FpgaImplFlow._resolve_sources`` (configs.toml
    + source globbing) with ``fusesoc_registry.try_resolve_target``: the builtin
    dry-run ``_resolve_fpga_summary`` resolves the ``.core`` Target and splits its
    sources via ``_split_resolved_sources``. These dry-run tests therefore mock
    ``try_resolve_target`` (a non-fusesoc fake EDAM) instead of the deleted
    ``_resolve_sources``; part and XDC come from the Target.
    """
    return patch.object(
        fusesoc_registry,
        "try_resolve_target",
        side_effect=lambda config="default", **k: _fake_fpga_resolved(
            tmp_path,
            config=config,
        ),
    )


# ---------------------------------------------------------------------------
# FuseSoC resolution stub (ADR 0022 Phase 2)
# ---------------------------------------------------------------------------


def _fake_fpga_resolved(
    work_dir: Path,
    *,
    config: str = "default",
    toplevel: str = "dut_top",
) -> ResolvedTarget:
    """A ResolvedTarget shaped like a real vivado-flow EDAM.

    ``build_root`` is *work_dir* so each ``ResolvedFile.absolute()`` lands on the
    real file ``_write_project_config`` created — the Edalize ``configure()`` in
    the integration tests then sees genuine sources. The header is an
    ``is_include_file`` (surfaced as an include dir, not a source); the TB file
    is ``tb``-tagged and excluded from the vivado source set.
    """
    build_root = Path(work_dir)
    files = (
        ResolvedFile(
            name="rtl/include/defs.svh",
            file_type="systemVerilogSource",
            is_include=True,
        ),
        ResolvedFile(name="rtl/top.sv", file_type="systemVerilogSource"),
        ResolvedFile(name="rtl/legacy.v", file_type="verilogSource"),
        # ADR 0031: XDC travels with the Target as a file_type:xdc fileset (the
        # sole source of truth now that the legacy [flows.fpga].xdc key is
        # gone). TB-tagged constraints would be excluded, like sdc_files.
        ResolvedFile(name="constraints/timing.xdc", file_type="xdc"),
        ResolvedFile(name="tb/tb_top.sv", file_type="systemVerilogSource", tags=("tb",)),
    )
    params = {
        "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8},
        "CFG_DEFAULT": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
    }
    return ResolvedTarget(
        name=config,
        vlnv="::fpga_demo:0",
        toplevel=toplevel,
        eda_tool="vivado",
        flow_options={"tool": "vivado", "part": "xc7a200tfbg484-1"},
        files=files,
        parameters=params,
        build_root=build_root,
        edam_path=build_root / "fpga_demo_0.eda.yml",
    )


# Captured before the autouse fixture below patches the attribute, so the
# real-fusesoc e2e can reach the genuine resolver.
_REAL_RESOLVE = fusesoc_registry.resolve_target


@pytest.fixture(autouse=True)
def _stub_fusesoc_resolution(tmp_path: Path):
    """Default every test's FuseSoC resolution to a fake vivado-flow EDAM.

    The Edalize-path tests mock only the boundary executor; without this,
    ``_prepare_fpga_command`` would shell out to a real ``fusesoc run --setup``
    against a project with no ``.core``. Tests that exercise resolution itself
    re-patch ``resolve_target`` inside a ``with`` block (that inner patch wins);
    the e2e uses ``_REAL_RESOLVE``.
    """
    with patch.object(
        fusesoc_registry,
        "resolve_target",
        side_effect=lambda target="default", **k: _fake_fpga_resolved(tmp_path, config=target),
    ):
        yield


def test_dry_run_uses_project_fpga_config(tmp_path: Path, state_file: Path) -> None:
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file, "--dry-run")

    with _patch_fpga_resolve(tmp_path):
        result = flow._run()

    assert result.exit_code == EXIT_SUCCESS
    assert "part=xc7a200tfbg484-1" in result.report_text
    assert "xdc=" in result.report_text


def test_dry_run_resolves_sources_from_work_dir(
    tmp_path: Path,
    state_file: Path,
) -> None:
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file, "--dry-run")

    # The dry-run sources its source counts from the resolved .core Target
    # (_resolve_fpga_summary → try_resolve_target → _split_resolved_sources): one .sv
    # (rtl/top.sv) and one .v (rtl/legacy.v), excluding the include header & TB.
    with _patch_fpga_resolve(tmp_path):
        result = flow._run()

    assert result.exit_code == EXIT_SUCCESS
    assert "sv_files=1 v_files=1" in result.report_text


def test_negative_wns_fails_fpga_criterion(
    tmp_path: Path,
    state_file: Path,
) -> None:
    """A routed design that misses timing (negative WNS) fails the criterion.

    Decoupled from a live Edalize ``configure()``: the project materialization
    seam (``_prepare_fpga_command``) is stubbed and ``parse_fpga_reports`` is
    fed a negative-WNS metric dict, so this unit-tests the metric→verdict mapping
    on the edalize path without a real Vivado/edalize install.
    """
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file)

    parsed = {
        "status": "pass",  # route completed; the FAIL is the negative slack
        "lut_count": 1200,
        "ff_count": 700,
        "wns_ns": -0.01,
        "whs_ns": 0.1,
    }
    with (
        patch.object(
            FpgaImplFlow,
            "_prepare_fpga_command",
            return_value=(["make", "-C", "x"], tmp_path),
        ),
        patch("booley.flows.fpga.edam.parse_fpga_reports", return_value=parsed),
        patch.object(
            flow,
            "_execute_boundary",
            return_value=SubprocessResult(returncode=0, stdout="routed\n"),
        ),
    ):
        result = flow._run()

    assert result.exit_code == EXIT_FAILURE
    state = DevelopmentState.load(state_file)
    entry = state.criteria["fpga_impl_ok_default"]
    assert entry.met is False
    assert entry.detail["timing_met"] is False


def test_enable_out_of_context_appends_synth_property(tmp_path: Path) -> None:
    """The OOC patch lands the -mode out_of_context property in the project tcl
    exactly once, even when applied to an already-patched materialization."""
    from booley.flows.fpga import edam as fpga_edam

    tcl = tmp_path / "fpga_t.tcl"
    tcl.write_text("create_project fpga_t -force\n", encoding="utf-8")

    fpga_edam.enable_out_of_context(tmp_path, "fpga_t")
    fpga_edam.enable_out_of_context(tmp_path, "fpga_t")

    content = tcl.read_text(encoding="utf-8")
    assert content.count("-mode out_of_context") == 1
    assert "STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS" in content


def test_enable_out_of_context_missing_tcl_raises(tmp_path: Path) -> None:
    """A missing project tcl is a hard setup error (surfaces as infra_error)."""
    from booley.flows.fpga import edam as fpga_edam

    with pytest.raises(FileNotFoundError, match="out_of_context"):
        fpga_edam.enable_out_of_context(tmp_path, "fpga_missing")


# ===========================================================================
# Session Runtime execution
# ===========================================================================


class TestFlowEnablement:
    def test_default_runs_in_session_runtime(self, tmp_path: Path, state_file: Path):
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        assert flow._flow_enabled()
        assert flow._resolve_job_class() == job_slots.CLASS_HEAVY


def _patch_edam_build(captured: dict):
    """Patch Vivado materialization for unit tests without a live tool."""

    def fake_build_fpga_edam(**kwargs):
        captured.update(kwargs)
        return {"name": kwargs.get("name", "fpga")}

    def fake_configure(_flow, edam, work_root):
        Path(work_root).mkdir(parents=True, exist_ok=True)
        params = " ".join(f"{name}={value}" for name, value in captured["vlogparams"].items())
        (Path(work_root) / f"{edam['name']}.tcl").write_text(
            f"set_property generic {{{params}}} [get_filesets sources_1]\n",
            encoding="utf-8",
        )

    return (
        patch("booley.flows.fpga.edam.build_fpga_edam", side_effect=fake_build_fpga_edam),
        patch("booley.flows.edam.configure", side_effect=fake_configure),
        patch("booley.flows.fpga.edam.fpga_run_command", return_value=["make", "-C", "x"]),
    )


class TestFpgaResolution:
    def test_resolution_forwards_sources_top_and_defines(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        """The resolved RTL sources, include dirs, top, and typed defines reach
        ``build_fpga_edam`` from the one resolved Target."""
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        captured: dict = {}
        build_p, cfg_p, run_p = _patch_edam_build(captured)

        with (
            build_p,
            cfg_p,
            run_p,
            patch("booley.flows.fpga.flow.validate_top_parameter_intent") as guard,
        ):
            run_cmd, _work_root = flow._prepare_fpga_command("default")
        guard.assert_called_once()

        assert run_cmd == ["make", "-C", "x"]
        assert captured["toplevel"] == "dut_top"  # from resolved.toplevel
        # These are absolute workspace Paths (Edalize relativizes them into
        # the .vc later); compare in POSIX form so a Windows host's backslashes
        # don't fail the suffix checks.
        sv = [p.as_posix() for p in captured["sv_files"]]
        v = [p.as_posix() for p in captured["v_files"]]
        assert any(s.endswith("rtl/top.sv") for s in sv)
        assert any(s.endswith("rtl/legacy.v") for s in v)
        # The include header is surfaced as a dir, never a compiled source.
        assert not any("defs.svh" in s for s in sv + v)
        inc = [p.as_posix() for p in captured["include_dirs"]]
        assert any(d.endswith("rtl/include") for d in inc)
        # Sources are absolute (under work_dir = the EDAM workspace_root).
        assert all(Path(s).is_absolute() for s in sv + v)
        assert "CFG_DEFAULT" in captured["defines"]  # vlogdefine, default true
        assert captured["vlogparams"] == {"WIDTH": 8}
        # Part and XDC both come from the Target and reach
        # build_fpga_edam as a one-element list.
        assert captured["part"] == "xc7a200tfbg484-1"
        # Compare in POSIX form so Windows backslashes don't fail the suffix test.
        xdc = [Path(p).as_posix() for p in captured["xdc_files"]]
        assert any(x.endswith("constraints/timing.xdc") for x in xdc)

    def test_resolution_forwards_target_and_isolated_build_root(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        """resolve_target gets the config name, the project root, and an isolated
        per-variant build dir distinct from the vivado configure() work_root."""
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        captured: dict = {}
        build_p, cfg_p, run_p = _patch_edam_build(captured)
        seen: dict = {}

        def fake_resolve(target, *, project_root, build_root, **kw):
            seen.update(target=target, project_root=project_root, build_root=build_root)
            return _fake_fpga_resolved(tmp_path, config=target)

        with (
            build_p,
            cfg_p,
            run_p,
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fake_resolve,
            ),
        ):
            flow._prepare_fpga_command("default")

        assert seen["target"] == "default"
        assert seen["project_root"] == tmp_path
        # FuseSoC build dir is keyed distinctly so it can't clobber the vivado dir.
        # Compare build_root in POSIX form so the assertion is portable.
        assert seen["build_root"].as_posix().endswith("fpga/default-fusesoc")


# ===========================================================================
# Flow-config trust boundary (plan P2 2026-07-05)
# ===========================================================================


class TestTargetRecipeBoundary:
    """Wrong-typed Target build inputs fail loudly at the trust boundary.

    Same class as the asic ``--sdc``/``timing_engine`` bugs: booley.toml is
    user-authored, so an untyped read leaks a wrong-typed value across the
    host boundary where it fails opaquely (or worse, silently flips behavior —
    a string ``"false"`` is truthy and would *enable* out-of-context mode).
    """

    def test_string_out_of_context_rejected(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        """``out_of_context = "false"`` must raise, not silently enable OOC."""
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        build_p, cfg_p, run_p = _patch_edam_build({})
        resolved = dataclasses.replace(
            _fake_fpga_resolved(tmp_path),
            flow_options={
                "tool": "vivado",  # upstream FuseSoC/Edalize schema field
                "part": "xc7a200tfbg484-1",
                "out_of_context": "false",
            },
        )

        with (
            build_p,
            cfg_p,
            run_p,
            patch.object(fusesoc_registry, "resolve_target", return_value=resolved),
            pytest.raises(
                BoundaryError,
                match="out_of_context",
            ),
        ):
            flow._prepare_fpga_command("default")

    def test_non_string_part_rejected(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        with pytest.raises(ValueError, match=r"part must be a non-empty string"):
            flow._resolve_part({"part": 123})

    def test_xdc_resolves_from_fileset(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        """ADR 0031: the Target's file_type:xdc fileset is the XDC source of
        truth, and its files resolve absolute against the build root."""
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        base = _fake_fpga_resolved(tmp_path)  # already carries constraints/timing.xdc
        resolved = dataclasses.replace(
            base,
            files=(
                *base.files,
                ResolvedFile(name="constraints/pins.xdc", file_type="xdc"),
            ),
        )
        xdc = flow._resolve_xdc_files(resolved, "default")
        assert [p.name for p in xdc] == ["timing.xdc", "pins.xdc"]  # every fileset entry
        assert all(p.is_absolute() for p in xdc)

    def test_missing_xdc_is_hard_error(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        """No file_type:xdc fileset → hard error (ADR 0031): XDC is mandatory and
        its only source is the Target fileset."""
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)
        base = _fake_fpga_resolved(tmp_path)
        resolved = dataclasses.replace(
            base,
            files=tuple(f for f in base.files if f.file_type != "xdc"),
        )
        with pytest.raises(ValueError, match=r"no FPGA constraints"):
            flow._resolve_xdc_files(resolved, "default")

    def test_setup_failure_recorded_as_infra_error(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        """A FuseSoC resolution failure becomes a Flow infrastructure error, not a crash."""
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)

        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=fusesoc_registry.TargetResolutionError("no such target"),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert "infrastructure error" in result.report_text
        state = DevelopmentState.load(state_file)
        assert state.criteria["fpga_impl_ok_default"].met is False

    def test_real_fusesoc_fpga_setup(self, state_file: Path, tmp_path: Path) -> None:  # noqa: PLR0915 — end-to-end test with many sequential setup steps and assertions
        """End-to-end: a real `fusesoc run --setup` leaves a resolved vivado EDAM.

        Proves the RTL sources/top/typed-params resolve, the build dir is
        relocatable, and fpga_impl feeds the resolved filelist + include dir +
        defines into the vivado EDAM builder.
        """
        pytest.importorskip("fusesoc")
        pytest.importorskip("edalize")
        import shutil
        import sys

        work_dir = tmp_path / "proj"
        (work_dir / "rtl" / "include").mkdir(parents=True)
        (work_dir / "tb").mkdir(parents=True)
        (work_dir / "constraints").mkdir(parents=True)
        (work_dir / "rtl" / "include" / "defs.svh").write_text(
            "`define FOO 1\n",
            encoding="utf-8",
        )
        (work_dir / "rtl" / "pkg.sv").write_text(
            "package pkg; endpackage\n",
            encoding="utf-8",
        )
        (work_dir / "rtl" / "dut.sv").write_text(
            "module dut_top #(parameter WIDTH=8)(input logic clk); endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "rtl" / "legacy.v").write_text(
            "module legacy; endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "tb" / "tb_dut.sv").write_text(
            "module tb_dut; dut_top d(.clk(1'b0)); endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "constraints" / "timing.xdc").write_text(
            "create_clock\n",
            encoding="utf-8",
        )
        (work_dir / "fpga_demo.core").write_text(_FPGA_CORE_TEXT, encoding="utf-8")

        # Part and XDC both ride the Target, not booley.toml.
        project_dir = work_dir / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text("[flows.fpga]\n", encoding="utf-8")

        flow = FpgaImplFlow()
        flow.parse_args(
            [
                "--target",
                "fpga",
                "--work-dir",
                str(work_dir),
            ]
        )
        flow.read_state()

        if shutil.which("fusesoc"):
            fusesoc_cmd = list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        else:
            fusesoc_cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

        captured: dict = {}
        build_p, cfg_p, run_p = _patch_edam_build(captured)

        with (
            build_p,
            cfg_p,
            run_p,
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: _REAL_RESOLVE(
                    *a,
                    **{**k, "fusesoc_cmd": fusesoc_cmd},
                ),
            ),
        ):
            flow._prepare_fpga_command("fpga")

        assert captured["toplevel"] == "dut_top"
        # Absolute workspace Paths (Edalize relativizes them downstream);
        # compare in POSIX form so a Windows host's backslashes don't fail.
        sv = [p.as_posix() for p in captured["sv_files"]]
        v = [p.as_posix() for p in captured["v_files"]]
        assert any(s.endswith("rtl/dut.sv") for s in sv)
        assert any(s.endswith("rtl/pkg.sv") for s in sv)
        assert any(s.endswith("rtl/legacy.v") for s in v)
        # Include header surfaces as a dir, TB file is excluded from sources.
        assert not any("defs.svh" in s for s in sv + v)
        assert not any("tb_dut.sv" in s for s in sv + v)
        inc = [p.as_posix() for p in captured["include_dirs"]]
        assert any(d.endswith("rtl/include") for d in inc)
        # The vlogdefine param reached the EDAM defines.
        assert "SYNTH" in captured["defines"]
        assert captured["vlogparams"] == {"WIDTH": 8}
        # Resolved build dir lives under the worktree and is relocatable — no
        # absolute workspace path (in either separator form) leaked into the EDAM.
        edam = next((work_dir / ".booley_project" / ".runtime").rglob("*.eda.yml"))
        edam_text = edam.read_text(encoding="utf-8")
        assert str(work_dir) not in edam_text
        assert work_dir.as_posix() not in edam_text


# ===========================================================================
# Per-clock timing round-trip (Fmax/critical-path are per-clock, no scalars)
# ===========================================================================


class TestPerClockMetrics:
    """FpgaMetrics carries timing as a per_clock map, not flat scalars.

    The legacy top-level ``critical_path_ps``/``fmax_mhz`` were hard-removed
    (a single scalar is meaningless for a multi-clock design); metric detail and
    the parsed-report round-trip must speak per_clock instead.
    """

    def test_metrics_detail_emits_per_clock_not_flat_scalars(self):
        metrics = FpgaMetrics(
            lut_count=100,
            ff_count=50,
            wns_ns=0.25,
            whs_ns=0.1,
            per_clock={
                "clk": ClockTiming(
                    clock="clk",
                    period_ns=10.0,
                    wns_ns=0.25,
                    whs_ns=0.1,
                    critical_path_ps=9750.0,
                    fmax_mhz=102.5641,
                ),
            },
        )
        detail = _metrics_detail(metrics)
        # per_clock is present as the nested sub-metric map...
        assert detail["per_clock"] == {
            "clk": {
                "period_ns": 10.0,
                "wns_ns": 0.25,
                "whs_ns": 0.1,
                "critical_path_ps": 9750.0,
                "fmax_mhz": 102.5641,
            },
        }
        # ...and the removed flat scalars must NOT reappear at the top level.
        assert "critical_path_ps" not in detail
        assert "fmax_mhz" not in detail

    def test_metrics_detail_none_stays_none(self):
        assert _metrics_detail(None) is None

    def test_metrics_from_parsed_reports_round_trips_per_clock(self):
        """The parsed-report dict's per_clock map rebuilds into ClockTiming."""
        raw = {
            "lut_count": 100,
            "ff_count": 50,
            "wns_ns": 0.25,
            "whs_ns": 0.1,
            "per_clock": {
                "clk": {
                    "period_ns": 10.0,
                    "wns_ns": 0.25,
                    "whs_ns": 0.1,
                    "critical_path_ps": 9750.0,
                    "fmax_mhz": 102.5641,
                },
                "clk2": {
                    "period_ns": 5.0,
                    "wns_ns": -0.1,
                    "whs_ns": 0.05,
                    "critical_path_ps": 5100.0,
                    "fmax_mhz": 196.0784,
                },
            },
            "status": "pass",
        }
        metrics = FpgaImplFlow._metrics_from_parsed_reports(raw, elapsed_s=1.5)
        assert set(metrics.per_clock) == {"clk", "clk2"}
        clk = metrics.per_clock["clk"]
        assert isinstance(clk, ClockTiming)
        assert clk.period_ns == pytest.approx(10.0)
        assert clk.critical_path_ps == pytest.approx(9750.0)
        assert clk.fmax_mhz == pytest.approx(102.5641, rel=1e-4)
        assert metrics.per_clock["clk2"].wns_ns == pytest.approx(-0.1)
        # Aggregate whole-design scalars are still carried alongside per_clock.
        assert metrics.wns_ns == pytest.approx(0.25)
        assert metrics.whs_ns == pytest.approx(0.1)

    def test_metrics_from_parsed_reports_no_per_clock_is_empty_map(self):
        """A report with no constrained clock leaves per_clock an empty dict."""
        metrics = FpgaImplFlow._metrics_from_parsed_reports(
            {"lut_count": 10, "ff_count": 5},
            elapsed_s=0.0,
        )
        assert metrics.per_clock == {}


# ===========================================================================
# Typed-parameter mapping (vlogdefine -> Verilog define strings)
# ===========================================================================


class TestVlogdefineArgs:
    def test_bool_true_is_bare_define(self):
        params = {"SYNTH": {"paramtype": "vlogdefine", "default": True}}
        assert _vlogdefine_args(params) == ["SYNTH"]

    def test_value_is_named_define(self):
        params = {"WIDTH": {"paramtype": "vlogdefine", "default": 16}}
        assert _vlogdefine_args(params) == ["WIDTH=16"]

    def test_false_or_absent_is_undefined(self):
        params = {
            "OFF": {"paramtype": "vlogdefine", "default": False},
            "NONE": {"paramtype": "vlogdefine"},
        }
        assert _vlogdefine_args(params) == []

    def test_non_vlogdefine_ignored(self):
        params = {
            "D": {"paramtype": "vlogdefine", "default": True},
            "P": {"paramtype": "vlogparam", "default": 4},
            "X": {"paramtype": "plusarg", "default": "y"},
        }
        assert _vlogdefine_args(params) == ["D"]

    def test_empty_or_none(self):
        assert _vlogdefine_args(None) == []
        assert _vlogdefine_args({}) == []


# A minimal vivado-flow `.core` for the real-fusesoc resolution e2e. Part and
# constraints ride the Target. The header
# is an `is_include_file`; the TB file is `tb`-tagged and excluded from synthesis.
_FPGA_CORE_TEXT = """\
CAPI=2:
name: ::fpga_demo:0
description: fpga_impl slice fixture
filesets:
  rtl:
    files:
      - rtl/include/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
      - rtl/legacy.v: {file_type: verilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
    tags: [tb]
  constraints:
    files:
      - constraints/timing.xdc: {file_type: xdc}
parameters:
  WIDTH: {datatype: int, default: 8, paramtype: vlogparam}
  SYNTH: {datatype: bool, paramtype: vlogdefine, default: true}
targets:
  default:
    filesets: [rtl]
  fpga:
    default_tool: vivado
    flow: generic
    flow_options: {tool: vivado, part: xc7a200tfbg484-1}
    filesets: [rtl, tb, constraints]
    parameters: [WIDTH, SYNTH]
    toplevel: dut_top
"""


# ===========================================================================
# Route-report age gating (stale "cached" result trap) + stderr surfacing
# ===========================================================================


def _write_route_report(work_root: Path, name: str, text: str, mtime: float) -> Path:
    impl_dir = work_root / "fpga.runs" / "impl_1"
    impl_dir.mkdir(parents=True, exist_ok=True)
    rpt = impl_dir / name
    rpt.write_text(text, encoding="utf-8")
    os.utime(rpt, (mtime, mtime))
    return rpt


class TestRouteReportAgeGating:
    """A run that produced nothing must not parse a previous stale report."""

    def test_stale_report_predating_dispatch_is_skipped(self, tmp_path: Path) -> None:
        work_root = tmp_path / "wr"
        # Report last written an hour before the dispatch instant.
        rpt = _write_route_report(
            work_root,
            "top_timing_summary_routed.rpt",
            "old metrics\n",
            mtime=time.time() - 3600,
        )
        collected = FpgaImplFlow()._collect_route_reports(
            work_root,
            min_mtime=time.time(),
        )
        assert collected == ""
        assert rpt.exists()  # not deleted, just ignored

    def test_fresh_report_after_dispatch_is_collected(self, tmp_path: Path) -> None:
        work_root = tmp_path / "wr"
        dispatch = time.time()
        _write_route_report(
            work_root,
            "top_timing_summary_routed.rpt",
            "fresh metrics\n",
            mtime=dispatch + 5,
        )
        flow = _collect_flow(tmp_path)
        collected = flow._collect_route_reports(work_root, min_mtime=dispatch)
        assert "fresh metrics" in collected

    def test_no_min_mtime_collects_everything(self, tmp_path: Path) -> None:
        """Back-compat: without a dispatch instant, age gating is disabled."""
        work_root = tmp_path / "wr"
        _write_route_report(
            work_root,
            "top_timing_summary_routed.rpt",
            "any\n",
            mtime=time.time() - 99999,
        )
        collected = _collect_flow(tmp_path)._collect_route_reports(work_root)
        assert "any" in collected


class TestFailureTailSurfacesStderr:
    """make's real error lands on stderr; the failure report must include it."""

    def test_stderr_included_in_tail(self) -> None:
        tail = FpgaImplFlow._failure_tail(
            "make: Entering directory '/workspace'\nmake: Leaving directory '/workspace'\n",
            "Makefile:12: *** No rule to make target. Stop.\n",
        )
        assert "No rule to make target" in tail
        assert "stderr tail" in tail
        assert "Entering directory" in tail

    def test_empty_stderr_omits_section(self) -> None:
        tail = FpgaImplFlow._failure_tail("Vivado crashed\n", "")
        assert "Vivado crashed" in tail
        assert "stderr tail" not in tail


# ===========================================================================
# Timeout resolution (unified with asic_synthesize, change #2)
# ===========================================================================


class TestTimeoutResolution:
    """--timeout (ms) > [flows.fpga].timeout_ms > 7200000 default.

    Only the resolution *mechanism* mirrors asic; the fallback VALUE stays
    FPGA's larger 2h default (impl runs are legitimately longer than synth).
    """

    def test_cli_timeout_wins(self, tmp_path: Path, state_file: Path) -> None:
        _write_project_config(
            tmp_path,
            execution_lines="timeout_ms = 300000\n",
        )
        flow = _flow(tmp_path, state_file, "--timeout", "5000")
        assert flow._timeout_ms() == 5000
        assert flow._get_timeout() == 5  # whole seconds

    def test_toml_timeout_ms_fallback(self, tmp_path: Path, state_file: Path) -> None:
        _write_project_config(
            tmp_path,
            execution_lines="timeout_ms = 300000\n",
        )
        flow = _flow(tmp_path, state_file)  # no --timeout
        assert flow._timeout_ms() == 300000
        assert flow._get_timeout() == 300

    def test_default_when_neither_set(self, tmp_path: Path, state_file: Path) -> None:
        _write_project_config(tmp_path)  # no timeout_ms in [flows.fpga]
        flow = _flow(tmp_path, state_file)  # no --timeout
        assert flow._timeout_ms() == 7_200_000
        assert flow._get_timeout() == 7200


# ===========================================================================
# Debuggability capture: failure_output / log_path / reports (change #4)
# ===========================================================================


class TestFailureCapture:
    """A route-not-reached failure keeps infra_error concise while surfacing the
    log/stderr tail via failure_output, the read report files via reports, and a
    persisted run.log via log_path (mirrors asic_synthesize)."""

    def test_route_not_reached_populates_capture_fields(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)

        # A fresh route report on disk (future mtime beats the dispatch instant
        # _execute_boundary stamps) so _collect_route_reports records its path.
        work_root = tmp_path / "wr"
        _write_route_report(
            work_root,
            "top_utilization_placed.rpt",
            "utilization data\n",
            mtime=time.time() + 3600,
        )

        with (
            patch.object(
                FpgaImplFlow,
                "_prepare_fpga_command",
                return_value=(["make", "-C", "wr"], work_root),
            ),
            patch.object(
                FpgaImplFlow,
                "_execute_local",
                return_value=SubprocessResult(
                    returncode=1,
                    stdout="make: Entering directory\nmake: Leaving directory\n",
                    stderr="ERROR: no rule to make target. Stop.\n",
                ),
            ),
            # No status:pass => route not completed => the exit code (1) fails it.
            patch(
                "booley.flows.fpga.edam.parse_fpga_reports",
                return_value={"lut_count": 10, "ff_count": 5},
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        report = json.loads(
            (tmp_path / "reports" / "fpga_default.json").read_text(encoding="utf-8")
        )
        # infra_error stays the concise reason — the tail is NOT concatenated in.
        assert report["infra_error"] == ("Vivado (edalize) did not reach route_design (exit 1).")
        assert "no rule to make target" not in report["infra_error"]
        # ...the bulky tail moved to failure_output.
        assert "no rule to make target" in report["metrics"]["failure_output"]
        assert "stderr tail" in report["metrics"]["failure_output"]
        # The impl run dir holding the route reports is named (the report points
        # at directories, not a per-file inventory).
        assert report["metrics"]["artifacts"]["dirs"]["impl"].endswith("impl_1")
        # A run.log was persisted for this PRIMARY run.
        assert report["metrics"]["log_path"].endswith("run.log")
        assert (tmp_path / report["metrics"]["log_path"]).is_file()
        # The display/report text shows the concise reason and the separate
        # subprocess-output + log lines.
        assert "did not reach route_design (exit 1)." in result.report_text
        assert "subprocess output:" in result.report_text
        assert "default: log:" in result.report_text


class TestArtifactPointers:
    """The ``artifacts`` block: present for the run that owns the files,
    absent for the baseline whose files no longer exist."""

    def test_baseline_metrics_carry_no_artifacts(self):
        """A baseline runs in a throwaway worktree that is deleted on exit,
        and its report paths were relativized against THAT root — so the same
        string read against the real project root resolves to the CURRENT
        run's report files. Silently wrong, not merely dangling: a
        baseline-vs-current comparison would read the same numbers twice.
        """
        from booley.flows.fpga.metrics import FpgaMetrics, _metrics_detail

        metrics = FpgaMetrics(
            lut_count=10,
            ff_count=5,
            log_path="",  # already blanked upstream for baseline runs
            dirs={"impl": "build/proj.runs/impl_1"},
        )

        current = _metrics_detail(metrics)
        baseline = _metrics_detail(metrics, baseline=True)

        assert current["artifacts"]["dirs"]["impl"] == "build/proj.runs/impl_1"
        assert "artifacts" not in baseline
        # The parsed numbers stay either way — only the pointer block goes.
        assert baseline["lut_count"] == 10

    def test_run_detail_carries_per_target_artifacts(self):
        """fpga_impl returned no ``detail`` at all, so its pointers reached
        state.json and the per-target JSON but never the MCP structuredContent
        an agent actually reads."""
        from booley.flows.fpga.metrics import FpgaMetrics, _metrics_detail

        metrics = FpgaMetrics(log_path="build/run.log", dirs={"impl": "build/impl_1"})
        detail = _metrics_detail(metrics)

        assert detail["artifacts"] == {
            "log": "build/run.log",
            "dirs": {"impl": "build/impl_1"},
        }


class TestArtifactDirs:
    """The report names directories, not a file inventory."""

    def _seed(self, work_root: Path) -> None:
        impl = work_root / "fpga.runs" / "impl_1"
        synth = work_root / "fpga.runs" / "synth_1"
        impl.mkdir(parents=True, exist_ok=True)
        synth.mkdir(parents=True, exist_ok=True)
        for p in (
            impl / "soc_top_power_routed.rpt",
            synth / "runme.log",
            work_root / "vivado.log",
        ):
            p.write_text("body\n", encoding="utf-8")

    def test_names_build_impl_and_synth_dirs(self, tmp_path: Path):
        work_root = tmp_path / "wr"
        self._seed(work_root)
        flow = _collect_flow(tmp_path)

        dirs = flow._artifact_dirs(work_root)

        assert dirs["build"] == "wr"
        assert dirs["impl"] == "wr/fpga.runs/impl_1"
        assert dirs["synth"] == "wr/fpga.runs/synth_1"
        # Three entries, not a per-file listing: everything Vivado wrote is
        # reachable by listing these, and no key hardcodes a filename that a
        # Vivado version bump could rename out from under it.
        assert set(dirs) == {"build", "impl", "synth"}

    def test_absent_run_dirs_are_omitted(self, tmp_path: Path):
        """A run that never reached implementation names only what exists."""
        work_root = tmp_path / "wr"
        (work_root / "fpga.runs" / "synth_1").mkdir(parents=True)
        flow = _collect_flow(tmp_path)

        dirs = flow._artifact_dirs(work_root)

        assert set(dirs) == {"build", "synth"}

    def test_dirs_are_work_dir_relative(self, tmp_path: Path):
        work_root = tmp_path / "wr"
        self._seed(work_root)
        flow = _collect_flow(tmp_path)

        for key, rel in flow._artifact_dirs(work_root).items():
            assert not rel.startswith("/"), f"{key} must not be absolute"
            assert (tmp_path / rel).is_dir(), f"{key} does not resolve"
