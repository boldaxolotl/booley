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
