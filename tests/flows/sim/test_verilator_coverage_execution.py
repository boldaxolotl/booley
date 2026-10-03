from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.coverage_execution import (
    _coverage_observation,
    _publish_coverage_build_failure,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.serial_execution import _attempt, _build_attempt
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.coverage_overlay import CoverageOverlay
from booley.flows.sim.coverage_transaction import CoverageTargetOutcome
from booley.flows.sim.execution.contract import PreSimEvidence, SimulationOptions
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    VERILATOR_COVERAGE_INSTRUMENTATION,
    CoverageTarget,
    SelectedCoverageTest,
    SimulationBuildRequest,
    SimulationBuildResult,
    SimulationBuildVariant,
    SimulationRunRequest,
)
from booley.flows.sim.verilator_coverage_execution import (
    VerilatorCoverageExecution,
    prepare_coverage_collection,
)
from booley.fusesoc.fusesoc_registry import core_target_coverage_errors
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.test_campaign_crash_matrix import _admission
from tests.flows.sim.test_campaign_manifest_codec import _manifest, _sha


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
    assert core_target_coverage_errors(core, "sim") == []
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


def test_coverage_runs_pre_sim_commands_before_the_adapter(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    marker = tmp_path / "pre-sim.txt"
    staged = tmp_path / "staged-input.txt"
    staged.write_text("stale", encoding="utf-8")
    project_dir.joinpath("booley.toml").write_text(
        "[flows.sim]\n"
        'pre_run_commands = [\'printf "%s\\n%s\\n%s\\n%s\\n%s" '
        '"$BOOLEY_TEST_NAME" "$BOOLEY_TEST_NAMES" "$BOOLEY_TARGET" '
        '"$BOOLEY_RUN_CWD" "${BOOLEY_BUILD_ROOT-unset}" > pre-sim.txt\', '
        '\'printf "%s" "$BOOLEY_TEST_NAME" > staged-input.txt\']\n',
        encoding="utf-8",
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project_dir.joinpath("booley.toml").write_text(
        "[flows.sim]\n"
        'pre_run_commands = [\'printf "%s\\n" "$BOOLEY_TEST_NAME" >> firings.txt\', '
        '\'printf "%s" "$BOOLEY_TEST_NAME" > staged-input.txt\']\n',
        encoding="utf-8",
    )
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch, cocotb=True)
    staged = tmp_path / "staged-input.txt"
    staged.write_text("stale", encoding="utf-8")
    captured["staged_input_path"] = staged
    assert _build_coverage(execution, target).success

    for index, name in enumerate(("gap", "full"), start=1):
        request = replace(
            _run_request(target, raw_path),
            test=SelectedCoverageTest(name),
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


def test_snapshot_bound_coverage_uses_authoritative_run_cwd_for_pre_sim(
    tmp_path: Path, monkeypatch
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project_dir.joinpath("booley.toml").write_text(
        '[flows.sim]\npre_run_commands = [\'printf "%s" "$BOOLEY_RUN_CWD" > bound-cwd.txt\']\n',
        encoding="utf-8",
    )
    execution, target, raw_path, _captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    source_root, artifacts = execution.authenticated_image()
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    for artifact in artifacts:
        destination = snapshot_root / artifact.relative_to(source_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, destination)
    run_cwd = tmp_path / "attempt" / "run"
    run_cwd.mkdir(parents=True)
    execution.bind_authenticated_attempt(snapshot_root, run_cwd)

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "pass"
    assert (tmp_path / "bound-cwd.txt").read_text(encoding="utf-8") == str(run_cwd)


@pytest.mark.parametrize("snapshot_bound", [False, True])
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
def test_pre_sim_reauthenticates_direct_and_snapshot_bound_images(
    tmp_path: Path, monkeypatch, snapshot_bound: bool, mutation: str
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    source_root, artifacts = execution.authenticated_image()
    image = artifacts[0]
    if snapshot_bound:
        snapshot_root = tmp_path / "snapshot"
        destination = snapshot_root / image.relative_to(source_root)
        destination.parent.mkdir(parents=True)
        shutil.copy2(image, destination)
        run_cwd = tmp_path / "attempt" / "run"
        run_cwd.mkdir(parents=True)
        execution.bind_authenticated_attempt(snapshot_root, run_cwd)
        image = destination
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project_dir.joinpath("booley.toml").write_text(
        "[flows.sim]\npre_run_commands = ['booley-command-that-does-not-exist']\n",
        encoding="utf-8",
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project_dir.joinpath("booley.toml").write_text(
        "[flows.sim]\npre_run_commands = ['true']\npre_sim_build_access = 'legacy-per-test'\n",
        encoding="utf-8",
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project_dir.joinpath("booley.toml").write_text(
        "[flows.sim]\npre_sim_build_access = 'legacy-per-test'\n",
        encoding="utf-8",
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
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    command = (
        f"find {shlex.quote(str(tmp_path))} -name Vcounter_tb -exec "
        "sh -c 'printf changed >> \"$1\"' _ {} \\;"
        if surface == "image"
        else f"printf changed >> {shlex.quote(str(paths[surface]))}"
    )
    project_dir.joinpath("booley.toml").write_text(
        f"[flows.sim]\npre_run_commands = [{json.dumps(command)}]\n",
        encoding="utf-8",
    )
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


def test_build_reports_unpinned_collector_version(tmp_path: Path, monkeypatch) -> None:
    execution, target, _raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    captured["version"] = SubprocessResult(
        returncode=0,
        stdout="Verilator 5.050 2026-08-31 rev UNKNOWN.REV\n",
    )

    build = _build_coverage(execution, target)

    expected = (
        "Verilator 5.050 is not the pinned coverage collector "
        f"(expected {PINNED_VERILATOR.tag} @ {PINNED_VERILATOR.commit}); "
        "rebuild the Sandbox image (booley session refresh)"
    )
    assert build.success is False
    assert expected in build.output


def test_build_reports_when_collector_version_cannot_be_determined(
    tmp_path: Path, monkeypatch
) -> None:
    execution, target, _raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    captured["version"] = SubprocessResult(returncode=0, stdout="unexpected output\n")

    build = _build_coverage(execution, target)

    assert build.success is False
    assert "Verilator coverage collector version could not be determined" in build.output
    assert f"expected {PINNED_VERILATOR.tag} @ {PINNED_VERILATOR.commit}" in build.output


def test_build_reports_collector_provenance_mismatch(tmp_path: Path, monkeypatch) -> None:
    execution, target, _raw_path, _captured = _execution_fixture(tmp_path, monkeypatch)
    (tmp_path / "BOOLEY-SOURCE.txt").write_text(
        f"release={PINNED_VERILATOR.tag}\nsource_revision={'0' * 40}\n",
        encoding="utf-8",
    )

    build = _build_coverage(execution, target)

    assert build.success is False
    assert "Verilator coverage collector provenance does not match the pinned collector" in (
        build.output
    )
    assert f"expected {PINNED_VERILATOR.tag} @ {PINNED_VERILATOR.commit}" in build.output


def test_coverage_image_changed_after_build_cannot_launch(tmp_path: Path, monkeypatch) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    build = _build_coverage(execution, target)
    assert build.success
    image = captured["prepare"]["build_root"] / "Vcounter_tb"
    image.write_text("changed image", encoding="utf-8")

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == "inconclusive"
    assert "image changed" in result.output
    assert "run_script" not in captured


def _execution_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    cocotb: bool = False,
    options: SimulationOptions | None = None,
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
            cocotb_module="test_counter" if cocotb else "",
            parameters={},
            files=(),
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
                returncode=0, stdout="Verilator 5.052 2026-09-05\n"
            )
        script = command[2]
        if "BOOLEY_BUILD_STAGE" in script:
            token = re.search(r"token=([0-9a-f]+)", script).group(1)
            image = captured["prepare"]["build_root"] / (
                "Vtop" if captured.get("cocotb") else "Vcounter_tb"
            )
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


def _run_request(target: CoverageTarget, raw_path: Path) -> SimulationRunRequest:
    return SimulationRunRequest(
        target=target,
        test=SelectedCoverageTest("wrap"),
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


def _pin_reusable_session(monkeypatch) -> None:
    """Prove reuse wiring without a real toolchain: pin the key and closure."""
    session = "booley.flows.sim.build_session.SimulationBuildSession"
    monkeypatch.setattr(f"{session}.reusable_key", lambda *_args, **_kwargs: "pinned-key")
    monkeypatch.setattr(f"{session}._fresh_read_closure", lambda *_args: ({}, ""))
    monkeypatch.setattr(f"{session}._read_closure_matches", lambda *_args: True)
    monkeypatch.setattr(
        f"{session}._restore_prepared_build",
        lambda _self, root, candidate: replace(candidate, work_root=root, build_root=root),
    )


def _build_stages(captured) -> int:
    return sum("BOOLEY_BUILD_STAGE" in command[-1] for command, _ in captured["timeouts"])


def test_coverage_variant_reuses_its_promoted_image(tmp_path: Path, monkeypatch) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    _pin_reusable_session(monkeypatch)

    first = _build_coverage(execution, target)
    first_root = execution.authenticated_image()[0]
    assert first.success and _build_stages(captured) == 1
    assert execution.run(_run_request(target, raw_path)).verdict == "pass"

    second = _build_coverage(execution, target)

    assert second.success, second.output
    assert "verified Simulation build reuse (hit)" in second.output
    assert _build_stages(captured) == 1
    root, artifacts = execution.authenticated_image()
    assert root == first_root
    assert [path.name for path in artifacts] == ["Vcounter_tb"]
    assert len(list(root.parent.iterdir())) == 1  # the unused candidate was discarded
    assert execution.run(_run_request(target, raw_path)).verdict == "pass"


def test_tampered_retained_coverage_image_rebuilds(tmp_path: Path, monkeypatch) -> None:
    execution, target, _raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    _pin_reusable_session(monkeypatch)
    assert _build_coverage(execution, target).success
    root, artifacts = execution.authenticated_image()
    artifacts[0].write_text("stale image", encoding="utf-8")

    rebuilt = _build_coverage(execution, target)

    assert rebuilt.success
    assert _build_stages(captured) == 2
    assert execution.authenticated_image()[0] != root


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


class _CoverageFailedBuildExecutor:
    def execute(self, request):
        attempt, _run_directory = _attempt(request)
        request.store.publish_attempt(request.attempt_directory, attempt)
        directory = request.attempt_directory / "private-build"
        directory.mkdir()
        build_attempt = _build_attempt(request)
        request.store.publish_build_attempt(directory, build_attempt)
        outcome = CoverageTargetOutcome(
            "sim",
            2,
            directory / "coverage.json",
            directory / "sim.json",
            {"error": "collector build transport unavailable"},
        )
        return _publish_coverage_build_failure(
            request,
            directory,
            build_attempt,
            outcome,
            SimulationBuildResult(
                success=False,
                infrastructure_error=True,
                reason="collector build transport unavailable",
                output="compiler could not be started",
            ),
            0.1,
            lambda _boundary: None,
        )


def _refresh_coverage_work_identity(document):
    workload = document["workload"]
    item = document["work_items"][0]
    variant = document["build_variants"][0]
    variant["kind"] = "coverage"
    recipe = {
        "kind": "coverage",
        "source_closure": variant["source_closure"],
        **{
            key: workload[key]
            for key in ("source_recipe", "build_recipe", "eda", "trace", "coverage")
        },
    }
    variant["recipe_sha256"] = _sha(recipe)
    variant["build_variant_id"] = "variant:" + _sha(recipe).removeprefix("sha256:")
    item["build_variant_id"] = variant["build_variant_id"]
    item_digest = _sha(
        {
            key: value
            for key, value in item.items()
            if key not in {"work_item_id", "fingerprint_sha256"}
        }
    )
    item["fingerprint_sha256"] = item_digest
    item["work_item_id"] = "item:0000:" + item_digest.removeprefix("sha256:")[:16]


def _coverage_build_failure_request(tmp_path: Path):
    document = _manifest()
    document.pop("fingerprints")
    workload = document["workload"]
    workload["coverage"] = True
    workload["eda"] = {"kind": "verilator", "version": "5.052"}
    workload["build_recipe"]["eda_tool"] = "verilator"
    item = document["work_items"][0]
    item["kind"] = "coverage_aggregate"
    item["selection"] = {"kind": "named", "names": ["wrap"]}
    document["required_suite"]["names"] = ["wrap"]
    document["required_suite"]["default_invocation"] = False
    document["required_suite"]["source_path"] = "tests.toml"
    _refresh_coverage_work_identity(document)
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        create_simulation_campaign_plan(finalize_manifest(document)),
        project,
        invocation.parent,
        CampaignPolicy(),
        invocation,
        _admission(),
    )

    return request, invocation


def test_coverage_infrastructure_build_failure_publishes_terminal_campaign(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request, invocation = _coverage_build_failure_request(tmp_path)
    # Isolate Simulation build/result durability from nested collector publication.
    monkeypatch.setattr(SimulationCampaign, "_publish_coverage_reference", lambda *_args: None)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.coverage_execution._coverage_evidence", lambda *_args: []
    )

    outcome = SimulationCampaign(_CoverageFailedBuildExecutor()).run(request)

    assert outcome.complete is True
    assert outcome.aggregate_grade == "error"
    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    recovery = store.scan()
    assert recovery.complete
    assert not recovery.interrupted
    item = recovery.items[0]
    result = json.loads((store.work_item_directory(item.work_item_id) / "result.json").read_text())
    assert result["state"] == "aborted"
    assert result["executable_snapshot"] is None
    assert result["observations"]
    observation = result["observations"][0]
    assert observation["execution"] == "aborted"
    assert observation["failure_class"] == "infrastructure"
    assert observation["functional"] == "not_observed"
    assert observation["detail"]["code"] == "build_transport"
    resumed = SimulationCampaign(_CoverageFailedBuildExecutor()).run(request)
    assert resumed.complete
    assert store.scan().items[0].attempt_count == 1


@pytest.mark.parametrize("verdict", ["pass", "fail", "guard"])
def test_coverage_cleanup_failure_retains_authenticated_result(verdict) -> None:
    from booley.flows.sim.execution.attempt import AdapterAttemptOutcome
    from booley.flows.sim.verilator_coverage_execution import _simulation_run_result

    guarded = verdict == "guard"
    result = AdapterResult(
        verdict == "pass",
        False,
        0,
        ("smoke",),
        detail="primary diagnostic",
        termination="disk_budget" if guarded else "completed",
        failure_kind="infrastructure" if guarded else "",
        test_results=(
            AdapterTestResult(
                "smoke",
                "fail" if guarded else verdict,
                termination="disk_budget" if guarded else "completed",
                failure_kind="infrastructure" if guarded else "",
            ),
        ),
    )
    outcome = _simulation_run_result(
        AdapterAttemptOutcome(
            SubprocessResult(returncode=0), result, "partial cleanup denied", "cleanup"
        ),
        "smoke",
    )
    assert outcome.verdict == ("fail" if guarded else verdict)
    assert outcome.termination == result.termination
    assert outcome.infrastructure_error is guarded
    assert "primary diagnostic" in outcome.output
    assert "artifact_persistence: partial cleanup denied" in outcome.output
    assert outcome.diagnostics[0].severity == "warning"
    assert outcome.diagnostics[0].code == "COV_ARTIFACT_PERSISTENCE_CLEANUP_FAILED"
