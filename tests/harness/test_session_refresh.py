"""Interaction tests for recoverable Session refresh orchestration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.harness import session_refresh
from booley.harness import session_runtime as sr
from booley.harness.image_lifecycle import LifecycleResult, Status


def _result() -> LifecycleResult:
    return LifecycleResult(
        "booley-sandbox",
        "sha256:fresh",
        Status.CHANGED,
        payload_fingerprint="payload-123",
    )


def _parked(root: Path) -> sr.ParkedRefreshSession:
    name = sr.session_container_name(root)
    return sr.ParkedRefreshSession(name, f"{name}-pre-refresh", "project-id", True, True)


def test_running_target_is_parked_before_host_bootstrap_refresh(tmp_path: Path) -> None:
    active = True
    result = _result()
    parked = _parked(tmp_path)
    events: list[str] = []

    def park(*_args) -> sr.ParkedRefreshSession:
        nonlocal active
        active = False
        events.append("park")
        return parked

    def refresh_image(*_args, **_kwargs) -> LifecycleResult:
        if active:
            raise RuntimeError(
                "cannot replace stale booley-proxy while active Booley Sessions exist"
            )
        events.append("bootstrap")
        return result

    with (
        patch.object(sr, "conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "inspect_refreshable_session_image"),
        patch.object(session_refresh, "capture_session_spec", return_value=object()),
        patch.object(
            session_refresh.runtime_spec,
            "load_issued_snapshot",
            return_value=SimpleNamespace(),
        ),
        patch.object(sr, "park_session_for_refresh", side_effect=park),
        patch.object(session_refresh, "refresh_session_image", side_effect=refresh_image),
        patch.object(
            session_refresh,
            "reissue_session_spec",
            side_effect=lambda *_args, **_kwargs: events.append("reissue"),
        ),
        patch.object(
            sr,
            "_up_unlocked",
            side_effect=lambda *_args, **_kwargs: events.append("up"),
        ),
        patch.object(
            sr,
            "discard_refresh_session",
            side_effect=lambda *_args: events.append("discard"),
        ),
    ):
        assert session_refresh.refresh(tmp_path) is result

    assert events == ["park", "bootstrap", "reissue", "up", "discard"]


def test_bootstrap_failure_restores_exact_parked_session_and_spec(tmp_path: Path) -> None:
    parked = _parked(tmp_path)
    snapshot = object()
    events: list[str] = []
    with (
        patch.object(sr, "conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "inspect_refreshable_session_image"),
        patch.object(session_refresh, "capture_session_spec", return_value=snapshot),
        patch.object(
            session_refresh.runtime_spec,
            "load_issued_snapshot",
            return_value=SimpleNamespace(),
        ),
        patch.object(sr, "park_session_for_refresh", return_value=parked),
        patch.object(
            session_refresh,
            "refresh_session_image",
            side_effect=RuntimeError("other active Session"),
        ),
        patch.object(
            session_refresh,
            "restore_session_spec",
            side_effect=lambda root, saved: events.append(
                f"spec:{root == tmp_path}:{saved is snapshot}"
            ),
        ),
        patch.object(
            sr,
            "restore_refresh_session",
            side_effect=lambda saved: events.append(f"runtime:{saved is parked}"),
        ),
        pytest.raises(RuntimeError, match="other active Session"),
    ):
        session_refresh.refresh(tmp_path)

    assert events == ["spec:True:True", "runtime:True"]


def test_incomplete_rollback_reports_recovery_container(tmp_path: Path) -> None:
    parked = _parked(tmp_path)
    with (
        patch.object(sr, "conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "inspect_refreshable_session_image"),
        patch.object(session_refresh, "capture_session_spec", return_value=object()),
        patch.object(
            session_refresh.runtime_spec,
            "load_issued_snapshot",
            return_value=SimpleNamespace(),
        ),
        patch.object(sr, "park_session_for_refresh", return_value=parked),
        patch.object(
            session_refresh,
            "refresh_session_image",
            side_effect=RuntimeError("bootstrap failed"),
        ),
        patch.object(
            session_refresh,
            "restore_session_spec",
            side_effect=RuntimeError("stamp busy"),
        ),
        patch.object(
            sr,
            "restore_refresh_session",
            side_effect=sr.SessionError("network missing"),
        ),
        pytest.raises(sr.SessionError, match="rollback was incomplete") as raised,
    ):
        session_refresh.refresh(tmp_path)

    assert parked.backup in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_vscode_owner_is_rejected_before_image_inspection(tmp_path: Path) -> None:
    with (
        patch.object(sr, "conflicting_vscode_session", return_value="vscode-owned"),
        patch.object(session_refresh, "inspect_refreshable_session_image") as inspect_image,
        pytest.raises(sr.SessionError, match="VS Code owns"),
    ):
        session_refresh.refresh(tmp_path)

    inspect_image.assert_not_called()
