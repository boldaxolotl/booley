"""Drive one ordinary Simulation group the way Simulation Records do.

Stdlib + booley only (no pytest) so a plain Docker acceptance script can
import it.

The helper leaves three record steps out on purpose: planning-disclosure
authentication, the durable Build Result and bundle records, and
Pre-Sim hooks. Tests of those belong to the record layer, not the engine.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from booley.flows.sim.config import resolve_run_cwd
from booley.flows.sim.execution import SimulationExecution, SimulationTargetOutcome
from booley.flows.sim.record.codec import SimulationRecordIntegrityError
from booley.flows.sim.runtime_inputs import materialize_simulation_record_runtime_inputs
from booley.targets.domain import TargetHandle


def _capture_bundle(
    build_root: Path,
    artifact_paths: tuple[Path, ...],
    runtime_inputs: tuple[str, ...],
    bundle: Path,
    *,
    copy_build_root: bool,
) -> None:
    """Copy the image into ``bundle``, as a record's private image capture does.

    A real build authorizes its artifact paths; copy those plus the declared
    runtime inputs, keeping their paths relative to ``build_root``. The
    ``canned_build_session`` fakes authorize nothing (``authorize_fresh_image``
    is stubbed to return ``()``), so only a caller that uses them opts in to
    copying the whole build root. Anywhere else an empty set is an error, as in
    a record.
    """
    if copy_build_root:
        shutil.copytree(build_root, bundle, symlinks=True)
        return
    if not artifact_paths:
        raise SimulationRecordIntegrityError("successful build has no authenticated image")
    bundle.mkdir(parents=True, exist_ok=True)
    for source in (*artifact_paths, *(build_root / name for name in runtime_inputs)):
        target = bundle / source.relative_to(build_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def run_prepared_group(
    execution: SimulationExecution,
    handle: TargetHandle,
    names: tuple[str, ...] = (),
    *,
    attempt_root: Path | None = None,
    copy_build_root: bool = False,
) -> SimulationTargetOutcome:
    """Compile one ordinary group and launch a copy of its image, as records do.

    The bundle and the launch's execution evidence live under
    ``attempt_root``, which must be fresh per call. By default it is a new
    directory beside the project, so copied ``.core`` files never reach core
    discovery. Pass ``copy_build_root=True`` only with ``canned_build_session``
    fakes, which authorize no artifact paths.
    """
    if attempt_root is None:
        attempt_root = Path(tempfile.mkdtemp(prefix="attempt-", dir=handle.project_root.parent))
    with execution.ordinary_group(handle, tuple(names)) as group:
        if not group.compile().passed:
            return group.finish_build_failure()
        runtime_inputs = tuple(group.work.runtime_inputs)
        attempt_root.mkdir(parents=True, exist_ok=True)
        bundle = (attempt_root / "bundle").resolve()
        # Read build_root after compile(): a reuse hit re-points it at the
        # retained generation.
        _capture_bundle(
            group.build_root,
            group.artifact_paths,
            runtime_inputs,
            bundle,
            copy_build_root=copy_build_root,
        )
    # Like a record, launch from the configured source cwd, not group.work.run_cwd:
    # under a Doctor fail-path run the latter already names the attempt's view.
    cwd = (handle.project_root / resolve_run_cwd(handle.project_root)).resolve()
    # Simulation Record planning declares each copyto destination as its own source path.
    declarations = [
        {"declaration_id": name, "source_artifact_path": name, "destination": name}
        for name in runtime_inputs
    ]
    # Mirror the record order: Doctor runtime view outside, inputs inside it.
    with (
        group.runtime_view(cwd, uuid.uuid4().hex) as view,
        materialize_simulation_record_runtime_inputs(
            bundle_root=bundle,
            attempt_root=attempt_root,
            run_cwd=Path(view),
            declarations=declarations,
            owned_run_directory=False,
        ),
    ):
        return group.launch_bundle(
            bundle, attempt_root.resolve() / "execution-evidence", Path(view)
        )
