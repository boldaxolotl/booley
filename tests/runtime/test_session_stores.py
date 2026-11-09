"""The four per-Project Session stores share one naming, scan, and repair contract."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from booley.core import private_store
from booley.runtime import (
    issuance_invalidation,
    session_admission,
    session_issuance,
    session_refresh,
)

if TYPE_CHECKING:
    from booley.core.private_store import ProjectStore

# Store factories by (module, attribute), resolved at call time so each test,
# not collection, meets a missing store API.
_FACTORIES = {
    "issuance": (session_issuance, "stamp_store"),
    "admission": (session_admission, "claim_store"),
    "refresh": (session_refresh, "journal_store"),
    "invalidation": (issuance_invalidation, "journal_store"),
}
STORE_NAMES = list(_FACTORIES)


def _store(name: str) -> ProjectStore:
    module, attribute = _FACTORIES[name]
    return getattr(module, attribute)()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "Project"
    path.mkdir()
    return path.resolve()


def _scan_through_entry_point(name: str, project: Path) -> Callable[[], object]:
    """Return each store's own read-only scan entry point."""
    entry_points: dict[str, Callable[[], object]] = {
        "issuance": lambda: _store("issuance").scan(),
        "admission": lambda: session_admission.has_pending_claim(project),
        "refresh": session_refresh.pending_refresh_projects,
        "invalidation": issuance_invalidation.pending_invalidations,
    }
    return entry_points[name]


def _plant(store: ProjectStore, filename: str, document: object) -> Path:
    """Write one private mode-600 file straight into a store directory."""
    store.private.ensure_directory()
    store.private.atomic_write_text(filename, json.dumps(document))
    return store.private.root / filename


def _old_rule_name(project_root: str) -> str:
    """The name earlier releases gave a Project Root: the raw, un-normalised hash."""
    return hashlib.sha256(project_root.encode()).hexdigest() + ".json"


def test_store_scan_rejects_stray_entry(project: Path) -> None:
    for name in STORE_NAMES:
        store = _store(name)
        stray = _plant(store, "notes.txt", {"project_root": str(project)})

        with pytest.raises(store.private.error_type, match=r"notes\.txt") as caught:
            _scan_through_entry_point(name, project)()

        assert str(stray) in str(caught.value)
        stray.unlink()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs a privilege on Windows")
def test_store_scan_rejects_symlinked_entry(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"project_root": str(project)}), encoding="utf-8")
    for name in STORE_NAMES:
        store = _store(name)
        store.private.ensure_directory()
        link = store.private.root / store.filename(str(project))
        link.symlink_to(outside)

        with pytest.raises(store.private.error_type, match="unexpected") as caught:
            _scan_through_entry_point(name, project)()

        assert str(link) in str(caught.value)
        link.unlink()


def test_store_names_project_root_by_platform_case_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    upper, mixed = "C:\\Work\\Project", "c:\\work\\PROJECT"
    monkeypatch.setattr(private_store.os.path, "normcase", ntpath.normcase)
    assert session_issuance.stamp_path_for_identity(upper) == (
        session_issuance.stamp_path_for_identity(mixed)
    )

    monkeypatch.setattr(private_store.os.path, "normcase", posixpath.normcase)
    assert session_issuance.stamp_path_for_identity(upper) != (
        session_issuance.stamp_path_for_identity(mixed)
    )


def test_old_windows_rule_file_fails_closed_naming_file_and_repair(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_store.os.path, "normcase", ntpath.normcase)
    root = str(project)
    reads: dict[str, Callable[[], object]] = {
        "issuance": lambda: session_issuance.recovery_stamp_path(project),
        "refresh": lambda: session_refresh.has_pending_refresh(project),
        "invalidation": lambda: issuance_invalidation.has_pending(project),
    }
    for name, read in reads.items():
        store = _store(name)
        old = _plant(store, _old_rule_name(root), {"project_root": root})
        assert old.name != store.filename(root)

        with pytest.raises(store.private.error_type) as caught:
            read()

        message = str(caught.value)
        assert str(old) in message
        assert f"booley init --seed --project {root}" in message
        assert old.exists()


def test_scan_reports_old_windows_rule_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_store.os.path, "normcase", ntpath.normcase)
    root = str(project)
    store = issuance_invalidation.journal_store()
    old = _plant(store, _old_rule_name(root), {"project_root": root})

    with pytest.raises(issuance_invalidation.InvalidationError) as caught:
        store.scan()

    assert str(old) in str(caught.value)
    assert "booley init --seed --project" in str(caught.value)


def test_withdrawal_removes_old_windows_rule_stamp(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_store.os.path, "normcase", ntpath.normcase)
    root = str(project)
    old = _plant(session_issuance.stamp_store(), _old_rule_name(root), {"project_root": root})

    session_issuance.invalidate_project(root)

    assert not old.exists()
    assert not any(path.exists() for path in session_issuance.stamp_store().paths(root))


def test_scan_rejects_unreadable_and_anonymous_files(project: Path) -> None:
    store = issuance_invalidation.journal_store()
    path = _plant(store, store.filename(str(project)), ["not", "an", "object"])

    with pytest.raises(issuance_invalidation.InvalidationError, match="does not name a Project"):
        store.scan()

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(issuance_invalidation.InvalidationError, match="cannot read"):
        store.scan()


def test_read_and_write_round_trip_by_project_root(project: Path) -> None:
    store = issuance_invalidation.journal_store()
    assert store.read(str(project)) is None

    store.write(str(project), json.dumps({"project_root": str(project)}))

    assert store.read(str(project)) == {"project_root": str(project)}
    assert [entry.project_root for entry in store.scan()] == [str(project)]
