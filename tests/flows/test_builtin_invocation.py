"""Contract tests for the invocation seam shared by shipped Flows."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest import mock

import pytest

from booley.core.boundary import BoundaryError
from booley.criteria.state import DevelopmentState
from booley.flows.base import BooleyFlow, BuiltinFlow, SubprocessResult
from booley.flows.flow_session import FlowSession
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.invocation import BudgetPlan, resolve_timeout_ms
from booley.flows.lint.flow import LintFlow
from booley.flows.plan import FlowPlan, WorkUnitPlan
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.base import EXIT_ERROR
from booley.mcp.flow_adapter import flow_schema
from booley.runtime.endpoint_execution import EndpointOutcome

BUILTINS = (
    (SimulateFlow, "sim", 600_000),
    (LintFlow, "lint", 120_000),
    (AsicSynthesizeFlow, "synth", 1_800_000),
    (FpgaImplFlow, "fpga", 7_200_000),
)


@pytest.mark.parametrize(("flow_type", "_name", "_default_ms"), BUILTINS)
def test_builtin_schema_exposes_one_canonical_timeout(
    flow_type: type[BooleyFlow],
    _name: str,
    _default_ms: int,
) -> None:
    schema = flow_schema(flow_type())
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert properties["timeout_ms"]["type"] == "integer"
    assert properties["timeout_ms"]["minimum"] == 1
    assert "timeout" not in properties
    assert "_legacy_timeout_ms" not in properties
    assert properties["dry_run"]["type"] == "boolean"


def test_fpga_schema_exposes_portable_profile_vocabulary() -> None:
    properties = flow_schema(FpgaImplFlow())["properties"]

    assert properties["ppa_profile"]["enum"] == ["compact", "balanced", "max_frequency"]
    assert "synth" not in properties
    assert "pnr" not in properties


@pytest.mark.parametrize(("flow_type", "_name", "_default_ms"), BUILTINS)
def test_builtin_parsers_accept_canonical_timeout(
    flow_type: type[BooleyFlow],
    _name: str,
    _default_ms: int,
) -> None:
    args = flow_type().parse_args(["--target", "demo", "--timeout-ms", "2500"])

    assert args.timeout_ms == 2500


@pytest.mark.parametrize(("flow_type", "_name", "_default_ms"), BUILTINS)
def test_deprecated_timeout_alias_normalizes_once(
    flow_type: type[BooleyFlow],
    _name: str,
    _default_ms: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        args = flow_type().parse_args(["--target", "demo", "--timeout", "2500"])

    assert args.timeout_ms == 2500
    assert caplog.messages == ["--timeout is deprecated; use --timeout-ms"]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "words"])
def test_builtin_timeout_rejects_non_positive_or_non_integer_values(value: str) -> None:
    with pytest.raises(SystemExit):
        LintFlow().parse_args(["--target", "demo", "--timeout-ms", value])


def test_builtin_timeout_spellings_conflict() -> None:
    with pytest.raises(SystemExit):
        LintFlow().parse_args(["--target", "demo", "--timeout-ms", "1000", "--timeout", "2000"])


@pytest.mark.parametrize(("_flow_type", "name", "default_ms"), BUILTINS)
def test_timeout_precedence_is_call_then_config_then_default(
    _flow_type: type[BooleyFlow],
    name: str,
    default_ms: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = default_ms + 1
    monkeypatch.setattr(
        "booley.runtime.shared_infra._load_rtl_config",
        lambda _work_dir: {"flows": {name: {"timeout_ms": configured}}},
    )

    assert resolve_timeout_ms(name, tmp_path, default_ms + 2) == default_ms + 2
    assert resolve_timeout_ms(name, tmp_path, None) == configured
    assert resolve_timeout_ms(name, None, None) == configured

    monkeypatch.setattr(
        "booley.runtime.shared_infra._load_rtl_config",
        lambda _work_dir: {},
    )
    assert resolve_timeout_ms(name, None, None) == default_ms


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000", float("nan")])
def test_malformed_configured_timeout_is_a_boundary_error(
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "booley.runtime.shared_infra._load_rtl_config",
        lambda _work_dir: {"flows": {"lint": {"timeout_ms": value}}},
    )

    with pytest.raises(BoundaryError, match=r"\[flows\.lint\]\.timeout_ms"):
        resolve_timeout_ms("lint", tmp_path, None)


def test_budget_plan_derives_aggregate_execution_and_setup() -> None:
    plan = BudgetPlan(
        timeout_ms=4_000,
        work_units=3,
        setup_grace_per_unit_s=2,
        finalize_grace_s=5,
        outer_floor_s=10,
    )

    assert plan.execution_s == 12
    assert plan.setup_grace_s == 6
    assert plan.outer_timeout_s == 23


def test_builtin_pre_state_gate_validates_timeout_before_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "booley.runtime.shared_infra._load_rtl_config",
        lambda _work_dir: {"flows": {"lint": {"timeout_ms": "invalid"}}},
    )
    monkeypatch.setattr(
        "booley.flows.base.runtime_context.container_only_error",
        lambda _command: None,
    )
    flow = LintFlow()
    flow.parse_args(["--target", "demo", "--work-dir", str(tmp_path), "--dry-run"])

    rejection = flow._pre_state_gate()

    assert rejection is not None
    assert rejection.exit_code == 2
    assert "timeout_ms must be an integer" in rejection.report_text


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


def test_custom_flow_keeps_its_own_timeout_and_dry_run_surface() -> None:
    args = _CustomTimeoutFlow().parse_args(
        ["--target", "demo", "--timeout", "long", "--dry-run", "summary"]
    )

    assert args.timeout == "long"
    assert args.dry_run == "summary"
    assert not hasattr(args, "timeout_ms")


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


@pytest.mark.parametrize(("flow_type", "name", "_default_ms"), BUILTINS)
def test_builtin_dry_run_renderer_has_one_common_shape(
    flow_type: type[BuiltinFlow],
    name: str,
    _default_ms: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    flow = flow_type()
    flow.parse_args(["--target", "demo", "--dry-run"])

    result = flow._dry_run_result(_plan(name))

    assert result.exit_code == 0
    assert set(result.detail) == {
        "schema_version",
        "flow",
        "mode",
        "semantic_plan_fingerprint",
        "work_units",
        "aggregate_errors",
        "planning_disclosures",
    }
    assert json.loads(capsys.readouterr().out) == result.detail


def test_builtin_dry_run_aggregate_errors_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    flow = LintFlow()
    flow.parse_args(["--target", "demo", "--dry-run"])

    result = flow._dry_run_result(_plan("lint", errors=("demo: invalid",)))

    assert result.exit_code == EXIT_ERROR
    assert result.detail["aggregate_errors"] == ["demo: invalid"]
    assert json.loads(capsys.readouterr().out) == result.detail


class _DryLifecycleFlow(BuiltinFlow):
    """Minimal built-in proving the shared non-persisting lifecycle."""

    name = "dry_contract"

    def _resolve_job_class(self) -> str:
        return "heavy"

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return None

    def _run(self) -> EndpointOutcome:
        return self._dry_run_result(_plan(self.name))


def test_builtin_dry_run_skips_admission_and_normal_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "state.json"
    DevelopmentState.load(state_file).save()
    state_before = state_file.read_bytes()
    logs_dir = tmp_path / "logs"
    report_dir = tmp_path / "reports"
    logs_dir.mkdir()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs_dir))
    flow = _DryLifecycleFlow()

    with (
        mock.patch.object(
            FlowSession,
            "_acquire_job_slot",
            side_effect=AssertionError("dry-run acquired a heavy slot"),
        ),
        mock.patch.object(
            FlowSession,
            "_post_run",
            side_effect=AssertionError("dry-run entered normal persistence"),
        ),
    ):
        execution = flow.execute_cli(
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

    assert execution.exit_code == 0
    assert state_file.read_bytes() == state_before
    assert [path.relative_to(report_dir).as_posix() for path in report_dir.rglob("*")] == [
        "dry_contract",
        "dry_contract/flow_plan.json",
    ]
    plan = json.loads((report_dir / "dry_contract" / "flow_plan.json").read_text())
    assert plan == execution.outcome.detail
    events = [
        json.loads(line)
        for line in (logs_dir / ".runtime" / "display.jsonl").read_text().splitlines()
    ]
    lifecycle = [event for event in events if event["type"] in {"endpoint_start", "endpoint_end"}]
    assert [event["type"] for event in lifecycle] == ["endpoint_start", "endpoint_end"]
    assert all(event["dry_run"] is True for event in lifecycle)
    assert not (report_dir / "dry_contract.json").exists()
    assert not any(path.name == "report.json" for path in report_dir.rglob("*"))
