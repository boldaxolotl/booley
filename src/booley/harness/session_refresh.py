"""Project Initialization composition for Session Runtime refresh."""

from __future__ import annotations

from pathlib import Path

from booley.harness import init_cmd
from booley.runtime import session_refresh as runtime_refresh
from booley.runtime.image_lifecycle import LifecycleResult
from booley.runtime.session_runtime import SessionError


class _RuntimeImages:
    def __init__(self) -> None:
        self._inspection: LifecycleResult | None = None

    def inspect(self, project_root: Path, *, verbose: bool) -> None:
        self._inspection = init_cmd.inspect_refreshable_runtime_image(
            project_root,
            verbose=verbose,
        )

    def refresh(
        self,
        project_root: Path,
        *,
        verbose: bool,
    ) -> runtime_refresh.RefreshImage:
        result = init_cmd.refresh_runtime_image(
            project_root,
            verbose=verbose,
            inspection=self._inspection,
        )
        if result.selected_id is None:
            raise SessionError("image refresh did not return an immutable Runtime Image ID")
        return runtime_refresh.RefreshImage(
            result.selected_reference,
            result.selected_id,
            result.payload_fingerprint,
        )

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
    """Refresh the selected image and replace its Session Runtime atomically."""
    return runtime_refresh.refresh(project_root, _RuntimeImages(), verbose=verbose)
