"""Target-contract resolution tests for Reviewer Specialist."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.specialists.review_contract import ReviewContractError, resolve_review_target


def _write_project(root: Path) -> None:
    (root / "tb").mkdir()
    (root / "tb" / "test_uart.py").write_text("import cocotb\n", encoding="utf-8")
    (root / "uart.core").write_text(
        """CAPI=2:
name: acme:uart:uart:1
filesets:
  tb:
    files:
      - tb/test_uart.py: {file_type: user, copyto: test_uart.py}
    tags: [tb]
targets:
  sim_cocotb:
    filesets: [tb]
    toplevel: uart
    flow: sim
    flow_options: {tool: verilator, cocotb_module: test_uart}
  sim_hdl:
    filesets: [tb]
    toplevel: uart_tb
    flow: sim
    flow_options: {tool: verilator}
""",
        encoding="utf-8",
    )


def test_mixed_target_kinds_require_explicit_selector(tmp_path: Path) -> None:
    _write_project(tmp_path)

    with pytest.raises(ReviewContractError, match="pass --target"):
        resolve_review_target(
            tmp_path,
            ["tb/test_uart.py"],
            category="tb",
        )


def test_explicit_target_narrows_candidate_set(tmp_path: Path) -> None:
    _write_project(tmp_path)

    contract = resolve_review_target(
        tmp_path,
        ["tb/test_uart.py"],
        category="tb",
        target_hint="sim_cocotb",
    )

    assert contract.selectors == ("sim_cocotb",)
    assert contract.kind == "cocotb"


def test_explicit_target_must_contain_scope(tmp_path: Path) -> None:
    _write_project(tmp_path)
    (tmp_path / "tb" / "other.py").write_text("# other\n", encoding="utf-8")

    with pytest.raises(ReviewContractError, match="does not contain"):
        resolve_review_target(
            tmp_path,
            ["tb/other.py"],
            category="tb",
            target_hint="sim_cocotb",
        )


def test_scope_matching_uses_condition_selected_target_inputs(tmp_path: Path) -> None:
    (tmp_path / "conditional.core").write_text(
        "CAPI=2:\n"
        "name: acme:ip:conditional:1.0\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - tool_verilator ? (tb/selected.py): {tags: [tb]}\n"
        "      - tool_icarus ? (tb/unselected.py): {tags: [tb]}\n"
        "targets:\n"
        "  sim:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, cocotb_module: selected}\n"
        "    filesets: [tb]\n"
        "    toplevel: dut\n",
        encoding="utf-8",
    )

    contract = resolve_review_target(
        tmp_path,
        ["tb/selected.py"],
        category="tb",
        target_hint="sim",
    )

    assert contract.selectors == ("sim",)
    assert contract.kind == "cocotb"
