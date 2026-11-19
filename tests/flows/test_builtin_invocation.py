"""Contract tests for the invocation seam shared by shipped Flows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from booley.core.boundary import BoundaryError
from booley.flows import endpoint_cli
from booley.flows.base import BooleyFlow, BuiltinFlow, SubprocessResult
from booley.flows.builtin_cli import parse_request
from booley.flows.endpoint_cli import normalize_target_arg
from booley.flows.endpoint_state import EndpointState
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.invocation import resolve_timeout_ms
from booley.flows.lint.flow import LintFlow
from booley.flows.lint.request import LintRequest
from booley.flows.plan import FlowPlan, WorkUnitPlan
from booley.flows.request import FlowRequest
from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow
from booley.flows.sim.record_invocation import SimulationRecordInvocation
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.flows.synth.request import SynthRequest
from booley.mcp.base import EXIT_ERROR
from booley.runtime import runtime_context
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import DuplicateTargetError
from tests.flows.invocation_doubles import prepare_as, replace_run

BUILTINS = (
    (SimulateFlow, "sim", 600_000),
    (LintFlow, "lint", 120_000),
    (AsicSynthesizeFlow, "synth", 1_800_000),
    (FpgaImplFlow, "fpga", 7_200_000),
)
FLOW_TYPES = [flow_type for flow_type, *_ in BUILTINS]


def _write_config(root: Path, text: str) -> None:
    (root / ".booley").mkdir(exist_ok=True)
    (root / ".booley" / "booley.toml").write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.parametrize("flow_type", FLOW_TYPES)
def test_builtin_target_repetition_normalizes_to_ordered_scalar(
    flow_type: type[BuiltinFlow],
) -> None:
    repeated = flow_type().parse_args(["--target", "a", "--target", "b,c"])
    comma_list = flow_type().parse_args(["--target", "a,b,c"])

    assert repeated.target == "a,b,c"
    assert repeated == comma_list


def test_request_value_error_is_a_cli_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class RefusingRequest(SynthRequest):
        def __post_init__(self) -> None:
            raise ValueError("refused by the request")

    flow = AsicSynthesizeFlow()
    monkeypatch.setattr(flow, "request_type", RefusingRequest)

    with pytest.raises(SystemExit) as exit_info:
        parse_request(flow, ["--target", "t"])

    assert exit_info.value.code == 2
    assert "refused by the request" in capsys.readouterr().err


def test_optional_builtin_target_absence_and_authored_punctuation_stay_scalar() -> None:
    assert SimulateFlow().parse_args([]).target == ""
    assert SimulateFlow().parse_args(["--target", ""]).target == ""
    assert SimulateFlow().parse_args(["--target", " ", "--target", ","]).target == " ,,"


def test_target_normalization_uses_strict_boundary_validation() -> None:
    args = argparse.Namespace(target=1)

    with pytest.raises(BoundaryError, match="target must be a string"):
        normalize_target_arg(args)


@pytest.mark.parametrize(
    ("flow_type", "extra_args"),
    [
        (LintFlow, ()),
        (AsicSynthesizeFlow, ()),
        (FpgaImplFlow, ()),
        (SimulateFlow, ()),
        (SimulateFlow, ("--mode", "elab-only")),
    ],
)
def test_duplicate_target_fails_before_start_event_or_admission(
    flow_type: type[BuiltinFlow],
    extra_args: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DuplicateCatalog:
        def select_many(self, target: str, *, for_flow: str):
            raise DuplicateTargetError(
                "Target selector 'a' resolves to 'acme:ip:dut:1#a', "
                "which was already selected by 'a'"
            )

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _cls, _root: DuplicateCatalog())
    )
    monkeypatch.setattr(
        EndpointState,
        "admission",
        lambda *_args, **_kwargs: pytest.fail("entered admission for invalid Target selection"),
    )
    flow = flow_type()

    result = flow.execute_cli(
        [
            "--target",
            "a",
            "--target",
            "a",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
            *extra_args,
        ]
    )

    assert result.exit_code == 2
    assert f"{flow.name} Target selection failed" in result.outcome.report_text
    assert "already selected" in result.outcome.report_text
    report = json.loads((tmp_path / "reports" / f"{flow.name}.json").read_text())
    assert report["exit_code"] == 2
    assert "already selected" in report["report_text"]
    # The display log is written when a start event is published.
    assert not (tmp_path / "runtime" / "display.jsonl").exists()


@pytest.mark.parametrize("flow_type", FLOW_TYPES)
@pytest.mark.parametrize("option,expected", [("--timeout-ms", 2500), ("--timeout", 2500000)])
def test_builtin_timeout_conversion(
    flow_type: type[BooleyFlow], option: str, expected: int
) -> None:
    args = flow_type().parse_args(["--target", "demo", option, "2500"])
    assert args.timeout_ms == expected


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "words"])
def test_builtin_timeout_rejects_non_positive_or_non_integer_values(value: str) -> None:
    with pytest.raises(SystemExit):
        LintFlow().parse_args(["--target", "demo", "--timeout-ms", value])


@pytest.mark.parametrize(("_flow_type", "name", "default_ms"), BUILTINS)
def test_timeout_precedence_is_call_then_config_then_default(
    _flow_type: type[BooleyFlow],
    name: str,
    default_ms: int,
    tmp_path: Path,
) -> None:
    configured = default_ms + 1
    _write_config(tmp_path, f"[flows.{name}]\ntimeout_ms = {configured}\n")
    unconfigured = tmp_path / "unconfigured"
    unconfigured.mkdir()

    assert resolve_timeout_ms(name, tmp_path, default_ms + 2) == default_ms + 2
    assert resolve_timeout_ms(name, tmp_path, None) == configured
    assert resolve_timeout_ms(name, None, None) == configured
    assert resolve_timeout_ms(name, unconfigured, None) == default_ms


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", '"1000"', "nan"])
def test_malformed_configured_timeout_is_a_boundary_error(value: str, tmp_path: Path) -> None:
    _write_config(tmp_path, f"[flows.lint]\ntimeout_ms = {value}\n")

    with pytest.raises(BoundaryError, match=r"\[flows\.lint\]\.timeout_ms"):
        resolve_timeout_ms("lint", tmp_path, None)


def test_builtin_pre_state_gate_validates_timeout_before_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, '[flows.lint]\ntimeout_ms = "invalid"\n')
    monkeypatch.setattr(
        "booley.flows.base.runtime_context.container_only_error",
        lambda _command: None,
    )

    result = LintFlow().execute_cli(["--target", "demo", "--work-dir", str(tmp_path), "--dry-run"])

    assert result.exit_code == 2
    assert "timeout_ms must be an integer" in result.outcome.report_text


class _CustomTimeoutFlow(BooleyFlow):
    """Custom Flow proving built-in flags are not injected globally."""

    name = "custom_timeout"

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--timeout", choices=("short", "long"))
        parser.add_argument("--dry-run", choices=("summary", "full"))

    def _build_command(self) -> list[str]:
        return ["true"]

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:
        return EndpointOutcome(exit_code=result.returncode)


class _OptionalCustomTimeoutFlow(_CustomTimeoutFlow):
    target_required = False


def _unusable_report_dir(kind: str, root: Path) -> Path | None:
    if kind == "unwritable":
        if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            return None  # directory permissions are not enforced here
        locked = root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        return locked
    afile = root / "afile"
    afile.write_bytes(b"")
    return afile / "sub"


def test_unusable_report_dir_is_refused_before_any_job_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
    for flow_type in (LintFlow, _CustomTimeoutFlow):
        for kind in ("uncreatable", "unwritable"):
            root = tmp_path / f"{flow_type.__name__}-{kind}"
            root.mkdir()
            report_dir = _unusable_report_dir(kind, root)
            if report_dir is None:
                continue
            argv = ["--target", "t", "--work-dir", str(root), "--report-dir", str(report_dir)]
            refusal: BaseException | None = None
            try:
                flow_type().execute_cli(argv)
            except BaseException as exc:  # noqa: BLE001 - anything but a refusal fails below
                refusal = exc
            finally:
                if kind == "unwritable":
                    report_dir.chmod(0o700)

            errors = [line for line in capsys.readouterr().err.splitlines() if "error:" in line]
            assert isinstance(refusal, SystemExit), f"not refused up front: {refusal!r}"
            assert refusal.code == 2
            assert len(errors) == 1
            assert "argument --report-dir:" in errors[0]
            assert str(report_dir) in errors[0]
            assert not (runtime / "display.jsonl").exists()
            assert not (root / "reports").exists()


def test_typed_request_with_unusable_report_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "afile").write_bytes(b"")
    request = LintRequest(
        target="t", work_dir=str(tmp_path), report_dir=tmp_path / "afile" / "sub"
    )
    with pytest.raises(ValueError, match="argument --report-dir:"):
        LintFlow().execute(request)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="directory permissions are not enforced here",
)
def test_report_dir_that_cannot_be_inspected_is_refused(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    report_dir = locked / "reports"
    locked.chmod(0)
    try:
        with pytest.raises(
            endpoint_cli.ReportDirError, match="argument --report-dir: cannot write reports"
        ):
            endpoint_cli.require_usable_report_dir(report_dir)
    finally:
        locked.chmod(0o700)


def test_custom_flow_keeps_its_own_timeout_and_dry_run_surface() -> None:
    args = _CustomTimeoutFlow().parse_args(
        ["--target", "demo", "--timeout", "long", "--dry-run", "summary"]
    )

    assert args.timeout == "long"
    assert args.dry_run == "summary"
    assert not hasattr(args, "timeout_ms")


def test_custom_flow_target_repetition_normalizes_to_scalar() -> None:
    endpoint = _CustomTimeoutFlow()

    assert endpoint.parse_args(["--target", "a", "--target", "b,c"]).target == "a,b,c"
    assert _OptionalCustomTimeoutFlow().parse_args([]).target == ""


def test_cli_report_preserves_authored_repeated_target_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = LintFlow()
    report_dir = tmp_path / "reports"
    argv = [
        "--target",
        "a",
        "--target",
        "b,c",
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
    ]
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    replace_run(
        monkeypatch,
        flow,
        lambda: EndpointOutcome(detail={"targets": flow.args.target.split(",")}),
    )

    with mock.patch.object(EndpointState, "admission", return_value=nullcontext(None)):
        result = flow.execute_cli(argv)

    report = json.loads((report_dir / "lint.json").read_text())
    assert result.outcome.detail["targets"] == ["a", "b", "c"]
    assert report["target"] == "a,b,c"
    assert report["argv"] == argv


def _plan(flow: str, *, errors: tuple[str, ...] = ()) -> FlowPlan:
    return FlowPlan(
        flow=flow,
        mode="default",
        work_units=(
            WorkUnitPlan(
                unit_id=f"{flow}-ordinary-demo",
                role="ordinary",
                revision=None,
                selector="demo",
                target_identity="vendor:library:demo:1.0#demo",
                test_or_module_scope=("demo",),
                eda_tool="tool",
                timeout_ms=1_000,
            ),
        ),
        aggregate_errors=errors,
    )


class _DryLifecycleFlow(BuiltinFlow):
    """Minimal built-in proving the shared non-persisting lifecycle."""

    name = "dry_contract"

    def __init__(self, errors: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.errors = errors

    def _resolve_job_class(self) -> str:
        return "memory"

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return None

    # Private seam: only a built-in Flow's own run reaches the shared dry-run
    # renderer, and every shipped Flow needs resolvable Targets and its
    # toolchain to plan.
    def _run(self) -> EndpointOutcome:
        return self._dry_run_result(_plan(self.name, errors=self.errors))


class _VerdictLifecycleFlow(BuiltinFlow):
    """Minimal Flow returning a verdict without owning console transport."""

    name = "verdict_contract"

    def __init__(self, outcome: EndpointOutcome, *, self_print: bool = False) -> None:
        super().__init__()
        self.outcome = outcome
        self.self_print = self_print

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return None

    def _run(self) -> EndpointOutcome:
        if self.self_print:
            print(self.outcome.report_text)
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "self_print", "shown", "absent"),
    [
        (EndpointOutcome(report_text="Simulation verdict: PASS"), False, "out", "err"),
        (
            EndpointOutcome(exit_code=EXIT_ERROR, report_text="Simulation verdict: ERROR"),
            False,
            "err",
            "out",
        ),
        (
            EndpointOutcome(exit_code=EXIT_ERROR, report_text="Simulation verdict: FAIL"),
            True,
            "out",
            "err",
        ),
    ],
    ids=["success", "failure", "self-printed"],
)
def test_state_backed_flow_verdict_reaches_one_stream_once(
    outcome: EndpointOutcome,
    self_print: bool,
    shown: str,
    absent: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    flow = _VerdictLifecycleFlow(outcome, self_print=self_print)

    execution = flow.execute_cli(["--target", "demo", "--work-dir", str(tmp_path)])

    streams = dict(zip(("out", "err"), capsys.readouterr(), strict=True))
    assert execution.exit_code == outcome.exit_code
    assert streams[shown].count(outcome.report_text) == 1
    assert outcome.report_text not in streams[absent]


def test_typed_flow_execution_does_not_publish_to_process_streams(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    flow = _VerdictLifecycleFlow(EndpointOutcome(report_text="Simulation verdict: PASS"))

    execution = flow.execute(FlowRequest(target="demo", work_dir=tmp_path))

    captured = capsys.readouterr()
    assert execution.exit_code == 0
    assert captured.out == ""
    assert "Simulation verdict: PASS" not in captured.err


def test_state_backed_simulation_coverage_verdict_reaches_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    flow = SimulateFlow()
    prepared_simulation_record = PreparedSimulationEndpoint((), None, (), {})
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    prepare_as(monkeypatch, flow, SimulationRecordInvocation(flow, prepared_simulation_record))
    monkeypatch.setattr(
        flow,
        "run_prepared_simulation",
        lambda prepared, admission: EndpointOutcome(
            report_text="Coverage verdict: PASS",
            detail={"coverage": True},
        ),
    )

    with mock.patch.object(EndpointState, "admission", return_value=nullcontext(None)):
        execution = flow.execute_cli(
            ["--target", "demo", "--coverage", "--work-dir", str(tmp_path)]
        )

    captured = capsys.readouterr()
    assert execution.exit_code == 0
    assert execution.outcome.detail["coverage"] is True
    assert captured.out.count("Coverage verdict: PASS") == 1
    assert "Coverage verdict: PASS" not in captured.err


@pytest.mark.parametrize("errors", [(), ("demo: invalid",)], ids=["planned", "planning-failed"])
def test_builtin_dry_run_skips_admission_and_normal_persistence(
    errors: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_bytes(b'{"opaque": true}\n')
    state_before = state_file.read_bytes()
    logs_dir = tmp_path / "logs"
    report_dir = tmp_path / "reports"
    logs_dir.mkdir()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(logs_dir / ".runtime"))
    admitted: list[object] = []
    admission = EndpointState.admission

    @contextmanager
    def spy_admission(self: EndpointState, prepared: Any, *, requeued: bool = False):
        with admission(self, prepared, requeued=requeued) as borrowed:
            admitted.append(borrowed)
            yield borrowed

    monkeypatch.setattr(EndpointState, "admission", spy_admission)

    execution = _DryLifecycleFlow(errors).execute_cli(
        [
            "--target",
            "demo",
            "--dry-run",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(report_dir),
        ]
    )

    captured = capsys.readouterr()
    assert admitted == [None]
    assert set(execution.outcome.detail) == {
        "schema_version",
        "flow",
        "mode",
        "semantic_plan_fingerprint",
        "work_units",
        "aggregate_errors",
        "planning_disclosures",
    }
    assert state_file.read_bytes() == state_before
    assert [path.relative_to(report_dir).as_posix() for path in report_dir.rglob("*")] == [
        "dry_contract",
        "dry_contract/flow_plan.json",
    ]
    plan = json.loads((report_dir / "dry_contract" / "flow_plan.json").read_text())
    assert plan == execution.outcome.detail
    if errors:
        assert execution.exit_code == EXIT_ERROR
        assert json.loads(captured.out) == execution.outcome.detail
        assert captured.err.count("Dry-run planning failed: demo: invalid") == 1
        return
    assert execution.exit_code == 0
    plan_text, separator, summary = captured.out.rpartition("\nDry run: 1 work unit(s)\n")
    assert separator
    assert summary == ""
    assert json.loads(plan_text) == execution.outcome.detail
    assert (
        captured.err
        == "booley: --work-dir is deprecated; use --project instead (removal after one compatibility release)\n"
    )
    events = [
        json.loads(line)
        for line in (logs_dir / ".runtime" / "display.jsonl").read_text().splitlines()
    ]
    lifecycle = [event for event in events if event["type"] in {"endpoint_start", "endpoint_end"}]
    assert [event["type"] for event in lifecycle] == ["endpoint_start", "endpoint_end"]
    assert all(event["dry_run"] is True for event in lifecycle)
    assert not (report_dir / "dry_contract.json").exists()
    assert not any(path.name == "report.json" for path in report_dir.rglob("*"))


def test_direct_dry_run_uses_project_data_default_without_numbering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    flow = _DryLifecycleFlow()

    execution = flow.execute_cli(["--target", "demo", "--dry-run", "--work-dir", str(tmp_path)])

    report_root = tmp_path / "flow-reports"
    assert execution.exit_code == 0
    assert (report_root / "dry_contract/flow_plan.json").is_file()
    assert not (report_root / "dry_contract/1").exists()
    assert not (report_root / "dry_contract.json").exists()
    assert not any(path.name == "report.json" for path in report_root.rglob("*"))


def test_sim_verbose_cli_and_typed_request_construct_identical_output_request(tmp_path):
    from booley.flows.sim.request import SimRequest

    manifest = tmp_path / "manifest.json"
    cli = parse_request(SimulateFlow(), ["--resume-from", str(manifest), "--verbose", "--dry-run"])
    typed = SimRequest(resume_from=manifest, verbose=True, dry_run=True)
    assert cli == typed
