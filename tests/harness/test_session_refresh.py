"""Roll-forward ``booley session refresh``: orchestration, failure states, and rerun convergence.

The runtime half is driven against a stateful fake host (``_Host``) behind the ``sr``
primitives, so a rerun after a failed step sees the state the failure left behind.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from booley.harness import init_cmd
from booley.harness import session_refresh as harness_refresh
from booley.runtime import devcontainer as dc
from booley.runtime import (
    issuance_invalidation,
    runtime_context,
    session_admission,
    session_refresh,
)
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.image_lifecycle import (
    ImageLifecycleError,
    Intent,
    LifecycleResult,
    ProjectImageScope,
    Status,
)
from booley.runtime.sandbox_apply import Attachment
from tests.fake_docker_executable import install_fake_docker
from tests.git_support import repository
from tests.harness import test_session_runtime as resolution_fixtures
from tests.harness.session_docker_fake import SandboxDocker, serve_sandbox_docker
from tests.runtime import test_session_issuance as issuance_fixtures
from tests.runtime.test_sandbox_remedy_workflows import (
    EXTERNAL,
    HEADLESS_RESEED,
    VSCODE_SEED,
)

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
    pending_claim: bool | Exception = False
    # Failure injections.
    check_error: BaseException | None = None
    start_error: Exception | None = None
    leave_candidate_on_start_failure: bool = False
    break_lookup_after_start: bool = False
    lookup_broken: bool = False
    vscode_appears_after_removal: bool = False
    docker: SandboxDocker = field(default_factory=SandboxDocker)
    original_start: Callable[..., str] = sr.start_refreshed_sandbox

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

    def has_pending_claim(self, _project: Path) -> bool:
        if isinstance(self.pending_claim, Exception):
            raise self.pending_claim
        return self.pending_claim

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
        self.lookup_broken = self.break_lookup_after_start
        if self.relay_exists:
            raise sr.SessionError("the old license relay is still present")
        if self.start_error is not None:
            if self.leave_candidate_on_start_failure:
                self.canonical_exists = True
            raise self.start_error
        self.running = _sandbox(self.canonical, "new-id")
        self.container_image = image.selected_id
        self.canonical_exists = True
        return "new-id"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """A stateful fake host over the shared Docker fake; real lifecycle lock and guards."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    fake = _Host(project, docker=serve_sandbox_docker(monkeypatch))
    monkeypatch.setattr(session_admission, "has_pending_claim", fake.has_pending_claim)
    monkeypatch.setattr(sr, "resolve_running_sandbox", fake.resolve_running_sandbox)
    monkeypatch.setattr(sr.idk, "container_exists", fake.container_exists)
    monkeypatch.setattr(sr, "verified_headless_container", fake.verified_headless_container)
    monkeypatch.setattr(sr, "check_refresh_start", fake.check_refresh_start)
    monkeypatch.setattr(sr, "remove_refresh_predecessor", fake.remove_refresh_predecessor)
    monkeypatch.setattr(sr, "remove_refresh_relay", fake.remove_refresh_relay)
    monkeypatch.setattr(sr, "start_refreshed_sandbox", fake.start_refreshed_sandbox)
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
    ]
    images.converge.assert_called_once_with(host.project.resolve(), verbose=True)
    images.prepare_spec.assert_called_once_with(host.project.resolve(), image)
    assert host.running is not None and host.running.container_id == "new-id"
    assert host.docker.calls == []


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
    assert host.running is not None and host.running.container_id == "new-id"

    # A stale spec, or a changed graph, is not kept even when the Sandbox is running.
    for spec_current, graph_changed in ((False, False), (True, True)):
        host.events.clear()
        again = session_refresh.refresh(
            host.project,
            _images(host, _image(graph_changed=graph_changed), spec_current=spec_current),
        )
        assert again.outcome == "replaced"


def test_harness_converge_uses_host_init_engine(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.image_lifecycle import ImageRole

    plan = SimpleNamespace(nodes=(SimpleNamespace(role=ImageRole.PROJECT_OVERLAY),))
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", lambda *_args: plan)
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

    # Private seam: the converged image is observable publicly only once the spec is
    # prepared from a real image graph and a Sandbox runs on it.
    image = harness_refresh._RuntimeImages().converge(host.project, verbose=True)

    reconcile.assert_called_once_with(ProjectImageScope(host.project), Intent.ENSURE, verbose=True)
    assert image == session_refresh.RefreshImage(NEW_IMAGE, "wheel-src", "wheel-sha", True)

    # A lifecycle failure, or a result without an immutable ID to pin the Sandbox to,
    # fails the whole refresh before any spec or Docker change.
    host.run_old_sandbox()
    for failure, cause in (
        (ImageLifecycleError("registry unreachable"), "registry unreachable"),
        (None, "immutable Sandbox Image ID"),
    ):
        reconcile.side_effect = failure
        reconcile.return_value = LifecycleResult("booley-sandbox", None, Status.CHANGED)
        with pytest.raises(sr.SessionError) as caught:
            harness_refresh.refresh(host.project)
        message = str(caught.value)
        assert "session refresh failed while reconciling Sandbox Images" in message
        assert cause in message and UNCHANGED in message and RERUN in message
        assert host.mutations() == [] and host.docker.calls == []
        assert not (host.project / ".devcontainer").exists()

    # An actionable plan error (#1083) reaches the user unchanged, before any image work.
    reconcile.reset_mock()
    plan_error = Mock(side_effect=ImageLifecycleError("user-managed"))
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", plan_error)
    with pytest.raises(ImageLifecycleError, match=r"^user-managed$"):
        harness_refresh.refresh(host.project)
    reconcile.assert_not_called()


@pytest.mark.parametrize(
    ("leave_candidate", "state", "rerun_message"),
    [
        (False, "No Sandbox is running.", "created new-id"),
        (
            True,
            "An unverified Sandbox container {canonical!r} was left in place; "
            "the next refresh verifies or replaces it.",
            "replaced candidate-id → new-id",
        ),
    ],
    ids=["nothing-left", "candidate-left"],
)
def test_start_failure_names_what_is_left_then_rerun_converges(
    host: _Host, leave_candidate: bool, state: str, rerun_message: str
) -> None:
    host.run_old_sandbox()
    host.start_error = sr.SessionError("docker run failed: no such network")
    host.leave_candidate_on_start_failure = leave_candidate

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    # The old Sandbox is not restored: it was replaced and only a candidate may remain.
    assert str(caught.value) == (
        "session refresh failed while starting the new Sandbox: docker run failed: no such "
        f"network. {state.format(canonical=host.canonical)} {RERUN}"
    )
    assert host.running is None and host.canonical_exists is leave_candidate

    host.start_error = None
    stable = _image(graph_changed=False)
    rerun = session_refresh.refresh(host.project, _images(host, stable, spec_current=True))
    # The message names the outcome and both container IDs.
    assert (rerun.image, rerun.message) == (stable, rerun_message)


@pytest.fixture
def project_local_booley_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the harness-wide BOOLEY_DIR so ``issued`` uses its own Project data."""
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.delenv("BOOLEY_DIR")
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
    host.docker.networks.add(dc.EGRESS_NETWORK)
    host.docker.images[prepared.spec["image"]] = {"Id": stamp.image_id}
    assert sr.up(project) == sr.session_container_name(project)
    assert sr.session_container_name(project) in host.docker.containers


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


def test_missing_or_withdrawn_stamp_refreshes(
    project_local_booley_dir, issued_preview, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.eda.provisioning.licensing import flexnet_docker

    project, prepared, _path, stamp = issued_preview
    host.project = project
    host.run_old_sandbox()
    monkeypatch.setattr(
        flexnet_docker, "cleanup_project_resources_for_identity", lambda _r, **_kwargs: ()
    )
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


@pytest.mark.parametrize("sandbox_survives", [False, True])
def test_invalidation_cleanup_failure_reports_the_state_it_left(
    host: _Host, monkeypatch: pytest.MonkeyPatch, sandbox_survives: bool
) -> None:
    """Cleanup of a pending EDA Grant change can remove Sandboxes, so the message looks."""
    if sandbox_survives:
        host.run_old_sandbox()
    monkeypatch.setattr(
        issuance_invalidation,
        "recover_all_locked",
        Mock(side_effect=RuntimeError("could not remove network")),
    )

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert "failed while completing pending EDA Grant invalidations" in message
    assert "could not remove network" in message
    if sandbox_survives:
        assert f"Sandbox {host.canonical!r} is running." in message
    else:
        assert "No Sandbox is running; pending EDA Grant cleanup may have removed it." in message
    assert RERUN in message
    assert host.mutations() == []


@pytest.mark.parametrize(
    ("owner", "refusal"),
    [
        ("vscode", "VS Code owns the active Sandbox 'vscode-owned'; use 'Dev Containers: "),
        ("claim", "VS Code is starting a Sandbox for this Project; wait for it, or run `booley"),
        ("unreadable", "claim store unreadable"),
    ],
)
def test_vscode_ownership_refuses_before_any_change(host: _Host, owner: str, refusal: str) -> None:
    if owner == "vscode":
        host.run_other_sandbox("vscode-owned", vscode=True)
    else:
        host.run_old_sandbox()
        unreadable = session_admission.AdmissionError("claim store unreadable")
        host.pending_claim = True if owner == "claim" else unreadable

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    assert str(caught.value).startswith(refusal)
    assert host.events == []


_EXPORT = "export BOOLEY_GATEWAY_KEY before refreshing"
_NOT_CANONICAL = "is running and is not this Project's canonical headless Sandbox"


@pytest.mark.parametrize(
    ("running", "canonical_exists", "cause", "state"),
    [
        ("canonical", True, _EXPORT, "the previous Sandbox {canonical!r} is still running."),
        (None, True, _EXPORT, "container {canonical!r} was left in place."),
        ("other", True, _NOT_CANONICAL, "Sandbox 'other' is running and was not touched."),
        (
            "{canonical}-pre-refresh",
            False,
            _NOT_CANONICAL,
            "Sandbox '{canonical}-pre-refresh' is running and was not touched.",
        ),
    ],
    ids=["previous-running", "stopped-left", "other-running", "parked-running"],
)
def test_failure_before_removal_reports_the_state_it_left(
    host: _Host, running: str | None, canonical_exists: bool, cause: str, state: str
) -> None:
    """A refused start check, or another running Sandbox, removes nothing."""
    if running == "canonical":
        host.run_old_sandbox()
    elif running is not None:
        # Only resolve_running_sandbox is faked, so the real canonical-Sandbox guard runs.
        host.run_other_sandbox(running.format(canonical=host.canonical))
    host.canonical_exists = canonical_exists
    host.relay_exists = True
    host.check_error = sr.SessionError(_EXPORT)

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert message.startswith("session refresh failed while removing the previous Sandbox: ")
    assert cause in message
    assert "The Sandbox spec was reissued; " + state.format(canonical=host.canonical) in message
    assert message.endswith(RERUN)
    assert host.mutations() == ["reissue"] and host.relay_exists


@pytest.mark.parametrize(
    ("start_error", "failure", "state"),
    [
        (
            sr.SessionError("docker run failed"),
            "starting the new Sandbox: docker run failed",
            "The Sandbox state could not be read: docker ps failed.",
        ),
        (
            None,
            "checking for a VS Code Sandbox: docker ps failed",
            "The new Sandbox new-id is running.",
        ),
    ],
    ids=["start-failed", "started"],
)
def test_state_lookup_failure_keeps_step_and_rerun_advice(
    host: _Host, start_error: Exception | None, failure: str, state: str
) -> None:
    host.run_old_sandbox()
    host.start_error = start_error
    host.break_lookup_after_start = True

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    assert str(caught.value) == f"session refresh failed while {failure}. {state} {RERUN}"
    assert "finalize" not in host.events


@pytest.mark.parametrize(
    ("owner", "steps"),
    [
        (Attachment.HEADLESS, EXTERNAL + HEADLESS_RESEED),
        (Attachment.VSCODE, EXTERNAL + VSCODE_SEED),
    ],
    ids=["headless", "vscode-stopped"],
)
def test_refresh_refuses_external_image_with_owner_steps(
    host: _Host, monkeypatch: pytest.MonkeyPatch, owner: Attachment, steps: str
) -> None:
    from booley.runtime.image_lifecycle import ImageRole

    host.run_old_sandbox()
    plan = SimpleNamespace(nodes=(SimpleNamespace(role=ImageRole.EXTERNAL),))
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", lambda *_args: plan)
    monkeypatch.setattr(sr, "image_is_managed", lambda _root: False)
    monkeypatch.setattr(sr, "sandbox_attachment", lambda _root: owner)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(runtime_spec, "load_issued_snapshot", lambda _root: object())

    with pytest.raises(RuntimeError) as caught:
        harness_refresh.refresh(host.project)

    assert str(caught.value) == (
        "the selected Sandbox Image cannot use managed refresh: it is externally managed; " + steps
    )
    assert host.mutations() == [] and host.docker.calls == []


def test_vscode_appearing_during_start_is_reported_untouched(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.run_old_sandbox()
    host.vscode_appears_after_removal = True
    # The real start refuses while any Sandbox runs, before it makes any Docker call.
    monkeypatch.setattr(sr, "start_refreshed_sandbox", host.original_start)

    with pytest.raises(sr.SessionError) as caught:
        session_refresh.refresh(host.project, _images(host, _image()))

    message = str(caught.value)
    assert "session refresh failed while starting the new Sandbox" in message
    assert "VS Code started Sandbox 'vscode-owned' during refresh; it was not touched." in message
    assert RERUN in message
    assert host.docker.calls == []


def test_harness_refresh_reinitializes_project_after_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = session_refresh.RefreshResult(_image(), "replaced", "old-id", "new-id")
    docker = install_fake_docker(tmp_path, monkeypatch, {"run": (0, "git version 2.50.0")})
    refresh = Mock(return_value=outcome)
    monkeypatch.setattr(harness_refresh.runtime_refresh, "refresh", refresh)
    reinitialize = Mock(return_value=[])
    monkeypatch.setattr(init_cmd, "reinitialize_project", reinitialize)

    assert harness_refresh.refresh(tmp_path, verbose=True) is outcome

    assert refresh.call_args.kwargs == {"verbose": True}
    # The Git version comes from the new Sandbox Image, probed after the replacement.
    assert docker.calls == [["run", "--rm", NEW_IMAGE, "git", "--version"]]
    reinitialize.assert_called_once_with(tmp_path, init_cmd.ProjectFacts((2, 50, 0)))


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
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    cause = "multiple running Sandboxes" if problem == "duplicate" else "identity disagrees"
    with pytest.raises(sr.SessionError, match=cause):
        session_refresh.refresh(workspace, images)
    assert images.method_calls == []
    assert all(call[1] in ("ps", "container") for call in fixture.calls)


@pytest.mark.parametrize("failure", [False, True])
def test_image_gc_deferral_does_not_block_completed_refresh(
    host: _Host, monkeypatch, caplog, failure
):
    from booley.runtime import image_lifecycle

    def cleanup(_project):
        if failure:
            raise image_lifecycle.ImageLifecycleError("inventory unavailable")
        return ("immutable image retained by another Project",)

    # The real cleanup inventories images with `docker image ls`, which no shared fake serves.
    monkeypatch.setattr(image_lifecycle, "cleanup_owned_images", cleanup)
    host.run_old_sandbox()
    result = session_refresh.refresh(host.project, _images(host, _image()))
    assert result.outcome == "replaced"
    assert ("deferred" if failure else "retained by another Project") in caplog.text


@pytest.mark.parametrize(
    ("layout", "excluded"),
    [
        ("plain", ["/.devcontainer", "/.claude"]),
        ("stealth", ["/.devcontainer", "/.claude", "/.booley"]),
        ("relocated", ["/.devcontainer", "/.claude", "/.booley", "/tools/booley"]),
    ],
)
def test_finalization_reconciles_dashboard_task_and_git_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, excluded: list[str]
) -> None:
    from booley.runtime.booley_dir import reset_cache

    project = repository(tmp_path / "project")
    source = project / ".booley"
    stealth = "[stealth]\nenabled = true\n"
    if layout == "relocated":
        (project / "booley.toml").write_text('[project]\ndir = "tools/booley"\n', "utf-8")
        (project / "tools" / "booley").mkdir(parents=True)
        (project / "tools" / "booley" / "booley.toml").write_text(stealth, "utf-8")
        source = tmp_path / "authorized"  # a relocated Project authorizes its data by BOOLEY_DIR
        monkeypatch.setenv("BOOLEY_DIR", str(source))
    else:
        monkeypatch.delenv("BOOLEY_DIR")
    source.mkdir()
    if layout == "stealth":
        (source / "booley.toml").write_text(stealth, "utf-8")
    reset_cache()

    session_refresh.finalize_project(project)

    assert (source / "runtime" / "dashboard-task.json").is_file()
    exclude = (project / ".git" / "info" / "exclude").read_text("utf-8").splitlines()
    assert [line for line in exclude if line.startswith("/")] == ["/.vscode", *excluded]


def test_refresh_prepares_spec_in_host_egress_mode(project_local_booley_dir, issued) -> None:
    from booley.config.host_config import host_config_path

    project, _spec, _path, stamp = issued
    host_config_path().parent.mkdir(parents=True, exist_ok=True)
    host_config_path().write_text('[sandbox]\negress = "unproxied"\n', encoding="utf-8")
    # Private seam: the prepared spec reaches Docker only through a full refresh, which
    # needs a converged image graph and a started Sandbox.
    images = harness_refresh._RuntimeImages()

    images.prepare_spec(project, _image(stamp.image_id))

    spec = images._spec.spec
    networks = [spec["runArgs"][i + 1] for i, a in enumerate(spec["runArgs"]) if a == "--network"]
    assert networks == [dc.UNPROXIED_NETWORK]
    assert spec["containerEnv"]["BOOLEY_EGRESS"] == "unproxied"
    assert not images.spec_is_current(project)
