"""Project Initialization composition for Sandbox refresh."""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.harness import image_lifecycle, init_cmd
from booley.runtime import session_issuance as issuance
from booley.runtime import session_refresh as runtime_refresh
from booley.runtime.image_lifecycle import Intent, ProjectImageScope
from booley.runtime.lifecycle_lock import LifecycleLockError, host_lifecycle_lock
from booley.runtime.session_runtime import SessionError


class _RuntimeImages:
    def __init__(self) -> None:
        self._spec: issuance.PreparedSessionSpec | None = None
        self._sources: init_cmd._InteractiveSpecSources | None = None
        self._image_id: str | None = None

    def inspect(self, project_root: Path) -> None:
        init_cmd.inspect_refreshable_runtime_image(project_root)

    def converge(self, project_root: Path, *, verbose: bool) -> runtime_refresh.RefreshImage:
        """Converge the image graph with host init's engine: tags change only on success."""
        init_cmd._report_project_requirements(project_root, query_base_image=True)
        result = image_lifecycle.reconcile_planned(
            ProjectImageScope(project_root), Intent.ENSURE, verbose=verbose
        )
        if result.selected_id is None:
            raise SessionError("image refresh did not return an immutable Sandbox Image ID")
        return runtime_refresh.RefreshImage(
            result.selected_id,
            result.wheel_source_fingerprint or result.payload_fingerprint,
            result.wheel_sha256,
            bool(result.changed_images),
        )

    def _current_sources(
        self, project_root: Path, image_id: str | None
    ) -> init_cmd._InteractiveSpecSources:
        return init_cmd._current_interactive_spec_sources(project_root, image_id)

    def prepare_spec(self, project_root: Path, image: runtime_refresh.RefreshImage) -> None:
        self._image_id = image.selected_id
        self._sources = self._current_sources(project_root, image.selected_id)
        self._spec = issuance.preview(
            project_root, self._sources.build, expected_image_id=image.selected_id
        )

    def spec_is_current(self, project_root: Path) -> bool:
        assert self._spec is not None
        try:
            issuance.inspect_prepared(project_root, self._spec)
        except issuance.RuntimeSpecError:
            return False
        return True

    def finalize(self, project_root: Path) -> None:
        runtime_refresh.finalize_project(project_root)

    def reissue(self, project_root: Path) -> None:
        assert self._spec is not None and self._sources is not None
        if self._sources.mask_paths:
            init_cmd._mask_source_dir().mkdir(parents=True, exist_ok=True)
        issuance.issue_prepared(
            project_root,
            self._spec,
            current_builder=lambda: self._current_sources(project_root, self._image_id).build,
            reconcile_dashboard=False,
        )


def _initialize_refreshed_project(
    project_root: Path, result: runtime_refresh.RefreshResult
) -> None:
    """Re-apply Project Initialization after the Sandbox replacement has settled.

    A failure keeps the Sandbox and fails the command with the container outcome in the
    message. Tracked files are never rewritten.
    """
    prefix = f"Sandbox refreshed ({result.message}), but Project Initialization failed"
    try:
        facts = init_cmd.ProjectFacts(init_cmd._sandbox_git_version(result.image.selected_id))
        with host_lifecycle_lock("session refresh"):
            outcomes = init_cmd.reinitialize_project(project_root, facts)
    except (OSError, subprocess.SubprocessError, ValueError, LifecycleLockError) as exc:
        raise SessionError(f"{prefix}: {exc}; run `booley init` on the host") from exc
    failed = [f"{step.name}: {step.detail}" for step in outcomes if step.status == "err"]
    if failed:
        raise SessionError(f"{prefix}: {'; '.join(failed)}; run `booley init` on the host")


def refresh(
    project_root: Path,
    *,
    verbose: bool = False,
) -> runtime_refresh.RefreshResult:
    """Refresh the selected image and replace its Sandbox, then re-apply Project files."""
    result = runtime_refresh.refresh(project_root, _RuntimeImages(), verbose=verbose)
    _initialize_refreshed_project(project_root, result)
    return result
