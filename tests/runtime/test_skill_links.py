"""Desired-state reconciliation for Booley-managed agent skill links."""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import require_symlinks

from booley.runtime.skill_links import MANIFEST_FILENAME, reconcile_skill_links


def _skill(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    return skill


def _outcomes(report) -> dict[str, str]:
    return {event.name: event.outcome for event in report.events}


def test_creates_links_and_second_run_is_unchanged(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    skill = _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"

    first = reconcile_skill_links(target, packaged)
    second = reconcile_skill_links(target, packaged)

    assert first.fatal is None
    assert _outcomes(first) == {"booley-setup": "created"}
    assert (target / "booley-setup").resolve(strict=True) == skill.resolve()
    assert _outcomes(second) == {"booley-setup": "unchanged"}


def test_adopts_exact_current_link_without_replacing_it(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    skill = _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(skill)
    original = link.readlink()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "adopted"}
    assert link.readlink() == original


def test_preserves_unrecorded_legacy_lookalike_link(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    lookalike = _skill(
        tmp_path / "user-project" / "booley" / "data" / "skills",
        "booley-setup",
    )
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(lookalike)

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "conflict"}
    assert link.resolve(strict=True) == lookalike.resolve()


def test_manifest_owned_link_retargets_to_new_package(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    old_packaged = tmp_path / "old" / "skills"
    old_skill = _skill(old_packaged, "booley-setup")
    new_packaged = tmp_path / "new" / "skills"
    new_skill = _skill(new_packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"

    reconcile_skill_links(target, old_packaged)
    assert (target / "booley-setup").resolve(strict=True) == old_skill.resolve()

    report = reconcile_skill_links(target, new_packaged)

    assert _outcomes(report) == {"booley-setup": "retargeted"}
    assert (target / "booley-setup").resolve(strict=True) == new_skill.resolve()


def test_removes_manifest_owned_skill_absent_from_source(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    skill = _skill(packaged, "booley-gone")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, packaged)
    (skill / "SKILL.md").unlink()
    skill.rmdir()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-gone": "removed"}
    assert not (target / "booley-gone").is_symlink()


def test_packaged_skill_wins_host_name_collision(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    packaged_skill = _skill(packaged, "shared")
    sidecar = tmp_path / "host-sidecar"
    _skill(sidecar, "shared")
    target = tmp_path / "agent" / "skills"

    report = reconcile_skill_links(target, packaged, host_sidecar=sidecar)

    assert _outcomes(report) == {"shared": "created"}
    assert (target / "shared").resolve(strict=True) == packaged_skill.resolve()


def test_missing_sidecar_retires_recorded_host_link(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    sidecar = tmp_path / "host-sidecar"
    host_skill = _skill(sidecar, "personal")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, packaged, host_sidecar=sidecar)
    assert (target / "personal").resolve(strict=True) == host_skill.resolve()
    (host_skill / "SKILL.md").unlink()
    host_skill.rmdir()
    sidecar.rmdir()

    report = reconcile_skill_links(target, packaged, host_sidecar=sidecar)

    assert _outcomes(report) == {"personal": "removed"}


def test_corrupt_manifest_fails_closed(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    manifest = target / MANIFEST_FILENAME
    manifest.write_text("{bad json", encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert report.fatal is not None
    assert report.events == ()
    assert not (target / "booley-setup").exists()
    assert manifest.read_text(encoding="utf-8") == "{bad json"


def test_dry_run_reports_creation_without_writing(tmp_path: Path) -> None:
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"

    report = reconcile_skill_links(target, packaged, dry_run=True)

    assert _outcomes(report) == {"booley-setup": "created"}
    assert not target.exists()


def test_reserved_manifest_name_in_source_fails_closed(tmp_path: Path) -> None:
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, MANIFEST_FILENAME)
    target = tmp_path / "agent" / "skills"

    report = reconcile_skill_links(target, packaged)

    assert report.fatal is not None
    assert "reserved" in report.fatal
    assert not target.exists()


def test_wrong_kind_sidecar_fails_before_packaged_link_creation(tmp_path: Path) -> None:
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    sidecar = tmp_path / "host-sidecar"
    sidecar.write_text("not a directory", encoding="utf-8")
    target = tmp_path / "agent" / "skills"

    report = reconcile_skill_links(target, packaged, host_sidecar=sidecar)

    assert report.fatal is not None
    assert "not a directory" in report.fatal
    assert not target.exists()


def test_source_entry_inspection_error_fails_closed(tmp_path: Path, monkeypatch) -> None:
    packaged = tmp_path / "package" / "skills"
    skill = _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    original_lstat = Path.lstat

    def fail_for_skill(path: Path):
        if path == skill:
            raise PermissionError("source entry is unreadable")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_for_skill)

    report = reconcile_skill_links(target, packaged)

    assert report.fatal is not None
    assert str(packaged) in report.fatal
    assert "source entry is unreadable" in report.fatal
    assert report.events == ()
    assert not target.exists()


def test_missing_packaged_source_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "agent" / "skills"

    report = reconcile_skill_links(target, tmp_path / "missing-package")

    assert report.fatal is not None
    assert report.events == ()
    assert not target.exists()


def test_missing_managed_entry_retires_its_manifest_record(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    skill = _skill(packaged, "booley-gone")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, packaged)
    (target / "booley-gone").unlink()
    (skill / "SKILL.md").unlink()
    skill.rmdir()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-gone": "removed"}
    manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["links"] == {}


def test_foreign_takeover_is_preserved_and_relinquishes_ownership(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    foreign = _skill(tmp_path / "team-skills", "booley-setup")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, packaged)
    link = target / "booley-setup"
    link.unlink()
    link.symlink_to(foreign)

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "conflict"}
    assert link.resolve(strict=True) == foreign.resolve()
    manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["links"] == {}


def test_out_of_scope_host_record_blocks_same_name_package(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    sidecar = tmp_path / "host-sidecar"
    host_skill = _skill(sidecar, "shared")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, packaged, host_sidecar=sidecar)
    _skill(packaged, "shared")

    report = reconcile_skill_links(target, packaged, host_sidecar=None)

    assert _outcomes(report) == {"shared": "conflict"}
    assert (target / "shared").resolve(strict=True) == host_skill.resolve()


def test_manifest_is_versioned_json(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"

    reconcile_skill_links(target, packaged)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["links"]["booley-setup"]["source_kind"] == "packaged"
