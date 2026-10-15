"""Tests for the Interactive Mode Project Initialization planning seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from booley.harness.setup import interactive
from booley.runtime import session_issuance


def _request(tmp_path: Path, builder: Mock) -> interactive.InteractiveInitRequest:
    return interactive.InteractiveInitRequest(tmp_path, builder)


def _prepared(tmp_path: Path) -> session_issuance.PreparedSessionSpec:
    return session_issuance.PreparedSessionSpec(
        {"image": "sha256:prepared", "runArgs": []},
        "prepared-digest",
        session_issuance.SessionSpecInputs(tmp_path, (), (), None, None),
        SimpleNamespace(),
    )


def test_inspect_returns_one_complete_pending_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = Mock()
    prepared = _prepared(tmp_path)
    monkeypatch.setattr(
        interactive.session_issuance, "preview", lambda *_args, **_kwargs: prepared
    )
    monkeypatch.setattr(
        interactive.session_issuance,
        "inspect_prepared",
        Mock(side_effect=session_issuance.RuntimeSpecError("spec drifted")),
    )
    monkeypatch.setattr(interactive, "git_excludes_pending", lambda *_args: True)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: None)
    runtime_plan = Mock(return_value=SimpleNamespace(pending=True))
    monkeypatch.setattr(
        interactive.session_runtime,
        "plan_stopped_headless_runtime_reconciliation",
        runtime_plan,
    )

    plan = interactive.inspect(_request(tmp_path, builder))

    assert plan.prepared is prepared
    assert plan.pending_details == (
        "Sandbox specification/issuance (spec drifted)",
        "Git exclusions",
        "stopped Sandbox resources",
    )
    runtime_plan.assert_called_once_with(tmp_path, prepared.prospective_issuance)


def test_apply_persists_the_exact_prepared_plan_without_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = Mock()
    prepared = _prepared(tmp_path)
    request = _request(tmp_path, builder)
    plan = interactive.InteractiveInitPlan(request, prepared, None, "missing")
    issuance = SimpleNamespace(license_profile=None)
    persist = Mock(return_value=issuance)
    monkeypatch.setattr(interactive.session_issuance, "issue_prepared", persist)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        interactive.session_runtime,
        "reconcile_stopped_headless_runtime",
        lambda *_args: False,
    )
    monkeypatch.setattr(interactive, "add_git_excludes", lambda *_args: False)

    changes = interactive.apply(plan)

    persist.assert_called_once_with(tmp_path, prepared, force_dependencies=False)
    builder.assert_not_called()
    assert changes.issuance is issuance
    assert changes.issued is True


@pytest.mark.parametrize("stealth", [False, True])
def test_inspect_and_apply_use_the_same_history_visibility_policy(tmp_path, monkeypatch, stealth):
    prepared = _prepared(tmp_path)
    issuance = SimpleNamespace(license_profile=None)
    monkeypatch.setattr(interactive, "stealth_enabled", lambda _root: stealth)
    monkeypatch.setattr(interactive.session_issuance, "preview", lambda *_a, **_kw: prepared)
    monkeypatch.setattr(interactive, "_inspect_issuance", lambda *_a: (issuance, ""))
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        interactive.session_runtime,
        "plan_stopped_headless_runtime_reconciliation",
        lambda *_a: SimpleNamespace(pending=False),
    )
    monkeypatch.setattr(
        interactive.session_runtime,
        "reconcile_stopped_headless_runtime",
        lambda *_a: False,
    )
    inspect_exclusions = Mock(return_value=False)
    apply_exclusions = Mock(return_value=False)
    monkeypatch.setattr(interactive, "git_excludes_pending", inspect_exclusions)
    monkeypatch.setattr(interactive, "add_git_excludes", apply_exclusions)
    plan = interactive.inspect(_request(tmp_path, Mock()))
    interactive.apply(plan)
    expected = (".devcontainer", ".claude", ".booley") if stealth else (".devcontainer", ".claude")
    inspect_exclusions.assert_called_once_with(tmp_path, expected)
    apply_exclusions.assert_called_once_with(tmp_path, expected)


def test_refresh_preview_does_not_plan_or_apply_stopped_runtime_cleanup(tmp_path, monkeypatch):
    from booley.harness.session_refresh import _RuntimeImages
    from booley.runtime.session_refresh import RefreshImage

    forbidden = Mock(
        side_effect=AssertionError("init cleanup must not run while refresh parks predecessor")
    )
    monkeypatch.setattr(interactive, "inspect", forbidden)
    monkeypatch.setattr(interactive, "apply", forbidden)
    sources = Mock()
    sources.runtime_image_id = "sha256:prepared"
    adapter = _RuntimeImages()
    monkeypatch.setattr(adapter, "_current_sources", lambda *_args: sources)
    preview = Mock(return_value=_prepared(tmp_path))
    monkeypatch.setattr(session_issuance, "preview", preview)
    adapter.prepare_spec(tmp_path, RefreshImage("booley-sandbox", "sha256:prepared"))
    preview.assert_called_once_with(tmp_path, sources.build, expected_image_id="sha256:prepared")
    forbidden.assert_not_called()
