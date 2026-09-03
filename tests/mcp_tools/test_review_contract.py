"""Target-contract resolution tests for Reviewer Specialist."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.specialists.review_contract import ReviewContractError, resolve_review_target
from booley.targets.target import inspect_target as real_inspect_target


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


def test_same_kind_target_matches_are_still_ambiguous(tmp_path: Path) -> None:
    _write_project(tmp_path)
    core = tmp_path / "uart.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace(
            ", cocotb_module: test_uart",
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReviewContractError, match="ambiguously matches multiple TB Targets"):
        resolve_review_target(tmp_path, ["tb/test_uart.py"], category="tb")


def test_no_matching_tb_target_has_specific_diagnostic(tmp_path: Path) -> None:
    _write_project(tmp_path)
    (tmp_path / "tb" / "other.py").write_text("# other\n", encoding="utf-8")

    with pytest.raises(ReviewContractError, match="No selectable Target contains every TB"):
        resolve_review_target(tmp_path, ["tb/other.py"], category="tb")


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


def test_explicit_tb_target_must_be_simulation_capable(tmp_path: Path) -> None:
    _write_project(tmp_path)
    core = tmp_path / "uart.core"
    core.write_text(
        core.read_text(encoding="utf-8")
        + "  lint_tb:\n"
        + "    filesets: [tb]\n"
        + "    toplevel: uart_tb\n"
        + "    flow: lint\n"
        + "    flow_options: {tool: verilator}\n",
        encoding="utf-8",
    )

    with pytest.raises(FuseSocError, match=r"cannot be driven by the 'sim' Flow"):
        resolve_review_target(
            tmp_path,
            ["tb/test_uart.py"],
            category="tb",
            target_hint="lint_tb",
        )


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


def test_bound_target_ignores_unrelated_uninspectable_target(tmp_path: Path) -> None:
    _write_project(tmp_path)

    def inspect(root, handle):
        if handle.name == "sim_hdl":
            raise FuseSocError("missing optional dependency")
        return real_inspect_target(root, handle)

    with patch("booley.specialists.review_contract.inspect_target", side_effect=inspect):
        contract = resolve_review_target(
            tmp_path,
            ["tb/test_uart.py"],
            category="tb",
            target_hint="sim_cocotb",
        )

    assert contract.selectors == ("sim_cocotb",)


def test_bound_target_reports_relevant_inspection_failure(tmp_path: Path) -> None:
    _write_project(tmp_path)

    with (
        patch(
            "booley.specialists.review_contract.inspect_target",
            side_effect=FuseSocError("missing required dependency"),
        ),
        pytest.raises(ReviewContractError, match=r"Relevant Target.*missing required dependency"),
    ):
        resolve_review_target(
            tmp_path,
            ["tb/test_uart.py"],
            category="tb",
            target_hint="sim_cocotb",
        )


def test_unbound_candidate_failure_is_isolated_and_fails_closed(tmp_path: Path) -> None:
    _write_project(tmp_path)

    def inspect(root, handle):
        if handle.name == "sim_hdl":
            raise FuseSocError("missing optional dependency")
        return real_inspect_target(root, handle)

    with (
        patch("booley.specialists.review_contract.inspect_target", side_effect=inspect),
        pytest.raises(ReviewContractError, match=r"potentially relevant.*sim_hdl"),
    ):
        resolve_review_target(tmp_path, ["tb/test_uart.py"], category="tb")
