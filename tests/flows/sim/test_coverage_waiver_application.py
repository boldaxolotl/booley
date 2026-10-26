"""Captured-byte prediction preserves strict evaluation and symlink isolation."""

from pathlib import Path

import pytest

from booley.flows.sim.coverage_policy import evaluate_coverage_campaign
from booley.flows.sim.coverage_waiver_application import (
    WaiverPlanError,
    load_promoted_waiver_set,
    strict_reevaluation,
)
from booley.flows.sim.coverage_waivers import CoverageWaiverConfig, load_approved_waiver_set
from tests.conftest import symlink_or_skip
from tests.flows.sim.test_coverage_waivers import (
    _TARGET,
    _campaign,
    _criterion,
    _linked_booley_dir_roots,
    _roots,
    _write_valid_approval,
)

_DIRECTORY = "coverage-waivers"


@pytest.mark.parametrize("already_waived", [False, True])
def test_strict_reevaluation(tmp_path, already_waived):
    roots = _roots(tmp_path)
    config = CoverageWaiverConfig("project_data_repository", _DIRECTORY)
    (roots.project_data_repository / _DIRECTORY).mkdir()
    empty = load_approved_waiver_set(config, roots, (_TARGET,))
    _write_valid_approval(roots)
    approved = load_approved_waiver_set(config, roots, (_TARGET,))
    persisted = evaluate_coverage_campaign(
        _campaign(), _criterion(), approved if already_waived else empty
    )
    assert persisted.evaluation["status"] == ("pass" if already_waived else "fail")
    evaluated = strict_reevaluation(persisted, approved)
    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.evaluation["approved_waiver_set_digest"] == approved.digest
    assert evaluated.evaluation["diagnostics"] == ()


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
