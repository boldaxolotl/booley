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

# Import registers the real Goal worktree fixture for this module.
from tests.goals.conftest import (
    layout as goal_layout,  # noqa: F401 — pytest registers the imported fixture by name.
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
        entry = {"name": name, "core": "acme:test:dut:1", "file_type": "systemVerilogSource"}
        edam_path.write_text(
            yaml.safe_dump(
                {
                    "cores": {"acme:test:dut:1": {"core_file": str(core_file)}},
                    "files": [entry],
                }
            )
        )
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


def test_zero_match_compact_cocotb_suite_remains_inconclusive(tmp_path: Path) -> None:
    from booley.flows.sim.backends import cocotb_results as cr

    endpoint = _endpoint(tmp_path)
    results = cr.CocotbResults(
        state=cr.STATE_OK,
        tests=(cr.CocotbTest("run_test/ifg=12", "tb", "skipped"),),
    )
    output = cr.format_results_line(results, selected=["run_test"], verbosity="compact")
    runs = [
        MutationTestRun(
            "<cocotb-suite>", process=_process(1), output=output, requires_cocotb_results=True
        )
    ]
    verdict = endpoint._classify_variant_suite(runs)
    assert verdict.detected is False
    assert verdict.first_killing_test == ""
    assert "without a failing test" in verdict.inconclusive_reason


def test_auto_budget_uses_source_size_without_hdl_features(tmp_path: Path) -> None:
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not even valid HDL\n" * 16)

    breakdown = compute_source_size_budget(["rtl/dut.sv"], tmp_path)

    assert breakdown["method"] == "language_neutral_source_size"
    assert breakdown["source_lines"] == 16
    (source.parent / "unterminated.vhd").write_bytes(b"not HDL")
    (source.parent / "empty.sv").write_bytes(b"")
    paths = ["rtl/dut.sv", "rtl/unterminated.vhd", "rtl/empty.sv"]
    preview = compute_source_size_budget(paths, tmp_path)
    assert preview["files"] == [
        {"path": "rtl/dut.sv", "source_lines": 16, "source_bytes": 304},
        {"path": "rtl/unterminated.vhd", "source_lines": 1, "source_bytes": 7},
        {"path": "rtl/empty.sv", "source_lines": 0, "source_bytes": 0},
    ]
    for key in ["source_lines", "source_bytes"]:
        assert preview[key] == sum(file[key] for file in preview["files"])
    assert preview["readable_files"] == len(preview["files"])
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
        endpoint._pinned_build("sim", tmp_path, build_path)


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


# Reuse the real linked-worktree Goal fixture for publication, not an in-memory state.


@pytest.mark.timeout(120)
def test_mutation_concurrent_proposer_preserves_reviewer_goal_evidence(request, monkeypatch):
    layout = request.getfixturevalue("goal_layout")
    import json
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from dataclasses import replace
    from threading import Event

    from booley.core.models import AgentResult
    from tests.goals.test_proposals import observations

    mutation, reviewer = _concurrent_endpoints(layout, monkeypatch)
    repo = layout.worktree
    entered, release = Event(), Event()

    def provider(params, *, on_event):
        if params.label.startswith("mutation_creator"):
            entered.set()
            assert release.wait(20), "reviewer did not release proposer"
            assert (repo / "tb/tb.sv").is_file()
            return AgentResult(
                output=json.dumps(
                    {
                        "mutations": [
                            MutationSpec(1, "operator", "rtl.v", 1, "a + b", "a - b").to_dict()
                        ]
                    }
                )
            )
        assert (repo / "tb/tb.sv").is_file()
        assert (Path(params.cwd) / "tb/tb.sv").is_file()
        return AgentResult(output=json.dumps({"issues": []}))

    monkeypatch.setattr("booley.specialists.specialist._call_agent_sync", provider)
    monkeypatch.setattr(
        "booley.specialists.specialist.parent_death_watchdog", lambda *_: nullcontext()
    )
    plan = replace(_plan(repo, "rtl.v"), target="top", count=1, formula_count=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mutation._proposal_round, "propose", 1)
        try:
            assert entered.wait(10), "proposer did not enter provider"
            review_result = reviewer._run()
            assert review_result.exit_code == 0, review_result.report_text
            assert reviewer.evidence_discarded is None
            assert any(
                row["producer"] == "reviewer" and row["met"] for row in observations(layout)
            )
        finally:
            release.set()
        specs, elapsed, error = future.result(timeout=20)
    assert error is None
    _publish_concurrent_mutation(mutation, plan, specs, elapsed)
    rows = observations(layout)
    assert {row["producer"] for row in rows} == {"reviewer", "mutation_tester"}
    assert all(row["met"] for row in rows)
    assert (repo / "tb/tb.sv").is_file()


def _concurrent_endpoints(layout, monkeypatch):
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.specialists.reviewer import ReviewerSpecialist
    from tests.goals.conftest import bind, enter_goals, git

    repo = layout.worktree
    (repo / "tb").mkdir()
    (repo / "tb/tb.sv").write_text("module tb; endmodule\n")
    (repo / "design.core").write_text(
        "CAPI=2:\nname: ::fixture\nfilesets:\n"
        "  rtl:\n    files: [rtl.v]\n    file_type: verilogSource\n"
        "  tb:\n    files: [tb/tb.sv]\n    file_type: systemVerilogSource\n    tags: [tb]\n"
        "targets:\n  sim: {filesets: [rtl, tb], toplevel: tb}\n"
    )
    git(repo, "add", "tb/tb.sv", "design.core")
    git(repo, "commit", "-qm", "testbench")
    layout.record = enter_goals(
        layout,
        [
            {
                "paths": ["tb/tb.sv"],
                "family": "REVIEW",
                "review": "tb_quality",
                "verdict": "clean",
            },
            {"family": "MUTATION", "target": "top", "scope": ["rtl.v"], "total": 1},
        ],
    )
    (repo / "rtl.v").write_text("assign x = a + b;\n")
    endpoints = []
    for endpoint, arguments in [
        (MutationTesterSpecialist(), ["--target", "top", "--scope", "rtl.v"]),
        (ReviewerSpecialist(), ["--category", "tb", "--focus", "quality"]),
    ]:
        adapter = GoalFlowExecution(bind(layout, endpoint.name))
        endpoint.configure_flow_execution(adapter)
        endpoint.parse_args(["--work-dir", str(repo), *arguments])
        endpoint.args.state_file = adapter.state_file
        endpoint.read_state()
        monkeypatch.setattr(endpoint, "_resolve_model", lambda: "test")
        endpoints.append(endpoint)
    mutation, reviewer = endpoints
    mutation._target_campaign = types.SimpleNamespace(
        target="top", suite=types.SimpleNamespace(display_names=("suite",))
    )
    return mutation, reviewer


def _publish_concurrent_mutation(mutation, plan, specs, elapsed):
    from booley.specialists.mutation_tester import MutationSummary, RunResultInputs

    variants, problem = mutation._resolve_proposals(specs, plan)
    assert problem == "" and variants is not None
    # The deliberately live sweep runs only after reviewer evidence is durable.
    mutation._run_elab = lambda *_args: _process(0)
    mutation._run_target_test_suite = lambda *_args, **_kwargs: [
        MutationTestRun("suite", process=_process(1))
    ]
    results, tester_elapsed, infra = mutation._run_variant_sweep(plan, specs, variants)
    assert infra == "" and results[0].detected
    result = mutation._build_run_result(
        plan,
        RunResultInputs(
            summary=MutationSummary(specs=specs, results=results),
            count=1,
            tester_elapsed=tester_elapsed,
            creator_elapsed=elapsed,
            reused_lock=False,
            lock_created_at=None,
            verification_rounds=1,
            build_cached=False,
            variants=variants,
        ),
    )
    assert result.exit_code == 0 and mutation.evidence_discarded is None
