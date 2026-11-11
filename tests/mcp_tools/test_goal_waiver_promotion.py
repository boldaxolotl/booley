"""Promotion loading and evaluation JSON, tested outside the Goal Mode test lane."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest

from booley.config.coverage_waiver_inputs import CoverageWaiverConfig
from booley.flows.sim.coverage_policy import EvaluatedCoverageCampaign, evaluation_json
from booley.flows.sim.coverage_waivers import CoverageRepositoryRoots
from booley.mcp.goal_waivers import WaiverPlanError, _named_sources, load_promoted_waiver_set
from tests.conftest import symlink_or_skip
from tests.flows.sim.test_coverage_waivers import _TARGET, _roots, _write_valid_approval

_DIRECTORY = "coverage-waivers"


def _promote(roots: CoverageRepositoryRoots, anchor: str, overlay: Path, after_bytes):
    return load_promoted_waiver_set(
        CoverageWaiverConfig(anchor, _DIRECTORY), roots, overlay, (_TARGET,), after_bytes
    )


def test_project_data_promotion_loads_captured_bytes_without_publishing(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    document = _write_valid_approval(roots)
    original = document.read_bytes()
    relative = Path(_DIRECTORY) / "rtl/counter.sv.toml"

    # The captured image replaces the on-disk document in the overlay only.
    waivers = _promote(
        roots, "project_data_repository", tmp_path / "overlay", {relative: original}
    )

    assert [waiver.waiver_id for waiver in waivers.waivers] == ["counter-basic-block"]
    assert document.read_bytes() == original
    assert (tmp_path / "overlay" / relative).read_bytes() == original


def test_rtl_anchored_promotion_mirrors_only_named_sources(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    shared = CoverageRepositoryRoots(roots.rtl_repository, roots.rtl_repository)
    _write_valid_approval(shared)
    (roots.rtl_repository / "rtl" / "unrelated.sv").write_bytes(b"module other; endmodule\n")

    waivers = _promote(shared, "rtl_repository", tmp_path / "overlay", {})

    assert [waiver.waiver_id for waiver in waivers.waivers] == ["counter-basic-block"]
    assert (tmp_path / "overlay" / "rtl" / "counter.sv").is_file()
    assert not (tmp_path / "overlay" / "rtl" / "unrelated.sv").exists()


def test_named_sources_skips_documents_the_real_loader_will_report(tmp_path: Path) -> None:
    (tmp_path / "named.toml").write_text('source = "rtl/a.sv"\n', encoding="utf-8")
    (tmp_path / "broken.toml").write_text("not = [valid", encoding="utf-8")
    (tmp_path / "numeric.toml").write_text("source = 3\n", encoding="utf-8")
    (tmp_path / "binary.toml").write_bytes(b"\xff\xfe")

    assert _named_sources(tmp_path) == {"rtl/a.sv"}


def test_promotion_rejects_a_symlink_inside_the_approval_directory(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    document = _write_valid_approval(roots)
    link = document.parent / "linked.toml"
    symlink_or_skip(link, document)

    with pytest.raises(WaiverPlanError, match="symlink"):
        _promote(roots, "project_data_repository", tmp_path / "overlay", {})


def test_evaluation_json_is_the_plain_document_of_the_evaluation() -> None:
    campaign = SimpleNamespace(evaluation=MappingProxyType({"status": "pass", "metrics": ()}))

    assert evaluation_json(cast(EvaluatedCoverageCampaign, campaign)) == {
        "status": "pass",
        "metrics": [],
    }


def test_promotion_rejects_a_symlinked_approval_directory(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _write_valid_approval(roots)
    approvals = roots.project_data_repository / _DIRECTORY
    external = tmp_path / "external"
    approvals.rename(external)
    symlink_or_skip(approvals, external, target_is_directory=True)

    with pytest.raises(WaiverPlanError, match="symlink"):
        _promote(roots, "project_data_repository", tmp_path / "overlay", {})
