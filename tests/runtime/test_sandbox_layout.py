"""The image alias is the only prefix exempted from report nofollow guards."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import require_symlinks, symlink_or_skip

from booley.runtime import sandbox_layout


def configure_project_alias(
    monkeypatch: pytest.MonkeyPatch,
    alias: Path,
    target: Path,
    *,
    owner: int = 0,
    parent_mode: int = 0o755,
) -> None:
    """Use real links; substitute only image ownership/protection metadata."""
    if os.name != "posix":
        pytest.skip("authenticated image alias is POSIX-only")
    monkeypatch.setattr(sandbox_layout, "PROJECT_DIR_TARGET", str(alias))
    monkeypatch.setattr(sandbox_layout, "PROJECT_DATA_MOUNT_PATH", str(target))
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path not in (alias, alias.parent):
            return info
        fields = list(info)
        fields[4] = owner
        if path == alias.parent:
            fields[0] = stat.S_IFDIR | parent_mode
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", lstat)


@pytest.fixture
def project_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    require_symlinks(tmp_path)
    target = tmp_path / "real-project-data"
    target.mkdir()
    alias = tmp_path / "booley-project"
    symlink_or_skip(alias, target, target_is_directory=True)
    configure_project_alias(monkeypatch, alias, target)
    return alias, target


def test_trusted_alias_preserves_suffix_and_missing_descendants(project_alias):
    alias, target = project_alias
    assert (
        sandbox_layout.canonical_project_alias_path(alias / "missing/reports")
        == target / "missing/reports"
    )
    (target / "link").symlink_to(target, target_is_directory=True)
    result = sandbox_layout.canonical_project_alias_path(alias / "link/reports")
    assert result == target / "link/reports"
    assert result.parent.is_symlink()


@pytest.mark.parametrize("defect", ["target", "owner", "writable_parent", "directory", "missing"])
def test_untrusted_alias_is_unchanged(project_alias, monkeypatch, defect):
    alias, target = project_alias
    if defect in {"target", "directory", "missing"}:
        alias.unlink()
        if defect == "target":
            alias.symlink_to(target / "other", target_is_directory=True)
        elif defect == "directory":
            alias.mkdir()
    elif defect == "owner":
        configure_project_alias(monkeypatch, alias, target, owner=1000)
    else:
        configure_project_alias(monkeypatch, alias, target, parent_mode=0o777)
    path = alias / "reports"
    assert sandbox_layout.canonical_project_alias_path(path) == path


def test_alias_traversal_is_refused(project_alias):
    alias, _target = project_alias
    with pytest.raises(ValueError, match="traversal"):
        sandbox_layout.canonical_project_alias_path(alias / "../reports")


def test_other_prefixes_and_relative_paths_are_unchanged(project_alias):
    alias, _target = project_alias
    for path in (
        Path("booley-project/reports"),
        alias.with_name(alias.name + "-other") / "reports",
    ):
        assert sandbox_layout.canonical_project_alias_path(path) == path


def test_non_posix_paths_receive_no_exemption(monkeypatch):
    path = Path("/booley-project/reports")
    monkeypatch.setattr(sandbox_layout, "os", SimpleNamespace(name="nt"))
    assert sandbox_layout.canonical_project_alias_path(path) == path


def test_directory_alias_preserves_traversal(project_alias):
    alias, _target = project_alias
    alias.unlink()
    alias.mkdir()
    path = alias / "child/../reports"
    assert sandbox_layout.canonical_project_alias_path(path) == path


def test_alias_traversal_uses_reader_domain_errors(project_alias) -> None:
    from booley.flows.sim.campaign import SimulationCampaignIntegrityError
    from booley.flows.sim.campaign.resume import validate_resume_manifest
    from booley.flows.sim.campaign_retention import CampaignRetentionError, _invocation
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_campaign,
    )
    from booley.flows.sim.coverage_campaign_store import (
        CoverageCampaignStoreError,
        load_coverage_campaign,
        load_coverage_campaign_bytes,
        read_coverage_summary,
    )
    from booley.flows.sim.coverage_reference import (
        CoverageCampaignReferenceError,
        resolve_coverage_campaign_reference,
        resolve_persisted_coverage_campaign_reference,
    )

    alias, target = project_alias
    path = alias / "child/../coverage.json"
    for reader, error in (
        (load_coverage_campaign, CoverageCampaignStoreError),
        (
            lambda selected: load_coverage_campaign_bytes(selected, b"{}"),
            CoverageCampaignStoreError,
        ),
        (lambda selected: read_coverage_summary(selected, "target"), CoverageCampaignStoreError),
        (
            lambda selected: resolve_persisted_coverage_campaign_reference(selected, {}),
            CoverageCampaignReferenceError,
        ),
        (read_coverage_campaign, CoverageAnalysisError),
        (resolve_coverage_campaign_reference, CoverageCampaignReferenceError),
        (
            lambda selected: validate_resume_manifest(selected, project_root=target),
            SimulationCampaignIntegrityError,
        ),
        (lambda selected: _invocation(selected, 1), CampaignRetentionError),
    ):
        with pytest.raises(error, match="traversal") as caught:
            reader(path)
        if error is CoverageCampaignStoreError:
            assert caught.value.code == "COV_PATH_UNSAFE"
