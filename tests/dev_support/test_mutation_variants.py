"""Contract tests for exact, parser-free mutation variant materialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan


@dataclass
class Proposal:
    index: int = 1
    file: str = "rtl/dut.sv"
    line: int = 2
    original_code: str = "a + b"
    mutated_code: str = "a - b"


def _source(tmp_path: Path, text: str = "module dut;\nassign y = a + b;\nendmodule\n") -> Path:
    path = tmp_path / "rtl/dut.sv"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_materialized_variant_changes_only_private_source(tmp_path: Path):
    path = _source(tmp_path)
    pristine = path.read_bytes()
    plan = MutationVariantPlan.resolve([Proposal()], tmp_path, ["rtl/dut.sv"])

    root = tmp_path / "private"
    root.mkdir()
    staged = root / "dut.sv"
    staged.write_bytes(pristine)
    plan.materialize(1, [staged], root)
    assert staged.read_text(encoding="utf-8") == "module dut;\nassign y = a - b;\nendmodule\n"

    assert path.read_bytes() == pristine


def test_baseline_snapshot_is_never_rewritten(tmp_path: Path):
    path = _source(tmp_path)
    plan = MutationVariantPlan.resolve([Proposal()], tmp_path, ["rtl/dut.sv"])

    assert path.read_text(encoding="utf-8") == "module dut;\nassign y = a + b;\nendmodule\n"
    assert plan.source_fingerprint().startswith("sha256:")


def test_rejects_text_that_is_not_exactly_on_the_declared_line(tmp_path: Path):
    _source(tmp_path)

    with pytest.raises(MutationVariantError, match=r"not found.*dut.sv:3"):
        MutationVariantPlan.resolve(
            [Proposal(line=3)],
            tmp_path,
            ["rtl/dut.sv"],
        )


def test_rejects_ambiguous_text_on_one_line(tmp_path: Path):
    _source(tmp_path, "module dut;\nassign y = (a + b) + (a + b);\nendmodule\n")

    with pytest.raises(MutationVariantError, match="ambiguous"):
        MutationVariantPlan.resolve([Proposal()], tmp_path, ["rtl/dut.sv"])


def test_rejects_scope_escape_without_touching_the_file(tmp_path: Path):
    path = _source(tmp_path)

    with pytest.raises(MutationVariantError, match="escapes scope"):
        MutationVariantPlan.resolve(
            [Proposal(file="rtl/other.sv")],
            tmp_path,
            ["rtl/dut.sv"],
        )

    assert "a + b" in path.read_text(encoding="utf-8")


def test_writes_one_auditable_source_file_per_mutant(tmp_path: Path):
    _source(tmp_path)
    proposals = [
        Proposal(index=1, original_code="a + b", mutated_code="a - b"),
        Proposal(index=2, original_code="y =", mutated_code="y <="),
    ]
    plan = MutationVariantPlan.resolve(proposals, tmp_path, ["rtl/dut.sv"])

    written = plan.write_variants(tmp_path / "campaign/variants")

    assert "a - b" in written[1].read_text(encoding="utf-8")
    assert "y <= a + b" in written[2].read_text(encoding="utf-8")
    assert "a + b" in (tmp_path / "rtl/dut.sv").read_text(encoding="utf-8")


@pytest.mark.parametrize("alias", ["source", "symlink", "hardlink", "escape"])
def test_materialization_rejects_live_aliases_and_escape(tmp_path, alias):
    source = _source(tmp_path)
    plan = MutationVariantPlan.resolve([Proposal()], tmp_path, ["rtl/dut.sv"])
    root = tmp_path / "private"
    root.mkdir()
    destination = root / "dut.sv"
    if alias == "source":
        destination = source
    elif alias == "symlink":
        destination.symlink_to(source)
    elif alias == "hardlink":
        destination.hardlink_to(source)
    else:
        destination = tmp_path / "escaped.sv"
        destination.write_bytes(source.read_bytes())
    with pytest.raises(MutationVariantError):
        plan.materialize(1, [destination], root)
    assert "a + b" in source.read_text()


def test_materialization_preserves_live_edit_after_proposal_snapshot(tmp_path):
    source = _source(tmp_path)
    plan = MutationVariantPlan.resolve([Proposal()], tmp_path, ["rtl/dut.sv"])
    root = tmp_path / "private"
    root.mkdir()
    staged = root / "dut.sv"
    staged.write_bytes(source.read_bytes())
    source.write_bytes(source.read_bytes() + b"// new live edit\n")
    dirty = source.read_bytes()
    plan.materialize(1, [staged], root)
    assert source.read_bytes() == dirty
    assert b"a - b" in staged.read_bytes()


def test_inspection_export_cannot_follow_symlink_to_live_source(tmp_path):
    source = _source(tmp_path)
    plan = MutationVariantPlan.resolve([Proposal()], tmp_path, ["rtl/dut.sv"])
    destination = tmp_path / "variants"
    path = destination / "mutant_1/rtl/dut.sv"
    path.parent.mkdir(parents=True)
    path.symlink_to(source)
    with pytest.raises(MutationVariantError):
        plan.write_variants(destination)
    assert "a + b" in source.read_text()
