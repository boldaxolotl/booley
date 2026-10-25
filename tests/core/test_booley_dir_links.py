"""The shared rule for a symlinked Booley Directory entry."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import symlink_or_skip


def test_lexical_booley_path_is_identity_unless_the_entry_is_a_link(tmp_path: Path) -> None:
    from booley.core.booley_dir_links import is_booley_dir_entry, lexical_booley_path

    real = tmp_path / "real"
    (real / ".booley").mkdir(parents=True)
    alias = tmp_path / "alias"
    symlink_or_skip(alias, real, target_is_directory=True)

    # Plain layout: identity for every spelling, including an aliased Project Root.
    path = real / ".booley" / "x"
    assert lexical_booley_path(alias / ".booley", path) == path
    assert lexical_booley_path(real / ".booley", path) == path

    # Linked entry: a path below its physical target is re-spelled through the entry.
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    symlink_or_skip(project / ".booley", store, target_is_directory=True)
    entry = project / ".booley"
    assert lexical_booley_path(entry, store / "cores" / "x") == entry / "cores" / "x"
    assert lexical_booley_path(entry, store) == entry
    # Lexical spellings and paths outside the target are unchanged.
    assert lexical_booley_path(entry, entry / "cores") == entry / "cores"
    outside = tmp_path / "other" / "x"
    assert lexical_booley_path(entry, outside) == outside

    # The entry rule is purely lexical.
    assert is_booley_dir_entry(entry, entry)
    assert not is_booley_dir_entry(entry / "cores", entry)
    assert not is_booley_dir_entry(store, entry)
    assert not is_booley_dir_entry(entry, None)
