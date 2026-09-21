"""Interaction tests for recoverable Session refresh orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from booley.harness import session_refresh as harness_refresh
from booley.runtime import session_refresh
from booley.runtime import session_runtime as sr
from booley.runtime.image_lifecycle import LifecycleResult, Status
from booley.runtime.session_issuance import Issuance
from booley.runtime.session_spec import SessionSpecSnapshot


def _result() -> session_refresh.RefreshImage:
    return session_refresh.RefreshImage(
        "booley-sandbox",
        "sha256:fresh",
        payload_fingerprint="payload-123",
    )


def _parked(root: Path) -> sr.ParkedSession:
    name = sr.session_container_name(root)
    return sr.ParkedSession(
        name,
        f"{name}-pre-refresh",
        True,
        project_id="project-id",
        reconnect_egress=True,
        container_id="container-prior",
        image_id="sha256:prior",
        egress_network_id="network-egress",
    )


def _issuance(root: Path, image_id: str = "sha256:prior") -> Issuance:
    project = root.resolve()
    identity = hashlib.sha256(str(project).encode()).hexdigest()
    return Issuance(
        4,
        str(project),
        "a" * 64,
        image_id,
        image_id,
        f"booley-issued-{identity}:session",
        1,
        None,
        None,
        None,
        None,
        "b" * 64,
        "c" * 64,
        str(project / ".booley_project"),
    )


def _snapshot(root: Path) -> SessionSpecSnapshot:
    project = root.resolve()
    return SessionSpecSnapshot(
        project / ".devcontainer" / "devcontainer.json",
        b'{"image":"sha256:prior"}\n',
        0o644,
        session_refresh.runtime_spec.stamp_path(project),
        b'{"version":4}\n',
        0o600,
        "sha256:prior",
    )


def test_candidate_is_prepared_before_running_target_is_parked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    active = True
    result, parked = _result(), _parked(tmp_path)
    prior = _issuance(tmp_path)
    candidate = _issuance(tmp_path, "sha256:fresh")
    events: list[str] = []

    def park(*_args) -> sr.ParkedSession:
        nonlocal active
        active = False
        events.append("park")
        return parked

    def prepare_image(*_args, **_kwargs) -> session_refresh.RefreshImage:
        assert active
        events.append("prepare")
        return result

    def commit_image() -> session_refresh.RefreshImage:
        assert not active
        events.append("commit")
        return result

    images = Mock(spec=session_refresh.RuntimeImageOperations)
    images.prepare.side_effect = prepare_image
    images.commit.side_effect = commit_image
    images.reissue.side_effect = lambda *_args, **_kwargs: events.append("reissue")

    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "capture_session_spec", return_value=_snapshot(tmp_path)),
        patch.object(session_refresh, "_load_recovery_issuance", return_value=prior),
        patch.object(sr, "plan_session_refresh", return_value=parked),
        patch.object(sr, "park_planned_session", side_effect=park),
        patch.object(session_refresh.runtime_spec, "load_issued_snapshot", return_value=candidate),
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
        assert session_refresh.refresh(tmp_path, images) is result

    assert events == ["prepare", "park", "commit", "reissue", "up", "discard"]


def test_candidate_failure_leaves_running_session_and_spec_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    images.prepare.side_effect = RuntimeError("candidate build failed")
    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "capture_session_spec") as capture,
        patch.object(sr, "park_planned_session") as park,
        pytest.raises(RuntimeError, match="candidate build failed"),
    ):
        session_refresh.refresh(tmp_path, images)

    capture.assert_not_called()
    park.assert_not_called()
    images.abort.assert_called_once_with()


def test_incomplete_rollback_reports_recovery_container(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    parked = _parked(tmp_path)
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    images.prepare.return_value = _result()
    images.commit.side_effect = RuntimeError("commit failed")
    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "capture_session_spec", return_value=_snapshot(tmp_path)),
        patch.object(session_refresh, "_load_recovery_issuance", return_value=_issuance(tmp_path)),
        patch.object(sr, "plan_session_refresh", return_value=parked),
        patch.object(sr, "park_planned_session"),
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
        pytest.raises(sr.SessionError, match="recovery was incomplete") as raised,
    ):
        session_refresh.refresh(tmp_path, images)

    assert parked.backup in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_vscode_owner_is_rejected_before_image_inspection(tmp_path: Path) -> None:
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value="vscode-owned"),
        pytest.raises(sr.SessionError, match="VS Code owns"),
    ):
        session_refresh.refresh(tmp_path, images)

    images.inspect.assert_not_called()


def test_current_graph_issuance_and_running_sandbox_are_a_true_noop(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = session_refresh.RefreshImage(
        "booley-sandbox",
        "sha256:fresh",
        image_graph_changed=False,
    )
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    images.prepare.return_value = result
    images.commit.return_value = result
    parked = replace(_parked(tmp_path), image_id="sha256:fresh")
    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "capture_session_spec", return_value=_snapshot(tmp_path)),
        patch.object(
            session_refresh,
            "_load_recovery_issuance",
            return_value=_issuance(tmp_path, "sha256:fresh"),
        ),
        patch.object(sr, "plan_session_refresh", return_value=parked),
        patch.object(sr, "verify_refreshed_session"),
        patch.object(sr, "park_planned_session") as park,
        patch.object(sr, "_up_unlocked") as up,
    ):
        assert session_refresh.refresh(tmp_path, images) == result

    images.commit.assert_called_once_with()
    park.assert_not_called()
    up.assert_not_called()


def test_vscode_start_after_creation_discards_new_candidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = _result()
    snapshot = _snapshot(tmp_path)
    prior_issuance = _issuance(tmp_path)
    candidate_issuance = _issuance(tmp_path, "sha256:fresh")
    events: list[str] = []
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    images.prepare.return_value = result
    images.commit.return_value = result
    with (
        patch.object(
            sr,
            "strict_conflicting_vscode_session",
            side_effect=[None, None, "vscode-owned"],
        ),
        patch.object(session_refresh, "capture_session_spec", return_value=snapshot),
        patch.object(session_refresh, "_load_recovery_issuance", return_value=prior_issuance),
        patch.object(
            session_refresh.runtime_spec, "load_issued_snapshot", return_value=candidate_issuance
        ),
        patch.object(sr, "plan_session_refresh", return_value=None),
        patch.object(
            sr,
            "_up_unlocked",
            side_effect=lambda *_args, **_kwargs: events.append("up"),
        ),
        patch.object(
            session_refresh,
            "restore_session_spec",
            side_effect=lambda *_args: events.append("restore-spec"),
        ),
        patch.object(
            sr,
            "discard_refresh_candidate",
            side_effect=lambda root, issuance: events.append(
                f"discard:{root == tmp_path}:{issuance == candidate_issuance}"
            ),
        ),
        patch.object(session_refresh, "_verify_restored_journal"),
        pytest.raises(sr.SessionError, match="new headless Session is being rolled back"),
    ):
        session_refresh.refresh(tmp_path, images)

    assert events == ["up", "restore-spec", "discard:True:True"]


def test_invalid_recovery_issuance_is_a_session_error(tmp_path: Path) -> None:
    with (
        patch.object(
            session_refresh.runtime_spec,
            "load_issued_snapshot",
            side_effect=session_refresh.runtime_spec.RuntimeSpecError("stamp drift"),
        ),
        pytest.raises(sr.SessionError, match=r"cannot preserve.*stamp drift"),
    ):
        session_refresh._load_recovery_issuance(tmp_path)


def test_refresh_without_immutable_image_id_rolls_back_spec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = session_refresh.RefreshImage("booley-sandbox", None)  # type: ignore[arg-type]
    snapshot = _snapshot(tmp_path)
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    images.prepare.return_value = result
    with (
        patch.object(sr, "strict_conflicting_vscode_session", return_value=None),
        patch.object(session_refresh, "capture_session_spec", return_value=snapshot),
        patch.object(session_refresh, "_load_recovery_issuance", return_value=_issuance(tmp_path)),
        patch.object(sr, "plan_session_refresh", return_value=None),
        patch.object(session_refresh, "restore_session_spec") as restore,
        patch.object(session_refresh, "_verify_restored_journal"),
        pytest.raises(sr.SessionError, match="immutable Sandbox Image ID"),
    ):
        session_refresh.refresh(tmp_path, images)

    restore.assert_not_called()


def test_harness_refresh_composes_image_operations_in_order(tmp_path: Path) -> None:
    inspection = LifecycleResult("booley-sandbox", "sha256:old", Status.CURRENT)
    refreshed = LifecycleResult(
        "booley-sandbox",
        "sha256:fresh",
        Status.CHANGED,
        payload_fingerprint="payload-123",
    )
    events: list[tuple[object, ...]] = []

    prepared = Mock()
    prepared.plan.selected_reference = "booley-sandbox"
    prepared.plan.nodes = (Mock(wheel_source_fingerprint="payload-123"),)
    prepared.plan.steps = (Mock(action=object()),)
    prepared.candidates = (
        Mock(
            reference="booley-sandbox",
            candidate_reference="candidate",
            image_id="sha256:fresh",
            wheel_sha256=None,
        ),
    )
    prepared.prior_tags = ()

    def drive_runtime(project_root, images, *, verbose):
        images.inspect(project_root, verbose=verbose)
        result = images.prepare(project_root, verbose=verbose)
        result = images.commit()
        images.reissue(project_root, result.selected_id, verbose=verbose)
        return result

    with (
        patch.object(
            harness_refresh.init_cmd,
            "inspect_refreshable_runtime_image",
            side_effect=lambda root, *, verbose: (
                events.append(("inspect", root, verbose)) or inspection
            ),
        ),
        patch.object(
            harness_refresh.init_cmd,
            "prepare_runtime_image",
            side_effect=lambda root, *, verbose: (
                events.append(("prepare", root, verbose)) or prepared
            ),
        ),
        patch.object(
            harness_refresh.init_cmd,
            "commit_runtime_image",
            side_effect=lambda value: events.append(("commit", value)) or refreshed,
        ),
        patch.object(
            harness_refresh.init_cmd,
            "reissue_session_spec",
            side_effect=lambda root, image_id, *, verbose: events.append(
                ("reissue", root, image_id, verbose)
            ),
        ),
        patch.object(harness_refresh.runtime_refresh, "refresh", side_effect=drive_runtime),
    ):
        result = harness_refresh.refresh(tmp_path, verbose=True)

    assert result == session_refresh.RefreshImage(
        "booley-sandbox",
        "sha256:fresh",
        "payload-123",
        prepared_images=(
            session_refresh.RefreshPreparedImage(
                "booley-sandbox",
                "candidate",
                "sha256:fresh",
                None,
            ),
        ),
        wheel_source_fingerprint="payload-123",
    )
    assert events == [
        ("inspect", tmp_path, True),
        ("prepare", tmp_path, True),
        ("commit", prepared),
        ("reissue", tmp_path, "sha256:fresh", True),
    ]


def test_harness_runtime_images_requires_prepared_candidates() -> None:
    images = harness_refresh._RuntimeImages()
    with pytest.raises(sr.SessionError, match="were not prepared"):
        images.commit()
    with pytest.raises(sr.SessionError, match="were not prepared"):
        images.validate(_result())


def test_harness_runtime_images_rejects_commit_without_selected_id(monkeypatch) -> None:
    images = harness_refresh._RuntimeImages()
    prepared = Mock()
    prepared.plan.nodes = (Mock(wheel_source_fingerprint="wheel"),)
    prepared.plan.steps = (Mock(action=object()),)
    prepared.candidates = (
        Mock(reference="booley-sandbox", candidate_reference="candidate", image_id="id"),
    )
    prepared.prior_tags = ()
    images._prepared = prepared
    monkeypatch.setattr(
        harness_refresh.init_cmd,
        "commit_runtime_image",
        lambda _prepared: LifecycleResult("booley-sandbox", None, Status.CHANGED),
    )
    with pytest.raises(sr.SessionError, match="immutable Sandbox Image ID"):
        images.commit()


def test_harness_runtime_image_wrappers_delegate(monkeypatch, tmp_path: Path) -> None:
    plan = Mock()
    prepared = Mock()
    result = LifecycleResult("booley-sandbox", "sha256:id", Status.CURRENT)
    monkeypatch.setattr(
        harness_refresh.init_cmd, "inspect_refreshable_runtime_image", lambda *a, **k: result
    )
    monkeypatch.setattr(
        harness_refresh.init_cmd, "prepare_runtime_image", lambda *a, **k: prepared
    )
    prepared.plan.selected_reference = "booley-sandbox"
    prepared.plan.nodes = (Mock(wheel_source_fingerprint="wheel"),)
    prepared.plan.steps = (Mock(action=object()),)
    prepared.candidates = (
        Mock(
            reference="booley-sandbox",
            candidate_reference="candidate",
            image_id="sha256:id",
            wheel_sha256=None,
        ),
    )
    prepared.prior_tags = ()
    monkeypatch.setattr(harness_refresh.init_cmd, "commit_runtime_image", lambda value: value)
    monkeypatch.setattr(harness_refresh.init_cmd, "validate_runtime_image", lambda value: plan)
    monkeypatch.setattr(harness_refresh.init_cmd, "abort_runtime_image", lambda value: plan)
    images = harness_refresh._RuntimeImages()
    assert images.inspect(tmp_path, verbose=True) is None
    assert images.prepare(tmp_path, verbose=False).selected_id == "sha256:id"
    images.validate(_result())
    images.abort()


def test_init_runtime_image_inspection_reports_stale_steps(monkeypatch, tmp_path: Path) -> None:
    from booley.harness import init_cmd
    from booley.runtime.image_lifecycle import PlanAction

    plan = SimpleNamespace(
        selected_reference="booley-sandbox",
        nodes=(SimpleNamespace(wheel_source_fingerprint="wheel"),),
        steps=(
            SimpleNamespace(action=PlanAction.REUSE, reason=SimpleNamespace(code="current")),
            SimpleNamespace(
                action=PlanAction.BUILD,
                role=SimpleNamespace(value="wheel-overlay"),
                reason=SimpleNamespace(code="inputs-changed"),
                reference="booley-sandbox",
            ),
        ),
    )
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", lambda *_args: plan)
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "_docker_adapter",
        lambda: SimpleNamespace(image_id=lambda _reference: "sha256:id"),
    )
    result = init_cmd.inspect_refreshable_runtime_image(tmp_path)
    assert result.status is Status.STALE
    assert result.diagnostics == (plan.steps[1].reason,)
    assert result.wheel_source_fingerprint == "wheel"


def test_init_runtime_image_inspection_translates_plan_errors(monkeypatch, tmp_path: Path) -> None:
    from booley.harness import init_cmd
    from booley.runtime.image_lifecycle import ImageLifecycleError

    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "plan",
        lambda *_args: (_ for _ in ()).throw(ImageLifecycleError("user-managed")),
    )
    with pytest.raises(RuntimeError, match="cannot use managed refresh"):
        init_cmd.inspect_refreshable_runtime_image(tmp_path)


def test_init_runtime_image_prepare_commit_validate_and_abort_wrappers(
    monkeypatch, tmp_path: Path
) -> None:
    from booley.harness import init_cmd

    plan = SimpleNamespace(
        project_root=tmp_path,
        steps=(),
        nodes=(),
    )
    prepared = object()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", lambda *_args: plan)
    monkeypatch.setattr(init_cmd.image_lifecycle, "prepare", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(init_cmd.image_lifecycle, "commit", lambda value, **kwargs: value)
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "validate",
        lambda value, **kwargs: calls.append(("validate", value)),
    )
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "abort",
        lambda value, **kwargs: calls.append(("abort", value)),
    )
    monkeypatch.setattr(init_cmd, "info", lambda message: calls.append(("info", message)))

    assert init_cmd.prepare_runtime_image(tmp_path) is prepared
    assert init_cmd.commit_runtime_image(prepared) is prepared
    init_cmd.validate_runtime_image(prepared)
    init_cmd.abort_runtime_image(prepared)
    assert calls[-2:] == [("validate", prepared), ("abort", prepared)]
