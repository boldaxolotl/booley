"""Tests for SimulateFlow — multi-config sim, cycle parsing, dry-run, reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.sim.execution.telemetry import (
    parse_build_seconds,
    parse_run_seconds,
    parse_sim_cpu_seconds,
)
from booley.flows.sim.flow import (
    SimulateFlow as ProductionSimulateFlow,
)
from booley.flows.sim.flow import (
    _artifact_path_component,
    _filter_tests,
    _resolve_sim_campaign_work_units,
)
from booley.flows.sim.mode import SimulationMode
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime.endpoint_execution import ExecutionResult
from tests.flows.sim.campaign_boundary import (
    FAIL_PROCESS,
    INCONCLUSIVE_PROCESS,
    PASS_PROCESS,
    CampaignSimRun,
    Launch,
    run_campaign_sim,
)
from tests.flows.sim.leased_session import canned_build_session
from tests.target_test_support import write_sim_preview_project

# Built-in Flow execution inside the Sandbox.
_FLOW_ENABLED = True


class SimulateFlow(ProductionSimulateFlow):
    """Concrete Flow used by the campaign compatibility tests."""


@pytest.fixture(autouse=True)
def _canned_build_session():
    """Run canned Flow tests against a leased build session with stubbed inputs."""
    with canned_build_session():
        yield


def _display_flow(tmp_path: Path, target: str, *args: str) -> SimulateFlow:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", target, *args])
    return flow


def test_display_label_names_one_requested_test(tmp_path: Path):
    flow = _display_flow(tmp_path, "::demo:core:0#sim_mul", "--test", "smoke")

    with patch("booley.flows.sim.flow._get_test_names", return_value={}):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · test smoke"


def test_display_label_counts_distinct_exact_tests(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_mul", "--test", "mul_base", "--test", "mul_edge")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_mul": ["mul_base", "mul_edge"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={}),
    ):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · 2 tests"


def test_display_label_applies_configured_skips_before_naming_test(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_mul")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_mul": ["smoke", "edge"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={"sim_mul": ["edge"]}),
    ):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · test smoke"


def test_display_label_deduplicates_same_test_across_targets(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_a,sim_b", "--test", "smoke")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_a": ["smoke"], "sim_b": ["smoke"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={}),
    ):
        label = flow._resolve_display_label()

    assert label == "2 targets · test smoke"


def test_display_label_uses_unknown_suite_when_any_target_is_undeclared(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_a,sim_b")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_a": ["smoke"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={}),
    ):
        label = flow._resolve_display_label()

    assert label == "2 targets · tests"


def test_display_label_uses_elaboration_mode_without_test_discovery(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_core", "--mode", "elab-only-standalone")

    with patch("booley.flows.sim.flow._get_test_names") as get_tests:
        label = flow._resolve_display_label()

    assert label == "target sim_core · elaboration"
    get_tests.assert_not_called()


def test_display_label_failure_uses_neutral_tests_with_short_target(tmp_path: Path):
    flow = _display_flow(tmp_path, "::demo:core:0#sim_mul")

    with patch("booley.flows.sim.flow._get_test_names", side_effect=RuntimeError("boom")):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · tests"


def test_display_label_does_not_resolve_target_catalog(tmp_path: Path):
    flow = _display_flow(tmp_path, "::demo:core:0#sim_mul", "--test", "smoke")

    with (
        patch("booley.flows.sim.flow._get_test_names", return_value={}),
        patch("booley.targets.catalog.TargetCatalog.build") as build_catalog,
    ):
        flow._resolve_display_label()

    build_catalog.assert_not_called()


def test_campaign_work_units_count_native_tests_and_cocotb_batches(tmp_path: Path):
    with (
        patch(
            "booley.flows.sim.flow._target_is_cocotb",
            side_effect=lambda _root, target: target == "cocotb",
        ),
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={
                "native": ["smoke", "stress", "known_hang"],
                "cocotb": ["one", "two", "three"],
            },
        ),
        patch(
            "booley.flows.sim.flow._get_test_skips",
            return_value={"native": ["known_hang"]},
        ),
    ):
        units = _resolve_sim_campaign_work_units(
            tmp_path,
            "native,cocotb",
        )

    assert units == 3  # two native processes plus one cocotb batch


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SimulationMode.ELAB_ONLY, 2),
        (SimulationMode.ELAB_ONLY_STANDALONE, 3),
    ],
)
def test_campaign_work_units_count_elaboration_modes(
    tmp_path: Path,
    mode: SimulationMode,
    expected: int,
) -> None:
    assert _resolve_sim_campaign_work_units(tmp_path, "one,two", mode=mode) == expected


def test_artifact_path_component_never_embeds_unsafe_test_names():
    assert _artifact_path_component("sign_case_65") == "sign_case_65"
    encoded = _artifact_path_component("../../outside/reports")
    assert encoded.startswith("~sha256-")
    assert "/" not in encoded
    assert encoded != _artifact_path_component("../../different")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_with_state(state_file: Path, slug: str = "test-ticket") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


def _make_state(tmp_path: Path) -> Path:
    """Create a fresh state file and return its path."""
    state_file = tmp_path / "state.json"
    DevelopmentState.load(state_file).save()
    return state_file


def _make_flow(
    tmp_path: Path,
    *,
    config: str = "lite",
    tb_top: str = "alu_tb",
    extra_args: list[str] | None = None,
    seed_core: bool = True,
) -> SimulateFlow:
    """Build a SimulateFlow with parsed args and loaded state.

    tb_top left the CLI surface (ADR 0021); a sim Target's ``toplevel`` IS its
    TB top, so it comes from the resolved Target. ADR 0022 made the ``.core`` the
    sole design-description home (the configs.toml ``tb_top`` source was removed),
    so the helper seeds it via a co-located ``.core`` whose Target ``toplevel`` is
    *tb_top* — the cheap, no-subprocess read ``tb_top_for_target(resolved=None)``
    performs (``fusesoc_registry.core_target_toplevel``). Only written for a
    single valid Target name (a comma-list / empty ``config`` is not a Target).
    """
    state_file = _make_state(tmp_path)
    report_dir = tmp_path / "reports"
    # Seed tb_top via a co-located .core the no-resolve tb_top_for_target reads
    # (BOOLEY_DIR=tmp_path). Skip for multi/empty config (not a Target)
    # and when the caller authors its own .core (seed_core=False). Two distinct
    # cores sharing a Target name is legal now (ADR 0030, first-wins view), but
    # this helper still seeds only one to keep the resolved Target unambiguous.
    # ADR 0039: resolve_target_selection validates every token against the
    # .core surface unconditionally, so seed one core per named config
    # (comma-lists included — the old transitional zero-.core skip is gone).
    if seed_core and config and config.strip() == config:
        for name in [c.strip() for c in config.split(",") if c.strip()]:
            core_file = tmp_path / f"{name}.core"
            if not core_file.exists():
                core_file.write_text(
                    "CAPI=2:\n"
                    f"name: ::{name}:0\n"
                    "targets:\n"
                    f"  {name}:\n"
                    "    flow: sim\n"
                    "    flow_options: {tool: verilator}\n"
                    f"    toplevel: {tb_top}\n",
                    encoding="utf-8",
                )
    argv = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--target",
        config,
    ]
    if extra_args:
        argv.extend(extra_args)
    env = _env_with_state(state_file)
    booley_dir = tmp_path / ".booley"
    if booley_dir.exists():
        env["BOOLEY_DIR"] = str(booley_dir)
    flow = SimulateFlow()
    with patch.dict(os.environ, env):
        flow.parse_args(argv)
    flow.read_state()
    return flow


# ---------------------------------------------------------------------------
# Build-time attribution (first test of a run pays the edalize make)
# ---------------------------------------------------------------------------


class TestBuildTimeAttribution:
    """After an RTL edit the first test absorbs the whole model rebuild; the
    report must say so ("reset PASS 5.0s" with a 0.1s sim misleads timing
    triage)."""

    def test_parse_build_seconds_extracts_marker(self):
        output = "make stuff\nBOOLEY_BUILD_MILLISECONDS: 5000\n[SIM_RESULT] PASSED"
        assert parse_build_seconds(output) == 5.0

    def test_parse_run_seconds_extracts_authenticated_wrapper_record(self):
        output = "BOOLEY_RUN_STAGE token=abc123 rc=0 duration_ms=19"
        assert parse_run_seconds(output) == 0.019

    def test_parse_sim_cpu_seconds_extracts_user_and_system_time(self):
        output = "BOOLEY_SIM_CPU_SECONDS: user=1.250000 system=0.125000"
        assert parse_sim_cpu_seconds(output) == (1.25, 0.125)

    def test_parse_build_seconds_absent_marker_is_zero(self):
        assert parse_build_seconds("no marker here") == 0.0
        assert parse_build_seconds("") == 0.0

    def test_parse_build_seconds_ignores_mid_line_mention(self):
        # Only a line-anchored marker counts — a log quoting the string must
        # not be parsed as a measurement.
        assert parse_build_seconds("echo BOOLEY_BUILD_MILLISECONDS: 9000") == 0.0


# ---------------------------------------------------------------------------
# Test name filtering
# ---------------------------------------------------------------------------


class TestFilterTests:
    def test_partial_name_does_not_match(self):
        tests = ["lite_smoke", "lite_stress", "lite_boot"]
        assert _filter_tests(tests, "stress") == []

    def test_multiple_exact_names_preserve_request_order(self):
        tests = ["lite_smoke", "lite_stress", "lite_boot"]
        assert _filter_tests(tests, ("lite_boot", "lite_smoke")) == [
            "lite_boot",
            "lite_smoke",
        ]

    def test_exact_match(self):
        tests = ["lite_smoke"]
        assert _filter_tests(tests, "lite_smoke") == ["lite_smoke"]


# ---------------------------------------------------------------------------
# Skip-list (tests.toml `skip` + --skip): known-hang exclusion
# ---------------------------------------------------------------------------


class TestSkipList:
    _NAMES: ClassVar[dict[str, list[str]]] = {
        "lite": ["lite_smoke", "lite_stress", "lite_boot"],
    }

    def test_config_skip_excludes_named_tests(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch(
            "booley.flows.sim.flow._get_test_skips", return_value={"lite": ["lite_stress"]}
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == [
                "lite_smoke",
                "lite_boot",
            ]

    def test_multiple_config_skips(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch(
            "booley.flows.sim.flow._get_test_skips",
            return_value={"lite": ["lite_stress", "lite_boot"]},
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == ["lite_smoke"]

    def test_explicit_test_overrides_skip(self, tmp_path: Path):
        # Naming a skipped test by hand is a clear override of the skip list.
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_stress"])
        with patch(
            "booley.flows.sim.flow._get_test_skips", return_value={"lite": ["lite_stress"]}
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == ["lite_stress"]

    def test_all_skipped_is_rejected(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch(
            "booley.flows.sim.flow._get_test_skips",
            return_value={"lite": list(self._NAMES["lite"])},
        ):
            result = flow._validate_runnable_tests(["lite"], self._NAMES)
            assert result is not None
            assert result.exit_code == EXIT_ERROR
            assert "no runnable tests" in result.report_text
            assert flow._resolve_tests_to_run("lite", self._NAMES) == []

    def test_no_skip_runs_every_test(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch("booley.flows.sim.flow._get_test_skips", return_value={}):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == self._NAMES["lite"]


# ---------------------------------------------------------------------------
# --test validation: an unknown name must error, not run the default test
# (false-green footgun — contrast with --target, which is validated).
# ---------------------------------------------------------------------------


class TestUnknownTestSelector:
    _NAMES: ClassVar[dict[str, list[str]]] = {
        "lite": ["lite_smoke", "lite_stress", "lite_boot"],
    }

    def test_valid_exact_name_passes(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_stress"])
        assert flow._validate_test_selector(["lite"], self._NAMES) is None

    def test_no_selector_is_noop(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        assert flow._validate_test_selector(["lite"], self._NAMES) is None

    def test_no_declared_list_rejects_named_selection(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "anything"])
        assert flow._validate_test_selector(["lite"], {}) is not None

    def test_unknown_for_one_of_many_targets_errors(self, tmp_path: Path):
        # Valid for one target, absent from another → still an error: the target
        # missing the test would otherwise run its default and report a PASS.
        names = {"lite": ["lite_smoke"], "full": ["full_sign"]}
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_smoke"])
        result = flow._validate_test_selector(["lite", "full"], names)
        assert result is not None
        assert result.exit_code == EXIT_ERROR
        assert "full" in result.report_text


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


class TestExecutionValidation:
    def test_job_class_is_heavy(self, tmp_path: Path):
        from booley.runtime import job_slots

        flow = _make_flow(tmp_path, config="lite")
        assert flow._resolve_job_class() == job_slots.CLASS_HEAVY


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def _preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: dict[str, list[str]],
    **request: Any,
) -> ExecutionResult:
    """Dry-run ``SimulateFlow.execute`` over one ``.core`` per Target in *targets*.

    *targets* maps each selector to its configured Tests (empty: none configured).
    """
    from booley.flows.sim.request import SimRequest
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    data = write_sim_preview_project(tmp_path, targets)
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    reset_cache()
    return SimulateFlow().execute(
        SimRequest(dry_run=True, work_dir=tmp_path, report_dir=tmp_path / "reports", **request)
    )


class TestDryRun:
    def test_plan_keeps_non_shell_commands_unchanged(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path, config="lite")
        command = ("make", "-C", ".booley_work/sim")

        assert flow._redact_plan_environment("lite", command) == command

    def test_plan_collects_preview_errors(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path, config="lite")
        execution = MagicMock()
        execution.preview.side_effect = ValueError("preview failed")
        flow._simulation_execution_override = execution

        units, errors = flow._plan_simulation_targets(
            ["lite"],
            {},
            role="ordinary",
            revision=None,
        )

        assert units == []
        assert errors == ["lite: preview failed"]

    def test_plan_reports_a_refused_private_mirror_as_a_planning_error(
        self, tmp_path: Path
    ) -> None:
        """A mirror refusal (#2895) is a planning error, not an escaped exception."""
        from booley.flows.sim.build import SimulationBuildPreparationError

        flow = _make_flow(tmp_path, config="lite")
        execution = MagicMock()
        execution.preview.side_effect = SimulationBuildPreparationError("mirror refused")
        flow._simulation_execution_override = execution

        units, errors = flow._plan_simulation_targets(["lite"], {}, role="ordinary", revision=None)

        assert units == []
        assert errors == ["lite: mirror refused"]

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_plan_fingerprint_changes_with_configured_environment(
        self,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ) -> None:
        flow = _make_flow(tmp_path, config="lite")
        with patch.object(flow, "_target_sim_env", return_value={"FLAVOR": "fast"}):
            first = flow._plan_simulation(["lite"], {})
        with patch.object(flow, "_target_sim_env", return_value={"FLAVOR": "safe"}):
            second = flow._plan_simulation(["lite"], {})

        assert first.semantic_plan_fingerprint != second.semantic_plan_fingerprint

    @pytest.mark.parametrize(
        ("targets", "request_args", "expected"),
        [
            pytest.param({"lite": []}, {}, {"lite": 1}, id="default-selection"),
            pytest.param(
                {"lite": ["smoke", "stress", "boot"]}, {}, {"lite": 3}, id="one-item-per-test"
            ),
            pytest.param(
                {"lite": ["smoke", "stress"]}, {"test": ("smoke",)}, {"lite": 1}, id="test-filter"
            ),
            pytest.param(
                {"lite": ["smoke", "stress"], "full": ["smoke"]},
                {"target": "lite,full", "test": ("smoke",)},
                {"lite": 1, "full": 1},
                id="multi-target",
            ),
        ],
    )
    def test_dry_run_previews_one_work_item_per_selected_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        targets: dict[str, list[str]],
        request_args: dict[str, Any],
        expected: dict[str, int],
    ) -> None:
        request_args = {"target": "lite", **request_args}
        result = _preview(tmp_path, monkeypatch, targets, **request_args)

        outcome = result.outcome
        assert result.exit_code == EXIT_SUCCESS, outcome.report_text
        assert {
            preview["target"]["selector"]: preview["work_items"]
            for preview in outcome.detail["campaign_preview"]
        } == expected
        assert outcome.detail["mutated"] is False
        # A dry run reserves no invocation directory and takes no lock.
        report_root = tmp_path / "reports/sim"
        assert not [p for p in report_root.glob("*") if p.name.isdigit() or p.suffix == ".lock"]


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


class TestCommandBuilding:
    # Adapter command rendering is covered through SimulationExecution.preview.

    def test_trace_scope_left_the_surface(self, tmp_path: Path):
        """--trace-scope was removed (ADR 0022): the --trace overlay traces full
        hierarchy, so simulate neither accepts nor forwards a scope."""
        import pytest

        # The flag is no longer a recognized argument (argparse exits on it)...
        with pytest.raises(SystemExit):
            _make_flow(tmp_path, extra_args=["--trace", "--trace-scope", "tb.dut"])
        # ...and the Flow exposes no trace_scope.
        flow = _make_flow(tmp_path, extra_args=["--trace"])
        assert not hasattr(flow.args, "trace_scope")


# ---------------------------------------------------------------------------
# Ordinary simulation through the campaign path (SimulateFlow.execute)
#
# These run the real endpoint, campaign planning and execution, and Criteria
# reconciliation; only the process edge is faked (``campaign_boundary``).
# ---------------------------------------------------------------------------


def _projection(tmp_path: Path, target: str = "lite") -> dict[str, Any]:
    """Return the ``simulation.json`` projection Target *target* published."""
    path = tmp_path / f"reports/sim/1/targets/{target}/simulation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _launched_tests(run: CampaignSimRun) -> list[str]:
    return [test for _target, tests in run.launched for test in tests]


def test_campaign_rejects_unknown_test_without_launching(tmp_path: Path, monkeypatch):
    # The reported bug: --test no_such_test used to run the TB's default test and
    # report PASS. The campaign must reject it before any simulator launches.
    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress"]},
        PASS_PROCESS,
        target="lite",
        test=("typo",),
    )
    assert run.result.exit_code == EXIT_ERROR
    report_text = run.result.outcome.report_text
    assert "typo" in report_text
    assert "lite" in report_text
    # The declared tests are listed so the fix (a typo) is one hop away.
    assert "smoke" in report_text
    assert run.launched == []


@pytest.mark.parametrize(
    ("selector", "expected"),
    [(None, ["smoke", "boot"]), (("stress",), ["stress"])],
    ids=["one-skip", "explicit-test-overrides-skip"],
)
def test_campaign_runs_tests_left_by_skip_list(
    tmp_path: Path, monkeypatch, selector: tuple[str, ...] | None, expected: list[str]
):
    # Naming a skipped test by hand is a clear override of the skip list.
    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress", "boot"]},
        PASS_PROCESS,
        skip={"lite": ["stress"]},
        target="lite",
        test=selector,
    )
    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    assert _launched_tests(run) == expected


def test_campaign_rejects_target_whose_tests_are_all_skipped(tmp_path: Path, monkeypatch):
    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress"]},
        PASS_PROCESS,
        skip={"lite": ["smoke", "stress"]},
        target="lite",
    )
    assert run.result.exit_code == EXIT_ERROR
    assert "no runnable tests" in run.result.outcome.report_text
    assert run.launched == []


def _lite_passes_full_fails(launch: Launch) -> SubprocessResult:
    return PASS_PROCESS if launch.target == "lite" else FAIL_PROCESS


@pytest.mark.parametrize(
    ("targets", "process", "target", "exit_code", "grades"),
    [
        ({"lite": ["smoke"]}, PASS_PROCESS, "lite", EXIT_SUCCESS, {"lite": "pass"}),
        ({"lite": ["smoke"]}, FAIL_PROCESS, "lite", EXIT_FAILURE, {"lite": "fail"}),
        (
            {"lite": ["smoke"]},
            INCONCLUSIVE_PROCESS,
            "lite",
            EXIT_FAILURE,
            {"lite": "inconclusive"},
        ),
        (
            {"lite": ["smoke", "stress"]},
            PASS_PROCESS,
            "lite",
            EXIT_SUCCESS,
            {"lite": "pass"},
        ),
        (
            {"lite": ["smoke"], "full": ["smoke"]},
            _lite_passes_full_fails,
            "lite,full",
            EXIT_FAILURE,
            {"lite": "pass", "full": "fail"},
        ),
    ],
    ids=["summary-pass", "summary-fail", "inconclusive", "two-tests-pass", "mixed-targets"],
)
def test_campaign_verdict_sets_exit_code(
    tmp_path: Path, monkeypatch, targets, process, target, exit_code, grades
):
    run = run_campaign_sim(tmp_path, monkeypatch, targets, process, target=target)
    outcome = run.result.outcome
    assert run.result.exit_code == exit_code, outcome.report_text
    assert {name: row["grade"] for name, row in outcome.detail["campaigns"].items()} == grades
    assert f"report: {tmp_path / 'reports/sim.json'}" in outcome.report_text
    if grades == {"lite": "inconclusive"}:
        assert "INCONCLUSIVE" in outcome.report_text


def _ticket_state(tmp_path: Path, monkeypatch, keys: list[str]) -> Path:
    """Save a strict Ticket state declaring *keys*, and name it as the state file."""
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug, state.ticket_type = "ticket", "implementation"
    state.init_criteria(dict.fromkeys(keys, True), strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    return state_path


@pytest.mark.parametrize(
    ("process", "ticket", "expected"),
    [
        (_lite_passes_full_fails, True, {"sim_pass_lite": True, "sim_pass_full": False}),
        (INCONCLUSIVE_PROCESS, True, {"sim_pass_lite": False, "sim_pass_full": False}),
        (PASS_PROCESS, False, {"sim_pass_lite": True}),
        (FAIL_PROCESS, False, {"sim_pass_lite": False}),
        (INCONCLUSIVE_PROCESS, False, {}),
    ],
    ids=[
        "ticket-mixed",
        "ticket-inconclusive",
        "standalone-pass",
        "standalone-fail",
        "standalone-inconclusive",
    ],
)
def test_campaign_records_criteria(
    tmp_path: Path, monkeypatch, process, ticket: bool, expected: dict[str, bool]
):
    """Ticket Mode reconciles every declared Criterion through the acceptance
    coordinator; a standalone run records pass and fail but nothing when inconclusive."""
    from tests.flows.sim.test_coverage_flow import _AcceptanceAdapter

    targets = {"lite": ["smoke"], "full": ["smoke"]} if ticket else {"lite": ["smoke"]}
    if not ticket:
        monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
        run = run_campaign_sim(tmp_path, monkeypatch, targets, process, target="lite")
        criteria = run.flow.state.criteria
        assert {key: criterion.met for key, criterion in criteria.items()} == expected
        return
    state_path = _ticket_state(tmp_path, monkeypatch, list(expected))
    recorder = _AcceptanceAdapter(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    run_campaign_sim(tmp_path, monkeypatch, targets, process, target="lite,full", adapter=recorder)
    saved = DevelopmentState.load(state_path)
    assert {key: saved.is_met(key) for key in expected} == expected
    # One acceptance transaction per declared Criterion: the coordinator recorded them.
    assert len(saved.acceptance_transactions) == len(expected)


@pytest.mark.skipif(os.name == "nt", reason="POSIX unreadable-directory regression")
@pytest.mark.parametrize("relative", ["unreadable", ".booley/unreadable"])
def test_unreadable_unrelated_directory_does_not_block_plain_simulation(
    tmp_path: Path, monkeypatch, relative: str
) -> None:
    unreadable = tmp_path / relative
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        run = run_campaign_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite"
        )
    finally:
        unreadable.chmod(0o700)

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text


def test_campaign_projection_reports_tests_and_label(tmp_path: Path, monkeypatch):
    run = run_campaign_sim(
        tmp_path, monkeypatch, {"lite": ["smoke", "stress"]}, PASS_PROCESS, target="lite"
    )
    report = _projection(tmp_path)
    assert report["flow"] == "sim"
    assert report["target"] == "lite"
    assert report["passed"] is True
    assert [test["name"] for test in report["tests"]] == ["smoke", "stress"]
    assert report["tests"][0]["cycles"] == 2561
    assert run.result.outcome.display_label == "target lite · 2 tests"


def test_configured_cycle_sentinel_reaches_report_and_projection(tmp_path: Path, monkeypatch):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[flows.sim]\ncycle_sentinels = ["CoreMark completed in:"]\n', encoding="utf-8"
    )
    process = SubprocessResult(
        returncode=0,
        stdout=(
            "[SIM_RESULT] PASSED\n"
            "CoreMark completed in: coremark 31415\n"
            '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        ),
        duration_s=1.0,
    )
    run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["coremark"]}, process, target="lite")

    assert "coremark: cycles=31415" in run.result.outcome.report_text
    assert _projection(tmp_path)["tests"][0]["cycles"] == 31415


def _run_logs(tmp_path: Path) -> list[Path]:
    """Every work item's evidence ``run.log``, in work-item order."""
    return sorted((tmp_path / "reports").rglob("execution-evidence/run.log"))


def test_campaign_keeps_each_test_log_in_its_own_work_item(tmp_path: Path, monkeypatch):
    """Each HDL launch gets durable evidence before the next one starts."""
    first_bytes: list[bytes] = []

    def process(launch: Launch) -> SubprocessResult:
        if launch.tests == ("stress",):
            first_bytes.append(_run_logs(tmp_path)[0].read_bytes())
            return SubprocessResult(
                returncode=1, stdout="STRESS_ONLY\n[SIM_RESULT] FAILED\n", duration_s=0.1
            )
        return SubprocessResult(
            returncode=0, stdout="SMOKE_ONLY\n[SIM_RESULT] PASSED\n", duration_s=0.1
        )

    run = run_campaign_sim(
        tmp_path, monkeypatch, {"lite": ["smoke", "stress"]}, process, target="lite"
    )

    assert run.result.exit_code == EXIT_FAILURE
    assert _launched_tests(run) == ["smoke", "stress"]
    smoke_log, stress_log = _run_logs(tmp_path)
    assert smoke_log.read_bytes() == first_bytes[0]
    assert "SMOKE_ONLY" in smoke_log.read_text(encoding="utf-8")
    assert "STRESS_ONLY" not in smoke_log.read_text(encoding="utf-8")
    assert "STRESS_ONLY" in stress_log.read_text(encoding="utf-8")
    assert "SMOKE_ONLY" not in stress_log.read_text(encoding="utf-8")


def test_consecutive_campaigns_preserve_first_evidence(tmp_path: Path, monkeypatch):
    def run(output: str) -> tuple[Path, bytes]:
        run_campaign_sim(
            tmp_path,
            monkeypatch,
            {"lite": ["smoke"]},
            SubprocessResult(returncode=0, stdout=f"{output}\n[SIM_RESULT] PASSED\n"),
            target="lite",
            test=("smoke",),
        )
        path = _run_logs(tmp_path)[-1]
        return path, path.read_bytes()

    first_path, first_bytes = run("FIRST_ONLY")
    second_path, second_bytes = run("SECOND_ONLY")

    assert first_path != second_path
    assert first_path.read_bytes() == first_bytes
    assert b"FIRST_ONLY" in first_bytes
    assert b"SECOND_ONLY" in second_bytes


def test_campaign_crash_keeps_completed_target_in_progress(tmp_path: Path, monkeypatch):
    def process(launch: Launch) -> SubprocessResult:
        if launch.target == "full":
            raise RuntimeError("simulated outer interruption")
        return PASS_PROCESS

    monkeypatch.setenv("BOOLEY_RUN_ID", "sim-checkpoint-1")
    run = run_campaign_sim(
        tmp_path, monkeypatch, {"lite": ["smoke"], "full": ["smoke"]}, process, target="lite,full"
    )

    assert run.result.exit_code == EXIT_ERROR
    assert "simulated outer interruption" in run.result.outcome.report_text
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text(encoding="utf-8"))
    assert progress["run_id"] == "sim-checkpoint-1"
    assert progress["complete"] is True
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == ["lite"]
    assert progress["pending_targets"] == ["full"]


def test_default_report_root_uses_project_data(tmp_path: Path, monkeypatch):
    rtl = tmp_path / "rtl-checkout"
    rtl.mkdir()
    project_data = tmp_path / "project-data"
    run = run_campaign_sim(
        rtl,
        monkeypatch,
        {"lite": []},
        PASS_PROCESS,
        target="lite",
        report_dir=None,
        project_data=project_data,
    )
    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    invocation = project_data / "flow-reports/sim/1"
    assert (invocation / "progress.json").is_file()
    assert (invocation / "targets/lite/simulation.json").is_file()
    assert not (rtl / "flow-reports").exists()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_mcp_default_timeout_allows_trace_retry(self):
        assert SimulateFlow.default_timeout == 1290

    def test_default_timeout(self, tmp_path: Path):
        flow = _make_flow(tmp_path)
        assert flow._get_timeout() == 600  # 600000ms -> 600s

    def test_custom_timeout(self, tmp_path: Path):
        flow = _make_flow(tmp_path, extra_args=["--timeout-ms", "120000"])
        assert flow._get_timeout() == 120

    def test_timeout_results_in_fail(self, tmp_path: Path, monkeypatch):
        # The wrapper killed the launch: no adapter result, only the timed-out process.
        process = SubprocessResult(
            returncode=-1,
            stdout="partial output",
            timed_out=True,
            duration_s=600.0,
        )
        run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite")

        assert run.result.exit_code == EXIT_FAILURE
        test = _projection(tmp_path)["tests"][0]
        assert test["timed_out"] is True
        assert test["execution"] == "timeout"
        hint = "raise --timeout-ms or [flows.sim].timeout_ms if the test legitimately needs longer"
        assert "TIMEOUT: simulation exceeded" in test["error_tail"]
        assert hint in test["error_tail"]
        assert hint in run.result.outcome.report_text


# ---------------------------------------------------------------------------
# Elaboration failure detection
# ---------------------------------------------------------------------------


class TestErrorTailSource:
    """The error excerpt must surface the DUT's own (stdout) failure signal, not
    the build's stderr lint-warning storm — except for a failed build, whose only
    diagnostic lives on stderr with no sim stdout to fall back to."""

    def test_sim_failure_tail_from_stdout_not_stderr_noise(self, tmp_path: Path, monkeypatch):
        """A real sim FAIL: the build's stderr warning storm must not bury the
        stdout mismatch line in the 50-line excerpt window."""
        noise = "\n".join(
            f"/work/rtl/foo.sv:{i}: warning: @* is sensitive to all 16 words" for i in range(60)
        )
        process = SubprocessResult(
            returncode=1,
            stdout="[SIM_RESULT] FAILED\nFAIL: coeff mismatch at index 7\n",
            stderr=noise,
            duration_s=5.0,
        )
        run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite")

        assert run.result.exit_code == EXIT_FAILURE
        tail = _projection(tmp_path)["tests"][0]["error_tail"]
        assert "coeff mismatch at index 7" in tail
        assert "warning:" not in tail

    def test_elab_failure_tail_falls_back_to_stderr(self, tmp_path: Path, monkeypatch):
        """A failed build produces no sim stdout — the excerpt must carry the
        stderr diagnostic so it is not lost."""
        build = SubprocessResult(returncode=1, stderr="%Error: rtl/top.sv:17: syntax error\n")
        run = run_campaign_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite", build=build
        )

        assert run.result.exit_code == EXIT_FAILURE
        assert run.launched == []
        assert "syntax error" in _projection(tmp_path)["tests"][0]["error_tail"]


# ---------------------------------------------------------------------------
# ravenoc F-32 — a missing EDA binary is a Flow error, never a test failure
# ---------------------------------------------------------------------------


class TestMissingExecutableDetection:
    def test_dash_not_found(self):
        from booley.flows.sim.flow import find_missing_executable

        assert find_missing_executable("/bin/sh: 1: verilator: not found") == "verilator"

    def test_bash_command_not_found(self):
        from booley.flows.sim.flow import find_missing_executable

        assert find_missing_executable("bash: fusesoc: command not found") == "fusesoc"

    def test_make_no_such_file(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "make[1]: verilator: No such file or directory"
        assert find_missing_executable(text) == "verilator"

    def test_fusesoc_spawn_guard(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "could not invoke fusesoc (fusesoc): [Errno 2] No such file or directory: 'fusesoc'"
        assert find_missing_executable(text) == "fusesoc"

    def test_bare_spawn_failure_names_the_eda_tool(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "FileNotFoundError: [Errno 2] No such file or directory: 'xrun'"
        assert find_missing_executable(text) == "xrun"

    def test_missing_data_file_is_not_a_missing_binary(self):
        from booley.flows.sim.flow import find_missing_executable

        # A plain setup failure keeps its own message and exit-1 grading.
        text = "FileNotFoundError: [Errno 2] No such file or directory: '/work/foo.core'"
        assert find_missing_executable(text) is None

    def test_ordinary_compile_error_is_not_a_missing_binary(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "%Error: tb.sv:12: Cannot find file containing module: 'dut'\nnot found\n"
        assert find_missing_executable(text) is None


class TestMissingExecutableIsEdaToolError:
    """F-32: the verdict channel must not say FAIL when nothing ever ran."""

    def test_missing_verilator_in_build_exits_2(self, tmp_path: Path, monkeypatch):
        # What the sandbox script actually prints with verilator off PATH.
        build = SubprocessResult(
            returncode=127,
            stdout=(
                "/bin/sh: 1: verilator: not found\n"
                "make: *** [Makefile:9: Vtop] Error 127\n"
                "ERROR: Verilator elaboration failed (rc=127)\n"
            ),
        )
        run = run_campaign_sim(
            tmp_path, monkeypatch, {"lite": ["t1"]}, PASS_PROCESS, target="lite", build=build
        )
        outcome = run.result.outcome
        assert run.result.exit_code == EXIT_ERROR
        assert outcome.detail["eda_tool_error"] == "build_infrastructure"
        assert "missing tool: verilator" in outcome.report_text
        assert run.launched == []

    def test_live_sim_echoing_command_not_found_is_not_hijacked(self, tmp_path: Path, monkeypatch):
        """A running TB's own $system noise must not become a Flow error."""
        process = SubprocessResult(
            returncode=1,
            stdout=(
                "sh: 1: helper_script: not found\n"
                '[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n'
            ),
        )
        run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["t1"]}, process, target="lite")
        assert run.result.exit_code == EXIT_FAILURE
        assert "eda_tool_error" not in run.result.outcome.detail


# ---------------------------------------------------------------------------
# Selector round trip: producer argv -> run-half argparse (F-12 family)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("met", [True, False, None])
def test_standalone_elab_only_final_headline(tmp_path, met):
    from booley.flows.sim.build import BuildOutcome
    from booley.flows.sim.flow import ElabOnlyTargetResult
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.flows.report_headline import final_report_omits_headline

    flow = _make_flow(tmp_path, config="lite", extra_args=["--mode", "elab-only"])
    flow.args.state_file = None
    flow.read_state()
    build = BuildOutcome(
        True,
        None if met is None else ("pass" if met else "fail"),
        "infrastructure" if met is None else (None if met else "design"),
    )
    flow._record_elab_only_criterion(ElabOnlyTargetResult("lite", outcome=build))
    result = EndpointOutcome(exit_code=2 if met is None else (0 if met else 1))
    with flow.publication_resources:
        report = final_report_omits_headline(flow, result)
    assert report["exit_code"] == result.exit_code
    if met is None:
        assert "elab_pass_lite" not in flow.state.criteria
    else:
        assert flow.state.criteria["elab_pass_lite"].met is met
    assert flow.state._file_path is None


def test_target_report_write_survives_transient_replace_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.flow import _atomic_write_json
    from tests.sharing_error_support import hold_replaces

    path = tmp_path / "targets" / "sim_a" / "simulation.json"
    path.parent.mkdir(parents=True)
    attempts = hold_replaces(monkeypatch, {path: 1})
    _atomic_write_json(path, {"passed": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"passed": True}
    assert attempts[path] == 2


def test_target_report_write_retries_before_raising_lasting_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.flow import _atomic_write_json
    from tests.sharing_error_support import hold_replaces

    path = tmp_path / "targets" / "sim_a" / "simulation.json"
    path.parent.mkdir(parents=True)
    attempts = hold_replaces(monkeypatch, {path: 100})
    with pytest.raises(PermissionError):
        _atomic_write_json(path, {"passed": True})
    assert attempts[path] > 1
