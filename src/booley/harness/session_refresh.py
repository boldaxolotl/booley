"""Project Initialization composition for Sandbox refresh."""

from __future__ import annotations

from pathlib import Path

from booley.harness import init_cmd
from booley.runtime import session_refresh as runtime_refresh
from booley.runtime.image_lifecycle import LifecycleResult, PlanAction, PreparedConvergence
from booley.runtime.session_runtime import SessionError


class _RuntimeImages:
    def __init__(self) -> None:
        self._inspection: LifecycleResult | None = None
        self._prepared: PreparedConvergence | None = None

    def inspect(self, project_root: Path, *, verbose: bool) -> None:
        self._inspection = init_cmd.inspect_refreshable_runtime_image(
            project_root,
            verbose=verbose,
        )

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
        changed = any(
            step.action is not PlanAction.REUSE
            for step in self._prepared.plan.steps
        )
        return runtime_refresh.RefreshImage(
            self._prepared.plan.selected_reference,
            final.image_id,
            self._prepared.plan.nodes[-1].wheel_source_fingerprint,
            final.wheel_sha256,
            self._prepared_image_state(),
            changed,
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
            step.action is not PlanAction.REUSE
            for step in self._prepared.plan.steps
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
        )

    def abort(self) -> None:
        if self._prepared is not None:
            init_cmd.abort_runtime_image(self._prepared)
            self._prepared = None

    def reissue(
        self,
        project_root: Path,
        image_id: str,
        *,
        verbose: bool,
    ) -> None:
        init_cmd.reissue_session_spec(project_root, image_id, verbose=verbose)


def refresh(
    project_root: Path,
    *,
    verbose: bool = False,
) -> runtime_refresh.RefreshImage:
    """Refresh the selected image and replace its Sandbox atomically."""
    return runtime_refresh.refresh(project_root, _RuntimeImages(), verbose=verbose)
