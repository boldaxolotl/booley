"""Tests for copyable criterion-to-endpoint actions."""

from booley.dev_support.criteria_actions import planned_invocation
from booley.dev_support.development_state import CriterionEntry


def test_per_target_lint_action_uses_key_target() -> None:
    entry = CriterionEntry(met=False, mandatory=True)

    assert planned_invocation("lint_clean_lint_uart", entry) == "lint --target lint_uart"


def test_structured_sim_action_uses_sealed_target_and_selector() -> None:
    entry = CriterionEntry(
        met=False,
        mandatory=True,
        params={
            "tb_path": "tb/test_uart.py",
            "target": "sim_uart",
            "test_selector": "test_transmit",
        },
    )

    assert (
        planned_invocation("sim_pass_tb_test_uart.py_sim_uart_test_transmit", entry)
        == "sim --target sim_uart --test test_transmit"
    )


def test_action_prefers_sealed_callable_selector_over_durable_identity() -> None:
    entry = CriterionEntry(
        met=False,
        mandatory=True,
        params={
            "target": "acme:ip:uart:1.0#lint_uart",
            "_target_selector": "uart#lint_uart",
        },
    )

    assert (
        planned_invocation("lint_clean_acme:ip:uart:1.0#lint_uart", entry)
        == "lint --target uart#lint_uart"
    )


def test_target_independent_reviewer_action_omits_fabricated_target_guidance() -> None:
    entry = CriterionEntry(
        met=False,
        mandatory=True,
        params={"target": "acme:ip:uart:1.0#sim_uart"},
    )

    assert (
        planned_invocation("review_rtl_spec_done", entry) == "reviewer --category rtl --focus spec"
    )
