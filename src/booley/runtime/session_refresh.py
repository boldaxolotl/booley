"""Roll-forward Sandbox Image reconciliation and Sandbox replacement.

Refresh converges the Project's Sandbox Images, reissues the Sandbox spec, then replaces
the headless Sandbox. It keeps no journal and never restores the previous Sandbox: a
failed step names itself and what is left running, and running `booley session refresh`
again finishes the work from whatever state the failure left behind.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from booley.runtime import interactive_docker as idk
from booley.runtime import issuance_invalidation
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.docker_capacity import DockerCapacityError
from booley.runtime.lifecycle_lock import host_lifecycle_lock

_T = TypeVar("_T")
_UNCHANGED = "The running Sandbox is unchanged."
_RERUN = "Run `booley session refresh` again once the cause is fixed."


@dataclass(frozen=True, slots=True)
class RefreshImage:
    """Immutable Sandbox Image facts the replacement Sandbox must match."""

    selected_id: str
    wheel_source_fingerprint: str | None
    wheel_sha256: str | None
    graph_changed: bool


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Verified outcome, identified by immutable container IDs."""

    image: RefreshImage
    outcome: str
    previous_container_id: str | None
    container_id: str

    @property
    def message(self) -> str:
        if self.outcome == "kept":
            return "kept: already current"
        if self.outcome == "created":
            return f"created {self.container_id}"
        return f"replaced {self.previous_container_id} → {self.container_id}"


class RuntimeImageOperations(Protocol):
    """Image and spec operations composed by the Project Initialization caller."""

    def inspect(self, project_root: Path) -> None: ...

    def converge(self, project_root: Path, *, verbose: bool) -> RefreshImage: ...

    def prepare_spec(self, project_root: Path, image: RefreshImage) -> None: ...

    def spec_is_current(self, project_root: Path) -> bool: ...

    def reissue(self, project_root: Path) -> None: ...

    def finalize(self, project_root: Path) -> None: ...


def _step(name: str, state: str | Callable[[], str], call: Callable[[], _T]) -> _T:
    """Run one refresh step; a failure names the step and what the Sandbox looks like now.

    ``state`` may be a callable so it describes the Docker state after the failure.
    ``KeyboardInterrupt`` is not caught: a rerun converges from wherever it stopped.
    """
    try:
        return call()
    except DockerCapacityError:
        raise  # a capacity refusal changes nothing; the CLI reports it as `image-capacity`
    except (RuntimeError, OSError) as exc:
        detail = state() if callable(state) else state
        raise sr.SessionError(
            f"session refresh failed while {name}: {exc}. {detail} {_RERUN}"
        ) from exc


def _reject_existing_vscode(project: Path) -> None:
    """Refuse before any change when VS Code owns, or is about to start, the Sandbox."""
    from booley.runtime import session_admission

    vscode = sr.vscode_session_container(project)
    if vscode:
        raise sr.SessionError(
            f"VS Code owns the active Sandbox {vscode!r}; use "
            "'Dev Containers: Rebuild Container' so the editor can replace it safely"
        )
    try:
        pending = session_admission.has_pending_claim(project)
    except session_admission.AdmissionError as exc:
        raise sr.SessionError(str(exc)) from exc
    if pending:
        raise sr.SessionError(
            "VS Code is starting a Sandbox for this Project; wait for it, or run "
            "`booley session down` to clear a stale start claim"
        )


def _reject_vscode_started(project: Path) -> None:
    vscode = sr.vscode_session_container(project)
    if vscode:
        raise sr.SessionError(f"VS Code started Sandbox {vscode!r} during refresh")


def _state_after(project: Path, describe: Callable[[sr.Sandbox | None, str], str]) -> str:
    """Describe the Project's Sandboxes after a failure; a failed lookup says so."""
    try:
        return describe(sr.resolve_running_sandbox(project), sr.session_container_name(project))
    except sr.SessionError as exc:
        return f"The Sandbox state could not be read: {exc}."


def _replacement_state(project: Path) -> str:
    """State after a failure between reissuing the spec and removing the old Sandbox."""

    def describe(running: sr.Sandbox | None, canonical: str) -> str:
        prefix = "The Sandbox spec was reissued; "
        if running is not None and (running.vscode or running.name != canonical):
            return f"{prefix}Sandbox {running.name!r} is running and was not touched."
        if idk.container_exists(canonical):
            return f"{prefix}the previous Sandbox {canonical!r} was left in place."
        return f"{prefix}no Sandbox is running."

    return _state_after(project, describe)


def _start_state(project: Path) -> str:
    """State after a failure while starting the new Sandbox."""

    def describe(running: sr.Sandbox | None, canonical: str) -> str:
        if running is not None and running.vscode:
            return f"VS Code started Sandbox {running.name!r} during refresh; it was not touched."
        if running is not None and running.name != canonical:
            return f"Sandbox {running.name!r} is running and was not touched."
        if idk.container_exists(canonical):
            return (
                f"An unverified Sandbox container {canonical!r} was left in place; "
                "the next refresh verifies or replaces it."
            )
        return "No Sandbox is running."

    return _state_after(project, describe)


def _replace_sandbox(project: Path) -> tuple[sr.UpRequest, str | None]:
    """Remove the old headless Sandbox once the reissued spec is proven startable."""
    _reject_vscode_started(project)
    sr.require_only_canonical_sandbox(project)
    request = sr.check_refresh_start(project)
    return request, sr.remove_refresh_predecessor(project)


def _complete_invalidations() -> None:
    issuance_invalidation.recover_all_locked()


def refresh(
    project_root: Path,
    images: RuntimeImageOperations,
    *,
    verbose: bool = False,
) -> RefreshResult:
    """Converge the selected image, then replace the headless Sandbox or keep a current one."""
    project = project_root.resolve(strict=True)
    with host_lifecycle_lock("session refresh"):
        # A withdrawn stamp is what refresh repairs, so it completes pending invalidations
        # and continues instead of asking for a rerun.
        _step("completing pending EDA Grant invalidations", _UNCHANGED, _complete_invalidations)
        _reject_existing_vscode(project)
        images.inspect(project)
        image = _step(
            "reconciling Sandbox Images",
            _UNCHANGED,
            lambda: images.converge(project, verbose=verbose),
        )
        _step(
            "preparing the Sandbox spec", _UNCHANGED, lambda: images.prepare_spec(project, image)
        )
        current = _step(
            "checking the running Sandbox",
            _UNCHANGED,
            lambda: _current_container(project, image, images),
        )
        if current is not None:
            _step("reconciling Project files", _UNCHANGED, lambda: images.finalize(project))
            return RefreshResult(image, "kept", current, current)
        _step("reissuing the Sandbox spec", _UNCHANGED, lambda: images.reissue(project))
        request, previous = _step(
            "removing the previous Sandbox",
            lambda: _replacement_state(project),
            lambda: _replace_sandbox(project),
        )
        _step(
            "removing the license relay",
            "No Sandbox is running.",
            lambda: sr.remove_refresh_relay(project),
        )
        container_id = _step(
            "starting the new Sandbox",
            lambda: _start_state(project),
            lambda: sr.start_refreshed_sandbox(project, request, image),
        )
        running = f"The new Sandbox {container_id} is running."
        _step("checking for a VS Code Sandbox", running, lambda: _reject_vscode_started(project))
        _step("reconciling Project files", running, lambda: images.finalize(project))
        _cleanup_owned_images(project)
    return RefreshResult(image, "replaced" if previous else "created", previous, container_id)


def _current_container(
    project: Path, image: RefreshImage, images: RuntimeImageOperations
) -> str | None:
    """The running Sandbox's ID when nothing needs replacing; mutates nothing."""
    if image.graph_changed or not images.spec_is_current(project):
        return None
    return sr.verified_headless_container(project, image)


def finalize_project(project: Path) -> None:
    """Reconcile the ancillary files (excludes, Dashboard task); safe to repeat."""
    from booley.commit_policy import stealth_enabled
    from booley.runtime import dashboard_tasks
    from booley.runtime.git import add_git_excludes, stealth_exclude_names

    source = runtime_spec.authorized_project_data_source(project)
    dashboard_tasks.reconcile(project, source)
    names = (".devcontainer", ".claude")
    add_git_excludes(
        project, (*names, *stealth_exclude_names(project)) if stealth_enabled(project) else names
    )


def _cleanup_owned_images(project: Path) -> None:
    from booley.runtime import image_lifecycle

    try:
        retained_images = image_lifecycle.cleanup_owned_images(project)
    except image_lifecycle.ImageLifecycleError as exc:
        sr.logger.warning("refresh image cleanup deferred: %s", exc)
        return
    for retained in retained_images:
        sr.logger.warning("refresh image cleanup: %s", retained)
