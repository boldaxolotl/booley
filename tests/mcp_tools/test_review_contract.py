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
