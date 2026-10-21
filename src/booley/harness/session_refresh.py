"""Project Initialization composition for Sandbox refresh."""

from __future__ import annotations

from pathlib import Path

from booley.harness import init_cmd
from booley.runtime import session_issuance as issuance
from booley.runtime import session_refresh as runtime_refresh
from booley.runtime.image_lifecycle import PlanAction, PreparedConvergence
from booley.runtime.session_runtime import SessionError


class _RuntimeImages:
    def __init__(self) -> None:
        self._prepared: PreparedConvergence | None = None
        self._spec: issuance.PreparedSessionSpec | None = None
        self._sources: init_cmd._InteractiveSpecSources | None = None

    def inspect(self, project_root: Path, *, verbose: bool) -> None:
        init_cmd.inspect_refreshable_runtime_image(project_root)

    def prepare(
        self,
        project_root: Path,
        *,
        verbose: bool,
    ) -> runtime_refresh.RefreshImage:
        self._prepared = init_cmd.prepare_runtime_image(
            project_root,
            verbose=verbose,
        )
        final = self._prepared.candidates[-1]
        changed = any(step.action is not PlanAction.REUSE for step in self._prepared.plan.steps)
        return runtime_refresh.RefreshImage(
            self._prepared.plan.selected_reference,
            final.image_id,
            self._prepared.plan.nodes[-1].wheel_source_fingerprint,
            final.wheel_sha256,
            self._prepared_image_state(),
            changed,
            self._prepared.plan.nodes[-1].wheel_source_fingerprint,
        )

    def _prepared_image_state(self) -> tuple[runtime_refresh.RefreshPreparedImage, ...]:
        if self._prepared is None:
            return ()
        prior = {snapshot.reference: snapshot.image_id for snapshot in self._prepared.prior_tags}
        return tuple(
            runtime_refresh.RefreshPreparedImage(
                image.reference,
                image.candidate_reference,
                image.image_id,
                prior.get(image.reference),
            )
            for image in self._prepared.candidates
        )

    def commit(self) -> runtime_refresh.RefreshImage:
        if self._prepared is None:
            raise SessionError("image refresh candidates were not prepared")
        prepared_images = self._prepared_image_state()
        graph_changed = any(
            step.action is not PlanAction.REUSE for step in self._prepared.plan.steps
        )
        result = init_cmd.commit_runtime_image(self._prepared)
        self._prepared = None
        if result.selected_id is None:
            raise SessionError("image refresh did not return an immutable Sandbox Image ID")
        return runtime_refresh.RefreshImage(
            result.selected_reference,
            result.selected_id,
            result.wheel_source_fingerprint or result.payload_fingerprint,
            result.wheel_sha256,
            prepared_images,
            graph_changed,
            result.wheel_source_fingerprint or result.payload_fingerprint,
        )

    def validate(self, prepared: runtime_refresh.RefreshImage) -> None:
        del prepared
        if self._prepared is None:
            raise SessionError("image refresh candidates were not prepared")
        init_cmd.validate_runtime_image(self._prepared)

    def abort(self) -> None:
        if self._prepared is not None:
            init_cmd.abort_runtime_image(self._prepared)
            self._prepared = None

    def _current_sources(
        self, project_root: Path, image_id: str | None
    ) -> init_cmd._InteractiveSpecSources:
        pdk = init_cmd.nangate_pdk
        root = pdk.cache_root() if pdk.is_ready() else init_cmd._NANGATE_PDK_NOT_REQUESTED
        return init_cmd._interactive_spec_sources(
            init_cmd.InitContext(project_root=project_root, force=False, verbose=False),
            root,
            None,
            image_id,
        )

    def prepare_spec(self, project_root: Path, image: runtime_refresh.RefreshImage) -> None:
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

    def requires_license(self) -> bool:
        assert self._spec is not None
        return self._spec.inputs.license_profile_name is not None

    def finalize(self, project_root: Path) -> None:
        runtime_refresh.finalize_project(project_root)

    def reissue(self, project_root: Path, image_id: str, *, verbose: bool) -> None:
        del verbose
        assert self._spec is not None and self._sources is not None
        if self._sources.mask_paths:
            init_cmd._mask_source_dir().mkdir(parents=True, exist_ok=True)
        issuance.issue_prepared(
            project_root,
            self._spec,
            current_builder=lambda: self._current_sources(project_root, image_id).build,
            reconcile_dashboard=False,
        )


def refresh(
    project_root: Path,
    *,
    verbose: bool = False,
) -> runtime_refresh.RefreshResult:
    """Refresh the selected image and replace its Sandbox atomically."""
    return runtime_refresh.refresh(project_root, _RuntimeImages(), verbose=verbose)
