"""The host pin that guards a symlinked ``.booley`` entry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.core import booley_dir, booley_dir_pin

pytestmark = pytest.mark.skipif(os.name == "nt", reason="directory symlink requires privilege")


@pytest.fixture
def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Return (project, entry, target); the autouse fixture isolates the pin store."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    booley_dir.reset_cache()
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "ext-a"
    target.mkdir()
    (target / "booley.toml").write_text("[project]\n", encoding="utf-8")
    entry = project / ".booley"
    entry.symlink_to(target, target_is_directory=True)
    return project, entry, target


def test_host_resolvers_refuse_unpinned_and_retargeted_entry(linked, tmp_path: Path) -> None:
    project, entry, target = linked
    resolvers = (
        lambda: booley_dir.resolve_checkout_booley_dir(project),
        lambda: booley_dir.resolve_authoritative_booley_dir(project),
        lambda: booley_dir.resolve_booley_dir(project, cache_result=False),
    )
    for resolve in resolvers:
        with pytest.raises(booley_dir_pin.BooleyDirPinError, match="booley init"):
            resolve()

    booley_dir_pin.write_pin(project, booley_dir_pin.current_target(entry))
    for resolve in resolvers:
        assert resolve() == entry

    other = tmp_path / "ext-b"
    other.mkdir()
    (other / "booley.toml").write_text("[project]\n", encoding="utf-8")
    entry.unlink()
    entry.symlink_to(other, target_is_directory=True)
    for resolve in resolvers:
        with pytest.raises(booley_dir_pin.BooleyDirPinError) as raised:
            resolve()
        assert str(target.resolve()) in str(raised.value)
        assert str(other.resolve()) in str(raised.value)
        assert "--repin-booley-dir" in raised.value.remedy


def test_target_replaced_in_place_is_refused(linked) -> None:
    project, entry, target = linked
    booley_dir_pin.write_pin(project, booley_dir_pin.current_target(entry))
    target.rename(target.with_name("moved-away"))
    target.mkdir()
    (target / "booley.toml").write_text("[project]\n", encoding="utf-8")
    with pytest.raises(booley_dir_pin.BooleyDirPinError, match="replaced"):
        booley_dir_pin.require_pinned_entry(project)


def test_real_entry_sandbox_and_booley_dir_skip_the_pin(
    linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, entry, _target = linked
    # Sandbox: no check runs even for an unpinned link.
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    booley_dir_pin.require_pinned_entry(project)
    monkeypatch.delenv("BOOLEY_CONTAINER")
    # A real directory needs no pin.
    entry.unlink()
    entry.mkdir()
    booley_dir_pin.require_pinned_entry(project)
    assert booley_dir.resolve_checkout_booley_dir(project) == entry
    # An explicit host BOOLEY_DIR selection is not the linked entry.
    monkeypatch.setenv("BOOLEY_DIR", str(entry))
    assert booley_dir.resolve_booley_dir(project, cache_result=False) == entry
    assert not booley_dir_pin._store().root.exists()


def test_dangling_link_is_left_to_callers(linked) -> None:
    project, _entry, target = linked
    (target / "booley.toml").unlink()
    target.rmdir()
    booley_dir_pin.require_pinned_entry(project)
