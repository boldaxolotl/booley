"""Desired-state reconciliation for Booley-managed agent skill links."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests import mklink_fake
from tests.conftest import require_symlinks

from booley.runtime import skill_links
from booley.runtime.skill_links import MANIFEST_FILENAME, reconcile_skill_links


def _skill(root: Path, name: str = "booley-setup") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    return skill


def _outcomes(report) -> dict[str, str]:
    return {event.name: event.outcome for event in report.events}


def _manifest_links(target: Path) -> dict:
    return json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))["links"]


@pytest.fixture
def packaged(tmp_path: Path) -> Path:
    return tmp_path / "package" / "skills"


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "agent" / "skills"


@pytest.fixture
def windows_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # IS_WINDOWS is the host-OS boundary skill_links reads; mklink_fake answers its command.
    monkeypatch.setattr(skill_links, "IS_WINDOWS", True)
    mklink_fake.install(tmp_path / "bin", monkeypatch)


def test_creates_links_and_second_run_is_unchanged(packaged: Path, target: Path) -> None:
    require_symlinks(target.parent.parent)
    skill = _skill(packaged)

    first = reconcile_skill_links(target, packaged)
    second = reconcile_skill_links(target, packaged)

    assert first.fatal is None
    assert _outcomes(first) == {"booley-setup": "created"}
    assert (target / "booley-setup").resolve(strict=True) == skill.resolve()
    assert _outcomes(second) == {"booley-setup": "unchanged"}


def test_adopts_exact_current_link_without_replacing_it(packaged: Path, target: Path) -> None:
    require_symlinks(target.parent.parent)
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(_skill(packaged))
    original = link.readlink()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "adopted"}
    assert link.readlink() == original


@pytest.mark.parametrize(
    ("lookalike", "allow_retarget", "outcome"),
    [
        ("same", False, "equivalent"),
        ("same", True, "retargeted"),
        ("edited", True, "conflict"),
        ("holds-a-symlink", False, "conflict"),
        ("unreadable", False, "conflict"),
    ],
)
def test_cross_checkout_link_is_relinked_only_when_equivalent_and_authorized(
    tmp_path: Path,
    packaged: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    lookalike: str,
    allow_retarget: bool,
    outcome: str,
) -> None:
    require_symlinks(tmp_path)
    desired = _skill(packaged)
    other = _skill(tmp_path / "other-checkout" / "skills")
    for skill in (desired, other):
        (skill / "nested").mkdir()
    if lookalike == "edited":
        (other / "SKILL.md").write_text("# Team skill\n", encoding="utf-8")
    if lookalike == "holds-a-symlink":  # same content, but through a link
        (tmp_path / "outside.md").write_text("# Test skill\n", encoding="utf-8")
        (other / "SKILL.md").unlink()
        (other / "SKILL.md").symlink_to(tmp_path / "outside.md")
    if lookalike == "unreadable":
        original_rglob = Path.rglob

        def fail_rglob(path: Path, pattern: str):
            if path == other:
                raise PermissionError("identity denied")
            return original_rglob(path, pattern)

        monkeypatch.setattr(Path, "rglob", fail_rglob)
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(other)

    report = reconcile_skill_links(target, packaged, allow_retarget=allow_retarget)

    assert _outcomes(report) == {"booley-setup": outcome}
    assert report.failed is (outcome == "conflict")
    relinked = outcome == "retargeted"
    assert link.resolve(strict=True) == (desired if relinked else other).resolve()
    assert (target / MANIFEST_FILENAME).exists() is relinked


@pytest.mark.parametrize("host", ["posix", "windows"])
def test_manifest_owned_link_retargets_only_with_authority(
    request: pytest.FixtureRequest, tmp_path: Path, target: Path, host: str
) -> None:
    if host == "windows":
        request.getfixturevalue("windows_host")
    else:
        require_symlinks(tmp_path)
    old_skill = _skill(tmp_path / "old" / "skills")
    new_skill = _skill(tmp_path / "new" / "skills")
    reconcile_skill_links(target, old_skill.parent)

    refused = reconcile_skill_links(target, new_skill.parent)

    assert _outcomes(refused) == {"booley-setup": "conflict"}
    assert refused.events[0].detail == (
        f"`{target / 'booley-setup'}` is Booley's link to another skill source; "
        "rerun `booley bootstrap --force` to relink it"
    )
    assert (target / "booley-setup").resolve(strict=True) == old_skill.resolve()

    report = reconcile_skill_links(target, new_skill.parent, allow_retarget=True)

    assert _outcomes(report) == {"booley-setup": "retargeted"}
    assert report.failed is False
    assert (target / "booley-setup").resolve(strict=True) == new_skill.resolve()
    assert old_skill.is_dir()
    assert {path.name for path in target.iterdir()} == {"booley-setup", MANIFEST_FILENAME}


@pytest.mark.parametrize("link_present", [True, False])
def test_skill_gone_from_source_retires_its_link_and_record(
    packaged: Path, target: Path, link_present: bool
) -> None:
    require_symlinks(target.parent.parent)
    skill = _skill(packaged, "booley-gone")
    reconcile_skill_links(target, packaged)
    if not link_present:
        (target / "booley-gone").unlink()
    (skill / "SKILL.md").unlink()
    skill.rmdir()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-gone": "removed"}
    assert not (target / "booley-gone").is_symlink()
    assert _manifest_links(target) == {}


def test_dry_run_reports_creation_without_writing(packaged: Path, target: Path) -> None:
    _skill(packaged)

    report = reconcile_skill_links(target, packaged, dry_run=True)

    assert _outcomes(report) == {"booley-setup": "created"}
    assert not target.exists()


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        ("missing source", "missing"),
        ("source is a file", "not a directory"),
        ("target is a file", "not a directory"),
        ("reserved skill name", "reserved"),
    ],
)
def test_unusable_source_or_target_fails_closed(
    tmp_path: Path, packaged: Path, target: Path, layout: str, message: str
) -> None:
    if layout == "source is a file":
        packaged = tmp_path / "package-skills"
        packaged.write_text("not a directory", encoding="utf-8")
    elif layout == "target is a file":
        _skill(packaged)
        target.parent.mkdir(parents=True)
        target.write_text("not a directory", encoding="utf-8")
    elif layout == "reserved skill name":
        _skill(packaged, MANIFEST_FILENAME)

    report = reconcile_skill_links(target, packaged)

    assert message in (report.fatal or "")
    assert report.events == ()
    assert target.is_file() if layout == "target is a file" else not target.exists()


def test_source_entry_inspection_error_fails_closed(
    packaged: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _skill(packaged)
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


def _manifest(name: str = "booley-setup", *, version: int = 1, **record: str) -> str:
    links = {name: {"source_kind": "packaged", "target": "{root}", **record}}
    return json.dumps({"version": version, "links": links})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, "not a regular file"),
        ("{bad json", "cannot read"),
        (_manifest(version=2), "unsupported"),
        (_manifest(source_kind="host"), "source_kind"),
        (_manifest(MANIFEST_FILENAME), "reserved"),
        (_manifest(target="relative/skill"), "must be absolute"),
        (_manifest(target="{root}/nested/../skill"), "normalized"),
        *(
            (_manifest(name), "manifest name")
            for name in (".", "..", "nested/name", "nested\\name")
        ),
    ],
)
def test_invalid_manifest_fails_closed(
    tmp_path: Path, packaged: Path, target: Path, text: str | None, message: str
) -> None:
    _skill(packaged)
    manifest = target / MANIFEST_FILENAME
    if text is None:
        manifest.mkdir(parents=True)
    else:
        target.mkdir(parents=True)
        text = text.replace("{root}", json.dumps(str(tmp_path))[1:-1])
        manifest.write_text(text, encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert message in (report.fatal or "")
    assert report.events == ()
    assert not (target / "booley-setup").exists()
    assert manifest.is_dir() if text is None else manifest.read_text(encoding="utf-8") == text


def test_foreign_takeover_is_preserved_and_relinquishes_ownership(
    tmp_path: Path, packaged: Path, target: Path
) -> None:
    require_symlinks(tmp_path)
    _skill(packaged)
    foreign = _skill(tmp_path / "team-skills")
    reconcile_skill_links(target, packaged)
    link = target / "booley-setup"
    link.unlink()
    link.symlink_to(foreign)

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "conflict"}
    assert report.events[0].detail == (
        f"`{link}` is not Booley's: rename it if it is your own skill, "
        "delete it if it is a dangling link, then rerun `booley bootstrap`"
    )
    assert link.resolve(strict=True) == foreign.resolve()
    assert _manifest_links(target) == {}


def test_missing_managed_link_is_recreated_when_skill_still_exists(
    packaged: Path, target: Path
) -> None:
    require_symlinks(target.parent.parent)
    desired = _skill(packaged)
    reconcile_skill_links(target, packaged)
    (target / "booley-setup").unlink()

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "created"}
    assert (target / "booley-setup").resolve(strict=True) == desired.resolve()


def test_unrelated_target_entry_is_ignored(packaged: Path, target: Path) -> None:
    packaged.mkdir(parents=True)
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("mine", encoding="utf-8")

    report = reconcile_skill_links(target, packaged)

    assert report.events == ()
    assert (target / "notes.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize(
    ("race", "outcome", "detail"),
    [
        ("entry unreadable", "error", "cannot recheck entry: recheck denied"),
        ("entry appeared", "conflict", "changed while Booley was linking it"),
    ],
)
def test_entry_changed_after_planning_is_not_overwritten(
    packaged: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
    outcome: str,
    detail: str,
) -> None:
    _skill(packaged)
    entry = target / "booley-setup"
    original_lstat = Path.lstat

    def concurrent_writer(path: Path):
        # Planning saw no target directory; another process races the apply step.
        if path == entry and race == "entry unreadable":
            raise PermissionError("recheck denied")
        if path == entry and not os.path.lexists(entry):
            entry.write_text("someone else's", encoding="utf-8")
        return original_lstat(path)

    with monkeypatch.context() as patch:  # before 3.12, Path.is_symlink calls Path.lstat
        patch.setattr(Path, "lstat", concurrent_writer)
        report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": outcome}
    assert detail in report.events[0].detail
    assert not entry.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="Windows runs the real mklink")
def test_windows_mklink_failure_is_reported_per_skill(
    packaged: Path, target: Path, monkeypatch: pytest.MonkeyPatch, windows_host: None
) -> None:
    _skill(packaged)
    mklink_fake.fail(monkeypatch, "junction denied")

    report = reconcile_skill_links(target, packaged)

    assert _outcomes(report) == {"booley-setup": "error"}
    assert "junction denied" in report.events[0].detail
    assert not os.path.lexists(target / "booley-setup")


@pytest.mark.parametrize("fails", ["install", "backup cleanup"])
def test_windows_retarget_failure_keeps_one_working_link(
    tmp_path: Path, target: Path, monkeypatch: pytest.MonkeyPatch, windows_host: None, fails: str
) -> None:
    old_skill = _skill(tmp_path / "old" / "skills")
    new_skill = _skill(tmp_path / "new" / "skills")
    reconcile_skill_links(target, old_skill.parent)
    link = target / "booley-setup"
    original_rename = Path.rename
    original_unlink = Path.unlink
    installs = 0

    def fail_install(path: Path, destination: Path):
        nonlocal installs
        if destination == link:
            installs += 1
            if installs == 1:
                raise PermissionError("install denied")
        return original_rename(path, destination)

    def fail_cleanup(path: Path, *args, **kwargs):
        if path.parent == target and path != link:
            raise PermissionError("backup busy")
        return original_unlink(path, *args, **kwargs)

    if fails == "install":
        monkeypatch.setattr(Path, "rename", fail_install)
    else:  # POSIX removes the emulated junction with unlink, Windows with rmdir
        monkeypatch.setattr(Path, "unlink", fail_cleanup)
        monkeypatch.setattr(Path, "rmdir", fail_cleanup)

    report = reconcile_skill_links(target, new_skill.parent, allow_retarget=True)

    leftovers = {path.name for path in target.iterdir()} - {"booley-setup", MANIFEST_FILENAME}
    if fails == "install":
        assert _outcomes(report) == {"booley-setup": "error"}
        assert "could not install replacement junction" in report.events[0].detail
        assert link.resolve(strict=True) == old_skill.resolve()
        assert leftovers == set()
    else:
        assert _outcomes(report) == {"booley-setup": "retargeted"}
        assert "backup remains" in report.diagnostics[0]
        assert link.resolve(strict=True) == new_skill.resolve()
        assert len(leftovers) == 1


def test_manifest_temporary_cleanup_failure_is_diagnostic(
    packaged: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_symlinks(target.parent.parent)
    _skill(packaged)
    original_replace = Path.replace
    original_unlink = Path.unlink

    def temporary_manifest(path: Path) -> bool:
        return path.parent == target and path.name.startswith(f".{MANIFEST_FILENAME}")

    def fail_replace(path: Path, destination: Path):
        if temporary_manifest(path):
            raise PermissionError("replace denied")
        return original_replace(path, destination)

    def fail_cleanup(path: Path, *args, **kwargs):
        if temporary_manifest(path):
            raise PermissionError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    report = reconcile_skill_links(target, packaged)

    assert len(report.diagnostics) == 2
    assert "could not write" in report.diagnostics[0]
    assert "temporary manifest remains" in report.diagnostics[1]


@pytest.mark.parametrize("inspection", ["mount point", "denied"])
def test_windows_junction_is_told_by_its_mount_point_reparse_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_host: None, inspection: str
) -> None:
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)

    def lstat(_path: Path):
        if inspection == "denied":
            raise PermissionError("inspection denied")
        return SimpleNamespace(st_reparse_tag=mount_point)

    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(Path, "lstat", lstat)

    # A private call: POSIX hosts cannot create the NTFS junction that would reach this
    # check through reconcile_skill_links; Windows CI reaches it with real junctions.
    assert skill_links._is_junction(tmp_path / "junction") is (inspection == "mount point")


def test_independent_manifest_and_allowlist_do_not_prune_product_links(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    product = tmp_path / "product"
    qa = tmp_path / "qa"
    _skill(product)
    _skill(qa, "booley-qa-run")
    _skill(qa, "future-qa-skill")
    target = tmp_path / "skills"

    reconcile_skill_links(target, product)
    qa_report = reconcile_skill_links(
        target,
        qa,
        manifest_name=".booley-qa-skill-links.json",
        desired_names=frozenset({"booley-qa-run"}),
    )

    assert qa_report.count("created") == 1
    assert (target / "booley-setup").is_dir()
    assert (target / "booley-qa-run").is_dir()
    assert not (target / "future-qa-skill").exists()
    assert (target / MANIFEST_FILENAME).is_file()
    assert (target / ".booley-qa-skill-links.json").is_file()
