from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.file_lock import acquire_file_lock
from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.sim import verilator_coverage_execution
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import simulation_build_slot
from booley.flows.sim.coverage_overlay import CoverageOverlay
from booley.flows.sim.execution.contract import PreSimEvidence, SimulationOptions
from booley.flows.sim.record.coverage_execution import _coverage_observation
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    VERILATOR_COVERAGE_INSTRUMENTATION,
    CoverageTarget,
    SelectedCoverageRun,
    SimulationBuildRequest,
    SimulationBuildResult,
    SimulationBuildVariant,
    SimulationRunRequest,
)
from booley.flows.sim.verilator_coverage_execution import (
    VerilatorCoverageExecution,
    prepare_coverage_collection,
)
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.fusesoc.fusesoc_registry import ResolvedFile
from booley.runtime import resource_history
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS
from booley.targets.catalog import TargetCatalog
from tests.canned_process_support import canned_command, measured_flow


def _pre_sim_config(root: Path, text: str) -> None:
    """Write the project's `.booley/booley.toml`."""
    config = root / ".booley"
    config.mkdir(exist_ok=True)
    (config / "booley.toml").write_text(text, encoding="utf-8")


def _build_coverage(
    execution: VerilatorCoverageExecution, target: CoverageTarget
) -> SimulationBuildResult:
    return execution.build(
        SimulationBuildRequest(
            target,
            SimulationBuildVariant(trace=False, coverage=True),
            VERILATOR_COVERAGE_INSTRUMENTATION,
        )
    )


def _write_target(root: Path, *, custom_main: bool = False) -> None:
    hooks = "          custom_main_hooks: [start_hook, write_hook]\n" if custom_main else ""
    main = "      - tb/main.cpp: {file_type: cppSource, tags: [tb]}\n" if custom_main else ""
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    (root / "rtl" / "counter.sv").write_text("module counter; endmodule\n")
    (root / "tb" / "counter_tb.sv").write_text("module counter_tb; endmodule\n")
    if custom_main:
        (root / "tb" / "main.cpp").write_text("int main() { return 0; }\n")
    (root / "counter.core").write_text(
        "CAPI=2:\n"
        "name: acme:demo:counter:1\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        "      - rtl/counter.sv: {file_type: systemVerilogSource}\n"
        "  tb:\n"
        "    files:\n"
        "      - tb/counter_tb.sv: {file_type: systemVerilogSource}\n"
        f"{main}"
        "    tags: [tb]\n"
        "targets:\n"
        "  sim:\n"
        "    flow: sim\n"
        "    default_tool: verilator\n"
        "    flow_options:\n"
        "      tool: verilator\n"
        "      booley:\n"
        "        coverage:\n"
        f"          reset_included: {str(not custom_main).lower()}\n"
        f"{hooks}"
        "    filesets: [rtl, tb]\n"
        "    toplevel: counter_tb\n",
        encoding="utf-8",
    )


def test_prepare_collection_projects_resolved_sources_and_custom_main_recipe(
    tmp_path: Path,
) -> None:
    _write_target(tmp_path, custom_main=True)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    request = prepare_coverage_collection(
        handle,
        selected_tests=("wrap",),
        artifact_root=tmp_path / "coverage",
        trace=True,
    )

    assert request.target.identity == handle.identity
    assert request.target.harness == "custom_main"
    assert request.target.custom_main_hooks == ("start_hook", "write_hook")
    assert request.reset_included is False
    assert request.trace is True
    assert [(source.path, source.kind) for source in request.target.sources] == [
        ("rtl/counter.sv", "rtl"),
        ("tb/counter_tb.sv", "testbench"),
    ]


def test_prepare_collection_defaults_to_reset_inclusive_coverage(tmp_path: Path) -> None:
    _write_target(tmp_path)
    core = tmp_path / "counter.core"
    core.write_text(
        core.read_text().replace(
            "      booley:\n        coverage:\n          reset_included: true\n", ""
        )
    )
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    request = prepare_coverage_collection(
        handle,
        selected_tests=("reset",),
        artifact_root=tmp_path / "coverage",
    )

    assert request.reset_included is True


def test_prepare_collection_rejects_invalid_inherited_coverage_recipe(tmp_path: Path) -> None:
    _write_target(tmp_path)
    core = tmp_path / "counter.core"
    core.write_text(
        core.read_text()
        .replace(
            "targets:\n  sim:\n",
            "targets:\n"
            "  defaults: &defaults\n"
            "    flow_options:\n"
            "      booley:\n"
            '        coverage: {reset_included: "false"}\n'
            "  sim:\n"
            "    <<: *defaults\n",
        )
        .replace(
            "    flow_options:\n"
            "      tool: verilator\n"
            "      booley:\n"
            "        coverage:\n"
            "          reset_included: true\n",
            "    flow_options: {tool: verilator}\n",
        )
    )
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    with pytest.raises(
        ValueError,
        match=r"targets\.sim\.flow_options\.booley\.coverage\.reset_included "
        r"must be a boolean",
    ):
        prepare_coverage_collection(
            handle,
            selected_tests=("reset",),
            artifact_root=tmp_path / "coverage",
        )


def test_execution_uses_simulation_build_and_authenticated_run_adapters(
    tmp_path: Path, monkeypatch
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    build = _build_coverage(execution, target)
    run = execution.run(_run_request(target, raw_path))

    assert build.success is True
    assert build.collector == PINNED_VERILATOR
    assert captured["prepare"]["variant"] == "coverage"
    assert "g" in captured["prepare"]["build_root"].parts
    assert captured["prepare"]["resolution_vlnv"] == "::coverage:0"
    assert captured["work"].plusargs[-1] == f"+verilator+coverage+file+{raw_path}"
    assert "BOOLEY_COVERAGE_RUN_ID=run:001:wrap" in captured["run_script"]
    assert run.verdict == "pass"


_VARIANT_TESTS_TOML = (
    '[sim]\ntests = ["wrap"]\n\n[sim.variants.wrap]\ndefault = "short"\nshort = []\n'
    'soak = { args = ["+iters=9"], env = { SOAK = "1" } }\n'
)


def _declare_variants(root: Path) -> None:
    data = root / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(_VARIANT_TESTS_TOML, encoding="utf-8")


def test_execution_launches_the_test_run_with_its_variant_and_seed(
    tmp_path: Path, monkeypatch
) -> None:
    _declare_variants(tmp_path)
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    run = load_target_test_plan(tmp_path, "sim", identity="sim", adapter="verilator").run(
        "wrap", "soak", 7
    )
    _build_coverage(execution, target)

    result = execution.run(
        replace(_run_request(target, raw_path), test=SelectedCoverageRun("wrap", run))
    )

    assert result.verdict == "pass"
    # select, then the Variant's arguments, then the seed, then coverage's own.
    # (The shared launch path stores its own plusargs without their ``+``.)
    assert captured["work"].plusargs[-3:] == (
        "iters=9",
        "verilator+seed+7",
        f"+verilator+coverage+file+{raw_path}",
    )
    assert captured["work"].test_runs == (run,)
    assert "export SOAK=1\n" in captured["run_script"]


def test_coverage_run_labels_its_test_variant_and_source_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    _declare_variants(tmp_path)
    sources = tuple(
        ResolvedFile(str(tmp_path / name), "systemVerilogSource")
        for name in ("rtl/counter.sv", "tb/counter_tb.sv")
    )
    execution, target, raw_path, _captured = _execution_fixture(
        tmp_path, monkeypatch, files=sources
    )
    expected_bytes = sum(
        (tmp_path / name).stat().st_size for name in ("rtl/counter.sv", "tb/counter_tb.sv")
    )
    run = load_target_test_plan(tmp_path, "sim", identity="sim", adapter="verilator").run(
        "wrap", "soak", 7
    )
    labels: dict[str, job_resources.ResourceLabel | None] = {}
    invoke = execution._invoke

    def spy(command, *, timeout):
        text = " ".join(command)
        for marker, name in (("BOOLEY_BUILD_STAGE", "build"), ("BOOLEY_COVERAGE_RUN_ID", "run")):
            if marker in text:
                labels[name] = job_resources.current_label()
        return invoke(command, timeout=timeout)

    execution._invoke = spy
    job_resources.clear_label()
    _build_coverage(execution, target)
    result = execution.run(
        replace(_run_request(target, raw_path), test=SelectedCoverageRun("wrap", run))
    )
    job_resources.clear_label()

    assert result.verdict == "pass"
    # The Test Variant is named; the seed is not.
    assert labels["run"].tests == ("wrap+soak",)
    assert labels["build"] == job_resources.ResourceLabel(
        "sim", "sim_build", source_bytes=expected_bytes
    )
    assert labels["run"] == job_resources.ResourceLabel(
        "sim", "sim_run", ("wrap+soak",), source_bytes=expected_bytes
    )
    assert expected_bytes > 0


def test_prepare_collection_gives_every_selected_test_its_default_test_run(
    tmp_path: Path,
) -> None:
    _write_target(tmp_path)
    _declare_variants(tmp_path)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    request = prepare_coverage_collection(
        handle, selected_tests=("wrap",), artifact_root=tmp_path / "coverage"
    )

    assert [item.run_name for item in request.selected_tests] == ["wrap+short@1"]


def test_prepare_collection_keeps_several_test_runs_of_one_test(tmp_path: Path) -> None:
    _write_target(tmp_path)
    _declare_variants(tmp_path)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    plan = load_target_test_plan(tmp_path, "sim", identity="sim", adapter="verilator")
    runs = (plan.run("wrap", "soak", 7), plan.run("wrap"))

    request = prepare_coverage_collection(
        handle, selected_tests=("wrap",), artifact_root=tmp_path / "coverage", test_runs=runs
    )

    assert [(item.name, item.run) for item in request.selected_tests] == [
        ("wrap", runs[0]),
        ("wrap", runs[1]),
    ]
    with pytest.raises(ValueError, match="distinct runs of exactly the selected Tests"):
        prepare_coverage_collection(
            handle,
            selected_tests=("wrap",),
            artifact_root=tmp_path / "coverage",
            test_runs=(runs[0], runs[0]),
        )
    with pytest.raises(ValueError, match="distinct runs of exactly the selected Tests"):
        prepare_coverage_collection(
            handle, selected_tests=("reset",), artifact_root=tmp_path / "coverage", test_runs=runs
        )


def test_coverage_runs_pre_sim_commands_before_the_adapter(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "pre-sim.txt"
    staged = tmp_path / "staged-input.txt"
    staged.write_text("stale", encoding="utf-8")
    _pre_sim_config(
        tmp_path,
        "[flows.sim]\n"
        'pre_run_commands = [\'printf "%s\\n%s\\n%s\\n%s\\n%s" '
        '"$BOOLEY_TEST_NAME" "$BOOLEY_TEST_NAMES" "$BOOLEY_TARGET" '
        '"$BOOLEY_RUN_CWD" "${BOOLEY_BUILD_ROOT-unset}" > pre-sim.txt\', '
        '\'printf "%s" "$BOOLEY_TEST_NAME" > staged-input.txt\']\n',
    )
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    captured["staged_input_path"] = staged
    assert _build_coverage(execution, target).success

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "pass"
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "wrap",
        "wrap",
        "sim",
        str(tmp_path),
        "unset",
    ]
    assert captured["staged_values"] == ["wrap"]
    assert "run_script" in captured


def test_cocotb_coverage_runs_pre_sim_once_per_selected_test_process(
    tmp_path: Path, monkeypatch
) -> None:
    _pre_sim_config(
        tmp_path,
        "[flows.sim]\n"
        'pre_run_commands = [\'printf "%s\\n" "$BOOLEY_TEST_NAME" >> firings.txt\', '
        '\'printf "%s" "$BOOLEY_TEST_NAME" > staged-input.txt\']\n',
    )
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch, cocotb=True)
    staged = tmp_path / "staged-input.txt"
    staged.write_text("stale", encoding="utf-8")
    captured["staged_input_path"] = staged
    assert _build_coverage(execution, target).success

    for index, name in enumerate(("gap", "full"), start=1):
        request = replace(
            _run_request(target, raw_path),
            test=SelectedCoverageRun(name),
            run_id=f"run:{index:03d}:{name}",
        )
        assert execution.run(request).verdict == "pass"

    assert (tmp_path / "firings.txt").read_text(encoding="utf-8").splitlines() == [
        "gap",
        "full",
    ]
    assert len(captured["run_scripts"]) == 2
    assert captured["staged_values"] == ["gap", "full"]
    assert captured["work"].adapter == "cocotb"
    assert captured["work"].tests == ("full",)


def _bind_bundle(execution, tmp_path: Path, run_cwd: Path, verify=None) -> tuple[Path, Path]:
    """Copy the built image into a bundle directory and bind the execution to it."""
    source_root, artifacts = execution.authenticated_image()
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    for artifact in artifacts:
        destination = bundle_root / artifact.relative_to(source_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, destination)
    evidence_root = tmp_path / "attempt" / "execution-evidence"
    evidence_root.mkdir(parents=True)
    run_cwd.mkdir(parents=True, exist_ok=True)
    execution.bind_authenticated_attempt(
        bundle_root, evidence_root, run_cwd, verify or (lambda: None)
    )
    return bundle_root, evidence_root


def _bundle_entries(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_bundle_bound_coverage_launches_from_the_bundle_and_writes_evidence_outside_it(
    tmp_path: Path, monkeypatch
) -> None:
    _pre_sim_config(
        tmp_path,
        '[flows.sim]\npre_run_commands = [\'printf "%s" "$BOOLEY_RUN_CWD" > bound-cwd.txt\']\n',
    )
    observed: list[dict[str, object]] = []
    real_observe = verilator_coverage_execution.observe_stage

    def record_observation(*args, **kwargs):
        observed.append(kwargs)
        return real_observe(*args, **kwargs)

    monkeypatch.setattr(verilator_coverage_execution, "observe_stage", record_observation)
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    run_cwd = tmp_path / "attempt" / "run"
    bundle_root, evidence_root = _bind_bundle(execution, tmp_path, run_cwd)
    before = _bundle_entries(bundle_root)
    observed.clear()

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "pass"
    assert (tmp_path / "bound-cwd.txt").read_text(encoding="utf-8") == str(run_cwd)
    work = captured["work"]
    assert (tmp_path / work.build_dir).resolve() == bundle_root.resolve()
    assert Path(work.work_dir) == evidence_root
    assert Path(work.adapter_result_path).parent == evidence_root
    assert _bundle_entries(bundle_root) == before
    assert list(evidence_root.glob("*.json"))
    logs = [Path(item["log"].path) for item in observed if item.get("log") is not None]
    assert logs
    assert all(log.parent == evidence_root and log.name == "run.log" for log in logs)
    assert {item["evidence_root"] for item in observed} == {evidence_root}


class _BundleTamperedError(Exception):
    """Raised by the verify callback; the execution must not catch it."""


def test_bound_run_verifies_the_bundle_after_pre_sim_and_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    bundle_holder: list[Path] = []
    stray = "touch {}"
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success

    def verify() -> None:
        if (bundle_holder[0] / "stray.so").exists():
            raise _BundleTamperedError("undeclared bundle entry: stray.so")

    bundle_root, _evidence = _bind_bundle(
        execution, tmp_path, tmp_path / "attempt" / "run", verify
    )
    bundle_holder.append(bundle_root)
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.resolve_pre_sim_commands",
        lambda _root: (stray.format(shlex.quote(str(bundle_root / "stray.so"))),),
    )

    with pytest.raises(_BundleTamperedError, match="undeclared bundle entry"):
        execution.run(_run_request(target, raw_path))

    assert "run_script" not in captured


@pytest.mark.parametrize("bundle_bound", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            "chmod",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows has no POSIX execute-bit contract"
            ),
        ),
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows CI does not grant symlink privileges"
            ),
        ),
        "delete",
    ],
)
def test_pre_sim_reauthenticates_direct_and_bundle_bound_images(
    tmp_path: Path, monkeypatch, bundle_bound: bool, mutation: str
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    source_root, artifacts = execution.authenticated_image()
    image = artifacts[0]
    if bundle_bound:
        bundle_root, _evidence_root = _bind_bundle(
            execution, tmp_path, tmp_path / "attempt" / "run"
        )
        image = bundle_root / image.relative_to(source_root)
    commands = {
        "chmod": f"chmod -x {shlex.quote(str(image))}",
        "symlink": (
            f"cp {shlex.quote(str(image))} {shlex.quote(str(image) + '.copy')} && "
            f"rm {shlex.quote(str(image))} && "
            f"ln -s {shlex.quote(str(image) + '.copy')} {shlex.quote(str(image))}"
        ),
        "delete": f"rm {shlex.quote(str(image))}",
    }
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.resolve_pre_sim_commands",
        lambda _root: (commands[mutation],),
    )

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "inconclusive"
    assert result.infrastructure_error is True
    assert "coverage image" in result.output
    assert "run_script" not in captured


@pytest.mark.skipif(os.name == "nt", reason="chmod mutation is POSIX-specific")
def test_protected_surface_change_takes_priority_over_hook_failure(
    tmp_path: Path, monkeypatch
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    _source_root, artifacts = execution.authenticated_image()
    command = f"chmod -x {shlex.quote(str(artifacts[0]))}; false"
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.resolve_pre_sim_commands",
        lambda _root: (command,),
    )

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "inconclusive"
    assert result.infrastructure_error is True
    assert result.pre_sim is not None and result.pre_sim.status == "failed"
    assert "coverage image" in result.output
    assert "run_script" not in captured


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_pre_sim_failure_skips_the_coverage_adapter(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.run_pre_sim_commands",
        lambda *_args, **_kwargs: PreSimEvidence(
            ("prepare",), ("wrap",), status, 0.1, "staging failed"
        ),
    )

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "elab_error"
    assert result.pre_sim is not None
    assert result.pre_sim.status == status
    assert result.infrastructure_error is False
    assert "staging failed" in result.output
    assert "run_script" not in captured


def test_pre_sim_missing_executable_is_an_infrastructure_error(
    tmp_path: Path, monkeypatch
) -> None:
    _pre_sim_config(
        tmp_path,
        "[flows.sim]\npre_run_commands = ['booley-command-that-does-not-exist']\n",
    )
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "elab_error"
    assert result.pre_sim is not None
    assert result.pre_sim.status == "spawn_error"
    assert result.infrastructure_error is True
    assert "booley-command-that-does-not-exist" in result.output
    assert "run_script" not in captured


def test_legacy_build_access_with_commands_fails_before_coverage_build(
    tmp_path: Path, monkeypatch
) -> None:
    _pre_sim_config(
        tmp_path,
        "[flows.sim]\npre_run_commands = ['true']\npre_sim_build_access = 'legacy-per-test'\n",
    )
    execution, target, _raw_path, captured = _execution_fixture(tmp_path, monkeypatch)

    result = _build_coverage(execution, target)

    assert result.success is False
    assert result.infrastructure_error is False
    assert "legacy-per-test" in result.output
    assert "prepare" not in captured


def test_legacy_build_access_without_commands_remains_compatible(
    tmp_path: Path, monkeypatch
) -> None:
    _pre_sim_config(
        tmp_path,
        "[flows.sim]\npre_sim_build_access = 'legacy-per-test'\n",
    )
    execution, target, _raw_path, _captured = _execution_fixture(tmp_path, monkeypatch)

    assert _build_coverage(execution, target).success


@pytest.mark.parametrize(
    "surface",
    [
        "compile",
        pytest.param(
            "image",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="find mutation command is POSIX-specific"
            ),
        ),
        "raw",
        "hook_evidence",
    ],
)
def test_pre_sim_cannot_mutate_authenticated_coverage_surfaces(
    tmp_path: Path, monkeypatch, surface: str
) -> None:
    raw_path = tmp_path / "artifacts" / "raw.dat"
    hook_path = tmp_path / "artifacts" / "hook.json"
    paths: dict[str, Path] = {
        "compile": tmp_path / "rtl" / "counter.sv",
        "raw": raw_path,
        "hook_evidence": hook_path,
    }
    command = (
        f"find {shlex.quote(str(tmp_path))} -name Vcounter_tb -exec "
        "sh -c 'printf changed >> \"$1\"' _ {} \\;"
        if surface == "image"
        else f"printf changed >> {shlex.quote(str(paths[surface]))}"
    )
    _pre_sim_config(tmp_path, f"[flows.sim]\npre_run_commands = [{json.dumps(command)}]\n")
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    request = _run_request(target, raw_path)
    if surface == "hook_evidence":
        request = replace(request, hook_evidence_path=hook_path)

    result = execution.run(request)

    assert result.verdict == "inconclusive"
    assert "changed during Pre-Sim Commands" in result.output
    assert result.pre_sim is not None
    assert result.pre_sim.status == "passed"
    assert "run_script" not in captured


@pytest.mark.parametrize(
    ("version_output", "provenance_revision", "fragments"),
    [
        pytest.param(
            "Verilator 5.050 2026-08-31 rev UNKNOWN.REV\n",
            None,
            (
                "Verilator 5.050 is not the pinned coverage collector "
                f"(expected __PIN__); "
                f"recreate the Sandbox on a rebuilt Sandbox Image: {UNKNOWN_SANDBOX_STEPS}",
            ),
            id="unpinned",
        ),
        pytest.param(
            "unexpected output\n",
            None,
            (
                "Verilator coverage collector version could not be determined",
                "expected __PIN__",
                UNKNOWN_SANDBOX_STEPS,
            ),
            id="undetermined",
        ),
        pytest.param(
            None,
            "0" * 40,
            (
                "Verilator coverage collector provenance does not match the pinned collector",
                "expected __PIN__",
            ),
            id="provenance-mismatch",
        ),
    ],
)
def test_build_reports_collector_version_and_provenance_problems(
    tmp_path: Path,
    monkeypatch,
    version_output: str | None,
    provenance_revision: str | None,
    fragments: tuple[str, ...],
) -> None:
    execution, target, _raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    if version_output is not None:
        captured["version"] = SubprocessResult(returncode=0, stdout=version_output)
    if provenance_revision is not None:
        (tmp_path / "BOOLEY-SOURCE.txt").write_text(
            f"release={PINNED_VERILATOR.tag}\nsource_revision={provenance_revision}\n",
            encoding="utf-8",
        )

    build = _build_coverage(execution, target)

    assert build.success is False
    pin = f"{PINNED_VERILATOR.tag} @ {PINNED_VERILATOR.commit}"
    for fragment in fragments:
        assert fragment.replace("__PIN__", pin) in build.output
    if version_output is not None and "UNKNOWN.REV" in version_output:
        assert "(booley session refresh)" not in build.output


def _execution_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    cocotb: bool = False,
    options: SimulationOptions | None = None,
    files: tuple[ResolvedFile, ...] = (),
):
    _write_target(tmp_path)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    build_root = tmp_path / "build" / "coverage"
    build_root.mkdir(parents=True)
    provenance = tmp_path / "BOOLEY-SOURCE.txt"
    provenance.write_text(
        f"release={PINNED_VERILATOR.tag}\nsource_revision={PINNED_VERILATOR.commit}\n",
        encoding="utf-8",
    )
    prepared = PreparedSimulationBuild(
        target=handle.selector,
        target_identity=handle.identity,
        resolved=SimpleNamespace(
            vlnv="acme:demo:counter-booleycoverage:1",
            cocotb_module="test_counter" if cocotb else "",
            parameters={},
            files=files,
            build_root=build_root,
            edam_path=build_root / "target.eda.yml",
            flow_options={},
        ),
        work_root=build_root,
        build_root=build_root,
        eda_tool="verilator",
        toplevel="counter_tb",
        make_argv=("make",),
    )
    captured = {}
    captured["cocotb"] = cocotb
    raw_path = tmp_path / "artifacts" / "raw.dat"
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.write_coverage_overlay",
        _fake_overlay(tmp_path),
    )
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.prepare_simulation_build",
        _fake_prepare(prepared, captured),
    )
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.prepare_adapter_invocation",
        _fake_adapter(captured),
    )
    execution = VerilatorCoverageExecution(
        handle,
        invoke=_fake_invoke(captured, raw_path),
        options=options or SimulationOptions(),
        provenance_path=provenance,
    )
    target = CoverageTarget(handle.identity, handle.selector, "counter_tb", "generated_main", ())
    return execution, target, raw_path, captured


def _fake_overlay(tmp_path: Path):
    def fake_overlay(*args, **kwargs):
        return CoverageOverlay(
            tmp_path / "missing-overlay.core", "::coverage:0", TraceMode.VCD_FIFO
        )

    return fake_overlay


def _fake_prepare(prepared, captured):
    def fake_prepare(*args, **kwargs):
        captured["prepare"] = kwargs
        root = kwargs["build_root"]
        root.mkdir(parents=True, exist_ok=True)
        edam = root / "target.eda.yml"
        edam.write_text("cores: {}\nfiles: []\n")
        resolved = SimpleNamespace(**{**vars(prepared.resolved), "edam_path": edam})
        return replace(prepared, resolved=resolved, work_root=root, build_root=root)

    return fake_prepare


def _fake_adapter(captured):
    def fake_adapter(work):
        captured["work"] = work
        return ["true"]

    return fake_adapter


def _fake_invoke(captured, raw_path: Path):
    def fake_invoke(command, *, timeout):
        captured.setdefault("timeouts", []).append((tuple(command), timeout))
        if command == ["verilator", "--version"]:
            return captured.get("version") or SubprocessResult(
                returncode=0,
                stdout=f"Verilator {PINNED_VERILATOR.tag.removeprefix('v')} 2026-09-05\n",
            )
        script = command[2]
        if "BOOLEY_BUILD_STAGE" in script:
            token = re.search(r"token=([0-9a-f]+)", script).group(1)
            image = captured["prepare"]["build_root"] / (
                "Vtop" if captured.get("cocotb") else "Vcounter_tb"
            )
            if not captured.get("no_image"):
                image.write_text("compiled image", encoding="utf-8")
                image.chmod(0o755)
            return SubprocessResult(
                returncode=0,
                stdout=f"BOOLEY_BUILD_STAGE token={token} rc=0 duration_ms=1\n",
            )
        work = captured["work"]
        verdict = "pass"
        staged_path = captured.get("staged_input_path")
        if staged_path is not None:
            staged_value = staged_path.read_text(encoding="utf-8")
            captured.setdefault("staged_values", []).append(staged_value)
            verdict = "pass" if staged_value == work.tests[0] else "fail"
        identity = AdapterTransportIdentity(
            adapter=work.adapter,
            attempt_token=work.attempt_token,
            target_identity=work.target_identity,
            selected_tests=work.tests,
            result_path=Path(work.adapter_result_path),
        )
        write_adapter_result(
            identity,
            captured.get("result")
            or AdapterResult(
                passed=verdict == "pass",
                inconclusive=False,
                sva_errors=0,
                tests=work.tests,
                test_results=tuple(AdapterTestResult(name, verdict) for name in work.tests),
            ),
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text("# SystemC::Coverage-3\n", encoding="utf-8")
        captured["run_script"] = script
        captured.setdefault("run_scripts", []).append(script)
        return captured.get("process") or SubprocessResult(
            returncode=0, stdout="[SIM_RESULT] PASSED\n"
        )

    return fake_invoke


@pytest.mark.parametrize("case", ["pass", "failed-run", "missing-image"])
def test_coverage_record_outcomes_follow_the_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A coverage build or Test Run records the Flow's verdict, not the exit alone (#4964)."""
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    flow = measured_flow(tmp_path)
    replayed = execution._invoke

    def measured(command, *, timeout):
        return flow._execute_boundary(
            canned_command(replayed(command, timeout=timeout)), timeout=timeout
        )

    execution._invoke = measured
    captured["no_image"] = case == "missing-image"
    if case == "failed-run":
        captured["result"] = AdapterResult(
            False, False, 0, ("wrap",), test_results=(AdapterTestResult("wrap", "fail"),)
        )

    build = _build_coverage(execution, target)
    run = execution.run(_run_request(target, raw_path)) if build.success else None

    assert build.success == (case != "missing-image")
    records = resource_history.load_records(tmp_path / ".booley")
    by_phase = {r["key"]["phase"]: (r["outcome"], r["returncode"]) for r in records}
    assert by_phase.get("sim_build") == (
        "exit_nonzero" if case == "missing-image" else "exit_zero",
        0,
    )
    if run is not None:
        assert by_phase["sim_run"] == ("exit_nonzero" if case == "failed-run" else "exit_zero", 0)


def test_coverage_build_run_and_identity_keep_distinct_budgets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    execution, target, raw_path, captured = _execution_fixture(
        tmp_path,
        monkeypatch,
        options=SimulationOptions(timeout_ms=5000, build_timeout_ms=7000),
    )

    assert _build_coverage(execution, target).success
    assert execution.run(_run_request(target, raw_path)).verdict == "pass"

    timeouts = captured["timeouts"]
    assert timeouts[0] == (("verilator", "--version"), 30)
    assert timeouts[1][1] == 7
    assert timeouts[2][1] == 95


def test_traced_coverage_run_deadline_adds_conversion_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    execution, target, raw_path, captured = _execution_fixture(
        tmp_path,
        monkeypatch,
        options=SimulationOptions(timeout_ms=5000, build_timeout_ms=7000),
    )

    assert _build_coverage(execution, target).success
    traced = replace(_run_request(target, raw_path), trace=True)
    execution.run(traced)

    # simulation timeout + default conversion budget (1800 s) + the fixed 90 s margin
    assert captured["timeouts"][2][1] == 5 + 1800 + 90


def _run_request(target: CoverageTarget, raw_path: Path) -> SimulationRunRequest:
    return SimulationRunRequest(
        target=target,
        test=SelectedCoverageRun("wrap"),
        run_id="run:001:wrap",
        raw_path=raw_path,
        hook_evidence_path=None,
        trace=False,
        argv_suffix=(f"+verilator+coverage+file+{raw_path}",),
        environment={"BOOLEY_COVERAGE_RUN_ID": "run:001:wrap"},
    )


@pytest.mark.parametrize("verdict", ["pass", "fail", "timeout"])
def test_batch_timeout_preserves_authoritative_per_test_verdict(
    tmp_path: Path, monkeypatch, verdict: str
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    captured["result"] = AdapterResult(
        passed=False,
        inconclusive=False,
        sva_errors=0,
        tests=("wrap",),
        termination="timeout",
        failure_kind="timeout",
        detail="coverage batch timed out",
        test_results=(
            AdapterTestResult(
                "wrap",
                verdict,
                detail="coverage batch timed out" if verdict == "timeout" else "",
                termination="timeout" if verdict == "timeout" else "completed",
                failure_kind="timeout" if verdict == "timeout" else "",
            ),
        ),
    )

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == verdict


@pytest.mark.parametrize("process_timeout", [False, True])
def test_missing_per_test_evidence_is_rejected_without_losing_process_timeout(
    tmp_path: Path, monkeypatch, process_timeout: bool
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    captured["result"] = AdapterResult(
        passed=False,
        inconclusive=True,
        sva_errors=0,
        tests=("wrap",),
        failure_kind="inconclusive",
    )
    captured["process"] = SubprocessResult(returncode=1, timed_out=process_timeout)

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == ("timeout" if process_timeout else "inconclusive")
    assert "omits required per-test verdicts" in result.output


@pytest.mark.parametrize(
    "termination", ["disk_budget", "sim_time_stall", "trace_stall", "fatal_init"]
)
def test_coverage_infrastructure_guard_preserves_abort_evidence(
    tmp_path: Path, monkeypatch, termination: str
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    detail = f"coverage guard {termination} stopped the simulator"
    captured["result"] = AdapterResult(
        passed=False,
        inconclusive=False,
        sva_errors=0,
        tests=("wrap",),
        termination=termination,
        failure_kind="infrastructure",
        detail=detail,
        test_results=(
            AdapterTestResult(
                "wrap",
                "fail",
                detail=detail,
                termination=termination,
                failure_kind="infrastructure",
            ),
        ),
    )

    result = execution.run(_run_request(target, raw_path))

    assert result.infrastructure_error is True
    assert result.verdict == "fail"
    assert result.termination == termination
    assert result.failure_kind == "infrastructure"
    assert result.simulator_returncode == 0
    assert detail in result.output
    # An infrastructure-guarded run stops the aggregate before it publishes observations,
    # so no Flow input reaches the observation mapping for this run result.
    observation = _coverage_observation(
        {
            "name": "wrap",
            "verdict": result.verdict,
            "termination": result.termination,
            "failure_kind": result.failure_kind,
            "error_tail": result.output,
        }
    )
    assert observation["execution"] == "aborted"
    assert observation["failure_class"] == "infrastructure"
    assert observation["functional"] == "not_observed"
    assert observation["assertions"] == "not_observed"
    assert observation["detail"]["termination"] == termination


@pytest.mark.parametrize("verdict", ["pass", "fail", "guard"])
def test_coverage_cleanup_failure_retains_authenticated_result(
    tmp_path: Path, monkeypatch, verdict: str
) -> None:
    from booley.flows.sim.execution.attempt import AdapterAttemptOutcome

    execution, target, raw_path, _captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    guarded = verdict == "guard"
    result = AdapterResult(
        verdict == "pass",
        False,
        0,
        ("wrap",),
        detail="primary diagnostic",
        termination="disk_budget" if guarded else "completed",
        failure_kind="infrastructure" if guarded else "",
        test_results=(
            AdapterTestResult(
                "wrap",
                "fail" if guarded else verdict,
                termination="disk_budget" if guarded else "completed",
                failure_kind="infrastructure" if guarded else "",
            ),
        ),
    )
    monkeypatch.setattr(
        verilator_coverage_execution,
        "execute_adapter_attempt",
        lambda *_args, **_kwargs: AdapterAttemptOutcome(
            SubprocessResult(returncode=0), result, "partial cleanup denied", "cleanup"
        ),
    )

    outcome = execution.run(_run_request(target, raw_path))

    assert outcome.verdict == ("fail" if guarded else verdict)
    assert outcome.termination == result.termination
    assert outcome.infrastructure_error is guarded
    assert "primary diagnostic" in outcome.output
    assert "artifact_persistence: partial cleanup denied" in outcome.output
    assert outcome.diagnostics[0].severity == "warning"
    assert outcome.diagnostics[0].code == "COV_ARTIFACT_PERSISTENCE_CLEANUP_FAILED"


@pytest.mark.parametrize("configured", ["runs/literal", "runs/{test}/{attempt}"])
def test_coverage_pre_sim_project_relative_hook_uses_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    from booley.flows.sim.record.run_directory import expand_run_directory

    (tmp_path / "hook.py").write_text(
        "import json, os\n"
        "print(json.dumps(dict(cwd=os.getcwd(), root=os.environ['BOOLEY_PROJECT_ROOT'], "
        "run=os.environ['BOOLEY_RUN_CWD'])))\n",
        encoding="utf-8",
    )
    _pre_sim_config(
        tmp_path,
        "[flows.sim]\nrun_cwd = "
        + json.dumps(configured)
        + "\npre_run_commands = "
        + json.dumps([shlex.quote(sys.executable) + " hook.py"])
        + "\n",
    )
    run = expand_run_directory(
        configured,
        project_root=tmp_path,
        simulation_record_id="record",
        target_key="sim",
        work_item_key="alpha",
        attempt_key="attempt",
    ).path
    run.mkdir(parents=True)
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    _bind_bundle(execution, tmp_path, run)
    result = execution.run(_run_request(target, raw_path))
    assert result.verdict == "pass"
    assert result.pre_sim is not None and result.pre_sim.status == "passed"
    hook = json.loads(result.pre_sim.stdout_tail)
    assert Path(hook["cwd"]).resolve() == tmp_path.resolve()
    assert Path(hook["root"]).resolve() == tmp_path.resolve()
    assert Path(hook["run"]).resolve() == run.resolve() != tmp_path.resolve()
    assert "run_script" in captured


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("stage", ["build", "run"])
def test_coverage_log_failure_remains_advisory(tmp_path, monkeypatch, observed, stage):
    from contextlib import nullcontext
    from unittest.mock import patch

    from booley.flows.sim.live_progress import LiveProgressSink, install_progress

    execution, target, raw, captured = _execution_fixture(tmp_path, monkeypatch)
    sink = LiveProgressSink(tmp_path / "progress.json", tmp_path, "coverage")
    with install_progress(sink) if observed else nullcontext():
        if stage == "run":
            assert _build_coverage(execution, target).success
        with patch("booley.flows.run_log.begin_run_log", side_effect=OSError("full")):
            if stage == "build":
                assert _build_coverage(execution, target).success
            else:
                assert execution.run(_run_request(target, raw)).verdict == "pass"
                assert raw.exists()
    assert captured["timeouts"]
    if observed:
        assert sink.error == "OSError: full"


def test_coverage_lease_wait_is_bounded_by_the_build_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A busy slot ends the coverage build after the build timeout, not 60 s (#4843)."""
    execution, target, _raw_path, _captured = _execution_fixture(
        tmp_path, monkeypatch, options=SimulationOptions(build_timeout_ms=1000)
    )
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    slot = simulation_build_slot(handle, SimulationBuildVariant(trace=False, coverage=True).name)
    slot.mkdir(parents=True)

    with (slot / "lease.lock").open("a+", encoding="utf-8") as holder:
        acquire_file_lock(holder)
        started = time.monotonic()
        result = _build_coverage(execution, target)
        waited = time.monotonic() - started

    assert result.success is False
    assert result.infrastructure_error is True
    assert "busy for 1s" in result.output
    # A loose upper bound on a real 1 s wait; the old fixed wait was 60 s.
    assert waited < 30
