"""Source-scoped contract tests for Reviewer Specialist."""

from __future__ import annotations

import pytest

from booley.specialists.review_contract import ReviewContractError, resolve_review_scope


def test_rtl_scope_needs_only_hdl_files() -> None:
    contract = resolve_review_scope(["rtl/uart.sv", "rtl/pkg.vh"], category="rtl")

    assert contract.hdl_files == frozenset({"rtl/pkg.vh", "rtl/uart.sv"})
    assert contract.cocotb_files == frozenset()


def test_tb_scope_classifies_mixed_hdl_and_cocotb_files() -> None:
    contract = resolve_review_scope(
        ["tb/test_uart.py", "tb/uart_tb.sv"],
        category="tb",
    )

    assert contract.cocotb_files == frozenset({"tb/test_uart.py"})
    assert contract.hdl_files == frozenset({"tb/uart_tb.sv"})
    assert contract.has_cocotb
    assert contract.has_hdl


def test_tb_scope_rejects_unknown_file_kinds() -> None:
    with pytest.raises(ReviewContractError, match="Unsupported TB source kind"):
        resolve_review_scope(["tb/vectors.json"], category="tb")


def test_rtl_scope_rejects_python() -> None:
    with pytest.raises(ReviewContractError, match="Unsupported RTL source kind"):
        resolve_review_scope(["rtl/model.py"], category="rtl")


def test_scope_is_required() -> None:
    with pytest.raises(ReviewContractError, match="at least one source"):
        resolve_review_scope([], category="tb")


@pytest.mark.parametrize(
    "suffix", ["c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx", "C", "CPP", "HXX"]
)
def test_interactive_cpp_is_exact_support_scope(suffix):
    path = f"tb/source.{suffix}"
    contract = resolve_review_scope([path], category="tb")
    assert contract.support_files == frozenset({path})
    assert contract.contains_file(path)
    assert not contract.contains_file("tb/neighbor.cpp")
    assert not contract.has_hdl
    assert not contract.has_cocotb
    assert contract.has_cpp


def test_rtl_cpp_refusal_lists_interactive_supported_kinds():
    with pytest.raises(ReviewContractError) as caught:
        resolve_review_scope(["rtl/source.cpp"], category="rtl")
    assert ".svh" in str(caught.value)
    assert "CLI-REFERENCE" in str(caught.value)


def test_goal_arbitrary_support_is_not_cpp():
    contract = resolve_review_scope(["config.json"], category="tb", allow_support=True)
    assert not contract.has_cpp


def test_goal_mode_preserves_explicit_cpp_support_scope():
    contract = resolve_review_scope(["shared/main.cpp"], category="tb", allow_support=True)
    assert contract.support_files == frozenset({"shared/main.cpp"})
