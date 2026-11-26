"""Tests for the Sandbox Project Initialization planning seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from booley.harness.setup import interactive
from booley.runtime import session_issuance
from booley.runtime.booley_dir import reset_cache
from booley.runtime.git import add_git_excludes
from tests.git_support import repository


def _prepared(root: Path) -> session_issuance.PreparedSessionSpec:
    return session_issuance.PreparedSessionSpec(
        {"image": "sha256:prepared", "runArgs": []},
        "prepared-digest",
        session_issuance.SessionSpecInputs(root, (), (), None, None),
        SimpleNamespace(),
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git checkout on a host without Docker; Booley's exclude writer falls back to files."""
    root = repository(tmp_path / "project")
    (root / ".booley").mkdir()
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    reset_cache()
    return root


def _enable_stealth(project: Path, enabled: bool) -> None:
    (project / ".booley" / "booley.toml").write_text(
        f"[stealth]\nenabled = {str(enabled).lower()}\n", encoding="utf-8"
    )


@pytest.mark.parametrize("stealth", [False, True])
def test_inspect_returns_one_complete_pending_plan(
    project: Path, monkeypatch: pytest.MonkeyPatch, stealth: bool
) -> None:
    _enable_stealth(project, stealth)
    add_git_excludes(project, (".devcontainer", ".claude"))  # a non-Stealth init's exclusions
    prepared = _prepared(project)
    monkeypatch.setattr(interactive.session_issuance, "preview", lambda *_a, **_kw: prepared)
    monkeypatch.setattr(
        interactive.session_issuance,
        "inspect_prepared",
        Mock(side_effect=session_issuance.RuntimeSpecError("spec drifted")),
    )
    # Stopped-Sandbox planning reads Docker state; `session_runtime`'s tests own it.
    runtime_plan = Mock(return_value=SimpleNamespace(pending=True))
    monkeypatch.setattr(
        interactive.session_runtime, "plan_stopped_headless_runtime_reconciliation", runtime_plan
    )

    plan = interactive.inspect(interactive.InteractiveInitRequest(project, Mock()))

    assert plan.prepared is prepared
    assert plan.pending_details == (
        "Sandbox specification/issuance (spec drifted)",
        *(["Git exclusions"] if stealth else []),
        "stopped Sandbox resources",
    )
    runtime_plan.assert_called_once_with(project, prepared.prospective_issuance)


@pytest.mark.parametrize("stealth", [False, True])
@pytest.mark.parametrize("current", [False, True], ids=["missing", "current"])
def test_apply_issues_only_a_missing_plan_and_excludes_the_history_policy_names(
    project: Path, monkeypatch: pytest.MonkeyPatch, current: bool, stealth: bool
) -> None:
    _enable_stealth(project, stealth)
    prepared = _prepared(project)
    issuance = SimpleNamespace(license_profile=None)
    persist = Mock(return_value=issuance)
    monkeypatch.setattr(interactive.session_issuance, "issue_prepared", persist)
    # Stopped-Sandbox removal drives Docker; `session_runtime`'s tests own it.
    monkeypatch.setattr(
        interactive.session_runtime, "reconcile_stopped_headless_runtime", lambda *_a: False
    )
    builder = Mock()
    request = interactive.InteractiveInitRequest(project, builder)
    plan = interactive.InteractiveInitPlan(request, prepared, issuance if current else None)

    changes = interactive.apply(plan)

    builder.assert_not_called()
    expected_issue = [] if current else [call(project, prepared, force_dependencies=False)]
    assert persist.call_args_list == expected_issue
    assert (changes.issuance, changes.issued) == (issuance, not current)
    excluded = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    hidden = ["/.devcontainer", "/.claude", *(["/.booley"] if stealth else [])]
    assert [line for line in excluded.splitlines() if line.startswith("/")] == hidden


def test_refresh_preview_does_not_plan_or_apply_stopped_runtime_cleanup(tmp_path, monkeypatch):
    # private: `_RuntimeImages` is the refresh adapter `booley session refresh` builds inside a
    # Docker image rebuild; `_current_sources` reads the image's build sources from Docker.
    from booley.harness.session_refresh import _RuntimeImages
    from booley.runtime.session_refresh import RefreshImage

    forbidden = Mock(side_effect=AssertionError("init cleanup must not run during refresh"))
    monkeypatch.setattr(interactive, "inspect", forbidden)
    monkeypatch.setattr(interactive, "apply", forbidden)
    sources = Mock()
    sources.runtime_image_id = "sha256:prepared"
    adapter = _RuntimeImages()
    monkeypatch.setattr(adapter, "_current_sources", lambda *_args: sources)
    preview = Mock(return_value=_prepared(tmp_path))
    monkeypatch.setattr(session_issuance, "preview", preview)
    adapter.prepare_spec(tmp_path, RefreshImage("sha256:prepared", None, None, True))
    preview.assert_called_once_with(tmp_path, sources.build, expected_image_id="sha256:prepared")
    forbidden.assert_not_called()


def test_exclude_names_cover_relocated_booley_dir_in_stealth(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    (tmp_path / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    (tmp_path / "tools/booley").mkdir(parents=True)
    (tmp_path / "tools/booley/booley.toml").write_bytes(b"[stealth]\nenabled = true\n")
    reset_cache()

    assert interactive.exclude_names(tmp_path) == (
        ".devcontainer",
        ".claude",
        ".booley",
        "tools/booley",
    )
    reset_cache()
