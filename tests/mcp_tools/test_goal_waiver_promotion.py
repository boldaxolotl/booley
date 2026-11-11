"""Promotion loading and evaluation JSON, tested outside the Goal Mode test lane."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from booley.config.coverage_waiver_inputs import CoverageWaiverConfig
from booley.flows.sim.coverage_waivers import CoverageRepositoryRoots
from booley.mcp.goal_waivers import WaiverPlanError, load_promoted_waiver_set
from tests.conftest import symlink_or_skip
from tests.flows.sim.test_coverage_waivers import (
    _TARGET,
    _linked_booley_dir_roots,
    _roots,
    _write_valid_approval,
)

_DIRECTORY = "coverage-waivers"


def _promote(
    roots: CoverageRepositoryRoots, anchor: str, overlay: Path, after_bytes: Mapping[Path, bytes]
):
    return load_promoted_waiver_set(
        CoverageWaiverConfig(anchor, _DIRECTORY), roots, overlay, (_TARGET,), after_bytes
    )


def test_promotion_loads_captured_bytes_into_the_overlay_without_publishing(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    document = _write_valid_approval(roots)
    original = document.read_bytes()
    relative = Path(_DIRECTORY) / "rtl/counter.sv.toml"
    captured = original.replace(b"counter-basic-block", b"captured-block")

    waivers = _promote(
        roots, "project_data_repository", tmp_path / "overlay", {relative: captured}
    )

    assert [waiver.waiver_id for waiver in waivers.waivers] == ["captured-block"]
    assert document.read_bytes() == original
    assert (tmp_path / "overlay" / relative).read_bytes() == captured


def test_rtl_anchored_promotion_mirrors_only_named_sources(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    shared = CoverageRepositoryRoots(roots.rtl_repository, roots.rtl_repository)
    _write_valid_approval(shared)
    (roots.rtl_repository / "rtl" / "unrelated.sv").write_bytes(b"module other; endmodule\n")

    waivers = _promote(shared, "rtl_repository", tmp_path / "overlay", {})

    assert [waiver.waiver_id for waiver in waivers.waivers] == ["counter-basic-block"]
    assert (tmp_path / "overlay" / "rtl" / "counter.sv").is_file()
    assert not (tmp_path / "overlay" / "rtl" / "unrelated.sv").exists()


@pytest.mark.parametrize("link_kind", ["directory", "document", "proof"])
def test_prediction_rejects_symlinks_without_changing_their_targets(tmp_path, link_kind):
    roots = _roots(tmp_path)
    document = _write_valid_approval(roots)
    directory = roots.project_data_repository / _DIRECTORY
    (directory / "proofs").mkdir()
    (directory / "proofs" / "existing.md").write_bytes(b"Existing proof.\n")
    linked = {"directory": directory, "document": document, "proof": directory / "proofs"}[
        link_kind
    ]
    external = tmp_path / "external"
    linked.rename(external)
    try:
        linked.symlink_to(external, target_is_directory=external.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    files = list(external.rglob("*")) if external.is_dir() else [external]
    before = {path: path.read_bytes() for path in files if path.is_file()}

    with pytest.raises(WaiverPlanError, match="symlink"):
        load_promoted_waiver_set(
            CoverageWaiverConfig("project_data_repository", _DIRECTORY),
            roots,
            tmp_path / "overlay",
            (_TARGET,),
            {
                Path(_DIRECTORY) / "rtl/counter.sv.toml": b"document sentinel",
                Path(_DIRECTORY) / "proofs/existing.md": b"proof sentinel",
            },
        )

    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_promoted_waivers_follow_only_the_booley_dir_entry(tmp_path, monkeypatch, linked):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    roots = _linked_booley_dir_roots(tmp_path)
    if linked == "below":
        (tmp_path / "elsewhere").mkdir()
        symlink_or_skip(
            roots.project_data_repository / _DIRECTORY,
            tmp_path / "elsewhere",
            target_is_directory=True,
        )
    _write_valid_approval(roots)

    def promote():
        return load_promoted_waiver_set(
            CoverageWaiverConfig("project_data_repository", _DIRECTORY),
            roots,
            tmp_path / "overlay",
            (_TARGET,),
            {},
        )

    if linked == "below":
        with pytest.raises(WaiverPlanError, match="symlink"):
            promote()
    else:
        assert [waiver.waiver_id for waiver in promote().waivers] == ["counter-basic-block"]
