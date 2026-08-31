"""Desired-state reconciliation for Booley-managed agent skill links."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import require_symlinks

from booley.runtime import skill_links
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


def test_accepts_equivalent_cross_checkout_link_without_mutation(tmp_path: Path) -> None:
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

    original_target = link.readlink()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "equivalent"}
    assert report.failed is False
    assert link.readlink() == original_target
    assert not (target / MANIFEST_FILENAME).exists()


def test_explicit_authority_relinks_equivalent_cross_checkout_skill(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    desired = _skill(packaged, "booley-setup")
    lookalike = _skill(tmp_path / "other-checkout" / "skills", "booley-setup")
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(lookalike)

    report = reconcile_skill_links(target, packaged, allow_retarget=True)

    assert _outcomes(report) == {"booley-setup": "retargeted"}
    assert link.resolve(strict=True) == desired.resolve()


def test_explicit_authority_does_not_replace_non_equivalent_foreign_link(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    foreign = _skill(tmp_path / "team-skills", "booley-setup")
    (foreign / "SKILL.md").write_text("# Team skill\n", encoding="utf-8")
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(foreign)

    report = reconcile_skill_links(target, packaged, allow_retarget=True)

    assert _outcomes(report) == {"booley-setup": "conflict"}
    assert link.resolve(strict=True) == foreign.resolve()


def test_manifest_owned_link_retargets_to_new_package(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    old_packaged = tmp_path / "old" / "skills"
    old_skill = _skill(old_packaged, "booley-setup")
    new_packaged = tmp_path / "new" / "skills"
    new_skill = _skill(new_packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"

    reconcile_skill_links(target, old_packaged)
    assert (target / "booley-setup").resolve(strict=True) == old_skill.resolve()

    refused = reconcile_skill_links(target, new_packaged)

    assert _outcomes(refused) == {"booley-setup": "conflict"}
    assert (target / "booley-setup").resolve(strict=True) == old_skill.resolve()

    report = reconcile_skill_links(target, new_packaged, allow_retarget=True)

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


def test_manifest_is_versioned_json(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"

    reconcile_skill_links(target, packaged)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["links"]["booley-setup"]["source_kind"] == "packaged"


def test_skill_identity_rejects_non_regular_content_and_inspection_errors(
    tmp_path: Path, monkeypatch
) -> None:
    require_symlinks(tmp_path)
    skill = _skill(tmp_path / "skills", "booley-setup")
    (skill / "nested").mkdir()
    (skill / "z-linked").symlink_to(tmp_path / "outside")

    assert skill_links._skill_tree_identity(skill) is None

    original_rglob = Path.rglob

    def fail_rglob(path: Path, pattern: str):
        if path == skill:
            raise PermissionError("identity denied")
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    assert skill_links._skill_tree_identity(skill) is None


def test_source_and_target_wrong_kinds_fail_closed(tmp_path: Path) -> None:
    packaged_file = tmp_path / "package-skills"
    packaged_file.write_text("not a directory", encoding="utf-8")
    target = tmp_path / "agent" / "skills"

    source_report = reconcile_skill_links(target, packaged_file)

    assert "not a directory" in (source_report.fatal or "")

    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_text("not a directory", encoding="utf-8")

    target_report = reconcile_skill_links(target, packaged)

    assert "not a directory" in (target_report.fatal or "")


def test_non_regular_manifest_fails_closed(tmp_path: Path) -> None:
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    target = tmp_path / "agent" / "skills"
    (target / MANIFEST_FILENAME).mkdir(parents=True)

    report = reconcile_skill_links(target, packaged)

    assert "not a regular file" in (report.fatal or "")


@pytest.mark.parametrize(
    ("entry", "version", "message"),
    [
        ({}, 2, "unsupported"),
        ({"booley-setup": {"source_kind": "host", "target": "/tmp/skill"}}, 1, "source_kind"),
        (
            {MANIFEST_FILENAME: {"source_kind": "packaged", "target": "/tmp/skill"}},
            1,
            "reserved",
        ),
        (
            {"booley-setup": {"source_kind": "packaged", "target": "/tmp/../tmp/skill"}},
            1,
            "normalized",
        ),
    ],
)
def test_manifest_rejects_unsupported_semantics(
    tmp_path: Path, entry: dict, version: int, message: str
) -> None:
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    if message == "normalized":
        entry["booley-setup"]["target"] = str(tmp_path / "nested" / ".." / "skill")
    payload = {"version": version, "links": entry}
    (target / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert message in (report.fatal or "")


def test_missing_managed_link_is_recreated_when_skill_still_exists(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    desired = _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, packaged)
    (target / "booley-setup").unlink()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "created"}
    assert (target / "booley-setup").resolve(strict=True) == desired.resolve()


def test_unrelated_target_entry_is_ignored(tmp_path: Path) -> None:
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("mine", encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert report.events == ()
    assert (target / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_apply_recheck_error_is_reported_without_creating_link(
    tmp_path: Path, monkeypatch
) -> None:
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    original_read_entry = skill_links._read_entry
    calls = 0

    def fail_second_read(path: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("recheck denied")
        return original_read_entry(path)

    monkeypatch.setattr(skill_links, "_read_entry", fail_second_read)

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "error"}
    assert "recheck denied" in report.events[0].detail
    assert not (target / "booley-setup").exists()


def test_apply_detects_entry_changed_after_planning(tmp_path: Path, monkeypatch) -> None:
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    original_read_entry = skill_links._read_entry
    calls = 0

    def change_second_read(path: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return skill_links._Entry("foreign")
        return original_read_entry(path)

    monkeypatch.setattr(skill_links, "_read_entry", change_second_read)

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "conflict"}
    assert report.events[0].detail == "entry changed during reconciliation"


def test_windows_junction_inspection_error_is_not_a_junction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skill_links, "IS_WINDOWS", True)
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)

    def fail_lstat(_path: Path):
        raise PermissionError("inspection denied")

    monkeypatch.setattr(Path, "lstat", fail_lstat)

    assert skill_links._is_junction(tmp_path / "junction") is False


def test_windows_mklink_failure_is_loud(tmp_path: Path, monkeypatch) -> None:
    command_result = subprocess.CompletedProcess([], 1, "", "junction denied")
    monkeypatch.setattr(skill_links, "IS_WINDOWS", True)
    monkeypatch.setattr(skill_links.subprocess, "run", lambda *_args, **_kwargs: command_result)

    with pytest.raises(OSError, match="junction denied"):
        skill_links._make_link(tmp_path / "link", tmp_path / "target")


def test_windows_replacement_reports_backup_cleanup_failure(tmp_path: Path, monkeypatch) -> None:
    link = tmp_path / "link"
    desired = tmp_path / "desired"
    temporary = tmp_path / "temporary"
    backup = tmp_path / "backup"
    link.mkdir()
    desired.mkdir()
    paths = iter((temporary, backup))
    diagnostics: list[str] = []

    monkeypatch.setattr(skill_links, "_unique_path", lambda *_args: next(paths))
    monkeypatch.setattr(skill_links, "_make_link", lambda path, _target: path.mkdir())

    def fail_backup_cleanup(path: Path):
        if path == backup:
            raise PermissionError("backup busy")
        path.rmdir()

    monkeypatch.setattr(skill_links, "_remove_link", fail_backup_cleanup)

    skill_links._replace_link_windows(link, desired, diagnostics)

    assert link.is_dir()
    assert backup.is_dir()
    assert "backup remains" in diagnostics[0]


def test_windows_replacement_rolls_back_failed_install(tmp_path: Path, monkeypatch) -> None:
    link = tmp_path / "link"
    desired = tmp_path / "desired"
    temporary = tmp_path / "temporary"
    backup = tmp_path / "backup"
    link.mkdir()
    desired.mkdir()
    paths = iter((temporary, backup))
    original_rename = Path.rename

    monkeypatch.setattr(skill_links, "_unique_path", lambda *_args: next(paths))
    monkeypatch.setattr(skill_links, "_make_link", lambda path, _target: path.mkdir())
    monkeypatch.setattr(skill_links, "_remove_link", lambda path: path.rmdir())

    def fail_install(path: Path, destination: Path):
        if path == temporary and destination == link:
            raise PermissionError("install denied")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_install)

    with pytest.raises(OSError, match="could not install replacement"):
        skill_links._replace_link_windows(link, desired, [])

    assert link.is_dir()
    assert not temporary.exists()
    assert not backup.exists()


@pytest.mark.parametrize("name", [".", "..", "nested/name", "nested\\name"])
def test_manifest_rejects_names_that_are_not_single_path_components(
    tmp_path: Path, name: str
) -> None:
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    payload = {
        "version": 1,
        "links": {name: {"source_kind": "packaged", "target": str(tmp_path)}},
    }
    (target / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert report.fatal is not None
    assert "manifest name" in report.fatal


def test_manifest_rejects_relative_target(tmp_path: Path) -> None:
    packaged = tmp_path / "package" / "skills"
    packaged.mkdir(parents=True)
    target = tmp_path / "agent" / "skills"
    target.mkdir(parents=True)
    payload = {
        "version": 1,
        "links": {"booley-setup": {"source_kind": "packaged", "target": "relative/skill"}},
    }
    (target / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert report.fatal is not None
    assert "must be absolute" in report.fatal


def test_manifest_temporary_cleanup_failure_is_diagnostic(tmp_path: Path, monkeypatch) -> None:
    require_symlinks(tmp_path)
    packaged = tmp_path / "package" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    temporary = target / ".manifest-temporary"
    original_replace = Path.replace
    original_unlink = Path.unlink

    monkeypatch.setattr(skill_links, "_unique_path", lambda *_args: temporary)

    def fail_replace(path: Path, destination: Path):
        if path == temporary:
            raise PermissionError("replace denied")
        return original_replace(path, destination)

    def fail_cleanup(path: Path, *args, **kwargs):
        if path == temporary:
            raise PermissionError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    report = reconcile_skill_links(target, packaged)

    assert len(report.diagnostics) == 2
    assert "could not write" in report.diagnostics[0]
    assert "temporary manifest remains" in report.diagnostics[1]


def test_windows_junction_detection_uses_mount_point_reparse_tag(
    tmp_path: Path, monkeypatch
) -> None:
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    monkeypatch.setattr(skill_links, "IS_WINDOWS", True)
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(Path, "lstat", lambda _path: SimpleNamespace(st_reparse_tag=mount_point))

    assert skill_links._is_junction(tmp_path / "junction") is True


def test_windows_junction_creation_uses_mklink(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(skill_links, "IS_WINDOWS", True)
    monkeypatch.setattr(skill_links.subprocess, "run", fake_run)
    link = tmp_path / "link"
    desired = tmp_path / "desired"

    skill_links._make_link(link, desired)

    assert calls == [["cmd", "/c", "mklink", "/J", str(link), str(desired.absolute())]]


@pytest.mark.skipif(os.name != "nt", reason="requires NTFS junctions")
def test_windows_junction_retarget_requires_explicit_authority(tmp_path: Path) -> None:
    old_packaged = tmp_path / "old" / "skills"
    old_skill = _skill(old_packaged, "booley-setup")
    new_packaged = tmp_path / "new" / "skills"
    new_skill = _skill(new_packaged, "booley-setup")
    target = tmp_path / "agent" / "skills"
    reconcile_skill_links(target, old_packaged)

    refused = reconcile_skill_links(target, new_packaged)
    approved = reconcile_skill_links(target, new_packaged, allow_retarget=True)

    assert _outcomes(refused) == {"booley-setup": "conflict"}
    assert _outcomes(approved) == {"booley-setup": "retargeted"}
    assert (target / "booley-setup").resolve(strict=True) == new_skill.resolve()
    assert old_skill.is_dir()
