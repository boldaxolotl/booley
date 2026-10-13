"""Behavioral tests for compiler-isolated mutation variants."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest
import yaml

from booley.dev_support.mutation_build_sources import materialize_build_mutation
from booley.dev_support.mutation_variants import MutationVariantPlan
from booley.flows.sim.backends.cocotb_results import COCOTB_RESULTS_PREFIX
from booley.specialists.mutation_tester import (
    MutationRunPlan,
    MutationSpec,
    MutationTesterSpecialist,
    MutationTestRun,
    UnsupportedSimTargetError,
    compute_source_size_budget,
)


def _process(returncode: int, output: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=output, stderr="")


def _endpoint(tmp_path: Path) -> MutationTesterSpecialist:
    endpoint = MutationTesterSpecialist()
    endpoint._args = types.SimpleNamespace(work_dir=tmp_path, tb_top="tb")
    endpoint.emit_progress = lambda _line: None
    return endpoint


def _plan(tmp_path: Path, scope: str) -> MutationRunPlan:
    return MutationRunPlan(
        scope_files=[scope],
        scope_hashes={},
        work_dir=tmp_path,
        target="sim",
        tb_top="tb",
        report_dir=None,
        min_detected=1,
        count=2,
        auto_mode=False,
        formula_count=2,
        source_size_budget=None,
    )


def test_sweep_builds_one_exact_replacement_at_a_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    scope = "rtl/dut.sv"
    source = tmp_path / scope
    source.parent.mkdir(parents=True)
    pristine = "assign x = a + b;\nassign y = c & d;\n"
    source.write_text(pristine, encoding="utf-8")
    specs = [
        MutationSpec(1, "operator", scope, 1, "a + b", "a - b"),
        MutationSpec(2, "operator", scope, 2, "c & d", "c | d"),
    ]
    variants = MutationVariantPlan.resolve(specs, tmp_path, [scope])
    endpoint = _endpoint(tmp_path)
    built_sources: list[str] = []
    build_dirs: list[Path] = []

    def elaborate(_target: str, _work_dir: Path, build_dir: Path, mutation):
        assert source.read_text(encoding="utf-8") == pristine
        core_file = tmp_path / "dut.core"
        core_file.write_text(
            "CAPI=2:\n"
            + yaml.safe_dump(
                {
                    "name": "acme:test:dut:1",
                    "filesets": {"rtl": {"files": [scope]}},
                }
            )
        )
        staged = build_dir / "src/acme_test_dut_1" / scope
        staged.parent.mkdir(parents=True)
        staged.write_bytes(source.read_bytes())
        name = str(staged.relative_to(build_dir))
        edam_path = build_dir / "dut.eda.yml"
        edam_path.write_text(
            yaml.safe_dump(
                {
                    "cores": {"acme:test:dut:1": {"core_file": str(core_file)}},
                    "files": [{"name": name, "core": "acme:test:dut:1"}],
                }
            )
        )
        (build_dir / "Makefile").write_text("iverilog -c dut.scr\n")
        (build_dir / "dut.scr").write_text(name)
        resolved = types.SimpleNamespace(build_root=build_dir, edam_path=edam_path)
        materialize_build_mutation(mutation[0], mutation[1], tmp_path, resolved)
        built_sources.append(staged.read_text(encoding="utf-8"))
        build_dirs.append(build_dir)
        return _process(0)

    def run_suite(*_args, **_kwargs):
        assert source.read_text(encoding="utf-8") == pristine
        text = (build_dirs[-1] / "src/acme_test_dut_1" / scope).read_text(encoding="utf-8")
        return [MutationTestRun("suite", process=_process(1 if "a - b" in text else 0))]

    endpoint._run_elab = elaborate
    endpoint._run_target_test_suite = run_suite
    results, _elapsed, infra = endpoint._run_variant_sweep(_plan(tmp_path, scope), specs, variants)

    assert infra == ""
    assert [result.detected for result in results] == [True, False]
    assert built_sources == [
        "assign x = a - b;\nassign y = c & d;\n",
        "assign x = a + b;\nassign y = c | d;\n",
    ]
    assert build_dirs[0] != build_dirs[1]
    assert source.read_text(encoding="utf-8") == pristine


def test_cocotb_missing_results_are_inconclusive_not_a_kill(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    output = COCOTB_RESULTS_PREFIX + '{"state":"missing","detail":"no xml","tests":[]}'
    runs = [MutationTestRun("suite", process=_process(1), output=output)]

    verdict = endpoint._classify_variant_suite(runs)
    assert "state is missing" in verdict.inconclusive_reason
    assert verdict.detected is False
    assert verdict.first_killing_test == ""


@pytest.mark.parametrize("output", ["", COCOTB_RESULTS_PREFIX + "{broken"])
def test_cocotb_absent_or_malformed_results_are_inconclusive(
    tmp_path: Path,
    output: str,
) -> None:
    scope = "rtl/dut.sv"
    source = tmp_path / scope
    source.parent.mkdir(parents=True)
    source.write_text("assign x = a + b;\n", encoding="utf-8")
    specs = [MutationSpec(1, "operator", scope, 1, "a + b", "a - b")]
    variants = MutationVariantPlan.resolve(specs, tmp_path, [scope])
    endpoint = _endpoint(tmp_path)
    endpoint._run_elab = lambda *_args, **_kwargs: _process(0)

    def run_suite(*_args, **_kwargs):
        return [
            MutationTestRun(
                "<cocotb-suite>",
                process=_process(1),
                output=output,
                requires_cocotb_results=True,
            )
        ]

    endpoint._run_target_test_suite = run_suite

    results, _elapsed, infra = endpoint._run_variant_sweep(_plan(tmp_path, scope), specs, variants)

    assert "cocotb result line is missing or malformed" in infra
    assert len(results) == 1
    assert results[0].invalid is True
    assert results[0].detected is False
    assert results[0].first_killing_test == ""


def test_sweep_classifies_every_variant_after_inconclusive_result(tmp_path: Path) -> None:
    scope = "rtl/dut.sv"
    source = tmp_path / scope
    source.parent.mkdir(parents=True)
    source.write_text("assign x = a + b;\nassign y = c & d;\n", encoding="utf-8")
    specs = [
        MutationSpec(1, "operator", scope, 1, "a + b", "a - b"),
        MutationSpec(2, "operator", scope, 2, "c & d", "c | d"),
    ]
    variants = MutationVariantPlan.resolve(specs, tmp_path, [scope])
    endpoint = _endpoint(tmp_path)
    endpoint._run_elab = lambda *_args, **_kwargs: _process(0)

    def run_suite(*_args, **_kwargs):
        if _args[2].name == "mutant_1":
            output = COCOTB_RESULTS_PREFIX + '{"state":"missing","tests":[]}'
        else:
            output = COCOTB_RESULTS_PREFIX + (
                '{"state":"ok","tests":[{"name":"corner","module":"tb",'
                '"status":"fail","failure":"mismatch","elapsed_s":0.1}]}'
            )
        return [
            MutationTestRun(
                "<cocotb-suite>",
                process=_process(1),
                output=output,
                requires_cocotb_results=True,
            )
        ]

    endpoint._run_target_test_suite = run_suite

    results, _elapsed, infra = endpoint._run_variant_sweep(_plan(tmp_path, scope), specs, variants)

    assert "state is missing" in infra
    assert len(results) == 2
    assert results[0].invalid is True
    assert results[1].detected is True
    assert results[1].first_killing_test == "corner"


def test_cocotb_all_pass_does_not_fabricate_a_killing_suite(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    output = COCOTB_RESULTS_PREFIX + (
        '{"state":"ok","detail":"","tests":['
        '{"name":"reset","module":"tb","status":"pass",'
        '"failure":"","elapsed_s":0.1}]}'
    )
    runs = [MutationTestRun("<cocotb-suite>", process=_process(1), output=output)]

    verdict = endpoint._classify_variant_suite(runs)
    assert verdict.detected is False
    assert "without a failing test" in verdict.inconclusive_reason
    assert verdict.first_killing_test == ""


def test_auto_budget_uses_source_size_without_hdl_features(tmp_path: Path) -> None:
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir(parents=True)
    source.write_text("not even valid HDL\n" * 16, encoding="utf-8")

    breakdown = compute_source_size_budget(["rtl/dut.sv"], tmp_path)

    assert breakdown["method"] == "language_neutral_source_size"
    assert breakdown["source_lines"] == 16
    assert "always_blocks" not in breakdown


def test_auto_budget_fails_when_scope_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        compute_source_size_budget(["rtl/missing.sv"], tmp_path)


def test_missing_variant_marker_cannot_restage_pristine_build(tmp_path, monkeypatch):
    endpoint = _endpoint(tmp_path)
    build_path = tmp_path / "mutant_1"
    build_path.mkdir()
    monkeypatch.setattr(
        endpoint, "_resolve_elab_target", lambda *_args: pytest.fail("restaged source")
    )
    with pytest.raises(UnsupportedSimTargetError, match="mutation build marker missing"):
        endpoint._bin_dir_rel("sim", tmp_path, build_path)


def test_unsupported_variant_build_is_invalid_before_suite(tmp_path):
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir()
    source.write_text("assign x = a + b;\n")
    specs = [MutationSpec(1, "operator", "rtl/dut.sv", 1, "a + b", "a - b")]
    variants = MutationVariantPlan.resolve(specs, tmp_path, ["rtl/dut.sv"])
    endpoint = _endpoint(tmp_path)
    endpoint._run_elab = lambda *_args: _process(
        1, "unsupported generated/imperative source closure"
    )
    endpoint._run_target_test_suite = lambda *_args: pytest.fail("scored invalid build")
    results, _elapsed, _infra = endpoint._run_variant_sweep(
        _plan(tmp_path, "rtl/dut.sv"), specs, variants
    )
    assert results[0].invalid and not results[0].detected
    assert "unsupported generated/imperative" in results[0].sim_output_snippet
    assert "a + b" in source.read_text()


def test_generated_direct_source_rejected_before_fusesoc_setup(tmp_path, monkeypatch):
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir()
    source.write_text("assign x = a + b;\n")
    core = tmp_path / "dut.core"
    core.write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "acme:test:dut:1",
                "filesets": {"rtl": {"files": ["rtl/dut.sv", "generated/derivative.sv"]}},
                "generators": {"derivative": {"command": "generate"}},
                "targets": {"sim": {"filesets": ["rtl"], "generate": ["derivative"]}},
            }
        )
    )
    catalog = types.SimpleNamespace(
        select=lambda *_args, **_kwargs: object(),
        core_closure=lambda _handles: frozenset([core]),
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.TargetCatalog.build", lambda _root: catalog
    )
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.prepare_simulation_build",
        lambda *_args, **_kwargs: pytest.fail("generated pristine derivative"),
    )
    endpoint = _endpoint(tmp_path)
    specs = [MutationSpec(1, "operator", "rtl/dut.sv", 1, "a + b", "a - b")]
    variants = MutationVariantPlan.resolve(specs, tmp_path, ["rtl/dut.sv"])
    result = endpoint._run_elab("sim", tmp_path, tmp_path / "build", (variants, 1))
    assert result.returncode == 1
    assert "unsupported generated/imperative" in result.stderr
    assert source.read_text() == "assign x = a + b;\n"
