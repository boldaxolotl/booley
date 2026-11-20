import json
import os
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.progress_lifecycle import abort_open_progress
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.coverage_record_store import load_coverage_record
from booley.flows.sim.coverage_reference import (
    REFERENCE_SCHEMA,
    resolve_coverage_record_reference,
)
from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record import (
    SimulationRecord,
    collect_pre_sim_firings,
    resolve_report_artifact_reference,
)
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SimulationRunResult
from booley.mcp.flow_adapter import flow_schema
from booley.runtime.filesystem_utils import safe_rmtree
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.memory_budget_support import fail_budget
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S


def _coverage_project(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tests: str = '[sim_0]\ntests = ["reset"]\n',
    tools: tuple[str, ...] = ("verilator",),
) -> Path:
    """Create a coverage Project with its Tests registered; return its data directory."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(root, tools)
    data = root / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(tests)
    return data


def _coverage(
    work_dir: Path,
    execution: NativeExecution | None = None,
    *,
    checkpoint=None,
    factory=None,
    **request,
):
    """Run a coverage invocation, or a resume, against a fresh or shared execution."""
    if "resume_from" not in request:
        request = {"target": "sim_0", "coverage": True, **request}
    request.setdefault("report_dir", work_dir / "reports")
    flow = SimulateFlow(
        coverage_execution=factory or (lambda *_args: execution or NativeExecution()),
        simulation_record_publication_checkpoint=checkpoint,
    )
    return flow.execute(SimRequest(work_dir=work_dir, **request))


def _pin_revision(monkeypatch: pytest.MonkeyPatch, revision: str) -> None:
    """Fix the checkout revision every Flow module reads, as resume requires."""
    for module in ("flow", "flow_resume", "record.resume"):
        monkeypatch.setattr(f"booley.flows.sim.{module}.git_full_sha", lambda *_args: revision)


class _SplitMetricsExecution(NativeExecution):
    def payload(self, _hits):
        records = (
            ("line", 1, "block", 1),
            ("expr", 2, "true", 1),
            ("branch", 3, "true", 0),
        )
        body = "".join(
            "C '\x01f\x02rtl/counter.sv"
            f"\x01l\x02{line}\x01n\x021\x01h\x02TOP.counter"
            f"\x01t\x02{metric}\x01o\x02{outcome}' {hits}\n"
            for metric, line, outcome, hits in records
        )
        return "# SystemC::Coverage-3\n" + body


class _PassingSplitMetricsExecution(_SplitMetricsExecution):
    def payload(self, hits):
        return super().payload(hits).replace("\x01o\x02true' 0", "\x01o\x02true' 1")


class _CustomMainHookExecution(NativeExecution):
    def __init__(self, events: list[dict[str, object]], *, missing_raw: bool = False) -> None:
        super().__init__(missing=missing_raw)
        self.events = events

    def run(self, request):
        result = super().run(request)
        assert request.hook_evidence_path is not None
        request.hook_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        request.hook_evidence_path.write_text(
            json.dumps(
                {
                    "$schema": "booley.coverage-hook/v1",
                    "run_id": request.run_id,
                    "events": self.events,
                }
            ),
            encoding="utf-8",
        )
        return result


def _configure_custom_main_coverage(
    root: Path, recipe: str = "custom_main_hooks: [start_hook, write_hook], reset_included: false"
) -> None:
    (root / "main.cpp").write_text("int main() { return 0; }\n")
    core = root / "counter.core"
    core.write_text(
        core.read_text()
        .replace(
            "files: [rtl/counter.sv]",
            "files: [rtl/counter.sv, {main.cpp: {file_type: cppSource}}]",
        )
        .replace(
            "flow_options: {tool: verilator}",
            "flow_options: {tool: verilator, booley: {coverage: {" + recipe + "}}}",
        )
    )


def _prepare_console_scope_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _coverage_project(
        tmp_path,
        monkeypatch,
        '[sim_0]\ntests = ["half", "reset", "wrap", "carry", "zero"]\n'
        '[sim_1]\ntests = ["gap", "overflow", "underflow", "saturate"]\n',
        tools=("verilator", "verilator"),
    )
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))


def _lifecycle_labels(tmp_path: Path) -> list[object]:
    """The `display_label` of each lifecycle event the Flow wrote to the display sink."""
    events = [
        json.loads(line)
        for line in (tmp_path / "runtime" / "display.jsonl").read_text("utf-8").splitlines()
    ]
    return [
        event["display_label"]
        for event in events
        if event["type"] in {"endpoint_start", "endpoint_end"}
    ]


def test_console_lifecycle_reports_executed_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_console_scope_project(tmp_path, monkeypatch)

    result = _coverage(tmp_path, target="sim_0,sim_1")

    assert result.exit_code == 0, result.outcome
    assert _lifecycle_labels(tmp_path) == ["2 targets · 9 tests", "2 targets · 9 tests"]
    assert {
        observation["test"]
        for simulation_record in result.outcome.detail["records"].values()
        for observation in simulation_record["observations"]
    } == {"half", "reset", "wrap", "carry", "zero", "gap", "overflow", "underflow", "saturate"}
    for simulation_record in result.outcome.detail["records"].values():
        path = tmp_path / "reports/sim/1" / simulation_record["artifacts"]["coverage"]["path"]
        evaluation = resolve_coverage_record_reference(path).loaded.coverage_record.evaluation
        assert evaluation["status"] == "not_requested"


def test_pre_outcome_coverage_failure_keeps_prepared_console_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_console_scope_project(tmp_path, monkeypatch)

    def fail_before_outcome(_simulation_record, _request):
        raise RuntimeError("failed before nested outcome")

    monkeypatch.setattr(SimulationRecord, "run", fail_before_outcome)
    result = _coverage(tmp_path, target="sim_0,sim_1")

    assert result.exit_code == 2
    assert _lifecycle_labels(tmp_path) == ["2 targets · 9 tests", "2 targets · 9 tests"]
    assert result.outcome.detail["targets"]["sim_0"]["evaluation"] == "not_requested"
    assert result.outcome.detail["pending_targets"] == ["sim_0", "sim_1"]


COMPLETION_LABEL_CASES = (
    (({"test": "half"}, {"test": ""}), "2 targets · tests"),
    (({"test": "half"},), "2 targets · test half"),
)


def test_simulation_record_completion_label_requires_complete_test_identity(
    tmp_path: Path,
) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim_0,sim_1", "--coverage"])

    for observations, expected in COMPLETION_LABEL_CASES:
        # No Flow run records an observation with an empty Test name; only a direct call reaches that label.
        label = flow._simulation_record_completed_display_label(
            [SimpleNamespace(observations=observations)]  # type: ignore[list-item]
        )
        assert label == expected


def _target_headline(report_text: str, selector: str) -> str:
    return next(line for line in report_text.splitlines() if line.startswith(f"{selector}:"))


def _target_block(report_text: str, selector: str) -> list[str]:
    """The card lines of *selector*: its headline and the indented lines after it."""
    lines = report_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{selector}:"))
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return lines[start:end]


def _card_path(block: list[str], label: str) -> Path:
    """The path on the block's ``  <label>: <path>`` line."""
    prefix = f"  {label}: "
    return Path(next(line for line in block if line.startswith(prefix)).removeprefix(prefix))


def _assert_card_names_run_files(report_text: str, tmp_path: Path) -> None:
    """The ``sim_0`` card lists its metrics and names the existing run report and Simulation Record."""
    block = _target_block(report_text, "sim_0")
    assert _card_path(block, "report") == (tmp_path / "reports/sim/1/report.json").resolve()
    assert _card_path(block, "report").is_file()
    assert "  coverage: line 100.0%" in block
    coverage_record_file = (tmp_path / "reports/sim/1/targets/sim_0/coverage.json").resolve()
    assert _card_path(block, "Coverage Record") == coverage_record_file
    assert coverage_record_file.is_file()


def _assert_ungated_report(result, tmp_path: Path) -> None:
    simulation_record_id = result.outcome.detail["records"]["sim_0"]["simulation_record_id"]
    expected_headline = (
        "sim_0: simulation PASS · coverage collection COMPLETE · "
        f"evaluation NOT_REQUESTED (Simulation Record {simulation_record_id})"
    )
    assert _target_headline(result.outcome.report_text, "sim_0") == expected_headline
    persisted = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert persisted["report_text"] == result.outcome.report_text


def _prepare_sim_custom_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    core = tmp_path / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "sim_custom"))
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_custom]\ntests = ["gap"]\n')


def _assert_enclosing_result_is_authenticated(coverage_record_path, resolved) -> None:
    from booley.flows.sim.coverage_analysis_input import read_coverage_record
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.store import SimulationRecordStore

    store = SimulationRecordStore(coverage_record_path.parent / "record")
    work_item_id = resolved.reference.document["simulation_work_item_id"]
    simulation_result = store.work_item_directory(work_item_id) / "result.json"
    result_raw = simulation_result.read_bytes()
    hostile = json.loads(result_raw)
    hostile["evidence"][0]["sha256"] = "sha256:" + "0" * 64
    simulation_result.chmod(0o600)
    simulation_result.write_bytes(canonical_json_bytes(hostile))
    with pytest.raises(ValueError, match="enclosing Simulation Record"):
        read_coverage_record(coverage_record_path)
    simulation_result.write_bytes(result_raw)
    simulation_result.chmod(0o400)


def _assert_simulation_record_report_schema(
    tmp_path: Path, simulation_record: dict[str, object]
) -> None:
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert report["$schema"] == "booley.simulation-report/v3"
    assert report["detail"]["records"]["sim_0"]["artifacts"] == simulation_record["artifacts"]


def _assert_projection_tamper_rejected(coverage_record_path: Path) -> None:
    from booley.flows.sim.coverage_analysis_input import read_coverage_record

    projection_path = coverage_record_path.with_name("simulation.json")
    projection_raw = projection_path.read_bytes()
    projection = json.loads(projection_raw)
    projection["simulation_record_manifest"]["path"] = "other/manifest.json"
    projection_path.write_text(json.dumps(projection))
    with pytest.raises(ValueError, match="not a regular file"):
        read_coverage_record(coverage_record_path)
    projection_path.write_bytes(projection_raw)


def test_flow_produces_numbered_target_reports_with_public_coverage_input(tmp_path, monkeypatch):
    from booley.flows.sim.coverage_analysis_input import read_coverage_record

    _coverage_project(tmp_path, monkeypatch, '[sim_0]\ntests = ["reset", "wrap"]\n')
    result = _coverage(tmp_path)
    assert result.exit_code == 0
    _assert_card_names_run_files(result.outcome.report_text, tmp_path)
    target = result.outcome.detail["targets"]["sim_0"]
    coverage_reference = target["coverage_record"]
    assert coverage_reference["path_base"] == "report_invocation"
    coverage_record_path = tmp_path / "reports/sim/1" / coverage_reference["path"]
    assert coverage_record_path == tmp_path / "reports/sim/1/targets/sim_0/coverage.json"
    assert json.loads(coverage_record_path.read_text())["$schema"] == REFERENCE_SCHEMA
    resolved = resolve_coverage_record_reference(coverage_record_path)
    assert resolved.loaded.coverage_record.evaluation["status"] == "not_requested"
    _assert_ungated_report(result, tmp_path)
    simulation_record = result.outcome.detail["records"]["sim_0"]
    assert simulation_record["dependency"] == "local_record"
    assert simulation_record["artifacts"]["manifest"]["path"] == (
        "targets/sim_0/record/manifest.json"
    )
    assert simulation_record["artifacts"]["simulation"]["path"] == (
        "targets/sim_0/simulation.json"
    )
    assert simulation_record["artifacts"]["coverage"]["path"] == "targets/sim_0/coverage.json"
    _assert_simulation_record_report_schema(tmp_path, simulation_record)
    assert simulation_record["observation_counts"] == {
        "execution": {"completed": 2},
        "functional": {"pass": 2},
        "assertions": {"clean": 2},
    }
    assert read_coverage_record(coverage_record_path).coverage_record.coverage_record_id
    _assert_enclosing_result_is_authenticated(coverage_record_path, resolved)
    _assert_projection_tamper_rejected(coverage_record_path)
    assert not (tmp_path / "reports/sim_sim_0.json").exists()
    properties = flow_schema(SimulateFlow())["properties"]
    assert properties["coverage"]["type"] == "boolean"
    assert properties["no_waivers"]["type"] == "boolean"


def test_coverage_simulation_projection_error_tail_is_text(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")

    for verdict, exit_code in (("pass", 0), ("fail", 1)):
        root = tmp_path / verdict
        root.mkdir()
        project(root)
        data = root / ".booley"
        data.mkdir()
        (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

        result = _coverage(root, NativeExecution(verdict=verdict))

        assert result.exit_code == exit_code
        projection = json.loads((root / "reports/sim/1/targets/sim_0/simulation.json").read_text())
        error_tail = projection["tests"][0]["error_tail"]
        if verdict == "pass":
            assert error_tail == ""
        else:
            assert error_tail == "coverage simulation fail"
            assert "{" not in error_tail


def _frozen_hook_execution(tmp_path, data, received):
    class PreSimPassingExecution(NativeExecution):
        def run(self, request):
            result = super().run(request)
            from booley.flows.sim.execution.pre_sim import run_pre_sim_commands

            evidence = run_pre_sim_commands(
                SimpleNamespace(project_root=tmp_path, selector="sim_0"),
                test_names=(request.test.name,),
                build_root=tmp_path / "build",
                eda_tool="verilator",
                timeout_s=SUBPROCESS_TIMEOUT_S,
                resource_flags=(),
                commands=received[-1][1],
            )
            return SimulationRunResult(result.verdict, result.output, evidence)

    def execution_factory(_handle, options, commands, access):
        received.append((options, commands, access))
        (data / "booley.toml").write_text(
            '[flows.sim]\npre_run_commands = ["echo live-hook; exit 7"]\n'
        )
        return PreSimPassingExecution()

    return execution_factory


def test_injected_coverage_execution_receives_frozen_pre_sim_policy(tmp_path, monkeypatch) -> None:
    data = _coverage_project(tmp_path, monkeypatch)
    (data / "booley.toml").write_text(
        "[flows.sim]\n"
        "build_timeout_ms = 7000\n"
        "timeout_ms = 5000\n"
        'pre_run_commands = ["echo frozen-hook"]\n'
        'pre_sim_build_access = "legacy-per-test"\n'
    )
    received = []

    execution_factory = _frozen_hook_execution(tmp_path, data, received)

    result = _coverage(tmp_path, factory=execution_factory)

    assert result.exit_code == 0
    assert received == [
        (
            SimulationOptions(timeout_ms=5000, build_timeout_ms=7000),
            ("echo frozen-hook",),
            "legacy-per-test",
        )
    ]
    assert "pre_run_commands (1 line(s)) for sim_0/reset: rc=0 in " in result.outcome.report_text

    firings = collect_pre_sim_firings(
        tmp_path / "reports/sim/1/targets/sim_0/record/manifest.json"
    )
    assert len(firings) == 1
    assert firings[0].document["stdout_tail"] == "frozen-hook\n"


def _publish_real_hook_series(request, handle, commands, count):
    from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
    from booley.flows.sim.record.pre_sim_evidence import publish_pre_sim_firing

    for ordinal in range(1, count + 1):
        evidence = run_pre_sim_commands(
            handle,
            test_names=("alpha",),
            build_root=request.attempt_directory / "build",
            eda_tool="icarus",
            timeout_s=SUBPROCESS_TIMEOUT_S,
            resource_flags=(),
            commands=commands,
            run_cwd=".",
        )
        assert evidence is not None
        publish_pre_sim_firing(request, evidence, ordinal=ordinal)


def test_many_current_coverage_firings_keep_all_lines_and_bounded_preview(tmp_path, monkeypatch):
    import json

    from booley.flows.sim.record.store import SimulationRecordStore
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.flows.sim.simulation_record_test_support import _executor, _handle, _request
    from tests.flows.sim.test_endpoint_record_lifecycle import _pre_sim_plan

    def transform(document):
        document["workload"]["coverage"] = True
        document["work_items"][0]["kind"] = "coverage_aggregate"

    commands = ("echo hook-out; echo hook-err >&2",)
    plan, _disclosures = _pre_sim_plan("immutable", commands, transform)
    invocation = tmp_path / "reports/1"
    store = SimulationRecordStore(invocation / "targets/sim/record")
    store.publish_manifest(plan.manifest)
    handle = _handle(tmp_path)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    flow = SimulateFlow()
    flow._current_published_pre_sim_keys = set()
    # The preview's read counts (one Manifest read, at most 32 previews for 40 firings) show only in spies on its readers.
    request = replace(request, pre_sim_firing_published=flow._record_pre_sim_firing)
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    _publish_real_hook_series(request, handle, commands, 40)
    flow._reserved_invocation_dir = invocation
    reads = _observe_pre_sim_projection(monkeypatch)
    # The preview's read counts (one Manifest read, at most 32 previews for 40 firings) show only in spies on its readers.
    result = flow._attach_published_pre_sim(
        EndpointOutcome(exit_code=2, report_text="interrupted coverage")
    )
    assert result.detail["pre_sim_total"] == result.detail["pre_sim_current"] == 40
    assert (
        len(result.detail["pre_sim_lines"]) == result.report_text.count("pre_run_commands") == 40
    )
    assert len(result.detail["pre_sim_runs"]) <= 32
    assert len(json.dumps(result.detail["pre_sim_runs"]).encode()) <= 16 * 1024
    assert result.detail["pre_sim_preview_truncated"] is True
    assert reads["manifest"] == 1
    assert reads["decoder_manifest"] == 1
    assert reads["preview"] <= 32
    firings = collect_pre_sim_firings(store.manifest_path)
    assert len(firings) == 40
    assert all(
        firing.document["stdout_tail"] == "hook-out\n"
        and firing.document["stderr_tail"] == "hook-err\n"
        for firing in firings
    )


@pytest.mark.parametrize(
    ("coverage_recipe", "message"),
    [
        (
            '{reset_included: "false"}',
            "targets.sim_0.flow_options.booley.coverage.reset_included must be a boolean",
        ),
        (
            "{bogus_key: 1}",
            "targets.sim_0.flow_options.booley.coverage.bogus_key is not a supported coverage key",
        ),
        (
            "{custom_main_hooks: write_hook}",
            "targets.sim_0.flow_options.booley.coverage.custom_main_hooks must be an array",
        ),
    ],
)
def test_coverage_recipe_schema_errors_are_atomic_preflight_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coverage_recipe: str,
    message: str,
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    core = tmp_path / "counter.core"
    core.write_text(
        core.read_text().replace(
            "flow_options: {tool: verilator}",
            f"flow_options: {{tool: verilator, booley: {{coverage: {coverage_recipe}}}}}",
        )
    )
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    result = _coverage(tmp_path)

    assert result.exit_code == 2
    assert message in result.outcome.report_text
    assert result.outcome.detail["findings"][0]["code"] == "COV_TARGET_INVALID"
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_valid_custom_main_coverage_recipe_passes_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    _configure_custom_main_coverage(tmp_path)
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    result = _coverage(tmp_path)

    assert result.exit_code == 2
    assert "COV_WINDOW_HOOK_MISSING" in result.outcome.report_text
    assert (tmp_path / "reports/sim/1").is_dir()


def test_custom_main_without_write_hook_fails_preflight_with_its_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    _configure_custom_main_coverage(tmp_path, "reset_included: true")
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    result = _coverage(tmp_path)

    assert result.exit_code == 2
    assert _finding_codes(result.outcome) == ["COV_CUSTOM_MAIN_WRITE_HOOK_REQUIRED"]
    for expected in (
        "sim_0: ",
        "flow_options.booley.coverage.custom_main_hooks",
        ".core",
        "booley_coverage_write()",
    ):
        assert expected in result.outcome.report_text


@pytest.mark.parametrize(
    ("events", "missing_raw", "expected_code"),
    [
        (
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "write", "sequence": 2, "success": False},
            ],
            True,
            "COV_WRITE_HOOK_FAILED",
        ),
        (
            [
                {"hook": "write", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
                {"hook": "write", "sequence": 3, "success": True},
            ],
            False,
            "COV_CUSTOM_MAIN_HOOK_OUT_OF_ORDER",
        ),
    ],
)
def test_custom_main_hook_failures_are_public_collector_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, object]],
    missing_raw: bool,
    expected_code: str,
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    _configure_custom_main_coverage(tmp_path)
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    result = _coverage(tmp_path, _CustomMainHookExecution(events, missing_raw=missing_raw))

    assert result.exit_code == 2
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["collection"] == "collector_error"
    assert expected_code in result.outcome.report_text


def test_publication_failure_reports_collector_error_as_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-commit publication failure maps a nested `collector_error` to the endpoint value."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    _configure_custom_main_coverage(tmp_path)
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    def fail_after_summary(boundary: str) -> None:
        if boundary == "after:summary_replace":
            raise OSError("injected post-commit publication failure")

    result = _coverage(
        tmp_path,
        _CustomMainHookExecution(
            [{"hook": "start", "sequence": 1, "success": True}], missing_raw=True
        ),
        checkpoint=fail_after_summary,
    )

    assert result.exit_code == 2
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["collection"] == "infrastructure_error"


def test_post_commit_collection_failure_reports_incomplete_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed `incomplete` collection survives a later publication failure."""

    class MissingInput(NativeExecution):
        def run(self, request):
            return SimulationRunResult(
                "fail",
                "$readmemh: Cannot open memory.hex",
                termination="fatal_init",
                failure_kind="missing_input",
            )

        def command(self, request):
            raise AssertionError("merge must not run after a design abort")

    def fail_after_commit(boundary: str) -> None:
        if boundary == "after:coverage_record":
            raise OSError("injected post-commit publication failure")

    _coverage_project(tmp_path, monkeypatch)
    result = _coverage(tmp_path, MissingInput(), checkpoint=fail_after_commit)

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_0"]
    assert detail["collection"] == "incomplete"
    assert detail["evaluation"] == "not_requested"
    assert detail["passed"] is False
    assert detail["simulation"] == "aborted"
    assert "coverage_record" not in detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX unreadable-directory regression")
@pytest.mark.parametrize("relative", ["unreadable", ".booley/unreadable"])
def test_unreadable_unrelated_directory_does_not_block_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    _coverage_project(tmp_path, monkeypatch)
    unreadable = tmp_path / relative
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        result = _coverage(tmp_path)
    finally:
        unreadable.chmod(0o700)

    assert result.exit_code == 0
    assert result.outcome.detail["targets"]["sim_0"]["collection"] == "complete"


def test_copied_complete_invocation_remains_analyzable_and_independent(tmp_path, monkeypatch):
    from booley.flows.sim.coverage_analysis_input import read_coverage_record

    _coverage_project(tmp_path, monkeypatch, '[sim_0]\ntests = ["reset", "wrap"]\n')
    reports = tmp_path / "reports"
    result = _coverage(tmp_path)
    assert result.exit_code == 0
    original = reports / "sim/1"
    copied_reports = tmp_path / "copied-reports"
    copied = copied_reports / "sim/1"
    copied.parent.mkdir(parents=True)
    shutil.copytree(original, copied)
    copied_coverage = copied / "targets/sim_0/coverage.json"

    assert read_coverage_record(copied_coverage).coverage_record.coverage_record_id
    hidden_original = original.with_name(".origin-hidden")
    original.rename(hidden_original)
    try:
        assert read_coverage_record(copied_coverage).coverage_record.coverage_record_id
    finally:
        hidden_original.rename(original)
    safe_rmtree(copied_reports)

    original_coverage = original / "targets/sim_0/coverage.json"
    assert read_coverage_record(original_coverage).coverage_record.coverage_record_id


def test_multi_target_collector_error_preserves_completed_and_later_targets(tmp_path, monkeypatch):
    _coverage_project(
        tmp_path,
        monkeypatch,
        "".join(f'[sim_{i}]\ntests = ["reset"]\n' for i in range(3)),
        tools=("verilator", "verilator", "verilator"),
    )
    flow = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution(
            missing=handle.name == "sim_1"
        )
    )
    request = flow.parse_args(
        [
            "--target",
            "sim_2",
            "--target",
            "sim_0,sim_1",
            "--work-dir",
            str(tmp_path),
            "--coverage",
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    result = flow.execute(request)
    assert result.exit_code == 2
    assert request.target == "sim_2,sim_0,sim_1"
    assert list(result.outcome.detail["targets"]) == ["sim_2", "sim_0", "sim_1"]
    assert list(result.outcome.detail["records"]) == ["sim_2", "sim_0", "sim_1"]
    assert result.outcome.detail["targets"]["sim_2"]["collection"] == "complete"
    simulation_record_id = result.outcome.detail["records"]["sim_1"]["simulation_record_id"]
    coverage_record_path = next(
        (tmp_path / "reports/sim/1/targets/sim_1").rglob("coverage-record/coverage.json")
    )
    invalid_manifest = json.loads(coverage_record_path.read_text())
    assert invalid_manifest["scoring"] == {
        "status": "invalid",
        "reason": "collector_error",
    }
    assert invalid_manifest["rollups"] == []
    failed_block = _target_block(result.outcome.report_text, "sim_1")
    assert not any(line.startswith("  coverage: ") for line in failed_block)
    assert _card_path(failed_block, "Coverage Record").is_file()
    completed_block = _target_block(result.outcome.report_text, "sim_2")
    assert "  coverage: line 100.0%" in completed_block
    assert _card_path(completed_block, "Coverage Record").is_file()
    headline = _target_headline(result.outcome.report_text, "sim_1")
    assert headline == (
        "sim_1: simulation PASS · coverage collection COLLECTOR_ERROR "
        "(COV_RAW_FILE_MISSING) · evaluation NOT_REQUESTED "
        f"(Simulation Record {simulation_record_id})"
    )
    assert (
        f"sim_1: PASS (Simulation Record {simulation_record_id})" not in result.outcome.report_text
    )


def test_fatal_coverage_failure_lists_completed_simulation_records(tmp_path, monkeypatch):
    _coverage_project(
        tmp_path,
        monkeypatch,
        "".join(f'[sim_{i}]\ntests = ["reset"]\n' for i in range(2)),
        tools=("verilator", "verilator"),
    )
    original = SimulationRecord.run
    calls: list[None] = []

    def run(self, request):
        calls.append(None)
        if len(calls) == 2:  # Targets run in request order: the second one is sim_1
            raise RuntimeError("second Target exploded")
        return original(self, request)

    monkeypatch.setattr(SimulationRecord, "run", run)
    result = _coverage(tmp_path, target="sim_0,sim_1")

    assert result.exit_code == 2
    lines = result.outcome.report_text.splitlines()
    assert "second Target exploded" in lines[0]
    expected = (tmp_path / "reports/sim/1/targets/sim_0/coverage.json").resolve()
    assert [line for line in lines if line.startswith("  Coverage Record: ")] == [
        f"  Coverage Record: {expected}"
    ]
    assert expected.is_file()


def test_unmatched_coverage_failure_lists_completed_simulation_records(tmp_path, monkeypatch):
    from booley.flows.sim import record_presentation as sim_flow

    # `SimulateFlow` reaches the unmatched-failure card only for a failed request that matches no prepared plan; no Flow input does.
    monkeypatch.setattr(sim_flow, "_simulation_record_structured_details", lambda *_args: {})
    finished = SimpleNamespace(
        manifest_path=tmp_path / "targets/sim_0/record/manifest.json",
        coverage_reference=object(),
    )

    # `SimulateFlow` reaches the unmatched-failure card only for a failed request that matches no prepared plan; no Flow input does.
    result = sim_flow._unmatched_coverage_failure([finished], {}, RuntimeError("no such Target"))

    assert result.report_text.splitlines() == [
        "Simulation Record coverage execution failed: cannot match failed Target to the "
        "prepared Coverage plan (no such Target)",
        f"  Coverage Record: {(tmp_path / 'targets/sim_0/coverage.json').resolve()}",
    ]


def test_shared_execution_failure_terminalizes_without_losing_completed_reports(
    tmp_path, monkeypatch
):
    _coverage_project(
        tmp_path,
        monkeypatch,
        "".join(f'[sim_{i}]\ntests = ["reset"]\n' for i in range(3)),
        tools=("verilator", "verilator", "verilator"),
    )

    class Unavailable(NativeExecution):
        def build(self, request):
            raise FileNotFoundError("Sandbox executable disappeared")

    flow = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: (
            Unavailable() if handle.name == "sim_1" else NativeExecution()
        )
    )
    result = flow.execute(
        SimRequest(
            target="sim_0,sim_1,sim_2",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )
    assert result.exit_code == 2
    assert result.outcome.detail["targets"]["sim_0"]["collection"] == "complete"
    failed = result.outcome.detail["targets"]["sim_1"]
    assert failed["abort_remaining"] is False
    assert failed["passed"] is False
    assert failed["simulation"] == "aborted"
    assert failed["collection"] == "collector_error"
    outer = result.outcome.detail["targets"]["sim_1"]["evaluation"]
    target_root = tmp_path / "reports/sim/1/targets/sim_1"
    nested_path = next(
        target_root.glob("record/work-items/*/attempts/*/coverage-record/coverage.json")
    )
    nested = load_coverage_record(nested_path).coverage_record
    assert outer == nested.evaluation["status"] == "not_requested"
    assert set(result.outcome.detail["records"]) == {"sim_0", "sim_1", "sim_2"}
    assert result.outcome.detail["records"]["sim_1"]["grade"] == "error"
    assert result.outcome.detail["records"]["sim_2"]["grade"] == "pass"
    assert result.outcome.detail["pending_targets"] == []
    assert "completion_error" not in result.outcome.detail
    assert list((target_root / "record").glob("work-items/*/result.json"))


def test_nested_coverage_publication_failure_preserves_simulation_truth(tmp_path, monkeypatch):
    _prepare_sim_custom_project(tmp_path, monkeypatch)

    def checkpoint(boundary):
        if boundary == "before:coverage_record":
            raise OSError("injected nested Coverage Record publication failure")

    result = _coverage(tmp_path, checkpoint=checkpoint, target="sim_custom")

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_custom"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == "not_requested"


def test_atomic_preflight_creates_no_report_or_build_path(tmp_path, monkeypatch):
    _coverage_project(
        tmp_path,
        monkeypatch,
        '[sim_0]\ntests = ["reset"]\n[sim_1]\ntests = ["reset"]\n',
        tools=("verilator", "icarus"),
    )
    before = set(tmp_path.rglob("*"))
    result = _coverage(tmp_path, target="sim_0,sim_1")
    assert result.exit_code == 2
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("selected", "expected_runs", "exit_code"),
    [
        (("wrap",), ["wrap"], 1),
        (("reset", "wrap"), ["reset", "wrap"], 1),
        (None, ["reset"], 0),
    ],
)
def test_explicit_coverage_selection_executes_configured_skips(
    tmp_path, monkeypatch, selected, expected_runs, exit_code
):
    _coverage_project(
        tmp_path, monkeypatch, '[sim_0]\ntests = ["reset", "wrap"]\nskip = ["wrap"]\n'
    )

    class PerTestVerdict(NativeExecution):
        def run(self, request):
            super().run(request)
            return SimulationRunResult("fail" if request.test.name == "wrap" else "pass")

    execution = PerTestVerdict()
    result = _coverage(tmp_path, execution, test=selected)

    assert result.exit_code == exit_code
    assert [request.test.name for request in execution.runs] == expected_runs


def test_projection_publication_failure_returns_structured_report_with_target_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare_sim_custom_project(tmp_path, monkeypatch)

    def fail_projection(*_args, **_kwargs) -> None:
        raise OSError(5, "injected projection publication failure")

    monkeypatch.setattr(
        "booley.flows.sim.acceptance.write_compatibility_projection", fail_projection
    )
    result = _coverage(tmp_path, _SplitMetricsExecution(), target="sim_custom")

    assert result.exit_code == 2
    # The Target whose projection failed stays pending: no card entry, no Coverage Record line.
    assert result.outcome.detail["pending_targets"] == ["sim_custom"]
    assert result.outcome.detail["completion_error"]["operation"] == (
        "publish Simulation Record projections"
    )
    assert "acceptance" not in result.outcome.detail
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert report["detail"] == result.outcome.detail
    assert not (tmp_path / "reports/sim/1/targets/sim_custom/simulation.json").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("tests_toml", "selected", "execution"),
    [
        pytest.param(None, (), _PassingSplitMetricsExecution, id="registered-suite"),
        pytest.param(
            '[sim_custom]\ntests = ["gap", "other"]\n',
            ("other",),
            NativeExecution,
            id="other-test-selected",
        ),
    ],
)
def test_plain_coverage_headline_reports_evaluation_not_requested(
    tmp_path, monkeypatch, tests_toml, selected, execution
):
    _prepare_sim_custom_project(tmp_path, monkeypatch)
    if tests_toml is not None:
        (tmp_path / ".booley/tests.toml").write_text(tests_toml)
    options = {"test": selected} if selected else {}
    result = _coverage(tmp_path, execution(), target="sim_custom", **options)

    assert result.exit_code == 0
    simulation_record_id = result.outcome.detail["records"]["sim_custom"]["simulation_record_id"]
    assert _target_headline(result.outcome.report_text, "sim_custom") == (
        "sim_custom: simulation PASS · coverage collection COMPLETE · evaluation NOT_REQUESTED "
        f"(Simulation Record {simulation_record_id})"
    )


def test_public_cli_aliases_select_same_request():
    from booley.flows.builtin_cli import build_parser

    flow = SimulateFlow()
    help_text = build_parser(flow).format_help()
    assert "--coverage" in help_text
    assert "--cov" in help_text
    assert flow.parse_args(["--target", "sim", "--coverage"]).coverage is True
    assert flow.parse_args(["--target", "sim", "--cov"]).coverage is True
    assert flow.parse_args(["--target", "sim"]).coverage is False


@pytest.mark.parametrize("config", ["coverage = true", "flows = []", "[flows]\nsim = 1"])
def test_invalid_coverage_tables_return_preflight_error(tmp_path, monkeypatch, config):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley"
    data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    (data / "booley.toml").write_text(config)
    before = set(tmp_path.rglob("*"))
    result = SimulateFlow().execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True))
    assert result.exit_code == 2
    assert "Coverage Preflight" in result.outcome.report_text
    assert set(tmp_path.rglob("*")) == before


def test_shared_build_prerequisite_failure_publishes_durable_terminal_results(
    tmp_path, monkeypatch
):
    from booley.flows.sim.verilator_coverage import SimulationBuildResult

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    data = tmp_path / ".booley"
    data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    (data / "tests.toml").write_text('[sim_0]\ntests=["reset"]\n[sim_1]\ntests=["reset"]\n')
    built = []

    class Unavailable(NativeExecution):
        def build(self, request):
            built.append(request.target.identity)
            return SimulationBuildResult(
                False,
                "Verilator 5.050 is not the pinned coverage collector; refresh the Sandbox image",
                infrastructure_error=True,
            )

    result = _coverage(tmp_path, target="sim_0,sim_1", factory=lambda *_args: Unavailable())
    assert result.exit_code == 2
    assert "Verilator 5.050 is not the pinned coverage collector" in result.outcome.report_text
    assert len(built) == 2
    assert result.outcome.detail["pending_targets"] == []
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["passed"] is False
    assert target["simulation"] == "aborted"
    assert result.outcome.detail["records"]["sim_0"]["grade"] == "error"
    assert "completion_error" not in result.outcome.detail
    assert "coverage_record" in target
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
    assert progress["phase"] == "aborted"
    assert progress["complete"] is True
    assert progress["completed_targets"] == ["sim_0", "sim_1"]
    assert progress["pending_targets"] == []


def _prepare_coverage_origin(tmp_path, monkeypatch, *, skipped=()):
    """Create the two-test coverage Project shared by the interruption fixtures."""
    _pin_revision(monkeypatch, "a" * 40)
    _coverage_project(
        tmp_path,
        monkeypatch,
        f'[sim_0]\ntests = ["reset", "wrap"]\nskip = {json.dumps(list(skipped))}\n',
    )
    return tmp_path / "reports"


def _interrupt_coverage_collection(
    tmp_path,
    monkeypatch,
    *,
    selected=None,
    skipped=(),
    boundary=None,
    no_waivers=False,
    signalled=False,
    report_dir=None,
):
    """Interrupt a coverage run before its collection records a result.

    Without ``boundary`` the interrupt lands inside the executor, leaving the
    aggregate work item ``interrupted``.  With ``"after:manifest_commit"`` it
    lands right after the Manifest is published, leaving the item ``pending``.
    With ``signalled`` the process ends in the executor without unwinding, as a
    SIGTERM does: only the handler's progress abort runs.
    """
    reports = _prepare_coverage_origin(tmp_path, monkeypatch, skipped=skipped)
    reports = report_dir or reports

    class Interrupted(NativeExecution):
        def run(self, request):
            super().run(request)
            if signalled:
                abort_open_progress()
            raise KeyboardInterrupt("simulated process interruption")

    def checkpoint(actual):
        if actual == boundary:
            raise KeyboardInterrupt(f"simulated interruption at {boundary}")

    request = SimRequest(
        target="sim_0",
        test=selected,
        work_dir=tmp_path,
        coverage=True,
        no_waivers=no_waivers,
        report_dir=reports,
    )
    with pytest.raises(KeyboardInterrupt):
        SimulateFlow(
            coverage_execution=lambda handle, options, _commands, _access: Interrupted(),
            simulation_record_publication_checkpoint=checkpoint if boundary else None,
        ).execute(request)
    return reports, request


def test_signal_aborted_coverage_progress_stays_terminal(tmp_path, monkeypatch):
    reports, _request = _interrupt_coverage_collection(tmp_path, monkeypatch, signalled=True)
    progress_path = reports / "sim/1/progress.json"
    aborted = progress_path.read_bytes()
    assert json.loads(aborted)["phase"] == "aborted"

    refused = _coverage(tmp_path, resume_from=reports / "sim/1/targets/sim_0/record/manifest.json")
    assert refused.exit_code == 2
    assert progress_path.read_bytes() == aborted
    assert _coverage(tmp_path).exit_code == 0
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert (progress["phase"], progress["pending_targets"]) == ("aborted", ["sim_0"])


def _interrupt_coverage_publication(tmp_path, monkeypatch, *, selected=None, skipped=()):
    """Interrupt a coverage run after its collection result committed.

    The aggregate work item is complete, so the origin is a legitimate
    publication-only resume source.  Returns ``(reports, request, runs)``.
    """
    reports = _prepare_coverage_origin(tmp_path, monkeypatch, skipped=skipped)
    runs: list[str | None] = []

    class Counted(NativeExecution):
        def run(self, request):
            runs.append(request.test)
            return super().run(request)

    def checkpoint(actual):
        if actual == "before:summary_replace":
            raise KeyboardInterrupt("simulated publication interruption")

    request = SimRequest(
        target="sim_0",
        test=selected,
        work_dir=tmp_path,
        coverage=True,
        report_dir=reports,
    )
    with pytest.raises(KeyboardInterrupt):
        SimulateFlow(
            coverage_execution=lambda handle, options, _commands, _access: Counted(),
            simulation_record_publication_checkpoint=checkpoint,
        ).execute(request)
    return reports, request, runs


def test_resume_continues_its_origin_and_writes_no_dependency_receipt(tmp_path, monkeypatch):
    from booley.flows.sim.record.artifact_reference import (
        resolve_report_artifact_reference,
    )
    from booley.flows.sim.record.codec import MANIFEST_MAX_BYTES

    reports, request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    original_path = next(
        (reports / "sim/1/targets/sim_0/record").glob(
            "work-items/*/attempts/*/coverage-record/native/raw/001-reset@1.dat"
        )
    )
    original = original_path.read_bytes()
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    resumed_reports = reports
    result = _coverage(tmp_path, resume_from=manifest, report_dir=resumed_reports)
    assert result.exit_code == 0
    assert (reports / "sim/1/targets/sim_0/coverage.json").is_file()
    assert original_path.read_bytes() == original
    attempts = list((reports / "sim/1/targets/sim_0/record").glob("work-items/*/attempts/*"))
    assert len(attempts) == 1
    projection = reports / "sim/1/targets/sim_0/simulation.json"
    document = json.loads(projection.read_text())
    assert document["simulation_record_manifest"]["path"] == "record/manifest.json"
    assert document["simulation_record_manifest"]["path_base"] == "origin_target"
    assert document["coverage_record"]["path"] == "coverage.json"
    assert document["coverage_record"]["path_base"] == "origin_target"
    assert not (resumed_reports / "sim/2/targets/sim_0/simulation.json").exists()
    resume_report = json.loads((resumed_reports / "sim/2/report.json").read_text())
    resume_simulation_record = resume_report["detail"]["records"]["sim_0"]
    assert resume_simulation_record["dependency"] == "external_origin_record"
    assert resume_simulation_record["artifacts"]["manifest"]["path_base"] == "reports_root"
    assert resume_simulation_record["artifacts"]["manifest"]["path"] == (
        "sim/1/targets/sim_0/record/manifest.json"
    )
    assert not (manifest.parent / "dependency-receipts").exists()
    resolved = resolve_report_artifact_reference(
        resumed_reports / "sim/2/report.json",
        resume_simulation_record["artifacts"]["manifest"],
        expected_kind="simulation_record_manifest",
        expected_owner=resume_simulation_record["simulation_record_id"],
        maximum=MANIFEST_MAX_BYTES,
    )
    assert resolved.path == manifest
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(request)
    assert result.exit_code == 0
    assert (reports / "sim/3/targets/sim_0/coverage.json").is_file()


def test_resume_leaves_an_old_dependency_receipt_untouched(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    receipts = manifest.parent / "dependency-receipts"
    receipts.mkdir()
    old = receipts / f"{'0' * 64}.json"
    old_bytes = json.dumps(
        {
            "$schema": "booley.simulation-record-dependency/v1",
            "simulation_record_id": "00000000-0000-4000-8000-000000000000",
            "manifest_sha256": "0" * 64,
            "origin_invocation": str(reports / "sim/1"),
            "origin_target": str(reports / "sim/1/targets/sim_0"),
            "dependent_invocation": str(reports / "sim/9"),
            "dependent_invocation_id": 9,
        }
    ).encode()
    old.write_bytes(old_bytes)
    result = _coverage(tmp_path, resume_from=manifest)
    assert result.exit_code == 0
    assert (reports / "sim/2/report.json").is_file()
    assert list(receipts.iterdir()) == [old]
    assert old.read_bytes() == old_bytes


def test_resume_reports_busy_origin_lock(tmp_path, monkeypatch):
    from booley.flows.sim.record_reports import simulation_record_invocation_lock

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    fail_budget(monkeypatch)

    with simulation_record_invocation_lock(reports / "sim/1"):
        result = _coverage(tmp_path, resume_from=manifest)

    assert result.exit_code == 2
    assert "resume origin is busy" in result.outcome.report_text
    assert "memory_admission_warning" not in result.outcome.detail


def test_requeued_resume_attempt_keeps_the_origin_lock_it_already_holds(tmp_path, monkeypatch):
    """The OOM requeue resumes the same origin again: this process must not refuse its own lock."""
    from types import SimpleNamespace

    origin = tmp_path / "sim/1"
    validated = SimpleNamespace(
        path=origin / "targets/sim_0/record/manifest.json",
        booley_dir=None,
    )
    flow = SimulateFlow()
    origin.mkdir(parents=True)

    # The Job runner's OOM requeue repeats this call on the Flow object that already holds the
    # origin lock; no Flow input repeats it within one attempt.
    lock = flow._lock_simulation_record_resume_origin
    first = lock(validated, None)
    second = lock(validated, None)

    assert (first, second) == (None, None)
    flow.publication_resources.close()


def test_resume_retains_explicit_configured_skipped_test(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(
        tmp_path, monkeypatch, selected=("wrap",), skipped=("wrap",)
    )
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    execution = NativeExecution()

    result = _coverage(tmp_path, execution, resume_from=manifest, report_dir=tmp_path / "resumed")

    assert result.exit_code == 0
    # The origin collected "wrap"; a publication-only resume never re-runs it.
    assert execution.runs == []
    report_path = tmp_path / "resumed/sim/1/report.json"
    report = json.loads(report_path.read_text())
    detail = report["detail"]
    simulation_record = detail["records"]["sim_0"]
    assert simulation_record["dependency"] == "external_origin_record"
    assert set(simulation_record["artifacts"]) == {"manifest", "simulation", "coverage"}
    assert {reference["path_base"] for reference in simulation_record["artifacts"].values()} == {
        "external_origin_target"
    }
    assert (
        detail["targets"]["sim_0"]["coverage_record"] == simulation_record["artifacts"]["coverage"]
    )
    assert result.outcome.report_text == (
        "sim_0: simulation PASS · coverage collection COMPLETE · evaluation NOT_REQUESTED "
        f"(Simulation Record {simulation_record['simulation_record_id']})\n"
        f"  manifest: {manifest.resolve()}\n"
        f"  report: {(tmp_path / 'resumed/sim/1/report.json').resolve()}\n"
        "  coverage: line 100.0%\n"
        f"  Coverage Record: {(manifest.parents[1] / 'coverage.json').resolve()}"
    )
    resolved = resolve_report_artifact_reference(
        report_path,
        simulation_record["artifacts"]["manifest"],
        expected_kind="simulation_record_manifest",
        expected_owner=simulation_record["simulation_record_id"],
        maximum=1024 * 1024,
        external_origin_target=manifest.parents[1],
    )
    assert resolved.path == manifest


def test_coverage_resume_uses_manifest_when_origin_progress_is_missing(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    (reports / "sim/1/progress.json").unlink()
    resumed_reports = tmp_path / "resumed"

    result = _coverage(tmp_path, resume_from=manifest, report_dir=resumed_reports)

    assert result.exit_code == 0
    progress = json.loads((resumed_reports / "sim/1/progress.json").read_text())
    assert progress["phase"] == "complete"
    assert progress["completed_targets"] == ["sim_0"]
    assert progress["pending_targets"] == []


def test_interrupted_coverage_resume_publishes_terminal_progress(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    resumed_reports = tmp_path / "resumed"

    def interrupt_again(boundary):
        if boundary == "before:summary_replace":
            raise KeyboardInterrupt("resume interrupted")

    with pytest.raises(KeyboardInterrupt, match="resume interrupted"):
        _coverage(
            tmp_path, checkpoint=interrupt_again, resume_from=manifest, report_dir=resumed_reports
        )

    progress = json.loads((resumed_reports / "sim/1/progress.json").read_text())
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == []
    assert progress["pending_targets"] == ["sim_0"]


def _simulation_record_bytes(root):
    return {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("state", ["interrupted", "pending"])
def test_resume_refuses_unfinished_coverage_collection(tmp_path, monkeypatch, state, dry_run):
    from booley.flows.sim.record.store import SimulationRecordStore

    reports, _request = _interrupt_coverage_collection(
        tmp_path,
        monkeypatch,
        boundary="after:manifest_commit" if state == "pending" else None,
    )
    simulation_record = reports / "sim/1/targets/sim_0/record"
    manifest = simulation_record / "manifest.json"
    observed = SimulationRecordStore(manifest.parent).scan()
    assert len(getattr(observed, state)) == 1
    tree_before = set(reports.rglob("*"))
    bytes_before = _simulation_record_bytes(simulation_record)
    factory_calls = []

    def factory(*_args):
        factory_calls.append(_args)
        return NativeExecution()

    result = _coverage(tmp_path, factory=factory, resume_from=manifest, dry_run=dry_run)

    assert result.exit_code == 2
    text = result.outcome.report_text
    assert "booley flow sim --target sim_0 --coverage" in text
    assert "no recorded result" in text
    assert factory_calls == []
    assert set(reports.rglob("*")) == tree_before
    assert _simulation_record_bytes(simulation_record) == bytes_before
    assert result.outcome.detail[state] == list(getattr(observed, state))


@pytest.mark.parametrize(
    ("selected", "no_waivers", "root", "expected"),
    [
        pytest.param(("reset",), False, None, ["--test=reset@1", "--coverage"], id="subset"),
        pytest.param(
            None,
            True,
            "my reports",
            ["--coverage", "--no-waivers", "--report-dir"],
            id="default-suite",
        ),
        pytest.param(
            ("wrap@7", "reset"),
            False,
            "my reports",
            ["--test=reset@1", "--test=wrap@7", "--coverage", "--report-dir"],
            id="seeded",
        ),
    ],
)
def test_resume_refusal_command_repeats_the_record(
    tmp_path, monkeypatch, selected, no_waivers, root, expected
):
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    report_root = tmp_path / (root or ".booley/flow-reports")
    reports, _request = _interrupt_coverage_collection(
        tmp_path, monkeypatch, selected=selected, no_waivers=no_waivers, report_dir=report_root
    )
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    texts = {
        _coverage(tmp_path, resume_from=manifest, dry_run=dry_run).outcome.report_text
        for dry_run in (False, True)
    }
    assert len(texts) == 1
    text = texts.pop()
    line = next(line for line in text.splitlines() if "booley flow sim" in line)
    if expected[-1] == "--report-dir":
        expected = [*expected, str(report_root)]
    assert shlex.split(line) == ["booley", "flow", "sim", "--target", "sim_0", *expected]
    assert "Add the original" not in text
    argv = shlex.split(line)[3:]
    request = SimulateFlow().parse_args(["--work-dir", str(tmp_path), *argv])
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(request)
    assert result.exit_code == 0

    def frozen(root_dir, number):
        path = root_dir / f"sim/{number}/targets/sim_0/record/manifest.json"
        return json.loads(path.read_text())["work_items"][0]["test_runs"]

    assert frozen(report_root, 2) == frozen(report_root, 1)


def test_resume_refusal_preserves_corrupt_result_integrity_error(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    simulation_record = reports / "sim/1/targets/sim_0/record"
    result_path = next(simulation_record.glob("work-items/*/result.json"))
    raw = result_path.read_bytes()
    result_path.write_bytes(raw[: len(raw) // 2])

    result = _coverage(tmp_path, resume_from=simulation_record / "manifest.json")

    assert result.exit_code == 2
    assert result.outcome.report_text.startswith("Simulation Record integrity failure:")


def _crash_coverage_publication(tmp_path, monkeypatch, boundary, *, no_waivers=False):
    _pin_revision(monkeypatch, "b" * 40)
    _coverage_project(tmp_path, monkeypatch, '[sim_0]\ntests = ["reset", "wrap"]\n')
    reports = tmp_path / "reports"
    runs = []

    class CountedExecution(NativeExecution):
        def run(self, request):
            runs.append(request.test)
            return super().run(request)

    def execution_factory(_handle, _options, _commands, _access):
        return CountedExecution()

    armed = True

    def checkpoint(actual):
        nonlocal armed
        if armed and actual == boundary:
            armed = False
            raise OSError(f"injected crash at {boundary}")

    interrupted = _coverage(
        tmp_path, factory=execution_factory, checkpoint=checkpoint, no_waivers=no_waivers
    )
    assert interrupted.exit_code == 2
    public = reports / "sim/1/targets/sim_0/coverage.json"
    return reports, runs, public, execution_factory, interrupted


@pytest.mark.parametrize(
    ("boundary", "published"),
    [
        ("after:simulation_result", False),
        ("before:summary_replace", False),
        ("after:summary_replace", False),
        ("before:coverage_reference", False),
        ("after:coverage_reference", True),
    ],
)
def test_coverage_publication_crash_resumes_at_the_aggregate_boundary(
    tmp_path, monkeypatch, boundary, published
):
    """A finished collection with unfinished publication resumes without re-running."""
    from booley.flows.sim.coverage_record_store import load_coverage_record

    _reports, runs, public, execution_factory, interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, boundary
    )
    assert public.exists() is published
    failed = interrupted.outcome.detail["targets"]["sim_0"]
    assert failed["passed"] is True
    assert failed["simulation"] == "pass"
    assert failed["collection"] == "complete"
    assert failed["evaluation"] == "not_requested"
    assert ("coverage_record" in failed) is (boundary == "after:coverage_reference")
    completed_runs = tuple(runs)
    manifest = public.parent / "record/manifest.json"

    resumed = _coverage(tmp_path, factory=execution_factory, resume_from=manifest)

    assert resumed.exit_code == 0
    assert tuple(runs) == completed_runs
    assert resolve_coverage_record_reference(public).coverage_record_path.is_file()
    nested_coverage_records = list(
        public.parent.glob("record/work-items/*/attempts/*/coverage-record/coverage.json")
    )
    assert len(nested_coverage_records) == 1
    assert all(
        load_coverage_record(path).coverage_record.coverage_record_id
        for path in nested_coverage_records
    )


@pytest.mark.parametrize(
    "boundary", ["before:coverage_record", "after:coverage_record", "before:simulation_result"]
)
def test_coverage_collection_crash_refuses_resume(tmp_path, monkeypatch, boundary):
    """A collection without a recorded result is refused, never silently re-run."""
    _reports, runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, boundary
    )
    manifest = public.parent / "record/manifest.json"
    completed_runs = tuple(runs)
    nested_before = list(public.parent.glob("record/work-items/*/attempts/*/coverage-record"))

    resumed = _coverage(tmp_path, factory=execution_factory, resume_from=manifest)

    assert resumed.exit_code == 2
    assert "booley flow sim --target sim_0 --coverage" in resumed.outcome.report_text
    assert "no recorded result" in resumed.outcome.report_text
    assert tuple(runs) == completed_runs
    nested_after = list(public.parent.glob("record/work-items/*/attempts/*/coverage-record"))
    assert nested_after == nested_before


def test_resumed_coverage_publication_failure_preserves_current_attempt_truth(
    tmp_path, monkeypatch
):
    _reports, _runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result"
    )
    armed = True

    def checkpoint(boundary):
        nonlocal armed
        if armed and boundary == "before:summary_replace":
            armed = False
            raise OSError("injected resumed summary failure")

    resumed = _coverage(
        tmp_path,
        factory=execution_factory,
        checkpoint=checkpoint,
        resume_from=public.parent / "record/manifest.json",
    )

    assert resumed.exit_code == 2
    detail = resumed.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "complete"
    assert detail["evaluation"] == "not_requested"
    assert "coverage_metrics" in detail
    assert detail["coverage_metrics"]["line"] == {"percent": 100.0, "threshold": None, "met": None}


def test_failure_reference_encoding_cannot_mask_original_error(tmp_path, monkeypatch):
    _reports, _runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result"
    )
    armed = True

    def checkpoint(boundary):
        nonlocal armed
        if armed and boundary == "after:coverage_reference":
            armed = False
            raise OSError("injected resumed reference failure")

    def fail_reference(*_args, **_kwargs):
        raise OSError("injected report reference failure")

    monkeypatch.setattr(
        # No input makes reference encoding fail after the coverage reference checkpoint.
        "booley.flows.sim.record_presentation._report_artifact_reference",
        fail_reference,
    )
    resumed = _coverage(
        tmp_path,
        factory=execution_factory,
        checkpoint=checkpoint,
        resume_from=public.parent / "record/manifest.json",
    )

    assert resumed.exit_code == 2
    detail = resumed.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["error"] == "injected resumed reference failure"
    assert "coverage_record" not in detail


@pytest.mark.parametrize("resumed", [False, True], ids=["fresh", "resumed"])
def test_unreadable_publication_evidence_preserves_simulation_truth(
    tmp_path, monkeypatch, resumed
):
    if resumed:
        _reports, _runs, public, _factory, _interrupted = _crash_coverage_publication(
            tmp_path, monkeypatch, "after:simulation_result"
        )

    def unreadable(*_args, **_kwargs):
        raise OSError("nested coverage evidence unreadable")

    monkeypatch.setattr("booley.flows.sim.record_presentation.load_coverage_record", unreadable)
    if resumed:

        def checkpoint(boundary):
            if boundary == "before:summary_replace":
                raise OSError("resumed publication failed")

        result = _coverage(
            tmp_path,
            checkpoint=checkpoint,
            resume_from=public.parent / "record/manifest.json",
        )
    else:
        _reports, _runs, _public, _factory, result = _crash_coverage_publication(
            tmp_path, monkeypatch, "after:simulation_result"
        )

    assert result.exit_code == 2
    assert "targets" in result.outcome.detail
    detail = result.outcome.detail["targets"]["sim_0"]
    assert detail["simulation"] == "pass"
    assert detail["passed"] is True
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == "not_requested"
    assert [test["name"] for test in detail["tests"]] == ["reset", "wrap"]
    assert "coverage_record" not in detail


def _run_with_retention_reference_failure(monkeypatch, request, execution=None):
    calls = []

    def fail_once(public):
        calls.append(public)
        if len(calls) == 1:
            raise OSError("injected retention reference failure")
        return resolve_coverage_record_reference(public)

    with monkeypatch.context() as patch:
        patch.setattr(
            "booley.flows.sim.record_presentation.resolve_coverage_record_reference", fail_once
        )
        return SimulateFlow(
            coverage_execution=lambda *_args: execution or NativeExecution()
        ).execute(request)


def test_resumed_retention_failure_matches_fresh_failed_target(tmp_path, monkeypatch):
    reports, _runs, public, _factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result"
    )
    execution = NativeExecution()
    fresh = _run_with_retention_reference_failure(
        monkeypatch,
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "fresh"
        ),
    )
    resumed = _run_with_retention_reference_failure(
        monkeypatch,
        SimRequest(
            resume_from=public.parent / "record/manifest.json",
            work_dir=tmp_path,
            report_dir=reports,
        ),
        execution,
    )

    assert fresh.exit_code == resumed.exit_code == 2
    assert "targets" in resumed.outcome.detail
    detail = resumed.outcome.detail["targets"]["sim_0"]
    assert detail == fresh.outcome.detail["targets"]["sim_0"]
    assert detail["simulation"] == "pass"
    assert detail["passed"] is True
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == "not_requested"
    assert detail["abort_remaining"] is True
    assert "coverage_record" not in detail
    assert [test["name"] for test in detail["tests"]] == ["reset", "wrap"]
    assert execution.runs == []


def test_retention_failure_preserves_in_memory_simulation_record_truth(tmp_path, monkeypatch):
    _coverage_project(tmp_path, monkeypatch)

    result = _run_with_retention_reference_failure(
        monkeypatch,
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        ),
    )

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == "not_requested"
    assert detail["error"] == "injected retention reference failure"
    assert "coverage_record" not in detail
    assert [(test["name"], test["verdict"]) for test in detail["tests"]] == [("reset", "pass")]
    assert not list(tmp_path.rglob("simulation.json"))  # no projection is published


def test_coverage_lock_covers_final_flow_report_publication(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.flows.sim.record_reports import simulation_record_invocation_lock
    from booley.runtime.file_lock import LockContentionError

    _coverage_project(tmp_path, monkeypatch)
    replace = Path.replace
    checked = []

    def check_lock(path, destination):
        if destination.name == "report.json":
            with (
                pytest.raises(LockContentionError),
                simulation_record_invocation_lock(tmp_path / "reports/sim/1"),
            ):
                raise AssertionError("the producer lock must be held")
            checked.append(True)
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", check_lock)
    result = _coverage(tmp_path)
    assert result.exit_code == 0
    assert checked == [True]


def test_interactive_collection_then_exact_coverage_record_analysis(tmp_path, monkeypatch):
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
    from tests.mcp_tools.test_coverage_analyst import Model

    _coverage_project(tmp_path, monkeypatch)
    result = _coverage(tmp_path)
    assert result.exit_code == 0
    coverage_record = next(tmp_path.rglob("targets/sim_0/coverage.json"))
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--record", str(coverage_record)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = analyst.coverage_analyst(coverage_record).to_dict()
    assert report["$schema"] == "booley.coverage-analysis/v4"
    assert report["observed_evidence"]["coverage_record_manifest"]["$schema"] == (
        "booley.coverage-record/v1"
    )
    assert "points" not in report["observed_evidence"]
    assert report["observed_evidence"]["point_store_sha256"].startswith("sha256:")
    assert report["eligibility"] == "eligible"
    assert len(model.calls) == 1
    prompt = json.loads(model.calls[0].prompt)
    assert "campaign" not in prompt
    assert prompt["coverage_record_reference"]["storage_schema"] == "booley.coverage-record/v1"
    assert prompt["coverage_record_reference"]["point_count"] == 1
    assert "points" not in prompt
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_failed_test_exit_code_is_recorded_per_target_in_progress(tmp_path, monkeypatch):
    _coverage_project(tmp_path, monkeypatch)

    result = _coverage(tmp_path, NativeExecution(verdict="fail"))

    assert result.exit_code == 1
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
    assert progress["detail"]["sim_0"]["exit_code"] == 1


# --- #992: --no-waivers -----------------------------------------------------------


def _configure_missing_waiver_directory(root: Path) -> None:
    """Configure waivers whose directory is absent: a load-time error for gated runs."""
    (root / ".booley" / "booley.toml").write_text(
        '[coverage.waivers]\nanchor = "project_data_repository"\ndirectory = "missing-waivers"\n'
    )


def _coverage_evaluation_status(reports: Path, selector: str) -> str:
    coverage_record = next(reports.rglob(f"targets/{selector}/coverage.json"))
    return resolve_coverage_record_reference(coverage_record).loaded.coverage_record.evaluation[
        "status"
    ]


def _finding_codes(outcome) -> list[str]:
    return [item["code"] for item in outcome.detail["findings"]]


def _workload(reports: Path, selector: str) -> dict[str, object]:
    manifest = next(reports.rglob(f"targets/{selector}/record/manifest.json"))
    return json.loads(manifest.read_text())["workload"]


def test_no_waivers_reports_raw(tmp_path, monkeypatch):
    _prepare_sim_custom_project(tmp_path, monkeypatch)
    _configure_missing_waiver_directory(tmp_path)

    def run(name: str, **options):
        _coverage(
            tmp_path,
            _PassingSplitMetricsExecution(),
            target="sim_custom",
            report_dir=tmp_path / name,
            **options,
        )

    # Control: the same run with waivers applied is blocked by the missing directory.
    run("control")
    assert _coverage_evaluation_status(tmp_path / "control", "sim_custom") == "blocked"
    run("raw", no_waivers=True)
    assert _coverage_evaluation_status(tmp_path / "raw", "sim_custom") != "blocked"
    assert _workload(tmp_path / "raw", "sim_custom")["no_waivers"] is True
    assert "no_waivers" not in _workload(tmp_path / "control", "sim_custom")


def test_default_coverage_manifest_omits_no_waivers(tmp_path, monkeypatch):
    _coverage_project(tmp_path, monkeypatch)

    def run(name: str, **options):
        result = _coverage(tmp_path, report_dir=tmp_path / name, **options)
        assert result.exit_code == 0, result.outcome
        return _workload(tmp_path / name, "sim_0")

    assert "no_waivers" not in run("default")
    assert run("raw", no_waivers=True)["no_waivers"] is True


def test_ungated_invalid_waivers_exit_2_with_no_waivers_hint(tmp_path, monkeypatch):
    _coverage_project(tmp_path, monkeypatch)
    _configure_missing_waiver_directory(tmp_path)

    def run(name: str, **options):
        return _coverage(tmp_path, report_dir=tmp_path / name, **options)

    blocked = run("default")
    assert blocked.exit_code == 2
    assert _coverage_evaluation_status(tmp_path / "default", "sim_0") == "blocked"
    assert "evaluation BLOCKED (COV_WAIVER_DIRECTORY_MISSING)" in blocked.outcome.report_text
    assert "--no-waivers" in blocked.outcome.report_text

    raw = run("raw", no_waivers=True)
    assert raw.exit_code == 0
    assert _coverage_evaluation_status(tmp_path / "raw", "sim_0") == "not_requested"


def test_no_waivers_resume_of_ungated_manifest_does_not_rerun_tests(tmp_path, monkeypatch):
    _reports, runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result", no_waivers=True
    )
    completed_runs = tuple(runs)

    resumed = _coverage(
        tmp_path,
        factory=execution_factory,
        resume_from=public.parent / "record/manifest.json",
    )

    assert resumed.exit_code == 0, resumed.outcome
    assert tuple(runs) == completed_runs


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_public_coverage_simulation_record_headline_fresh_and_resume(
    tmp_path, monkeypatch, verdict
):
    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    runs = []

    class Counted(NativeExecution):
        def run(self, request):
            runs.append(request.test)
            return super().run(request)

    def invoke(request):
        return SimulateFlow(
            coverage_execution=lambda *_args: Counted(hits=0, verdict=verdict)
        ).execute(request)

    fresh = invoke(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    completed = tuple(runs)
    assert tuple(item.name for item in completed) == ("reset", "wrap")
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    resumed = invoke(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert tuple(runs) == completed
    for result in (fresh, resumed):
        assert result.outcome.detail["targets"]["sim_0"]["evaluation"] == "not_requested"
        assert result.outcome.detail["targets"]["sim_0"]["simulation"] == verdict
        assert result.exit_code == (1 if verdict == "fail" else 0), result.outcome
    assert not (tmp_path / "state.json").exists()
    report = json.loads((reports / "sim.json").read_text())
    assert "criterion_key" not in report and "criterion_met" not in report


@pytest.mark.parametrize("dry_run", [False, True])
def test_public_publication_resumable_coverage_mismatch_diagnostics(
    tmp_path, monkeypatch, dry_run
):
    reports, _request, runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    before = {
        path.relative_to(manifest.parent).as_posix(): path.read_bytes()
        for path in manifest.parent.rglob("*")
        if path.is_file()
    }
    report_before = {
        path.relative_to(reports).as_posix(): path.read_bytes()
        for path in reports.rglob("*")
        if path.is_file()
    }
    (tmp_path / "rtl/counter.sv").write_text("module counter; endmodule\n// changed\n")

    def forbidden_execution(*_args):
        pytest.fail("mismatch refusal must precede coverage build and collection")

    result = _coverage(
        tmp_path,
        factory=forbidden_execution,
        resume_from=manifest,
        dry_run=dry_run,
    )
    assert result.exit_code == 2
    assert "source changed: rtl/counter.sv" in result.outcome.report_text
    assert "5 derived fingerprints differ" in result.outcome.report_text
    assert result.outcome.detail["derived_fingerprint_count"] == 5
    assert result.outcome.detail["mismatches"]
    assert [test.name for test in runs] == ["reset", "wrap"]
    after = {
        path.relative_to(manifest.parent).as_posix(): path.read_bytes()
        for path in manifest.parent.rglob("*")
        if path.is_file()
    }
    assert after == before
    if dry_run:
        assert {
            path.relative_to(reports).as_posix(): path.read_bytes()
            for path in reports.rglob("*")
            if path.is_file()
        } == report_before


def _coverage_hook_flow_fixture(tmp_path, monkeypatch):
    data = _coverage_project(tmp_path, monkeypatch)
    (data / "booley.toml").write_text('[flows.sim]\npre_run_commands = ["echo frozen-hook"]\n')
    return SimulateFlow(coverage_execution=_frozen_hook_execution(tmp_path, data, []))


@pytest.mark.parametrize("side", ["before", "after"])
def test_actual_coverage_hook_publication_crash_keeps_only_durable_evidence(
    tmp_path, monkeypatch, side
):
    from booley.flows.sim.record.coverage_execution import CoverageAggregateExecutor
    from tests.flows.sim.test_simulation_record_crash_matrix import (
        _CrashOnce,
        _InjectedProcessDeath,
    )

    original = CoverageAggregateExecutor.__init__
    crash = _CrashOnce(f"{side}:pre_sim_evidence")

    def with_checkpoint(executor, **kwargs):
        original(executor, **{**kwargs, "publication_checkpoint": crash})

    monkeypatch.setattr(CoverageAggregateExecutor, "__init__", with_checkpoint)
    flow = _coverage_hook_flow_fixture(tmp_path, monkeypatch)
    with pytest.raises(_InjectedProcessDeath):
        flow.execute(
            SimRequest(
                target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
            )
        )
    assert crash.tripped
    manifest = tmp_path / "reports/sim/1/targets/sim_0/record/manifest.json"

    firings = collect_pre_sim_firings(manifest)
    assert len(firings) == int(side == "after")
    assert all(f.document["stdout_tail"] == "frozen-hook\n" for f in firings)
    assert not list(manifest.parent.glob("work-items/*/result.json"))


def test_coverage_hook_publication_integrity_failure_keeps_error_category(tmp_path, monkeypatch):
    from booley.flows.sim import flow as flow_module
    from booley.flows.sim.record.codec import SimulationRecordIntegrityError
    from booley.flows.sim.record.store import SimulationRecordStore

    original = SimulationRecordStore.publish_pre_sim_evidence
    # The integrity error and its record carrier render the same text; only the type passed here tells them apart.
    failure_outcome = flow_module._coverage_failure_outcome
    categories = []

    def preserve_category(*args, **kwargs):
        categories.append(type(args[3]))
        return failure_outcome(*args, **kwargs)

    # The integrity error and its record carrier render the same text; only the type passed here tells them apart.
    monkeypatch.setattr(flow_module, "_coverage_failure_outcome", preserve_category)

    def fail_after_publication(store, directory, ordinal, raw, **kwargs):
        original(store, directory, ordinal, raw, **kwargs)
        raise SimulationRecordIntegrityError("hook publication integrity probe")

    monkeypatch.setattr(SimulationRecordStore, "publish_pre_sim_evidence", fail_after_publication)
    flow = _coverage_hook_flow_fixture(tmp_path, monkeypatch)
    result = flow.execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 2
    assert "hook publication integrity probe" in result.outcome.report_text
    assert "collector_error" not in result.outcome.report_text
    assert categories == [SimulationRecordIntegrityError]

    firings = collect_pre_sim_firings(
        tmp_path / "reports/sim/1/targets/sim_0/record/manifest.json"
    )
    assert len(firings) == 1
    assert firings[0].document["stdout_tail"] == "frozen-hook\n"
    assert result.outcome.detail["pre_sim_current"] == 1


def _observe_pre_sim_projection(monkeypatch):
    from booley.flows.sim import record_presentation as flow_module
    from booley.flows.sim.record import pre_sim_evidence

    reads = {"manifest": 0, "preview": 0, "decoder_manifest": 0}
    digest = pre_sim_evidence.manifest_digest
    # Spies on the bounded preview's readers: the Flow result does not show their read counts.
    artifact = flow_module._report_artifact_reference
    # Spies on the bounded preview's readers: the Flow result does not show their read counts.
    preview = flow_module._pre_sim_preview_record

    def reference(*args, **kwargs):
        reads["manifest"] += int(kwargs["kind"] == "simulation_record_manifest")
        return artifact(*args, **kwargs)

    def record(*args):
        reads["preview"] += 1
        return preview(*args)

    def manifest_digest(manifest):
        reads["decoder_manifest"] += 1
        return digest(manifest)

    monkeypatch.setattr(pre_sim_evidence, "manifest_digest", manifest_digest)
    # Spies on the bounded preview's readers: the Flow result does not show their read counts.
    monkeypatch.setattr(flow_module, "_report_artifact_reference", reference)
    # Spies on the bounded preview's readers: the Flow result does not show their read counts.
    monkeypatch.setattr(flow_module, "_pre_sim_preview_record", record)
    return reads


def _owned_coverage_hook_request(tmp_path):
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.planning import finalize_manifest
    from booley.flows.sim.record.serial_execution import simulation_attempt
    from booley.flows.sim.record.store import SimulationRecordStore
    from tests.flows.sim.simulation_record_test_support import (
        _maximum_hook_selection_plan,
        _request,
    )
    from tests.flows.sim.test_endpoint_record_lifecycle import _refresh_hook_workload_identity

    plan, _ = _maximum_hook_selection_plan(("wrap",))
    document = json.loads(canonical_json_bytes(plan.manifest.document))
    document.pop("fingerprints")
    document["target"]["vlnv"] = "acme:demo:counter:1"
    document["workload"]["source_recipe"]["pre_sim_commands"] = ["echo owned-hook"]
    _refresh_hook_workload_identity(document)
    manifest = finalize_manifest(document)
    store = SimulationRecordStore(tmp_path / "reports/1/targets/sim/record")
    store.publish_manifest(manifest)
    request = _request(store, manifest, manifest.document["work_items"][0], tmp_path, invocation=1)
    store.publish_attempt(request.attempt_directory, simulation_attempt(request)[0])
    return request


@pytest.mark.parametrize("side", ["before", "after"])
def test_owned_coverage_sink_crash_preserves_actual_hook_before_adapter_launch(
    tmp_path, monkeypatch, side
):
    from booley.flows.sim.record.coverage_execution import _publish_hook
    from tests.flows.sim.test_simulation_record_crash_matrix import (
        _CrashOnce,
        _InjectedProcessDeath,
    )
    from tests.flows.sim.test_verilator_coverage_execution import (
        _build_coverage,
        _execution_fixture,
        _run_request,
    )

    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    request = _owned_coverage_hook_request(tmp_path)
    assert target.identity == "acme:demo:counter:1#sim"
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.resolve_pre_sim_commands",
        lambda _root: ("echo owned-hook",),
    )
    crash = _CrashOnce(f"{side}:pre_sim_evidence")
    execution.set_pre_sim_evidence_sink(
        # The crash boundary is inside the executor's hook sink, below the public execution.
        lambda evidence: _publish_hook(request, evidence, 1, crash)
    )
    with pytest.raises(_InjectedProcessDeath):
        execution.run(_run_request(target, raw_path))
    assert crash.tripped
    assert "run_script" not in captured
    firings = collect_pre_sim_firings(request.store.manifest_path)
    assert len(firings) == int(side == "after")
    assert all(f.document["stdout_tail"] == "owned-hook\n" for f in firings)
    assert all(f.document["returncode"] == 0 for f in firings)
    assert not list(request.store.root.glob("work-items/*/result.json"))


def test_coverage_next_stage_keeps_returned_target_and_coverage_flag(tmp_path: Path) -> None:
    from booley.flows.sim.coverage_progress import CoverageProgress
    from booley.flows.sim.coverage_transaction import CoverageTargetOutcome
    from booley.flows.sim.live_progress import LiveProgressSink, attempt_scope, install_progress

    path = tmp_path / "progress.json"
    progress = CoverageProgress(tmp_path, ("first", "active"))
    first = CoverageTargetOutcome("first", 0, tmp_path / "coverage.json", {"grade": "pass"})
    sink = LiveProgressSink(path, tmp_path, os.environ.get("BOOLEY_RUN_ID", ""))
    with install_progress(sink):
        progress.checkpoint()
        progress.outcomes.append(first)
        progress.checkpoint()
        with attempt_scope("active"):
            payload = json.loads(path.read_text())
            assert payload["coverage"] is True
            assert payload["completed_targets"] == ["first"]
            assert payload["detail"]["first"] == {"grade": "pass", "exit_code": 0}
            assert payload["active"][0]["target"] == "active"
            assert payload["active"][0]["stage"] == "preparing"
        progress.checkpoint(complete=True, phase="aborted")
    final = json.loads(path.read_text())
    assert "active" not in final
    assert final["detail"]["first"]["grade"] == "pass"


def test_image_alias_coverage_flow_publishes_authenticated_references(tmp_path, monkeypatch):
    from booley.flows.sim.coverage_analysis_input import read_coverage_record
    from tests.conftest import require_symlinks, symlink_or_skip
    from tests.runtime.test_sandbox_layout import configure_project_alias

    require_symlinks(tmp_path)
    data = _coverage_project(tmp_path, monkeypatch, '[sim_0]\ntests=["reset", "wrap"]\n')
    alias = tmp_path / "booley"
    symlink_or_skip(alias, data, target_is_directory=True)
    configure_project_alias(monkeypatch, alias, data)
    result = _coverage(tmp_path, report_dir=alias / "reports")
    assert result.exit_code == 0, result.outcome.report_text
    path = data / "reports/sim/1/targets/sim_0/coverage.json"
    loaded = read_coverage_record(alias / path.relative_to(data))
    assert loaded.coverage_record.target.selector == "sim_0"
    projection = json.loads(path.with_name("simulation.json").read_text())
    assert (
        result.outcome.detail["targets"]["sim_0"]["coverage_record"]["path_base"]
        == "report_invocation"
    )
    assert projection["simulation_record_manifest"]["path"]
    (data / "linked").symlink_to(data / "reports", target_is_directory=True)
    with pytest.raises(ValueError, match=r"link|traversal"):
        read_coverage_record(alias / "linked/sim/1/targets/sim_0/coverage.json")


def test_job_report_coverage_metrics_match_coverage_record(tmp_path, monkeypatch):
    _coverage_project(
        tmp_path,
        monkeypatch,
        '[sim_0]\ntests = ["reset"]\n[sim_1]\ntests = ["reset"]\n',
        tools=("verilator", "verilator"),
    )
    reports = tmp_path / "reports"
    result = SimulateFlow(coverage_execution=lambda *_args: _SplitMetricsExecution()).execute(
        SimRequest(target="sim_0,sim_1", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    assert result.exit_code == 0, result.outcome.report_text
    report = json.loads((reports / "sim/1/report.json").read_text())
    for selector in ("sim_0", "sim_1"):
        assert "  coverage: line 100.0% · branch 0.0% · expression 100.0%" in _target_block(
            result.outcome.report_text, selector
        )
        public_path = _card_path(
            _target_block(result.outcome.report_text, selector), "Coverage Record"
        )
        assert public_path == (reports / f"sim/1/targets/{selector}/coverage.json").resolve()
        assert public_path.is_file()
        detail = report["detail"]["targets"][selector]
        assert "coverage_metrics" in detail
        assert detail["coverage_metrics"] == {
            "line": {"percent": 100.0, "threshold": None, "met": None},
            "branch": {"percent": 0.0, "threshold": None, "met": None},
            "expression": {"percent": 100.0, "threshold": None, "met": None},
        }
        assert detail["simulation"] == "pass" and detail["collection"] == "complete"
        assert detail["evaluation"] == "not_requested"
        assert detail["target"] == selector and detail["abort_remaining"] is False
        assert detail["coverage_record"] and "coverage_overall" not in detail
    assert report["detail"]["targets"]["sim_0"]["coverage_metrics"]["line"]["met"] is None


def _linked_coverage_project(tmp_path, monkeypatch):
    """A coverage Project whose ``.booley`` is a symlink outside the Project Root (#2192)."""
    from tests.fusesoc_test_support import symlinked_booley_dir

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    _pin_revision(monkeypatch, "a" * 40)
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    project(root)
    external = symlinked_booley_dir(root, tmp_path / "external-booley")
    (external / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    return root, root / ".booley" / "flow-reports"


def test_symlinked_booley_dir_publishes_and_analyses_coverage(tmp_path, monkeypatch):
    from booley.flows.sim.coverage_analysis_input import read_coverage_record

    root, reports = _linked_coverage_project(tmp_path, monkeypatch)

    result = _coverage(root, report_dir=reports)

    assert result.exit_code == 0
    coverage_record_path = reports / "sim/1/targets/sim_0/coverage.json"
    assert json.loads(coverage_record_path.read_text())["$schema"] == REFERENCE_SCHEMA
    resolved = resolve_coverage_record_reference(coverage_record_path)
    assert (
        read_coverage_record(coverage_record_path).coverage_record
        == resolved.loaded.coverage_record
    )


def test_symlinked_booley_dir_coverage_resume_supersedes_origin_progress(tmp_path, monkeypatch):
    root, reports = _linked_coverage_project(tmp_path, monkeypatch)

    def checkpoint(actual):
        if actual == "before:summary_replace":
            raise KeyboardInterrupt("simulated publication interruption")

    with pytest.raises(KeyboardInterrupt):
        _coverage(root, checkpoint=checkpoint, report_dir=reports)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"

    result = _coverage(root, resume_from=manifest, report_dir=reports)

    assert result.exit_code == 0
    progress = json.loads((reports / "sim/1/progress.json").read_text())
    assert progress["phase"] == "superseded"
    assert progress["superseded_by"]["invocation"] == 2


def test_coverage_analysis_refuses_a_coverage_record_written_by_booley_0_3_0(tmp_path):
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_record,
    )
    from tests.flows.sim.coverage_record_test_support import rewrite_manifest_as_v4
    from tests.mcp_tools.test_coverage_analyst import persist_coverage_record

    coverage_record_path = persist_coverage_record(tmp_path)
    manifest = json.loads(coverage_record_path.read_text(encoding="utf-8"))
    rewrite_manifest_as_v4(manifest)
    coverage_record_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CoverageAnalysisError) as error:
        read_coverage_record(coverage_record_path)

    # An older Booley's record, not an unreadable one: the invocation to delete is named.
    message = str(error.value)
    assert "was written by an older Booley and is no longer supported" in message
    assert "booley.coverage-campaign/v4" in message
    assert str(coverage_record_path.parents[2]) in message
    assert "COV_SCHEMA_VERSION_UNSUPPORTED" not in message
    assert not message.startswith("Cannot analyze Coverage Record")


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Map every path under *root* to its bytes (None for a directory)."""
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def test_coverage_dry_run_previews_each_target_without_admission_or_mutation(
    tmp_path, monkeypatch, capsys
):
    """AC1-AC3: a coverage dry run previews per Target and changes nothing."""
    data = _coverage_project(
        tmp_path,
        monkeypatch,
        '[sim_0]\ntests=["reset"]\n[sim_1]\ntests=["reset"]\n',
        tools=("verilator", "verilator"),
    )
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    factory_calls: list[tuple[object, ...]] = []

    def execution_factory(*args):
        factory_calls.append(args)
        return NativeExecution()

    before = _tree_snapshot(tmp_path)

    result = SimulateFlow(coverage_execution=execution_factory).execute_cli(
        [
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
            "--target",
            "sim_0,sim_1",
            "--coverage",
            "--dry-run",
        ]
    )

    lines = ["sim_0: 1 pending work item(s)", "sim_1: 1 pending work item(s)"]
    assert result.exit_code == 0, result.outcome.report_text
    assert result.outcome.report_text.splitlines() == lines
    printed = capsys.readouterr().out
    assert all(line in printed for line in lines)
    detail = result.outcome.detail
    assert [entry["target"]["selector"] for entry in detail["record_preview"]] == [
        "sim_0",
        "sim_1",
    ]
    assert all(entry["work_items"] == 1 for entry in detail["record_preview"])
    assert all(entry["build_variants"] >= 1 for entry in detail["record_preview"])
    assert detail["mutated"] is False
    assert detail["coverage"] is True
    assert factory_calls == []
    assert _tree_snapshot(tmp_path) == before


def _tool_refusal_project(root: Path) -> None:
    project(root, ("verilator", "icarus"))


def _table_refusal_project(root: Path) -> None:
    project(root)
    (root / ".booley").mkdir()
    (root / ".booley/booley.toml").write_text("[flows]\nsim = 1")


@pytest.mark.parametrize(
    ("configure", "target", "expected"),
    [
        (_tool_refusal_project, "sim_1", None),
        (_table_refusal_project, "sim_0", "Coverage Preflight"),
    ],
    ids=["unsupported-tool", "invalid-table"],
)
def test_coverage_preflight_refusal_is_unchanged_under_dry_run(
    tmp_path, monkeypatch, configure, target, expected
):
    """AC4: preflight refusals keep exit 2 and message under --dry-run."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    configure(tmp_path)
    if (tmp_path / ".booley").is_dir():
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    before = _tree_snapshot(tmp_path)

    def run(dry_run: bool):
        return SimulateFlow().execute(
            SimRequest(target=target, work_dir=tmp_path, coverage=True, dry_run=dry_run)
        )

    normal, dry = run(False), run(True)

    assert normal.exit_code == dry.exit_code == 2
    assert normal.outcome.report_text == dry.outcome.report_text
    if expected is None:
        assert _finding_codes(normal.outcome) == _finding_codes(dry.outcome) != []
    else:
        assert expected in dry.outcome.report_text
    assert _tree_snapshot(tmp_path) == before


def test_coverage_dry_run_reports_plan_failure_as_error(tmp_path, monkeypatch):
    """A plan that cannot be built after preflight fails the preview with an error."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    data = tmp_path / ".booley"
    data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    (data / "tests.toml").write_text('[sim_0]\ntests=["reset"]\n')

    def fail_plan(*_args, **_kwargs):
        raise ValueError("plan went stale")

    # No Project passes preflight and then fails planning, so the planning stage is made to fail.
    monkeypatch.setattr(SimulateFlow, "_coverage_simulation_record_plan", fail_plan)

    result = SimulateFlow().execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, dry_run=True)
    )

    assert result.exit_code == 2
    assert result.outcome.report_text == (
        "Simulation Record coverage preview failed: plan went stale"
    )


def test_coverage_progress_stamps_run_identity_and_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_RUN_ID", "sim-run-1")
    invocation = tmp_path / "reports/sim/1"
    CoverageProgress(invocation, ("sim_0",)).checkpoint()
    progress = json.loads((invocation / "progress.json").read_text())
    assert progress["run_id"] == "sim-run-1"
    assert progress["timestamp"].endswith("Z")


def test_coverage_progress_rejects_inconsistent_terminal_phase(tmp_path):
    progress = CoverageProgress(tmp_path / "reports/sim/1", ("sim_0",))

    with pytest.raises(ValueError, match="complete and phase disagree"):
        progress.checkpoint(phase="complete")
