"""Executable reproductions for issue #88, finding F-46.

The UART campaign found four ways that a criterion's displayed green state
could overstate the evidence behind it. These tests state the acceptance
contract enforced by the Ticket-mode criteria boundary.
"""

from __future__ import annotations

from pathlib import Path

from booley.dev_support.criteria import CriteriaTemplate
from booley.dev_support.development_state import (
    SOURCE_FINGERPRINT_DETAIL_KEY,
    DevelopmentState,
    compute_source_fingerprint,
)
from booley.ticket_board.criteria_acceptance import check_criteria_acceptance
from booley.ticket_board.criteria_summary_format import build_criteria_summary_lines


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    control = project / ".booley_project"
    rtl = project / "rtl"
    tb = project / "tb"
    control.mkdir(parents=True)
    rtl.mkdir()
    tb.mkdir()
    (control / "booley.toml").write_text(
        "[sources.rtl]\nsource_dirs = ['rtl']\n[sources.testbench]\nsource_dirs = ['tb']\n",
        encoding="utf-8",
    )
    (control / "tests.toml").write_text(
        "[sim_uart]\ntests = ['test_reset', 'test_transmit', 'test_receive']\n",
        encoding="utf-8",
    )
    (rtl / "uart.sv").write_text("module uart; endmodule\n", encoding="utf-8")
    (tb / "test_uart.py").write_text(
        "def uart_model(value):\n"
        "    return value & 0xff\n\n"
        "def test_model_reset():\n"
        "    assert uart_model(0) == 0\n\n"
        "def test_model_mask():\n"
        "    assert uart_model(0x1ff) == 0xff\n",
        encoding="utf-8",
    )
    return project


def _source_stamp(project: Path) -> dict:
    return {
        "categories": ["rtl", "tb"],
        "fingerprint": compute_source_fingerprint(project),
    }


def _state(tmp_path: Path, criteria: dict[str, bool]) -> DevelopmentState:
    state = DevelopmentState.load(tmp_path / "booley_state.json")
    state.slug = "issue-88-f46"
    state.init_criteria({**criteria, "_report_submitted": True}, strict=True)
    state.set_criterion("_report_submitted", True)
    return state


def test_dut_sim_criterion_rejects_explicit_model_only_evidence(tmp_path: Path) -> None:
    """A DUT criterion must not pass from a model compared only with itself."""
    project = _project(tmp_path)
    state = _state(tmp_path, {"sim_pass_sim_uart": True})
    state.work_dir = str(project)
    state.set_criterion(
        "sim_pass_sim_uart",
        True,
        detail={
            "tests_passed": 2,
            "tests_total": 2,
            "verification_subject": "model",
            "dut_observations": 0,
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.save()

    verdict = check_criteria_acceptance(state._file_path, work_dir=project)

    assert verdict.disposition == "failed"
    assert verdict.unmet_mandatory == ["sim_pass_sim_uart"]


def test_full_suite_criterion_rejects_partial_registry_evidence(tmp_path: Path) -> None:
    """One passing test must not satisfy a three-test Target registry."""
    project = _project(tmp_path)
    state = _state(tmp_path, {"sim_pass_sim_uart": True})
    state.work_dir = str(project)
    state.set_criterion(
        "sim_pass_sim_uart",
        True,
        detail={
            "tests_passed": 1,
            "tests_total": 1,
            "test_selector": "partial",
            "selected_tests": ["test_reset"],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.save()

    verdict = check_criteria_acceptance(state._file_path, work_dir=project)

    assert verdict.disposition == "failed"
    assert verdict.unmet_mandatory == ["sim_pass_sim_uart"]


def test_malformed_registry_cannot_weaken_sim_acceptance(tmp_path: Path) -> None:
    """Invalid tests.toml must fail closed instead of becoming an empty registry."""
    project = _project(tmp_path)
    state = _state(tmp_path, {"sim_pass_sim_uart": True})
    state.work_dir = str(project)
    state.set_criterion(
        "sim_pass_sim_uart",
        True,
        detail={
            "tests_passed": 1,
            "tests_total": 1,
            "selected_tests": ["test_reset"],
            "passed_tests": ["test_reset"],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.save()
    (project / ".booley_project" / "tests.toml").write_text(
        "[sim_uart\ntests = ['test_reset']\n",
        encoding="utf-8",
    )

    verdict = check_criteria_acceptance(state._file_path, work_dir=project)

    assert verdict.disposition == "failed"
    assert verdict.unmet_mandatory == ["sim_pass_sim_uart"]
    entry = DevelopmentState.load(state._file_path).criteria["sim_pass_sim_uart"]
    assert "tests.toml acceptance registry is invalid" in entry.detail["acceptance_error"]


def test_unmet_criterion_shows_copyable_target_invocation(tmp_path: Path) -> None:
    """Status should prevent wrong-Target calls by displaying the exact Target."""
    state = _state(tmp_path, {"lint_clean_lint_uart": True})
    state.save()

    lines, _totals = build_criteria_summary_lines(state._file_path)

    assert any("--target lint_uart" in line for line in lines)


def test_wrong_target_result_is_not_stored_as_optional_evidence(tmp_path: Path) -> None:
    """A wrong-Target Flow result must be rejected instead of hidden as optional."""
    state = _state(tmp_path, {"lint_clean_lint_uart": True})

    state.set_criterion("lint_clean_sim_uart", True, detail={"warnings": 0})

    assert "lint_clean_sim_uart" not in state.criteria


def test_fail_to_pass_requires_a_recorded_failing_half(tmp_path: Path) -> None:
    """A pass-only run must not satisfy an explicit fail-to-pass contract."""
    project = _project(tmp_path)
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "sim_pass": [
                    "tb/test_uart.py @ sim_uart @ test_transmit @ fail -> pass",
                ]
            }
        }
    )
    state = DevelopmentState.load(tmp_path / "booley_state.json")
    state.slug = "issue-88-f46-red-green"
    state.work_dir = str(project)
    state.init_criteria(
        {**template.expand(["sim_uart"]), "_report_submitted": True},
        criterion_params=template.expand_params(["sim_uart"]),
        flow_key_aliases=template.flow_key_aliases(),
        strict=True,
    )
    state.set_criterion(
        "sim_pass_sim_uart",
        True,
        detail={
            "tests_passed": 1,
            "tests_total": 1,
            "test_selector": "test_transmit",
            "selected_tests": ["test_transmit"],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.set_criterion("_report_submitted", True)
    state.save()

    verdict = check_criteria_acceptance(state._file_path, work_dir=project)

    assert verdict.disposition == "failed"
    assert verdict.unverified_transitions == []


def test_fail_to_pass_rejects_a_sibling_tests_red_evidence(tmp_path: Path) -> None:
    """The named test, not merely its suite, must supply the failing half."""
    project = _project(tmp_path)
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "sim_pass": [
                    "tb/test_uart.py @ sim_uart @ test_transmit @ fail -> pass",
                ]
            }
        }
    )
    state = DevelopmentState.load(tmp_path / "booley_state.json")
    state.slug = "issue-88-f46-exact-red"
    state.work_dir = str(project)
    state.init_criteria(
        {**template.expand(["sim_uart"]), "_report_submitted": True},
        criterion_params=template.expand_params(["sim_uart"]),
        flow_key_aliases=template.flow_key_aliases(),
        strict=True,
    )
    state.set_criterion(
        "sim_pass_sim_uart",
        False,
        detail={
            "test_selector": "all",
            "selected_tests": ["test_transmit", "test_receive"],
            "passed_tests": ["test_transmit"],
            "failed_tests": ["test_receive"],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.set_criterion(
        "sim_pass_sim_uart",
        True,
        detail={
            "tests_passed": 1,
            "tests_total": 1,
            "test_selector": "test_transmit",
            "selected_tests": ["test_transmit"],
            "passed_tests": ["test_transmit"],
            "failed_tests": [],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.set_criterion("_report_submitted", True)
    state.save()

    verdict = check_criteria_acceptance(state._file_path, work_dir=project)

    assert verdict.disposition == "failed"
    criterion_key = next(key for key in state.criteria if key.startswith("sim_pass_"))
    assert verdict.unmet_mandatory == [criterion_key]
    entry = DevelopmentState.load(state._file_path).criteria[criterion_key]
    assert "matching fingerprinted failing evidence" in entry.detail["acceptance_error"]


def test_fail_to_pass_accepts_structured_fingerprinted_red_evidence(tmp_path: Path) -> None:
    """The canonical structured source receipt is valid red-run provenance."""
    project = _project(tmp_path)
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "sim_pass": [
                    "tb/test_uart.py @ sim_uart @ test_transmit @ fail -> pass",
                ]
            }
        }
    )
    state = DevelopmentState.load(tmp_path / "booley_state.json")
    state.slug = "issue-88-f46-structured-red"
    state.work_dir = str(project)
    state.init_criteria(
        {**template.expand(["sim_uart"]), "_report_submitted": True},
        criterion_params=template.expand_params(["sim_uart"]),
        flow_key_aliases=template.flow_key_aliases(),
        strict=True,
    )
    state.set_criterion(
        "sim_pass_sim_uart",
        False,
        detail={
            "test_selector": "test_transmit",
            "selected_tests": ["test_transmit"],
            "passed_tests": [],
            "failed_tests": ["test_transmit"],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.set_criterion(
        "sim_pass_sim_uart",
        True,
        detail={
            "tests_passed": 1,
            "tests_total": 1,
            "test_selector": "test_transmit",
            "selected_tests": ["test_transmit"],
            "passed_tests": ["test_transmit"],
            "failed_tests": [],
            SOURCE_FINGERPRINT_DETAIL_KEY: _source_stamp(project),
        },
    )
    state.set_criterion("_report_submitted", True)
    state.save()

    verdict = check_criteria_acceptance(state._file_path, work_dir=project)

    assert verdict.disposition == "review"
