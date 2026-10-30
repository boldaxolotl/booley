"""Doctor groups ancestor links while retaining a complete verbose path report."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry
from booley.harness import doctor
from tests.conftest import symlink_or_skip
from tests.diagnostic_helpers import _Rec
from tests.harness.test_doctor import (
    _patch_environment,
    _schema_audit,
    _write_core,
    _write_project,
)

pytestmark = pytest.mark.usefixtures("isolated_git_attributes")


def _ancestor_links(root: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        library = root / f"lib{index:02d}"
        library.mkdir()
        symlink_or_skip(library / "self", "..", target_is_directory=True)
        paths.append(f"lib{index:02d}/self")
    return paths


@pytest.mark.parametrize("verbose,concise", [(False, False), (True, False), (False, True)])
def test_public_doctor_groups_54_links_with_complete_verbose_paths(
    tmp_path, monkeypatch, capsys, verbose, concise
):
    booley_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    paths = _ancestor_links(tmp_path, 54)

    result = doctor.run_doctor_result(
        argparse.Namespace(deep=False, verbose=verbose, concise=concise), tmp_path
    )

    output = capsys.readouterr().out
    failures = [f for f in result.findings if "FuseSoC recursive symlink" in f.message]
    assert len(failures) == 1
    assert result.counts["fail"] == 1
    assert result.exit_code == 1
    assert not result.clean
    message = failures[0].message
    assert "54 links" in message
    assert str(tmp_path.resolve()) in message
    assert ", ".join(paths[:3]) in message
    assert "+51 more" in message
    assert "--verbose" in message
    assert paths[3] not in message
    assert output.count("  ✗  FuseSoC recursive symlinks") == 1
    assert "each link's containing directory" in failures[0].fix
    for path in paths:
        assert (path in output) is (verbose or path in paths[:3])


@pytest.mark.parametrize("count", [1, 3, 5])
def test_group_preview_is_sorted_and_bounded_without_symlink_privileges(
    tmp_path, monkeypatch, count
):
    paths = [tmp_path / f"lib{index}/self" for index in range(count)]
    hazards = [
        fusesoc_registry.CoreSetupHazard(
            "recursive-symlink", path, f"directory link resolves to ancestor {tmp_path}", tmp_path
        )
        for path in reversed(paths)
    ]
    monkeypatch.setattr(fusesoc_registry, "core_setup_hazards", lambda _root: hazards)
    rec = _Rec()
    details = []

    doctor._check_core_setup_hazards(
        _schema_audit(tmp_path), tmp_path, rec.p, rec.f, _note=rec.n, _detail=details.append
    )

    assert len(rec.fails()) == 1
    message = rec.fails()[0]
    noun = "link" if count == 1 else "links"
    assert f"{count} {noun}" in message
    relative = [path.relative_to(tmp_path).as_posix() for path in paths]
    assert ", ".join(relative[:3]) in message
    assert ("more" in message) is (count > 3)
    assert len(details) == count
    for path, detail in zip(relative, details, strict=True):
        assert path in detail
        assert str(tmp_path) in detail


def test_different_ancestors_and_unresolved_cycles_are_separate(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    hazards = [
        fusesoc_registry.CoreSetupHazard(
            "recursive-symlink", tmp_path / "a/self", "ancestor", tmp_path
        ),
        fusesoc_registry.CoreSetupHazard(
            "recursive-symlink", nested / "a/self", "ancestor", nested
        ),
        fusesoc_registry.CoreSetupHazard(
            "recursive-symlink", tmp_path / "b/self", "ancestor", tmp_path
        ),
        # Exercise reporting of legacy scanner cycle hazards, without changing ELOOP detection.
        fusesoc_registry.CoreSetupHazard(
            "recursive-symlink", tmp_path / "cycle-a", "symlink chain contains a cycle"
        ),
        fusesoc_registry.CoreSetupHazard(
            "recursive-symlink", tmp_path / "cycle-b", "symlink chain contains a cycle"
        ),
    ]
    monkeypatch.setattr(fusesoc_registry, "core_setup_hazards", lambda _root: hazards)
    rec = _Rec()
    doctor._check_core_setup_hazards(_schema_audit(tmp_path), tmp_path, rec.p, rec.f)

    assert len(rec.fails()) == 4
    assert sum("2 links" in message and str(tmp_path) in message for message in rec.fails()) == 1
    assert sum("1 link" in message and str(nested) in message for message in rec.fails()) == 1
    assert sum("symlink chain contains a cycle" in message for message in rec.fails()) == 2
    assert any("cycle-a" in message for message in rec.fails())
    assert any("cycle-b" in message for message in rec.fails())


def test_recursive_group_does_not_hide_selected_provider(tmp_path):
    _write_core(
        tmp_path,
        "CAPI=2:\nname: ::demo:0\nprovider: {name: github, user: acme, repo: demo}\n"
        "targets:\n  sim:\n    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n",
    )
    _ancestor_links(tmp_path, 5)
    rec = _Rec()
    doctor._check_core_setup_hazards(_schema_audit(tmp_path), tmp_path, rec.p, rec.f, _note=rec.n)
    assert len(rec.fails()) == 2
    assert sum("remote fetch" in message for message in rec.fails()) == 1
    assert sum("5 links" in message for message in rec.fails()) == 1


def test_no_recursive_links_retains_existing_pass(tmp_path):
    rec = _Rec()
    doctor._check_core_setup_hazards(_schema_audit(tmp_path), tmp_path, rec.p, rec.f)
    assert not rec.fails()
    assert rec.events == [
        ("pass", ".core tree has no provider or recursive-symlink setup hazards")
    ]


def test_ignored_link_subtree_retains_existing_pass(tmp_path):
    _ancestor_links(tmp_path, 1)
    (tmp_path / "lib00/FUSESOC_IGNORE").write_text("", encoding="utf-8")
    rec = _Rec()
    doctor._check_core_setup_hazards(_schema_audit(tmp_path), tmp_path, rec.p, rec.f)
    assert not rec.fails()
    assert rec.events == [
        ("pass", ".core tree has no provider or recursive-symlink setup hazards")
    ]


def test_real_links_to_different_ancestors_are_grouped_by_resolved_target(tmp_path):
    for directory, target in (("a", ".."), ("nested/a", "../.."), ("nested/b", "..")):
        library = tmp_path / directory
        library.mkdir(parents=True)
        link = library / "self"
        # Windows relative symlink targets require native separators.
        symlink_or_skip(link, Path(target), target_is_directory=True)
        assert link.resolve(strict=True) == (library / target).resolve(strict=True)
    rec = _Rec()
    doctor._check_core_setup_hazards(_schema_audit(tmp_path), tmp_path, rec.p, rec.f)
    assert len(rec.fails()) == 2
    assert any(
        f"2 links resolve to ancestor {tmp_path.resolve()} (" in message for message in rec.fails()
    ), rec.fails()
    assert any(
        f"1 link resolves to ancestor {(tmp_path / 'nested').resolve()} (" in message
        for message in rec.fails()
    ), rec.fails()
