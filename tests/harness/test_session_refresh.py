"""Roll-forward ``booley session refresh``: orchestration, failure states, and rerun convergence.

The runtime half is driven against a stateful fake host (``_Host``) behind the ``sr``
primitives, so a rerun after a failed step sees the state the failure left behind.
"""

from __future__ import annotations

import copy
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from booley.harness import init_cmd
from booley.harness import session_refresh as harness_refresh
from booley.runtime import issuance_invalidation, session_admission, session_refresh
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.image_lifecycle import (
    ImageLifecycleError,
    Intent,
    LifecycleResult,
    ProjectImageScope,
    Status,
)
from tests.harness import test_session_runtime as resolution_fixtures
from tests.runtime import test_session_issuance as issuance_fixtures

project_sandbox_resolution = resolution_fixtures.project_sandbox_resolution
workspace = resolution_fixtures.workspace
issued = issuance_fixtures.issued
issued_preview = issuance_fixtures.issued_preview
trusted_validator = issuance_fixtures.trusted_validator

OLD_IMAGE = "sha256:old"
NEW_IMAGE = "sha256:new"
UNCHANGED = "The running Sandbox is unchanged."
RERUN = "Run `booley session refresh` again once the cause is fixed."


def _image(
    selected_id: str = NEW_IMAGE, *, graph_changed: bool = True
) -> session_refresh.RefreshImage:
    return session_refresh.RefreshImage(
        selected_id, "sha256:wheel-src", "wheel-sha", graph_changed
    )


def _sandbox(name: str, container_id: str, *, vscode: bool = False) -> session_admission.Sandbox:
    return session_admission.Sandbox(
        container_id,
        name,
        "/project",
        "project-id",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        True,
        vscode,
    )


@dataclass
class _Host:
    """Stateful fake of the Docker and host state that refresh's ``sr`` primitives act on."""

    project: Path
    events: list[str] = field(default_factory=list)
    running: session_admission.Sandbox | None = None
    container_image: str | None = None
    canonical_exists: bool = False
    relay_exists: bool = False
    pending_claim: bool = False
    # Failure injections.
    check_error: BaseException | None = None
    start_error: Exception | None = None
    leave_candidate_on_start_failure: bool = False
    break_lookup_on_start_failure: bool = False
    lookup_broken: bool = False
    vscode_appears_after_removal: bool = False
    real_start: bool = False
    docker_calls: list[list[str]] = field(default_factory=list)
    original_start: Callable[..., str] | None = None

    @property
    def canonical(self) -> str:
        return sr.session_container_name(self.project)

    def run_old_sandbox(self, container_id: str = "old-id", image: str = OLD_IMAGE) -> None:
        self.running = _sandbox(self.canonical, container_id)
        self.container_image = image
        self.canonical_exists = True

    def run_other_sandbox(self, name: str, *, vscode: bool = False) -> None:
        self.running = _sandbox(name, "other-id", vscode=vscode)

    def mutations(self) -> list[str]:
        """Events that changed Docker or host state."""
        return [
            event
            for event in self.events
            if event in {"reissue", "remove_predecessor", "remove_relay", "start", "finalize"}
        ]

    # -- fakes installed over the sr primitives ------------------------------------------

    def resolve_running_sandbox(self, _project: Path):
        if self.lookup_broken:
            raise sr.SessionError("docker ps failed")
        return self.running

    def container_exists(self, name: str) -> bool:
        return name == self.canonical and self.canonical_exists

    def verified_headless_container(self, _project: Path, image: session_refresh.RefreshImage):
        running = self.running
        if (
            running is None
            or running.vscode
            or running.name != self.canonical
            or self.container_image != image.selected_id
        ):
            return None
        return running.container_id

    def check_refresh_start(self, _project: Path):
        self.events.append("check")
        if self.check_error is not None:
            raise self.check_error
        return SimpleNamespace(name=self.canonical)

    def remove_refresh_predecessor(self, _project: Path) -> str | None:
        self.events.append("remove_predecessor")
        removed = None
        if self.canonical_exists:
            removed = "candidate-id" if self.running is None else self.running.container_id
        self.running = None
        self.container_image = None
        self.canonical_exists = False
        if self.vscode_appears_after_removal:
            self.run_other_sandbox("vscode-owned", vscode=True)
        return removed

    def remove_refresh_relay(self, _project: Path) -> None:
        self.events.append("remove_relay")
        self.relay_exists = False

    def start_refreshed_sandbox(self, _project: Path, _request, image) -> str:
        self.events.append("start")
        if self.relay_exists:
            raise sr.SessionError("the old license relay is still present")
        if self.start_error is not None:
            if self.leave_candidate_on_start_failure:
                self.canonical_exists = True
            self.lookup_broken = self.break_lookup_on_start_failure
            raise self.start_error
        self.running = _sandbox(self.canonical, "new-id")
        self.container_image = image.selected_id
        self.canonical_exists = True
        return "new-id"

    def docker(self, argv, **_kwargs):
        self.docker_calls.append(list(argv))
        raise AssertionError(f"unexpected Docker call: {argv}")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """A stateful fake host with the Docker boundary closed; real lifecycle lock and guards."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    fake = _Host(project, original_start=getattr(sr, "start_refreshed_sandbox", None))
    monkeypatch.setattr(sr, "vscode_session_container", lambda _project: None)
    monkeypatch.setattr(
        session_admission, "has_pending_claim", lambda _project: fake.pending_claim
    )
    monkeypatch.setattr(sr, "resolve_running_sandbox", fake.resolve_running_sandbox)
    monkeypatch.setattr(sr.idk, "container_exists", fake.container_exists)
    monkeypatch.setattr(
        sr, "verified_headless_container", fake.verified_headless_container, raising=False
    )
    monkeypatch.setattr(sr, "check_refresh_start", fake.check_refresh_start, raising=False)
    monkeypatch.setattr(
        sr, "remove_refresh_predecessor", fake.remove_refresh_predecessor, raising=False
    )
    monkeypatch.setattr(sr, "remove_refresh_relay", fake.remove_refresh_relay, raising=False)
    monkeypatch.setattr(sr, "start_refreshed_sandbox", fake.start_refreshed_sandbox, raising=False)
    monkeypatch.setattr(sr, "_run", fake.docker)
    monkeypatch.setattr(
        session_refresh, "_cleanup_owned_images", lambda _project: fake.events.append("cleanup")
    )
    return fake


def _images(
    host: _Host, image: session_refresh.RefreshImage, *, spec_current: bool = False
) -> Mock:
    """Fake image operations that record their order next to the host's events."""
    images = Mock(spec=session_refresh.RuntimeImageOperations)

    def record(name: str, result=None):
        def call(*_args, **_kwargs):
            host.events.append(name)
            return result

        return call

    images.inspect.side_effect = record("inspect")
    images.converge.side_effect = record("converge", image)
    images.prepare_spec.side_effect = record("prepare_spec")
    images.spec_is_current.side_effect = record("spec_is_current", spec_current)
    images.reissue.side_effect = record("reissue")
    images.finalize.side_effect = record("finalize")
    return images


def test_changed_image_replaces_running_sandbox(host: _Host) -> None:
    host.run_old_sandbox()
    image = _image()
    images = _images(host, image)

    result = session_refresh.refresh(host.project, images, verbose=True)

    assert result == session_refresh.RefreshResult(image, "replaced", "old-id", "new-id")
    assert result.message == "replaced old-id → new-id"
    # The spec is reissued and proven startable before the old Sandbox is removed.
    assert host.events == [
        "inspect",
        "converge",
        "prepare_spec",
        "reissue",
        "check",
        "remove_predecessor",
        "remove_relay",
        "start",
        "finalize",
        "cleanup",
    ]
    images.converge.assert_called_once_with(host.project.resolve(), verbose=True)
    images.prepare_spec.assert_called_once_with(host.project.resolve(), image)
    assert host.running is not None and host.running.container_id == "new-id"
    assert host.docker_calls == []


def test_absent_sandbox_is_created(host: _Host) -> None:
    image = _image()

    result = session_refresh.refresh(host.project, _images(host, image))

    assert result == session_refresh.RefreshResult(image, "created", None, "new-id")
    assert result.message == "created new-id"
    assert "remove_predecessor" in host.events and "start" in host.events


def test_rerun_after_success_is_kept_without_docker_mutation(host: _Host) -> None:
    host.run_old_sandbox()
    first = session_refresh.refresh(host.project, _images(host, _image()))
    assert first.outcome == "replaced"
    host.events.clear()

    # The rerun finds the graph unchanged and the spec current: nothing to replace.
    stable = _image(graph_changed=False)
    second = session_refresh.refresh(host.project, _images(host, stable, spec_current=True))

    assert second == session_refresh.RefreshResult(stable, "kept", "new-id", "new-id")
    assert second.message == "kept: already current"
    assert host.mutations() == ["finalize"]
    assert "cleanup" not in host.events
    assert host.running is not None and host.running.container_id == "new-id"

    # A stale spec, or a changed graph, is not kept even when the Sandbox is running.
    for spec_current, graph_changed in ((False, False), (True, True)):
        host.events.clear()
        again = session_refresh.refresh(
            host.project,
            _images(host, _image(graph_changed=graph_changed), spec_current=spec_current),
        )
        assert again.outcome == "replaced"


def test_image_failure_leaves_sandbox_and_tags_unchanged(host: _Host) -> None:
    host.run_old_sandbox()
    images = _images(host, _image())
    images.converge.side_effect = ImageLifecycleError("candidate build failed")

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, images)

    message = str(caught.value)
    assert "session refresh failed while reconciling Sandbox Images" in message
    assert "candidate build failed" in message
    assert UNCHANGED in message
    assert RERUN in message
    assert host.mutations() == []
    assert host.running is not None and host.running.container_id == "old-id"


def test_harness_converge_uses_host_init_engine(
    host: _Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reconciled = LifecycleResult(
        "booley-sandbox",
        NEW_IMAGE,
        Status.CHANGED,
        changed_images=("booley-sandbox",),
        payload_fingerprint="payload-123",
        wheel_source_fingerprint="wheel-src",
        wheel_sha256="wheel-sha",
    )
    reconcile = Mock(return_value=reconciled)
    monkeypatch.setattr(harness_refresh.image_lifecycle, "reconcile_planned", reconcile)
    monkeypatch.setattr(init_cmd, "_report_project_requirements", lambda *_a, **_k: None)
    spec_calls = Mock(side_effect=AssertionError("no spec work before images converge"))
    monkeypatch.setattr(init_cmd, "_current_interactive_spec_sources", spec_calls)
    monkeypatch.setattr(harness_refresh.issuance, "preview", spec_calls)

    image = harness_refresh._RuntimeImages().converge(tmp_path, verbose=True)

    reconcile.assert_called_once_with(ProjectImageScope(tmp_path), Intent.ENSURE, verbose=True)
    assert image == session_refresh.RefreshImage(NEW_IMAGE, "wheel-src", "wheel-sha", True)

    # A lifecycle failure propagates out of the whole refresh before any spec or Docker call.
    host.run_old_sandbox()
    reconcile.side_effect = ImageLifecycleError("registry unreachable")
    images = harness_refresh._RuntimeImages()
    monkeypatch.setattr(images, "inspect", lambda _project: None)
    with pytest.raises(sr.SessionError, match="registry unreachable") as caught:
        session_refresh.refresh(host.project, images)
    assert isinstance(caught.value.__cause__, ImageLifecycleError)
    spec_calls.assert_not_called()
    assert host.mutations() == [] and host.docker_calls == []

    # Without an immutable ID there is nothing to pin the Sandbox to.
    reconcile.side_effect = None
    reconcile.return_value = LifecycleResult("booley-sandbox", None, Status.CHANGED)
    with pytest.raises(sr.SessionError, match="immutable Sandbox Image ID"):
        harness_refresh._RuntimeImages().converge(tmp_path, verbose=False)


def test_start_failure_names_step_and_no_sandbox_then_rerun_creates(host: _Host) -> None:
    host.run_old_sandbox()
    host.start_error = sr.SessionError("docker run failed: no such network")

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert "session refresh failed while starting the new Sandbox" in message
    assert "docker run failed: no such network" in message
    assert "No Sandbox is running." in message
    assert RERUN in message
    # The old Sandbox is not restored: it was replaced and nothing runs now.
    assert host.running is None and not host.canonical_exists

    host.start_error = None
    host.events.clear()
    rerun = session_refresh.refresh(
        host.project, _images(host, _image(graph_changed=False), spec_current=True)
    )
    assert rerun.outcome == "created" and rerun.previous_container_id is None
    assert rerun.container_id == "new-id"


def test_start_failure_leaves_unverified_container_named(host: _Host) -> None:
    host.run_old_sandbox()
    host.start_error = sr.SessionError("verification failed")
    host.leave_candidate_on_start_failure = True

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert f"An unverified Sandbox container {host.canonical!r} was left in place" in message
    assert "the next refresh verifies or replaces it" in message
    assert "No Sandbox is running." not in message

    # The rerun removes the leftover candidate by ID and starts a fresh Sandbox.
    host.start_error = None
    rerun = session_refresh.refresh(
        host.project, _images(host, _image(graph_changed=False), spec_current=True)
    )
    assert rerun == session_refresh.RefreshResult(
        _image(graph_changed=False), "replaced", "candidate-id", "new-id"
    )


@pytest.fixture
def project_local_booley_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the harness-wide BOOLEY_DIR so ``issued`` uses its own Project data."""
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()


def _reissue_with(prepared: runtime_spec.PreparedSessionSpec, project: Path) -> Callable:
    """A ``reissue`` that issues the real spec and stamp, as the harness does."""

    def reissue(*_args, **_kwargs) -> None:
        runtime_spec.issue_prepared(project, prepared, reconcile_dashboard=False)

    return reissue


def test_start_failure_keeps_reissued_spec_for_session_up(
    project_local_booley_dir, issued_preview, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, prepared, _path, stamp = issued_preview
    host.project = project
    runtime_spec.invalidate_project(str(project))  # the previous stamp is gone
    images = _images(host, _image(stamp.image_id))
    images.reissue.side_effect = _reissue_with(prepared, project)
    host.start_error = sr.SessionError("docker run failed")

    with pytest.raises(sr.SessionError, match="starting the new Sandbox"):
        session_refresh.refresh(project, images)

    # No journal and no park: the reissued spec is a normal Sandbox spec again.
    assert runtime_spec.stamp_path(project).exists()
    assert sr.validate(project)
    assert sr.status(project) == "absent"
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    with (
        patch.object(sr, "_run", docker),
        patch.object(sr.idk, "network_exists", return_value=True),
        patch.object(sr.idk, "image_exists", return_value=True),
        patch.object(sr.idk, "inspect_image_metadata", return_value=None),
        patch.object(sr.idk, "image_id", return_value=None),
        patch.object(sr.idk, "container_running", return_value=False),
        patch.object(sr, "_strict_running_interactive_states", return_value=[]),
        patch.object(sr, "_strict_all_interactive_states", return_value=[]),
    ):
        assert sr.up(project) == sr.session_container_name(project)
    assert any(call.args[0][:2] == ["docker", "run"] for call in docker.call_args_list)


def test_interruption_after_reissue_keeps_old_sandbox_then_rerun_replaces(host: _Host) -> None:
    host.run_old_sandbox()
    host.check_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        session_refresh.refresh(host.project, _images(host, _image()))

    # Interrupted after the reissue and before any removal: the old Sandbox still runs.
    assert "reissue" in host.events
    assert "remove_predecessor" not in host.events and "start" not in host.events
    assert host.running is not None and host.running.container_id == "old-id"

    # The rerun sees an unchanged graph and a current spec, but the old Sandbox is on the
    # old image, so it is replaced rather than kept.
    host.check_error = None
    host.events.clear()
    rerun = session_refresh.refresh(
        host.project, _images(host, _image(graph_changed=False), spec_current=True)
    )
    assert rerun.outcome == "replaced"
    assert (rerun.previous_container_id, rerun.container_id) == ("old-id", "new-id")


def test_missing_gateway_export_fails_before_removal(host: _Host) -> None:
    host.run_old_sandbox()
    host.check_error = sr.SessionError("export BOOLEY_GATEWAY_KEY before refreshing")

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert "session refresh failed while removing the previous Sandbox" in message
    assert "export BOOLEY_GATEWAY_KEY" in message
    assert "The Sandbox spec was reissued" in message
    assert f"the previous Sandbox {host.canonical!r} was left in place" in message
    assert "remove_predecessor" not in host.events and "remove_relay" not in host.events
    assert host.running is not None and host.running.container_id == "old-id"


def test_missing_or_withdrawn_stamp_refreshes(
    project_local_booley_dir, issued_preview, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.eda.provisioning.licensing import flexnet_docker

    project, prepared, _path, stamp = issued_preview
    host.project = project
    host.run_old_sandbox()
    monkeypatch.setattr(flexnet_docker, "cleanup_project_resources_for_identity", lambda _r: ())
    root = str(project.resolve())

    for case in ("no stamp", "withdrawn by an EDA Grant change"):
        if case == "no stamp":
            runtime_spec.stamp_path(project).unlink()
        else:
            # A revocation prepared its journal and withdrew the stamp, then stopped.
            issuance_invalidation.prepare(root)
            issuance_invalidation.invalidate(root)
            assert issuance_invalidation.has_pending(project)
        assert not runtime_spec.stamp_path(project).exists(), case
        images = _images(host, _image(stamp.image_id))
        images.reissue.side_effect = _reissue_with(prepared, project)

        # Refresh completes the pending invalidation itself and carries on to replace.
        result = session_refresh.refresh(project, images)

        assert result.outcome == "replaced", case
        assert runtime_spec.stamp_path(project).exists(), case
        assert not issuance_invalidation.has_pending(project), case
        host.run_old_sandbox()


def test_licensed_sandbox_refreshes_with_relay_replaced(host: _Host) -> None:
    host.run_old_sandbox()
    host.relay_exists = True  # start refuses while the old relay is present

    result = session_refresh.refresh(host.project, _images(host, _image()))

    assert result.outcome == "replaced"
    assert host.events.index("remove_relay") < host.events.index("start")
    assert not host.relay_exists


def test_vscode_owner_is_rejected_before_image_work(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sr, "vscode_session_container", lambda _project: "vscode-owned")
    images = _images(host, _image())

    with pytest.raises(sr.SessionError, match="VS Code owns the active Sandbox 'vscode-owned'"):
        session_refresh.refresh(host.project, images)

    assert host.events == []


def test_pending_vscode_start_claim_refuses_before_any_change(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.run_old_sandbox()
    host.pending_claim = True
    with pytest.raises(sr.SessionError, match="VS Code is starting a Sandbox") as caught:
        session_refresh.refresh(host.project, _images(host, _image()))
    assert "booley session down" in str(caught.value)
    assert host.events == []

    # An unreadable claim store refuses the same way.
    def unreadable(_project: Path) -> bool:
        raise session_admission.AdmissionError("claim store unreadable")

    monkeypatch.setattr(session_admission, "has_pending_claim", unreadable)
    with pytest.raises(sr.SessionError, match="claim store unreadable"):
        session_refresh.refresh(host.project, _images(host, _image()))
    assert host.events == []


def test_other_running_project_sandbox_blocks_relay_removal(host: _Host) -> None:
    # Only resolve_running_sandbox is faked, so the real canonical-Sandbox guard runs.
    cases = (
        # canonical present, but a non-canonical Sandbox runs
        ("a-different-sandbox", True),
        # canonical absent, but a parked `-pre-refresh` container runs
        (f"{sr.session_container_name(host.project)}-pre-refresh", False),
    )
    for name, canonical_exists in cases:
        host.events.clear()
        host.canonical_exists = canonical_exists
        host.relay_exists = True
        host.run_other_sandbox(name)
        host.canonical_exists = canonical_exists

        with pytest.raises(sr.SessionError) as caught:
            session_refresh.refresh(host.project, _images(host, _image()))

        message = str(caught.value)
        assert "session refresh failed while removing the previous Sandbox" in message
        assert "The Sandbox spec was reissued" in message
        assert f"Sandbox {name!r} is running and was not touched" in message
        assert "remove_predecessor" not in host.events
        assert "remove_relay" not in host.events
        assert "start" not in host.events
        assert host.relay_exists


def test_start_state_lookup_failure_keeps_step_and_rerun_advice(host: _Host) -> None:
    host.run_old_sandbox()
    host.start_error = sr.SessionError("docker run failed")
    host.break_lookup_on_start_failure = True

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert "session refresh failed while starting the new Sandbox: docker run failed" in message
    assert "The Sandbox state could not be read: docker ps failed." in message
    assert message.endswith(RERUN)


def test_refresh_refuses_external_image_with_unchanged_message(
    host: _Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.runtime.image_lifecycle import ImageRole

    host.run_old_sandbox()
    plan = SimpleNamespace(nodes=(SimpleNamespace(role=ImageRole.EXTERNAL),))
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", lambda *_args: plan)
    converge = Mock(side_effect=AssertionError("external images are never converged"))
    monkeypatch.setattr(harness_refresh._RuntimeImages, "converge", converge)

    with pytest.raises(RuntimeError) as caught:
        session_refresh.refresh(host.project, harness_refresh._RuntimeImages())

    assert str(caught.value) == (
        "the selected Sandbox Image cannot use managed refresh: it is externally managed. "
        "Rebuild/pull it explicitly, then on the host run `booley session down`, "
        "`booley init --seed`, and `booley session up`."
    )
    assert "booley session refresh" not in str(caught.value)
    converge.assert_not_called()
    assert host.mutations() == []


def test_vscode_appearing_during_start_is_reported_untouched(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.run_old_sandbox()
    host.vscode_appears_after_removal = True
    # The real start refuses while any Sandbox runs, before it makes any Docker call.
    assert host.original_start is not None
    monkeypatch.setattr(sr, "start_refreshed_sandbox", host.original_start)

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert "session refresh failed while starting the new Sandbox" in message
    assert "VS Code started Sandbox 'vscode-owned' during refresh; it was not touched." in message
    assert RERUN in message
    assert host.docker_calls == []


def test_refresh_writes_no_journal(host: _Host, tmp_path: Path) -> None:
    config = tmp_path / "config"

    def refresh_files() -> set[str]:
        return {
            path.relative_to(tmp_path).as_posix()
            for path in tmp_path.rglob("*")
            if "refresh" in path.name.lower()
        }

    before = refresh_files()
    host.run_old_sandbox()
    session_refresh.refresh(host.project, _images(host, _image()))
    host.start_error = sr.SessionError("docker run failed")
    with pytest.raises(sr.SessionError):
        session_refresh.refresh(host.project, _images(host, _image(NEW_IMAGE + "2")))

    assert refresh_files() == before
    assert not (config / "eda" / "session-refresh").exists()


def test_harness_refresh_reinitializes_project_after_replacement(tmp_path: Path) -> None:
    outcome = session_refresh.RefreshResult(_image(), "replaced", "old-id", "new-id")
    events: list[tuple[object, ...]] = []

    def drive(root: Path, images: object, *, verbose: bool) -> session_refresh.RefreshResult:
        assert isinstance(images, harness_refresh._RuntimeImages)
        events.append(("refresh", root, verbose))
        return outcome

    with (
        patch.object(harness_refresh.runtime_refresh, "refresh", side_effect=drive),
        patch.object(
            init_cmd,
            "_sandbox_git_version",
            side_effect=lambda image_id: events.append(("git-version", image_id)) or (2, 50, 0),
        ),
        patch.object(
            init_cmd,
            "reinitialize_project",
            side_effect=lambda root, facts: events.append(("reinitialize", root, facts)) or [],
        ),
    ):
        assert harness_refresh.refresh(tmp_path, verbose=True) is outcome

    assert events == [
        ("refresh", tmp_path, True),
        ("git-version", NEW_IMAGE),
        ("reinitialize", tmp_path, init_cmd.ProjectFacts((2, 50, 0))),
    ]


def test_init_runtime_image_inspection_preserves_actionable_plan_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "plan",
        lambda *_args: (_ for _ in ()).throw(ImageLifecycleError("user-managed")),
    )
    with pytest.raises(RuntimeError, match="user-managed"):
        init_cmd.inspect_refreshable_runtime_image(tmp_path)


@pytest.mark.parametrize("problem", ["duplicate", "unknown"])
def test_refresh_project_sandbox_resolution_refuses_ambiguous_inventory(
    workspace, project_sandbox_resolution, problem
):
    fixture = project_sandbox_resolution
    if problem == "duplicate":
        second = copy.deepcopy(fixture.state)
        second["Id"], second["Name"] = "b" * 64, "/headless"
        second["Config"]["Labels"].pop("devcontainer.local_folder")
        fixture.documents[second["Id"]] = second
    else:
        fixture.state["Config"]["Labels"].pop("booley.project-id")
    cause = "multiple running Sandboxes" if problem == "duplicate" else "identity disagrees"
    with pytest.raises(sr.SessionError, match=cause):
        session_refresh._reject_existing_vscode(workspace)
    with pytest.raises(sr.SessionError, match=cause):
        session_refresh._reject_vscode_started(workspace)
    assert all(call[1] in ("ps", "container") for call in fixture.calls)


@pytest.mark.parametrize("failure", [False, True])
def test_image_gc_deferral_does_not_block_completed_runtime(
    tmp_path, monkeypatch, caplog, failure
):
    from booley.runtime import image_lifecycle

    def cleanup(_project):
        if failure:
            raise image_lifecycle.ImageLifecycleError("inventory unavailable")
        return ("immutable image retained by another Project",)

    monkeypatch.setattr(image_lifecycle, "cleanup_owned_images", cleanup)
    session_refresh._cleanup_owned_images(tmp_path)
    assert ("deferred" if failure else "retained by another Project") in caplog.text


@pytest.mark.parametrize("stealth", [False, True])
def test_finalization_uses_authorized_dashboard_source_and_exclusions(
    tmp_path, monkeypatch, stealth
):
    from booley import commit_policy
    from booley.runtime import dashboard_tasks
    from booley.runtime import git as runtime_git

    source = tmp_path / "authorized-data"
    calls = []
    monkeypatch.setattr(runtime_spec, "authorized_project_data_source", lambda _root: source)
    monkeypatch.setattr(commit_policy, "stealth_enabled", lambda _root: stealth)
    monkeypatch.setattr(
        dashboard_tasks,
        "reconcile",
        lambda project, directory: calls.append(("dashboard", project, directory)),
    )
    monkeypatch.setattr(
        runtime_git,
        "add_git_excludes",
        lambda project, patterns: calls.append(("excludes", project, patterns)),
    )
    session_refresh.finalize_project(tmp_path)
    expected = (".devcontainer", ".claude", ".booley") if stealth else (".devcontainer", ".claude")
    assert calls == [("dashboard", tmp_path, source), ("excludes", tmp_path, expected)]


def test_finalization_excludes_relocated_booley_dir_in_stealth(tmp_path, monkeypatch):
    from booley import commit_policy
    from booley.runtime import dashboard_tasks
    from booley.runtime import git as runtime_git
    from booley.runtime.booley_dir import reset_cache

    (tmp_path / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    (tmp_path / "tools/booley").mkdir(parents=True)
    reset_cache()
    excluded: list[tuple[str, ...]] = []
    monkeypatch.setattr(runtime_spec, "authorized_project_data_source", lambda _root: tmp_path)
    monkeypatch.setattr(commit_policy, "stealth_enabled", lambda _root: True)
    monkeypatch.setattr(dashboard_tasks, "reconcile", lambda _project, _directory: None)
    monkeypatch.setattr(
        runtime_git, "add_git_excludes", lambda _project, names: excluded.append(tuple(names))
    )

    session_refresh.finalize_project(tmp_path)

    assert excluded == [(".devcontainer", ".claude", ".booley", "tools/booley")]
    reset_cache()


def test_refresh_prepares_spec_in_host_egress_mode(project_local_booley_dir, issued) -> None:
    from booley.config.host_config import host_config_path
    from booley.runtime import devcontainer as dc

    project, _spec, _path, stamp = issued
    host_config_path().parent.mkdir(parents=True, exist_ok=True)
    host_config_path().write_text('[sandbox]\negress = "unproxied"\n', encoding="utf-8")
    images = harness_refresh._RuntimeImages()

    images.prepare_spec(project, _image(stamp.image_id))

    spec = images._spec.spec
    networks = [spec["runArgs"][i + 1] for i, a in enumerate(spec["runArgs"]) if a == "--network"]
    assert networks == [dc.UNPROXIED_NETWORK]
    assert spec["containerEnv"]["BOOLEY_EGRESS"] == "unproxied"
    assert not images.spec_is_current(project)
