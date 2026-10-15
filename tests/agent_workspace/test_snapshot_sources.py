"""Exact snapshot sanitization cannot damage live or authorized sources."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.agent_workspace.isolation import WorkspaceSnapshot, hide_opposite_sources
from tests.conftest import symlink_or_skip

FILES = {"rtl": ("rtl/source.sv", "data/dut.sv"), "tb": ("tb/source.sv", "data/tb.sv")}


@pytest.fixture
def snapshot(tmp_path: Path) -> WorkspaceSnapshot:
    live, copy = tmp_path / "live", tmp_path / "copy"
    for root in (live, copy):
        for rel in (*FILES["rtl"], *FILES["tb"]):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"// {rel}\n")
    return WorkspaceSnapshot(live, copy)


@pytest.mark.parametrize("category,opposite", [("rtl", "tb"), ("tb", "rtl")])
def test_removes_exact_opposite_copy_and_logs_every_path(snapshot, category, opposite, caplog):
    caplog.set_level("INFO")
    hidden = hide_opposite_sources(snapshot, category, category_files=FILES)
    assert set(hidden) == set(FILES[opposite])
    for name in FILES[opposite]:
        assert not (snapshot.snapshot_root / name).exists()
        assert (snapshot.real_root / name).is_file()
        assert name in caplog.text
    for name in FILES[category]:
        assert (snapshot.snapshot_root / name).is_file()


@pytest.mark.parametrize("path", [".", "./", "../live/tb/source.sv", "/", ".git"])
def test_invalid_path_rejected_before_removing_any_sources(snapshot, path):
    with pytest.raises(ValueError):
        hide_opposite_sources(
            snapshot, "rtl", category_files={"rtl": FILES["rtl"], "tb": (*FILES["tb"], path)}
        )
    assert (snapshot.snapshot_root / "tb/source.sv").is_file()
    assert (snapshot.real_root / "tb/source.sv").is_file()


def test_absolute_live_path_rejected(snapshot):
    with pytest.raises(ValueError):
        hide_opposite_sources(
            snapshot,
            "rtl",
            category_files={
                "rtl": FILES["rtl"],
                "tb": (str(snapshot.real_root / "tb/source.sv"),),
            },
        )
    assert (snapshot.real_root / "tb/source.sv").is_file()


def test_symlink_ancestor_cannot_delete_live_sources(snapshot):
    root = snapshot.snapshot_root
    symlink_or_skip(root / "outside", snapshot.real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="parent escapes"):
        hide_opposite_sources(
            snapshot, "rtl", category_files={"rtl": FILES["rtl"], "tb": ("outside/tb/source.sv",)}
        )
    assert (snapshot.real_root / "tb/source.sv").is_file()


def test_alias_removal_never_follows_live_target(snapshot):
    root = snapshot.snapshot_root
    symlink_or_skip(root / "outside", snapshot.real_root / "tb", target_is_directory=True)
    hide_opposite_sources(snapshot, "rtl", category_files=FILES)
    assert not (root / "outside").is_symlink()
    assert (snapshot.real_root / "tb/source.sv").is_file()


@pytest.mark.parametrize(
    "real,copy", [("live", "live"), ("live", "live/copy"), ("live/copy", "live")]
)
def test_overlapping_snapshot_roots_rejected(tmp_path, real, copy):
    with pytest.raises(ValueError, match="separate"):
        hide_opposite_sources(
            WorkspaceSnapshot(tmp_path / real, tmp_path / copy), "rtl", category_files=FILES
        )


def test_scoped_source_inside_excluded_root_refuses_before_removal(snapshot):
    with pytest.raises(ValueError, match="scope or agent cwd"):
        hide_opposite_sources(
            snapshot,
            "rtl",
            category_files=FILES,
            scope=("rtl/source.sv",),
            excluded_roots=(snapshot.real_root / "rtl",),
        )
    assert (snapshot.snapshot_root / "rtl/source.sv").is_file()


def test_agent_cwd_removal_rejected(snapshot):
    with pytest.raises(ValueError, match="agent cwd"):
        hide_opposite_sources(
            snapshot, "rtl", category_files=FILES, cwd=snapshot.snapshot_root / "tb/source.sv"
        )
    assert (snapshot.snapshot_root / "tb/source.sv").is_file()


def test_unknown_source_backup_removed_but_scoped_grant_and_support_retained(snapshot):
    root = snapshot.snapshot_root
    for name in ("new.sv", "copy.cpp.bak", "tb.py~", "README.md", "config.toml"):
        (root / name).write_text("content")
    hidden = hide_opposite_sources(snapshot, "rtl", category_files=FILES, scope=("new.sv",))
    assert (root / "new.sv").is_file()
    assert (root / "README.md").is_file()
    assert (root / "config.toml").is_file()
    assert {"copy.cpp.bak", "tb.py~"} <= set(hidden)


def test_own_grant_cannot_override_registered_opposite(snapshot):
    with pytest.raises(ValueError, match=r"opposite-category.*tb/source.sv"):
        hide_opposite_sources(snapshot, "rtl", category_files=FILES, scope=("tb/source.sv",))


def test_absent_opposite_is_reported(snapshot, caplog):
    (snapshot.snapshot_root / "tb/source.sv").unlink()
    caplog.set_level("INFO")
    hide_opposite_sources(snapshot, "rtl", category_files=FILES)
    assert "opposite=2 absent=1" in caplog.text


def test_owned_artifact_roots_remove_embedded_source_without_touching_live(snapshot):
    relative = Path("custom-output/result.md")
    for root in (snapshot.real_root, snapshot.snapshot_root):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text("```sv\nmodule secret; endmodule\n```\n")
    hidden = hide_opposite_sources(
        snapshot,
        "rtl",
        category_files=FILES,
        excluded_roots=(snapshot.real_root / "custom-output",),
    )
    assert relative.as_posix() in hidden
    assert not (snapshot.snapshot_root / relative).exists()
    assert (snapshot.real_root / relative).is_file()


def test_historical_campaign_manifest_excludes_embedded_reports(snapshot):
    import json

    root = snapshot.snapshot_root / "arbitrary-old-campaign"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "scope_hashes": {},
                "mutants": [],
                "artifacts": {"specs": "mutation-specs.md"},
            }
        )
    )
    (root / "mutation-results.md").write_text("private RTL and TB source text")
    hidden = hide_opposite_sources(snapshot, "tb", category_files=FILES)
    assert "arbitrary-old-campaign/mutation-results.md" in hidden
    assert not root.exists()


def test_hardlink_alias_with_non_source_suffix_cannot_reveal_opposite(snapshot):
    import os

    real = snapshot.real_root
    root = snapshot.snapshot_root
    os.link(real / "tb/source.sv", real / "notes.txt")
    (root / "notes.txt").write_bytes((real / "notes.txt").read_bytes())
    hide_opposite_sources(snapshot, "rtl", category_files=FILES)
    assert not (root / "notes.txt").exists()
    assert (real / "notes.txt").is_file()
