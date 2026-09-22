"""Deep execution boundary for one selected Simulation Target."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import shlex
import shutil
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from booley.config.project_config import load_test_configuration_field, lookup_target_section
from booley.core.build_paths import work_root_for
from booley.flows import edam as edam_layer
from booley.flows.base import DEFAULT_TIMEOUT_S, SubprocessResult
from booley.flows.run_log import begin_run_log, write_run_log
from booley.flows.sim import edam as sim_edam
from booley.flows.sim import trace_overlay
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportIdentity,
)
from booley.flows.sim.build import (
    BuildOutcome,
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_stage_script,
    classify_build_outcome,
    new_attempt_token,
    prepare_simulation_build,
)
from booley.flows.sim.build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    preview_generation_root,
    project_compile_surface,
    snapshot_build_inputs,
    verify_existing_build_inputs,
)
from booley.flows.sim.config import (
    resolve_cycle_sentinels,
    resolve_max_rundir_bytes,
    resolve_pre_sim_commands,
    resolve_run_cwd,
    resolve_sim_time_grace_s,
    resolve_sim_timeout_ms,
    resolve_trace_args,
    resolve_trace_files,
)
from booley.flows.sim.runner import resolve_sim_sentinels
from booley.flows.sim.runtime_inputs import (
    RuntimeInputError,
    declared_runtime_inputs,
    materialize_runtime_inputs,
    preview_runtime_inputs,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.workload import build_workload_snapshot, capture_workload_inputs
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.runtime.project_dir import resolve_project_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle

from .artifacts import CompatibilityArtifactPolicy, TraceArtifactPolicy, artifact_path_component
from .attempt import (
    AdapterAttemptOutcome,
    AdapterAttemptRequest,
    ProcessInvoker,
    execute_adapter_attempt,
)
from .composition import UnsupportedSimulationAdapterError, prepare_adapter_invocation
from .contract import (
    DefaultSelection,
    NamedTests,
    PreSimEvidence,
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
    SimulationOptions,
    SimulationPreview,
    SimulationSelection,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from .failures import find_missing_executable
from .freshness import (
    ArtifactValidationError,
    validate_fresh_artifact,
)
from .pre_sim import run_pre_sim_commands
from .telemetry import parse_build_seconds, parse_run_seconds, process_resources

_DEFAULT_CYCLE_SENTINEL = "[SIM_CYCLES]"
_TRACE_CLEANUP_MARGIN_S = 90
_NO_SENTINEL = "no pass/fail sentinel detected, simulation exited cleanly"
_NO_WAVEFORM = "the simulation passed, but --trace produced no queryable waveform"


@dataclass(frozen=True)
class _Attempt:
    prepared: PreparedSimulationBuild
    identity: AdapterTransportIdentity
    command: tuple[str, ...]
    test_names: tuple[str, ...]
    adapter: str
    trace_requested: bool
    work: PreparedSimulationWork
    wrapper_timeout_s: int
    pre_sim_commands: tuple[str, ...]
    simulator_environment: tuple[tuple[str, str], ...]
    cycle_sentinels: tuple[str, ...]
    workload_inputs: tuple[Mapping[str, Any], ...]
    configured_run_cwd: Path
    setup_s: float
    build_inputs: Mapping[str, str] = field(default_factory=dict)
    cache_key: str | None = None
    reused: bool = False
    cache_decision: str = ""


def _adapter_shell_command(attempt: _Attempt) -> tuple[str, str, str]:
    """Preserve the Target environment when build and run use separate processes."""
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in attempt.simulator_environment
    )
    invocation = shlex.join(prepare_adapter_invocation(attempt.work))
    return "sh", "-c", f"{exports}{invocation}"


@dataclass(frozen=True)
class _BuildPolicy:
    variant: str
    fresh_root_label: str | None


class _BuildRootResetError(RuntimeError):
    """A fresh Simulation build root could not be reset safely."""


class SimulationArtifactPersistenceError(RuntimeError):
    """A completed attempt could not preserve its required run evidence."""


class PreparedOrdinaryGroup:
    """One ordinary-HDL build split from its later attempt-local launch.

    Instances are yielded by :meth:`SimulationExecution.ordinary_group`.  The
    caller may inspect ``build_root`` and run a legacy build-aware hook before
    :meth:`compile`.  Compilation must happen while the context is active so
    the Target build lease protects preparation and authentication.  Snapshot
    creation and :meth:`launch_snapshot` happen after leaving the context; the
    simulator therefore never needs the broad Target lease.
    """

    def __init__(
        self,
        execution: SimulationExecution,
        handle: TargetHandle,
        attempt: _Attempt,
        session: SimulationBuildSession,
        started: float,
        sources_before: Mapping[str, str],
        prepared_surface: Mapping[str, str],
        prepared_inputs: Mapping[str, str],
    ) -> None:
        self._execution = execution
        self._handle = handle
        self._attempt = attempt
        self._session = session
        self._started = started
        self._sources_before = sources_before
        self._prepared_surface = prepared_surface
        self._prepared_inputs = prepared_inputs
        self._lease_active = True
        self._build_process: SubprocessResult | None = None
        self._build: BuildOutcome | None = None
        self._artifact_paths: tuple[Path, ...] = ()

    @property
    def build_root(self) -> Path:
        """Private generated build root available to a legacy pre-build hook."""
        return self._attempt.prepared.build_root

    @property
    def work(self) -> PreparedSimulationWork:
        """Resolved adapter facts, including declared runtime inputs."""
        return self._attempt.work

    @property
    def artifact_paths(self) -> tuple[Path, ...]:
        """Authenticated simulator artifacts after a successful compile."""
        return self._artifact_paths

    @property
    def build(self) -> BuildOutcome | None:
        """Normalized build outcome after :meth:`compile`."""
        return self._build

    @contextmanager
    def runtime_view(self, run_cwd: Path, attempt_token: str) -> Iterator[Path]:
        """Expose the attempt-scoped Doctor view when fail-path testing is active."""
        if not _doctor_bad_requested():
            yield run_cwd
            return
        project_dir = resolve_project_dir(self._handle.project_root)
        with selftest_overlay.managed_runtime_view(
            self._handle.project_root,
            project_dir,
            "sim",
            run_cwd,
            self._attempt.prepared.work_root.parent,
            attempt_token,
        ) as view:
            yield view

    def prepared_source_entries(self) -> tuple[dict[str, object], ...]:
        """Return the canonical staged source closure produced by setup."""
        entries: list[dict[str, object]] = []
        root = self._attempt.prepared.build_root.resolve()
        for item in self._attempt.prepared.resolved.files:
            source = item.absolute(root)
            try:
                if source.is_symlink() or not source.is_file() or not source.is_relative_to(root):
                    raise SimulationBuildSlotError(
                        f"unsafe prepared Simulation source: {item.name}"
                    )
                raw = source.read_bytes()
            except OSError as exc:
                raise SimulationBuildSlotError(
                    f"cannot authenticate prepared Simulation source: {item.name}: {exc}"
                ) from exc
            relative = Path(item.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise SimulationBuildSlotError(
                    f"prepared Simulation source has unsafe name: {item.name}"
                )
            entries.append(
                {
                    "path": relative.as_posix(),
                    "bytes": len(raw),
                    "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "kind": "generated_input",
                }
            )
        return tuple(sorted(entries, key=lambda entry: str(entry["path"])))

    def discard_prepared_generation(self) -> None:
        """Delete a preparation-only generation while its lease is held."""
        if self._build is not None:
            raise SimulationBuildSlotError("compiled generation cannot be planning scratch")
        self._session.discard_candidate(self._attempt.prepared.work_root)

    def planning_disclosure(self) -> dict[str, object]:
        """Describe this preparation using the campaign planning contract."""
        return _planning_disclosure(self.prepared_source_entries())

    def compile(self) -> BuildOutcome:
        """Compile and authenticate this group while its build lease is held."""
        if not self._lease_active:
            raise SimulationBuildSlotError("ordinary Simulation build lease has ended")
        if self._build is not None:
            raise SimulationBuildSlotError("ordinary Simulation group was already compiled")
        attempt = self._attempt
        verify_existing_build_inputs(attempt.prepared, self._prepared_inputs)
        if project_compile_surface(self._handle.project_root) != self._sources_before:
            raise SimulationBuildSlotError(
                "Project compile inputs changed during setup or Pre-Sim Commands; rerun the attempt"
            )
        if (
            project_compile_surface(
                self._handle.project_root, include_generated_isolated_cores=True
            )
            != self._prepared_surface
        ):
            raise SimulationBuildSlotError(
                "Project compile inputs changed during Pre-Sim Commands"
            )
        attempt = _with_workload_inputs(self._handle, attempt)
        inputs = self._session.capture_inputs(attempt.prepared)
        script = build_stage_script(
            attempt.prepared.make_argv,
            attempt.identity.attempt_token,
            environment=dict(attempt.simulator_environment),
        )
        process = self._execution._invoke(["sh", "-c", script], timeout=DEFAULT_TIMEOUT_S)
        build = classify_build_outcome(process, attempt.identity.attempt_token)
        self._attempt = attempt
        self._build_process = process
        self._build = build
        if build.passed and process.returncode == 0 and not process.timed_out:
            self._artifact_paths = self._session.authorize_fresh_image(
                attempt.prepared, inputs, None
            )
        return build

    def finish_build_failure(self) -> SimulationTargetOutcome:
        """Normalize a failed build without launching a simulator."""
        process, build = self._compiled()
        if build.passed:
            raise SimulationBuildSlotError("successful build requires a snapshot launch")
        executed = AdapterAttemptOutcome(process, None, None)
        early = _adapter_failure_outcome(
            self._handle, self._attempt, executed, build, None, self._started
        )
        if early is not None:
            return early
        trace_policy, compatibility_policy = _artifact_policies(self._handle, self._attempt)
        return self._execution._completed_group(
            self._handle,
            self._attempt,
            process,
            build,
            None,
            None,
            trace_policy,
            compatibility_policy,
            time.monotonic(),
            self._started,
        )

    def reuse_compilation_from(self, source: PreparedOrdinaryGroup) -> None:
        """Bind this test-specific launch to an authenticated successful build.

        The caller still prepares this group's adapter command under the Target
        lease, but no compiler is run.  The executable bytes are supplied later
        from the campaign-owned authenticated Simulator Bundle snapshot.
        """
        if not self._lease_active:
            raise SimulationBuildSlotError("ordinary Simulation build lease has ended")
        if self._build is not None:
            raise SimulationBuildSlotError("ordinary Simulation group already has a build")
        process, build = source._compiled()
        if not build.passed or process.returncode != 0 or process.timed_out:
            raise SimulationBuildSlotError("shared Simulation build is not reusable")
        self._build_process = process
        self._build = build

    def build_recovery_document(self) -> dict[str, object]:
        """Return the exact compiler process and normalized build for durability."""
        process, build = self._compiled()
        return {
            "$schema": "booley.simulation-build-execution/v1",
            "process": {
                "returncode": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "timed_out": process.timed_out,
                "duration_s": process.duration_s,
                "dispatched_unix": process.dispatched_unix,
                "peak_rss_mb": process.peak_rss_mb,
                "oom_kill_delta": process.oom_kill_delta,
            },
            "build": {
                "ran": build.ran,
                "verdict": build.verdict,
                "failure_kind": build.failure_kind,
                "elapsed_s": build.elapsed_s,
                "output": build.output,
                "returncode": build.returncode,
                "timed_out": build.timed_out,
                "peak_rss_mb": build.peak_rss_mb,
                "oom_kill_delta": build.oom_kill_delta,
                "terminal_record": build.terminal_record,
                "reason": build.reason,
                "cache_decision": build.cache_decision,
            },
        }

    def bind_authenticated_bundle(self, evidence: Mapping[str, object]) -> None:
        """Mark preparation ready to launch a campaign-authenticated bundle.

        This is the process-recovery counterpart of ``reuse_compilation_from``:
        the durable Build Result has already authenticated the compiler outcome,
        so only the test-specific adapter preparation is reconstructed.
        """
        if not self._lease_active:
            raise SimulationBuildSlotError("ordinary Simulation build lease has ended")
        if self._build is not None:
            raise SimulationBuildSlotError("ordinary Simulation group already has a build")
        process = evidence["process"]
        build = evidence["build"]
        assert isinstance(process, Mapping) and isinstance(build, Mapping)
        self._build_process = SubprocessResult(**process)  # type: ignore[arg-type]
        self._build = BuildOutcome(**build)  # type: ignore[arg-type]
        if not self._build.passed or self._build_process.returncode != 0:
            raise SimulationBuildSlotError("recovered bundle evidence is not successful")

    def launch_snapshot(
        self,
        snapshot_root: Path,
        run_cwd: Path,
        *,
        materialize_runtime_inputs: bool = False,
    ) -> SimulationTargetOutcome:
        """Launch only the supplied snapshot and normalize the resulting evidence."""
        if self._lease_active:
            raise SimulationBuildSlotError(
                "leave ordinary_group before launching its executable snapshot"
            )
        build_process, build = self._compiled()
        if not build.passed:
            raise SimulationBuildSlotError("cannot launch a failed Simulation build")
        attempt = self._snapshot_attempt(snapshot_root, run_cwd)
        trace_policy, compatibility_policy = _artifact_policies(self._handle, attempt)
        try:
            executed = self._launch(attempt, materialize_runtime_inputs)
        except RuntimeInputError as exc:
            return _runtime_input_failure(self._handle, attempt, None, str(exc), self._started)
        process = replace(
            executed.process,
            stdout=build_process.stdout + "\n" + executed.process.stdout,
            stderr=build_process.stderr + "\n" + executed.process.stderr,
            duration_s=build_process.duration_s + executed.process.duration_s,
        )
        executed = replace(executed, process=process)
        early = _adapter_failure_outcome(
            self._handle, attempt, executed, build, None, self._started
        )
        if early is not None:
            return early
        adapter = None if build.design_failed else executed.result
        try:
            return self._execution._completed_group(
                self._handle,
                attempt,
                process,
                build,
                adapter,
                None,
                trace_policy,
                compatibility_policy,
                time.monotonic(),
                self._started,
            )
        except SimulationArtifactPersistenceError as exc:
            return _artifact_failure(self._handle, attempt, build, None, str(exc), self._started)

    def _compiled(self) -> tuple[SubprocessResult, BuildOutcome]:
        if self._build_process is None or self._build is None:
            raise SimulationBuildSlotError("ordinary Simulation group is not compiled")
        return self._build_process, self._build

    def _snapshot_attempt(self, snapshot_root: Path, run_cwd: Path) -> _Attempt:
        if not snapshot_root.is_absolute() or not run_cwd.is_absolute():
            raise SimulationBuildSlotError("snapshot and run cwd must be absolute paths")
        evidence_root = snapshot_root.parent / "execution-evidence"
        evidence_root.mkdir(mode=0o700, exist_ok=True)
        identity = replace(
            self._attempt.identity,
            result_path=evidence_root / f"adapter-{self._attempt.identity.attempt_token}.json",
        )
        work = replace(
            self._attempt.work,
            build_dir=str(snapshot_root),
            run_cwd=str(run_cwd),
            work_dir=str(evidence_root),
            adapter_result_path=str(identity.result_path),
        )
        prepared = replace(self._attempt.prepared, build_root=evidence_root)
        return replace(
            self._attempt,
            prepared=prepared,
            identity=identity,
            work=work,
            command=_adapter_shell_command(replace(self._attempt, work=work, identity=identity)),
        )

    def _launch(
        self, attempt: _Attempt, should_materialize_runtime_inputs: bool
    ) -> AdapterAttemptOutcome:
        request = AdapterAttemptRequest(
            attempt.command,
            attempt.wrapper_timeout_s,
            attempt.identity,
            attempt.identity.result_path.parent,
        )
        if not should_materialize_runtime_inputs:
            return execute_adapter_attempt(self._execution._invoke, request)
        with materialize_runtime_inputs(
            Path(attempt.work.build_dir), Path(attempt.work.run_cwd), attempt.work.runtime_inputs
        ):
            return execute_adapter_attempt(self._execution._invoke, request)

    def _release_lease(self) -> None:
        self._lease_active = False


def _planning_disclosure(entries: tuple[dict[str, object], ...]) -> dict[str, object]:
    try:
        version = importlib.metadata.version("fusesoc")
    except importlib.metadata.PackageNotFoundError:
        version = "unavailable"
    return {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": list(entries),
        "tool_provenance": {
            "kind": "fusesoc",
            "version": version,
            "contract_version": "1",
        },
        "cleanup": {"removed": True},
    }


class SimulationExecution:
    """Resolve, preview, and execute one Target behind a two-method interface."""

    def __init__(
        self,
        *,
        invoke: ProcessInvoker,
        options: SimulationOptions,
        artifact_root: Path | None = None,
    ) -> None:
        self._invoke = invoke
        self._options = options
        self._artifact_root = artifact_root
        self._reset_build_roots: set[Path] = set()
        self._build_session: SimulationBuildSession | None = None
        self._fresh_generation: Path | None = None

    @contextmanager
    def ordinary_group(
        self, handle: TargetHandle, test_names: tuple[str, ...]
    ) -> Iterator[PreparedOrdinaryGroup]:
        """Prepare one ordinary-HDL group for separately controlled build and launch.

        The yielded group owns a freshly allocated private generation.  Its
        ``compile()`` method must be called before this context exits.  Exiting
        releases the Target build lease; only then may ``launch_snapshot()``
        run a caller-supplied attempt-local copy of the authenticated artifacts.
        Project Pre-Sim policy intentionally remains with the caller.
        """
        if self._build_session is not None:
            raise SimulationBuildSlotError("Simulation execution already owns a build lease")
        started = time.monotonic()
        sources_before = project_compile_surface(handle.project_root)
        policy = _build_policy(self._options.trace)
        with SimulationBuildSession(handle, policy.variant) as session:
            self._build_session = session
            group: PreparedOrdinaryGroup | None = None
            try:
                attempt = self._prepare_attempt(handle, test_names)
                begin_run_log(attempt.prepared.build_root, flow="sim", target=handle.selector)
                group = PreparedOrdinaryGroup(
                    self,
                    handle,
                    attempt,
                    session,
                    started,
                    sources_before,
                    project_compile_surface(
                        handle.project_root, include_generated_isolated_cores=True
                    ),
                    snapshot_build_inputs(attempt.prepared),
                )
                yield group
            finally:
                if group is not None:
                    group._release_lease()
                self._build_session = None
                self._fresh_generation = None

    def run(
        self,
        handle: TargetHandle,
        selection: SimulationSelection,
        *,
        planned_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> SimulationTargetOutcome:
        """Execute the selected Target and return immutable normalized evidence."""
        started = time.monotonic()
        self._reset_build_roots.clear()
        try:
            inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        except fusesoc_registry.FuseSocError as exc:
            return _setup_failure(handle, str(exc), started)
        groups = (
            planned_groups
            if planned_groups is not None
            else _work_groups(selection, _is_cocotb(inspection.flow_options))
        )
        try:
            results = self._run_groups_with_session(handle, groups)
        except SimulationBuildSlotError as exc:
            return _build_slot_failure(handle, exc, started)
        except _BuildRootResetError as exc:
            return _build_root_failure(handle, exc, started)
        except SimulationBuildPreparationError as exc:
            return _setup_failure(handle, str(exc), started)
        return _aggregate_group_results(handle, inspection.toplevel, results, started)

    def _run_groups_with_session(
        self, handle: TargetHandle, groups: tuple[tuple[str, ...], ...]
    ) -> list[SimulationTargetOutcome]:
        """Hold the Target lease through every group and artifact capture."""
        with SimulationBuildSession(handle, _build_policy(self._options.trace).variant) as session:
            self._build_session = session
            try:
                return [self._run_group(handle, names) for names in groups]
            finally:
                self._build_session = None

    def preview(
        self,
        handle: TargetHandle,
        selection: SimulationSelection,
    ) -> SimulationPreview:
        """Describe the same work grouping and adapter rendering without side effects."""
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        cocotb = _is_cocotb(inspection.flow_options)
        groups = _work_groups(selection, cocotb)
        commands = tuple(
            self._preview_group(handle, inspection, names, cocotb) for names in groups
        )
        inputs = getattr(inspection, "inputs", ())
        constraints = tuple(item.path for item in inputs if item.file_type.lower() == "sdc")
        sources = tuple(item.path for item in inputs if item.file_type.lower() != "sdc")
        return SimulationPreview(
            commands=commands,
            groups=groups,
            target_identity=handle.identity,
            toplevel=inspection.toplevel,
            eda_tool=inspection.eda_tool,
            sources=sources,
            constraints=constraints,
            parameters=getattr(inspection, "parameters", {}),
            flow_options=inspection.flow_options,
        )

    def plan_ordinary_group(
        self, handle: TargetHandle, test_names: tuple[str, ...]
    ) -> dict[str, object]:
        """Prepare disposable scratch and disclose its exact staged source closure."""
        with self.ordinary_group(handle, test_names) as group:
            disclosure = group.planning_disclosure()
            group.discard_prepared_generation()
        return disclosure

    def plan_campaign_group(
        self, handle: TargetHandle, test_names: tuple[str, ...]
    ) -> dict[str, object]:
        """Disclose one durable ordinary-HDL or Cocotb campaign build."""
        return self.plan_ordinary_group(handle, test_names)

    def _run_group(
        self,
        handle: TargetHandle,
        test_names: tuple[str, ...],
    ) -> SimulationTargetOutcome:
        started = time.monotonic()
        sources_before = project_compile_surface(handle.project_root)
        attempt = self._prepare_attempt(handle, test_names)
        try:
            with self._runtime_view(handle, attempt):
                return self._run_group_body(handle, attempt, sources_before, started)
        except selftest_overlay.SelftestOverlayError as exc:
            raise SimulationBuildSlotError(str(exc)) from exc

    def _run_group_body(
        self,
        handle: TargetHandle,
        attempt: _Attempt,
        sources_before: Mapping[str, str],
        started: float,
    ) -> SimulationTargetOutcome:
        """Run one prepared attempt while its Doctor view is owned."""
        try:
            begin_run_log(attempt.prepared.build_root, flow="sim", target=handle.selector)
        except OSError as exc:
            detail = f"could not establish current run log: {exc}"
            return _artifact_failure(handle, attempt, None, None, detail, started)
        prepared_surface = project_compile_surface(
            handle.project_root, include_generated_isolated_cores=True
        )
        prepared_inputs = snapshot_build_inputs(attempt.prepared)
        pre_sim = self._run_pre_sim(handle, attempt)
        if pre_sim is not None and pre_sim.status != "passed":
            failure = (
                _pre_sim_infrastructure_failure
                if pre_sim.status == "spawn_error"
                else _pre_sim_failure
            )
            return failure(handle, attempt, pre_sim, started)
        verify_existing_build_inputs(attempt.prepared, prepared_inputs)
        if project_compile_surface(handle.project_root) != sources_before:
            raise SimulationBuildSlotError(
                "Project compile inputs changed during setup or Pre-Sim Commands; rerun the attempt"
            )
        if (
            project_compile_surface(handle.project_root, include_generated_isolated_cores=True)
            != prepared_surface
        ):
            raise SimulationBuildSlotError(
                "Project compile inputs changed during Pre-Sim Commands"
            )
        attempt = self._select_generation_for_group(handle, attempt)
        attempt = _with_workload_inputs(handle, attempt)
        return self._run_authorized_group(handle, attempt, pre_sim, started)

    @contextmanager
    def _runtime_view(self, handle: TargetHandle, attempt: _Attempt) -> Iterator[None]:
        """Own the Doctor runtime view for the complete attempt lifecycle."""
        if not _doctor_bad_requested():
            yield
            return
        project_dir = resolve_project_dir(handle.project_root)
        with selftest_overlay.managed_runtime_view(
            handle.project_root,
            project_dir,
            "sim",
            attempt.configured_run_cwd,
            attempt.prepared.work_root.parent,
            attempt.identity.attempt_token,
        ):
            yield

    def _select_generation_for_group(self, handle: TargetHandle, attempt: _Attempt) -> _Attempt:
        """Resolve a post-hook cache decision without leaking candidate paths."""
        session = self._build_session
        if session is None or self._fresh_generation != attempt.prepared.work_root:
            return attempt
        inputs = session.capture_inputs(attempt.prepared)
        key = session.reusable_key(attempt.prepared, inputs, hooks=bool(attempt.pre_sim_commands))
        selected = session.try_reuse(attempt.prepared, key)
        generation = (
            selected.work_root.name if selected is not None else attempt.prepared.work_root.name
        )
        decision = (
            f"{session.cache_decision}; Target={handle.identity}; "
            f"digest={key or 'unavailable'}; generation={generation}"
        )
        if selected is None:
            return replace(attempt, build_inputs=inputs, cache_key=key, cache_decision=decision)
        candidate = attempt.prepared.work_root
        reused = self._prepare_attempt(handle, attempt.test_names, prepared_override=selected)
        self._fresh_generation = None
        session.discard_candidate(candidate)
        try:
            begin_run_log(selected.build_root, flow="sim", target=handle.selector)
        except OSError as exc:
            raise SimulationBuildSlotError(f"cannot open retained generation log: {exc}") from exc
        return replace(
            reused,
            cache_key=key,
            reused=True,
            cache_decision=decision,
        )

    def _run_authorized_group(
        self,
        handle: TargetHandle,
        attempt: _Attempt,
        pre_sim: PreSimEvidence | None,
        started: float,
    ) -> SimulationTargetOutcome:
        """Execute one selected image and normalize its evidence."""
        trace_policy, compatibility_policy = _artifact_policies(handle, attempt)
        try:
            executed = self._execute_adapter(handle, attempt)
        except RuntimeInputError as exc:
            return _runtime_input_failure(handle, attempt, pre_sim, str(exc), started)
        process = executed.process
        processing_started = time.monotonic()
        build = (
            BuildOutcome(
                ran=False,
                verdict="pass",
                failure_kind=None,
                reason="verified Simulation build reuse",
                cache_decision=attempt.cache_decision,
            )
            if attempt.reused
            else replace(
                classify_build_outcome(process, attempt.identity.attempt_token),
                cache_decision=attempt.cache_decision,
            )
        )
        early_failure = _adapter_failure_outcome(
            handle, attempt, executed, build, pre_sim, started
        )
        if early_failure is not None:
            return early_failure
        adapter = None if build.design_failed else executed.result
        try:
            return self._completed_group(
                handle,
                attempt,
                process,
                build,
                adapter,
                pre_sim,
                trace_policy,
                compatibility_policy,
                processing_started,
                started,
            )
        except SimulationArtifactPersistenceError as exc:
            return _artifact_failure(handle, attempt, build, pre_sim, str(exc), started)

    def _execute_adapter(self, handle: TargetHandle, attempt: _Attempt) -> AdapterAttemptOutcome:
        if attempt.reused:
            return self._execute_reused_adapter(handle, attempt)
        if self._build_session is not None:
            if self._fresh_generation != attempt.prepared.work_root:
                raise SimulationBuildSlotError("prepared build escaped its leased generation")
            return self._execute_fresh_adapter(handle, attempt)
        request = AdapterAttemptRequest(
            attempt.command,
            attempt.wrapper_timeout_s,
            attempt.identity,
            attempt.prepared.build_root,
        )
        run_cwd = Path(attempt.work.run_cwd)
        if not run_cwd.is_absolute():
            run_cwd = handle.project_root / run_cwd
        with materialize_runtime_inputs(
            attempt.prepared.build_root,
            run_cwd,
            attempt.work.runtime_inputs,
        ):
            return execute_adapter_attempt(self._invoke, request)

    def _execute_reused_adapter(
        self, handle: TargetHandle, attempt: _Attempt
    ) -> AdapterAttemptOutcome:
        session = self._build_session
        if session is None or session.try_reuse(attempt.prepared, attempt.cache_key) is None:
            raise SimulationBuildSlotError("cached Simulation image lost authorization")
        run_cwd = Path(attempt.work.run_cwd)
        if not run_cwd.is_absolute():
            run_cwd = handle.project_root / run_cwd
        request = AdapterAttemptRequest(
            _adapter_shell_command(attempt),
            attempt.wrapper_timeout_s,
            attempt.identity,
            attempt.prepared.build_root,
        )
        with materialize_runtime_inputs(
            attempt.prepared.build_root, run_cwd, attempt.work.runtime_inputs
        ):
            if session.try_reuse(attempt.prepared, attempt.cache_key) is None:
                raise SimulationBuildSlotError("cached Simulation image changed before launch")
            return execute_adapter_attempt(self._invoke, request)

    def _execute_fresh_adapter(
        self, handle: TargetHandle, attempt: _Attempt
    ) -> AdapterAttemptOutcome:
        """Authenticate compilation and its image before dispatching the adapter."""
        session = self._build_session
        if session is None:
            raise SimulationBuildSlotError("fresh build lost its slot lease")
        run_cwd = Path(attempt.work.run_cwd)
        if not run_cwd.is_absolute():
            run_cwd = handle.project_root / run_cwd
        with materialize_runtime_inputs(
            attempt.prepared.build_root, run_cwd, attempt.work.runtime_inputs
        ):
            inputs = attempt.build_inputs or session.capture_inputs(attempt.prepared)
            script = build_stage_script(
                attempt.prepared.make_argv,
                attempt.identity.attempt_token,
                environment=dict(attempt.simulator_environment),
            )
            build_process = self._invoke(["sh", "-c", script], timeout=DEFAULT_TIMEOUT_S)
            build = classify_build_outcome(build_process, attempt.identity.attempt_token)
            if not build.passed or build_process.returncode != 0 or build_process.timed_out:
                return AdapterAttemptOutcome(build_process, None, None)
            session.authorize_fresh_image(attempt.prepared, inputs, attempt.cache_key)
            request = AdapterAttemptRequest(
                _adapter_shell_command(attempt),
                attempt.wrapper_timeout_s,
                attempt.identity,
                attempt.prepared.build_root,
            )
            executed = execute_adapter_attempt(self._invoke, request)
            process = replace(
                executed.process,
                stdout=build_process.stdout + "\n" + executed.process.stdout,
                stderr=build_process.stderr + "\n" + executed.process.stderr,
                duration_s=build_process.duration_s + executed.process.duration_s,
            )
            return replace(executed, process=process)

    def _prepare_attempt(
        self,
        handle: TargetHandle,
        test_names: tuple[str, ...],
        *,
        prepared_override: PreparedSimulationBuild | None = None,
    ) -> _Attempt:
        started = time.monotonic()
        prepared, trace_mode = (
            (prepared_override, TraceMode.VCD_FIFO)
            if prepared_override is not None
            else self._prepare_build(handle)
        )
        adapter = "cocotb" if prepared.resolved.cocotb_module else prepared.eda_tool
        configured_run_cwd = _configured_run_cwd(handle.project_root)
        identity = _adapter_identity(
            handle,
            prepared,
            test_names,
            adapter,
            campaign_build=self._build_session is not None,
        )
        work = prepare_simulation_work(
            handle,
            prepared,
            identity,
            self._options,
            trace=self._options.trace,
            trace_mode=trace_mode.value,
            configured_run_cwd=configured_run_cwd,
        )
        pre_sim_commands = tuple(resolve_pre_sim_commands(handle.project_root))
        simulator_environment = tuple(simulation_target_environment(handle).items())
        return _Attempt(
            prepared=prepared,
            identity=identity,
            command=_adapter_command(prepared, identity, work, simulator_environment),
            test_names=test_names,
            adapter=adapter,
            trace_requested=self._options.trace,
            work=work,
            wrapper_timeout_s=work.timeout_s
            + (_TRACE_CLEANUP_MARGIN_S if self._options.trace else 0),
            pre_sim_commands=pre_sim_commands,
            simulator_environment=simulator_environment,
            cycle_sentinels=tuple(resolve_cycle_sentinels(handle.project_root)),
            workload_inputs=(),
            configured_run_cwd=configured_run_cwd,
            setup_s=time.monotonic() - started,
        )

    def _prepare_build(self, handle: TargetHandle) -> tuple[PreparedSimulationBuild, TraceMode]:
        policy = _build_policy(self._options.trace)
        session = self._build_session
        build_root = (
            session.new_generation()
            if session is not None
            else work_root_for(
                handle.project_root,
                "sim",
                handle.selector,
                variant=policy.variant,
            )
        )
        if session is None:
            self._reset_build_root(build_root, policy)
        overlay = _trace_overlay(handle) if self._options.trace else None
        try:
            prepared = prepare_simulation_build(
                handle,
                variant=policy.variant,
                **({"build_root": build_root} if session is not None else {}),
                resolution_vlnv=overlay.vlnv if overlay is not None else None,
                environment=simulation_target_environment(handle),
            )
            if overlay is not None and prepared.resolved.cocotb_module:
                trace_overlay.validate_cocotb_trace_mode(
                    handle.selector,
                    overlay.mode,
                )
            mode = overlay.mode if overlay is not None else TraceMode.VCD_FIFO
            if session is not None and prepared.work_root != build_root:
                raise SimulationBuildSlotError("FuseSoC preparation escaped its leased generation")
            self._fresh_generation = build_root if session is not None else None
            return prepared, mode
        finally:
            if overlay is not None:
                overlay.cleanup()

    def _run_pre_sim(self, handle: TargetHandle, attempt: _Attempt) -> PreSimEvidence | None:
        return run_pre_sim_commands(
            handle,
            test_names=attempt.test_names,
            build_root=attempt.prepared.build_root,
            eda_tool=attempt.prepared.eda_tool,
            timeout_s=attempt.wrapper_timeout_s,
            simulator_environment=dict(attempt.simulator_environment),
            commands=attempt.pre_sim_commands,
            run_cwd=attempt.work.run_cwd,
        )

    def _completed_group(
        self,
        handle: TargetHandle,
        attempt: _Attempt,
        process: SubprocessResult,
        build: BuildOutcome,
        adapter: AdapterResult | None,
        pre_sim: PreSimEvidence | None,
        trace_policy: TraceArtifactPolicy | None,
        compatibility_policy: CompatibilityArtifactPolicy,
        processing_started: float,
        started: float,
    ) -> SimulationTargetOutcome:
        output = process.stdout + ("\n" + process.stderr if process.stderr else "")
        logs = _persist_run_logs(handle, attempt, output, self._artifact_root)
        trace = _trace_artifact(attempt, adapter, trace_policy)
        compatibility = _compatibility_artifacts(attempt, compatibility_policy)
        if attempt.trace_requested and trace is None and adapter is not None and adapter.passed:
            adapter = _missing_trace_result(adapter)
        tests = _test_outcomes(
            handle,
            attempt,
            process,
            build,
            adapter,
            output,
            logs,
            trace,
        )
        tests = _attach_group_telemetry(
            tests,
            attempt,
            process,
            build,
            pre_sim,
            output,
            time.monotonic() - processing_started,
        )
        artifacts = (*logs, *compatibility, *((trace,) if trace is not None else ()))
        return _group_outcome(
            handle,
            attempt,
            tests,
            build,
            pre_sim,
            artifacts,
            started,
            adapter.diagnostics if adapter is not None else (),
            adapter.passed if adapter is not None else None,
        )

    def _preview_group(
        self,
        handle: TargetHandle,
        inspection: Any,
        test_names: tuple[str, ...],
        cocotb: bool,
    ) -> tuple[str, ...]:
        root = handle.project_root
        policy = _build_policy(self._options.trace)
        build_root = preview_generation_root(handle, policy.variant)
        setup = fusesoc_registry.setup_command_for_handle(
            handle,
            build_root=build_root,
        )
        rel = edam_layer.relpath_for_make(build_root, root)
        work = _preview_work(self, handle, inspection, test_names, cocotb, rel)
        steps = [
            *_preview_exports(handle, test_names, build_root),
            shlex.join(setup),
            *resolve_pre_sim_commands(root),
            shlex.join(edam_layer.make_command(rel)),
            shlex.join(prepare_adapter_invocation(work)),
        ]
        return ("sh", "-c", " && ".join(steps))

    def _effective_timeout_ms(self, handle: TargetHandle) -> int:
        return self._options.timeout_ms or resolve_sim_timeout_ms(handle.project_root)

    def _reset_build_root(self, build_root: Path, policy: _BuildPolicy) -> None:
        if policy.fresh_root_label is None:
            return
        key = build_root.resolve()
        if key in self._reset_build_roots:
            return
        try:
            shutil.rmtree(build_root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _BuildRootResetError(
                f"could not reset {policy.fresh_root_label} Simulation build root "
                f"{build_root}: {exc}"
            ) from exc
        self._reset_build_roots.add(key)


def _adapter_attempt_error(attempt: AdapterAttemptOutcome, build: BuildOutcome) -> str | None:
    if build.design_failed:
        return None
    if attempt.error is not None:
        return attempt.error
    result = attempt.result
    if result is None:
        return (
            None
            if attempt.process.timed_out or attempt.process.returncode < 0
            else "adapter completed without authenticated terminal result"
        )
    if result.passed and (attempt.process.returncode != 0 or not build.passed):
        return "adapter pass contradicts process or build evidence"
    if result.failure_kind == "infrastructure":
        return result.detail or "adapter infrastructure failure"
    return None


def _doctor_bad_requested() -> bool:
    return os.environ.get(selftest_overlay.INTERNAL_KIND_ENV) == selftest_overlay.BAD_KIND


def _build_policy(trace: bool) -> _BuildPolicy:
    variants = ["trace"] if trace else []
    doctor_bad = _doctor_bad_requested()
    if doctor_bad:
        variants.append("doctor-selftest-bad")
    fresh_root_label = "traced" if trace else "Doctor bad" if doctor_bad else None
    return _BuildPolicy("-".join(variants), fresh_root_label)


def _trace_overlay(handle: TargetHandle) -> Any:
    return trace_overlay.write_trace_overlay(handle)


def _is_cocotb(flow_options: Mapping[str, Any]) -> bool:
    module = flow_options.get("cocotb_module")
    return isinstance(module, str) and bool(module)


def _work_groups(selection: SimulationSelection, cocotb: bool) -> tuple[tuple[str, ...], ...]:
    if isinstance(selection, DefaultSelection):
        return ((),)
    if not isinstance(selection, NamedTests):
        raise TypeError(f"unsupported Simulation selection: {type(selection).__name__}")
    return (selection.names,) if cocotb else tuple((name,) for name in selection.names)


def simulation_target_environment(handle: TargetHandle) -> dict[str, str]:
    """Return the resolved project environment for one Simulation Target."""
    sections = load_test_configuration_field(handle.project_root, "env")
    environment = dict(lookup_target_section(sections, handle.selector) or {})
    return {str(name): str(value) for name, value in environment.items()}


def _simulation_plusargs(
    handle: TargetHandle,
    test_names: tuple[str, ...],
    parameters: Mapping[str, Any],
) -> list[str]:
    rendered = _parameter_plusargs(parameters)
    if len(test_names) != 1:
        return rendered
    registry = load_test_configuration_field(handle.project_root, "tests")
    available = lookup_target_section(registry, handle.selector) or []
    if test_names[0] not in available:
        return rendered
    from booley.config.project_config import render_test_selector

    selector = render_test_selector(
        handle.selector,
        available.index(test_names[0]),
        test_names[0],
        work_dir=handle.project_root,
    ).removeprefix("+")
    key = _plusarg_key(selector)
    return [value for value in rendered if _plusarg_key(value) != key] + [selector]


def _parameter_plusargs(parameters: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for raw_name, raw_spec in parameters.items():
        if not isinstance(raw_spec, Mapping) or raw_spec.get("paramtype") != "plusarg":
            continue
        if "default" not in raw_spec:
            continue
        name = str(raw_name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            raise ValueError(f"invalid resolved plusarg parameter name: {name!r}")
        value = raw_spec["default"]
        text = "1" if value is True else "0" if value is False else str(value)
        if "\x00" in text:
            raise ValueError(f"resolved plusarg parameter {name!r} contains a NUL byte")
        result.append(f"{name}={text}")
    return result


def _plusarg_key(value: str) -> str | None:
    stripped = value.removeprefix("+")
    return None if stripped.startswith("-") else stripped.partition("=")[0] or None


def _configured_run_cwd(root: Path) -> Path:
    """Freeze the configured source runtime directory for one attempt."""
    return (root / resolve_run_cwd(root)).resolve()


def _simulation_run_cwd(
    root: Path,
    work_root: Path,
    *,
    attempt_token: str | None = None,
    configured_run_cwd: Path | None = None,
) -> str:
    if os.environ.get(selftest_overlay.INTERNAL_KIND_ENV) == selftest_overlay.BAD_KIND:
        if attempt_token is None:
            return "<attempt>"
        return (
            selftest_overlay.doctor_runtime_view_path(root, work_root.parent, attempt_token)
            .relative_to(root)
            .as_posix()
        )
    if configured_run_cwd is None:
        return resolve_run_cwd(root)
    try:
        return configured_run_cwd.relative_to(root).as_posix() or "."
    except ValueError:
        return str(configured_run_cwd)


def prepare_simulation_work(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    identity: AdapterTransportIdentity,
    options: SimulationOptions,
    *,
    trace: bool,
    trace_mode: str,
    configured_run_cwd: Path | None = None,
    plusargs_suffix: tuple[str, ...] = (),
) -> PreparedSimulationWork:
    """Shape one prepared build through the shared Simulation adapter contract."""
    root = handle.project_root
    rel = edam_layer.relpath_for_make(prepared.build_root, root)
    cocotb = bool(prepared.resolved.cocotb_module)
    plusargs = _simulation_plusargs(handle, identity.selected_tests, prepared.resolved.parameters)
    passes, fails = resolve_sim_sentinels(root)
    return PreparedSimulationWork(
        adapter="cocotb" if cocotb else prepared.eda_tool,
        build_dir=rel,
        run_cwd=_simulation_run_cwd(
            root,
            prepared.work_root,
            attempt_token=identity.attempt_token,
            configured_run_cwd=configured_run_cwd,
        ),
        timeout_s=max(1, (options.timeout_ms or resolve_sim_timeout_ms(root)) // 1000),
        eda_tool=prepared.eda_tool,
        max_rundir_bytes=resolve_max_rundir_bytes(root),
        plusargs=(*plusargs, *plusargs_suffix),
        trace=trace,
        trace_mode=trace_mode,
        trace_scope=prepared.toplevel,
        trace_args=tuple(resolve_trace_args(root)),
        trace_files=tuple(resolve_trace_files(root)),
        runtime_inputs=declared_runtime_inputs(prepared.resolved),
        pass_sentinels=tuple(passes),
        fail_sentinels=tuple(fails),
        top=prepared.toplevel,
        cocotb_module=prepared.resolved.cocotb_module or "",
        tests=identity.selected_tests,
        result_verbosity=options.result_verbosity,
        sim_time_grace_s=resolve_sim_time_grace_s(root),
        adapter_result_path=str(identity.result_path),
        attempt_token=identity.attempt_token,
        target_identity=identity.target_identity,
    )


def _preview_work(
    execution: SimulationExecution,
    handle: TargetHandle,
    inspection: Any,
    test_names: tuple[str, ...],
    cocotb: bool,
    rel: str,
) -> PreparedSimulationWork:
    root = handle.project_root
    eda_tool = sim_edam.normalize_eda_tool(inspection.eda_tool)
    passes, fails = resolve_sim_sentinels(root)
    return PreparedSimulationWork(
        adapter="cocotb" if cocotb else eda_tool,
        build_dir=rel,
        run_cwd=_simulation_run_cwd(root, root / rel),
        timeout_s=max(1, execution._effective_timeout_ms(handle) // 1000),
        eda_tool=eda_tool,
        max_rundir_bytes=resolve_max_rundir_bytes(root),
        plusargs=tuple(_simulation_plusargs(handle, test_names, inspection.parameters)),
        trace=execution._options.trace,
        trace_scope=inspection.toplevel,
        trace_args=tuple(resolve_trace_args(root)),
        trace_files=tuple(resolve_trace_files(root)),
        runtime_inputs=preview_runtime_inputs(getattr(inspection, "inputs", ())),
        pass_sentinels=tuple(passes),
        fail_sentinels=tuple(fails),
        top=inspection.toplevel,
        cocotb_module=str(inspection.flow_options.get("cocotb_module") or ""),
        tests=test_names,
        result_verbosity=execution._options.result_verbosity,
        sim_time_grace_s=resolve_sim_time_grace_s(root),
    )


def _preview_exports(handle: TargetHandle, names: tuple[str, ...], build_root: Path) -> list[str]:
    root = handle.project_root
    values = {
        **simulation_target_environment(handle),
        "BOOLEY_TARGET": handle.selector,
        "BOOLEY_TEST_NAMES": " ".join(names),
        "BOOLEY_PROJECT_ROOT": str(root),
        "BOOLEY_RUN_CWD": str((root / resolve_run_cwd(root)).resolve()),
        "BOOLEY_BUILD_ROOT": str(build_root),
    }
    if len(names) == 1:
        values["BOOLEY_TEST_NAME"] = names[0]
    return [f"export {name}={shlex.quote(value)}" for name, value in values.items()]


def _pre_sim_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    evidence: PreSimEvidence,
    started: float,
) -> SimulationTargetOutcome:
    names = attempt.test_names or (handle.selector,)
    detail = evidence.detail or f"pre-sim commands {evidence.status}"
    tests = tuple(
        SimulationTestOutcome(
            name=name,
            verdict="elab_error",
            passed=False,
            elapsed_s=evidence.elapsed_s,
            error_tail=f"pre-sim commands failed ({evidence.status}): {detail}",
            elab_failed=True,
        )
        for name in names
    )
    tests = (
        replace(
            tests[0],
            phase_timings_s={
                "setup": round(attempt.setup_s, 3),
                "pre_sim": round(evidence.elapsed_s, 3),
                "build": 0.0,
                "run": 0.0,
                "result_processing": 0.0,
            },
        ),
        *tests[1:],
    )
    return _group_outcome(handle, attempt, tests, None, evidence, (), started)


def _artifact_policies(
    handle: TargetHandle,
    attempt: _Attempt,
) -> tuple[TraceArtifactPolicy | None, CompatibilityArtifactPolicy]:
    trace = _trace_artifact_policy(handle, attempt) if attempt.trace_requested else None
    compatibility = CompatibilityArtifactPolicy.capture(attempt.prepared.build_root)
    return trace, compatibility


def _pre_sim_infrastructure_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    evidence: PreSimEvidence,
    started: float,
) -> SimulationTargetOutcome:
    detail = evidence.detail or "could not start Pre-Sim Commands"
    failure = SimulationInfrastructureFailure(
        "pre_sim",
        "Pre-Sim Commands could not start",
        missing_executable=find_missing_executable(detail) or "",
        detail=detail,
    )
    return _error_outcome(handle, attempt, None, evidence, failure, started)


def _infrastructure_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome,
    pre_sim: PreSimEvidence | None,
    started: float,
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure("build", build.reason, detail=build.output)
    return _error_outcome(handle, attempt, build, pre_sim, failure, started)


def _adapter_failure_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    executed: AdapterAttemptOutcome,
    build: BuildOutcome,
    pre_sim: PreSimEvidence | None,
    started: float,
) -> SimulationTargetOutcome | None:
    if build.failure_kind == "infrastructure":
        return _infrastructure_failure(handle, attempt, build, pre_sim, started)
    adapter_error = _adapter_attempt_error(executed, build)
    if adapter_error is not None:
        return _transport_failure(handle, attempt, build, pre_sim, adapter_error, started)
    return None


def _transport_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome,
    pre_sim: PreSimEvidence | None,
    detail: str,
    started: float,
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure("adapter_protocol", detail, detail=detail)
    return _error_outcome(handle, attempt, build, pre_sim, failure, started)


def _runtime_input_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    pre_sim: PreSimEvidence | None,
    detail: str,
    started: float,
) -> SimulationTargetOutcome:
    message = f"Simulation runtime input setup failed: {detail}"
    failure = SimulationInfrastructureFailure("runtime_input", message, detail=message)
    return _error_outcome(handle, attempt, None, pre_sim, failure, started)


def _artifact_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome | None,
    pre_sim: PreSimEvidence | None,
    detail: str,
    started: float,
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure("artifact_persistence", detail, detail=detail)
    return _error_outcome(handle, attempt, build, pre_sim, failure, started)


def _error_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome | None,
    pre_sim: PreSimEvidence | None,
    failure: SimulationInfrastructureFailure,
    started: float,
) -> SimulationTargetOutcome:
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel=attempt.prepared.toplevel,
        eda_tool=attempt.prepared.eda_tool,
        passed=False,
        verdict="error",
        elapsed_s=time.monotonic() - started,
        tests=(),
        builds=(build,) if build is not None else (),
        pre_sim_runs=(pre_sim,) if pre_sim is not None else (),
        infrastructure_failure=failure,
    )


def _adapter_identity(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    test_names: tuple[str, ...],
    adapter: str,
    *,
    campaign_build: bool,
) -> AdapterTransportIdentity:
    token = new_attempt_token()
    result_name = f".a-{token[:24]}.json" if campaign_build else f".booley-adapter-{token}.json"
    return AdapterTransportIdentity(
        adapter=adapter,
        attempt_token=token,
        target_identity=handle.identity,
        selected_tests=test_names,
        result_path=prepared.build_root / result_name,
    )


def _adapter_command(
    prepared: PreparedSimulationBuild,
    identity: AdapterTransportIdentity,
    work: PreparedSimulationWork,
    environment: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    try:
        invocation = prepare_adapter_invocation(work)
    except UnsupportedSimulationAdapterError as exc:
        raise SimulationBuildPreparationError(str(exc)) from exc
    script = build_stage_script(
        prepared.make_argv,
        identity.attempt_token,
        run_line=shlex.join(invocation),
        environment=dict(environment),
    )
    return ("sh", "-c", script)


def _build_slot_failure(
    handle: TargetHandle, exc: SimulationBuildSlotError, started: float
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure(
        "build", "Simulation build provenance failed", detail=str(exc)
    )
    return _setup_infrastructure_failure(handle, failure, started)


def _build_root_failure(
    handle: TargetHandle, exc: _BuildRootResetError, started: float
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure(
        "build",
        "Simulation build root could not be reset",
        detail=str(exc),
    )
    return _setup_infrastructure_failure(handle, failure, started)


def _aggregate_group_results(
    handle: TargetHandle,
    toplevel: str,
    results: list[SimulationTargetOutcome],
    started: float,
) -> SimulationTargetOutcome:
    failure = next(
        (result.infrastructure_failure for result in results if result.infrastructure_failure),
        None,
    )
    tests = tuple(test for result in results for test in result.tests)
    builds = tuple(build for result in results for build in result.builds)
    pre_sim_runs = tuple(item for result in results for item in result.pre_sim_runs)
    artifacts = tuple(item for result in results for item in result.artifacts)
    return _aggregate(
        handle,
        toplevel,
        results,
        tests,
        builds,
        pre_sim_runs,
        artifacts,
        failure,
        started,
    )


def _setup_infrastructure_failure(
    handle: TargetHandle,
    failure: SimulationInfrastructureFailure,
    started: float,
) -> SimulationTargetOutcome:
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel="",
        eda_tool="",
        passed=False,
        verdict="error",
        elapsed_s=time.monotonic() - started,
        tests=(),
        infrastructure_failure=failure,
    )


def _setup_failure(handle: TargetHandle, detail: str, started: float) -> SimulationTargetOutcome:
    test = SimulationTestOutcome(
        name=handle.selector,
        verdict="elab_error",
        passed=False,
        error_tail=f"sim setup failed: {detail}",
        elab_failed=True,
    )
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel="",
        eda_tool="",
        passed=False,
        verdict="fail",
        elapsed_s=time.monotonic() - started,
        tests=(test,),
    )


def _group_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    tests: tuple[SimulationTestOutcome, ...],
    build: BuildOutcome | None,
    pre_sim: PreSimEvidence | None,
    artifacts: tuple[SimulationArtifactEvidence, ...],
    started: float,
    diagnostics: tuple[str, ...] = (),
    adapter_passed: bool | None = None,
) -> SimulationTargetOutcome:
    inconclusive = any(test.inconclusive for test in tests)
    passed = (
        bool(tests)
        and all(test.passed for test in tests)
        and not inconclusive
        and adapter_passed is not False
    )
    elapsed_s = time.monotonic() - started
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel=attempt.prepared.toplevel,
        eda_tool=attempt.prepared.eda_tool,
        passed=passed,
        verdict="pass" if passed else "inconclusive" if inconclusive else "fail",
        elapsed_s=elapsed_s,
        tests=tests,
        builds=(build,) if build is not None else (),
        pre_sim_runs=(pre_sim,) if pre_sim is not None else (),
        artifacts=artifacts,
        diagnostics=diagnostics,
        phase_timings_s=_target_phase_timings(tests, elapsed_s),
    )


def _aggregate(
    handle: TargetHandle,
    toplevel: str,
    groups: list[SimulationTargetOutcome],
    tests: tuple[SimulationTestOutcome, ...],
    builds: tuple[BuildOutcome, ...],
    pre_sim_runs: tuple[PreSimEvidence, ...],
    artifacts: tuple[SimulationArtifactEvidence, ...],
    failure: SimulationInfrastructureFailure | None,
    started: float,
) -> SimulationTargetOutcome:
    inconclusive = any(test.inconclusive for test in tests)
    passed = (
        bool(tests)
        and all(test.passed for test in tests)
        and all(group.passed for group in groups)
        and failure is None
    )
    eda_tool = next((group.eda_tool for group in groups if group.eda_tool), "")
    verdict = (
        "error" if failure else "pass" if passed else "inconclusive" if inconclusive else "fail"
    )
    elapsed_s = time.monotonic() - started
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel=toplevel,
        eda_tool=eda_tool,
        passed=passed,
        verdict=verdict,
        elapsed_s=elapsed_s,
        tests=tests,
        builds=builds,
        pre_sim_runs=pre_sim_runs,
        artifacts=artifacts,
        diagnostics=tuple(note for group in groups for note in group.diagnostics),
        infrastructure_failure=failure,
        phase_timings_s=_target_phase_timings(tests, elapsed_s),
    )


def _target_phase_timings(
    tests: tuple[SimulationTestOutcome, ...],
    elapsed_s: float,
) -> dict[str, float]:
    phases: dict[str, float] = {}
    for test in tests:
        for name, duration in test.phase_timings_s.items():
            phases[name] = phases.get(name, 0.0) + duration
    phases["unattributed"] = max(0.0, elapsed_s - sum(phases.values()))
    phases["execution_total"] = elapsed_s
    return {name: round(duration, 3) for name, duration in phases.items()}


def _test_outcomes(
    handle: TargetHandle,
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    adapter: AdapterResult | None,
    output: str,
    logs: tuple[SimulationArtifactEvidence, ...],
    trace: SimulationArtifactEvidence | None,
) -> tuple[SimulationTestOutcome, ...]:
    if build.design_failed:
        name = attempt.test_names[0] if attempt.test_names else handle.selector
        return (_build_failure_test(handle, attempt, process, build, _run_log_for(logs, name)),)
    names = _outcome_names(handle, attempt, adapter)
    by_name = {test.name: test for test in adapter.test_results} if adapter else {}
    results = []
    for index, name in enumerate(names):
        item = by_name.get(name)
        verdict = (
            item.verdict
            if item
            else "timeout"
            if process.timed_out
            else "crash"
            if process.returncode < 0
            else _adapter_verdict(adapter)
        )
        cycle_status, cycles = _cycle_observation(output, name, attempt.cycle_sentinels)
        results.append(
            _test_outcome(
                handle,
                attempt,
                process,
                build,
                adapter,
                item,
                name,
                verdict,
                cycle_status,
                cycles,
                _run_log_for(logs, name),
                trace if index == 0 else None,
                adapter.sva_errors if adapter is not None and index == 0 else 0,
            )
        )
    return tuple(results)


def _outcome_names(
    handle: TargetHandle,
    attempt: _Attempt,
    adapter: AdapterResult | None,
) -> tuple[str, ...]:
    if adapter and adapter.test_results:
        return tuple(test.name for test in adapter.test_results)
    fallback = "" if attempt.adapter == "cocotb" else handle.selector
    return attempt.test_names or (fallback,)


def _adapter_verdict(adapter: AdapterResult | None) -> str:
    if adapter is None:
        return "fail"
    if adapter.passed:
        return "pass"
    return "inconclusive" if adapter.inconclusive else "fail"


def _test_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    adapter: AdapterResult | None,
    item: Any,
    name: str,
    verdict: str,
    cycle_status: str,
    cycles: int | None,
    log: SimulationArtifactEvidence | None,
    trace: SimulationArtifactEvidence | None,
    sva_errors: int,
) -> SimulationTestOutcome:
    inconclusive = verdict == "inconclusive"
    passed = verdict == "pass" and adapter is not None and sva_errors == 0
    detail = item.detail if item else adapter.detail if adapter else ""
    reason = detail
    if verdict == "timeout" and not reason:
        reason = f"TIMEOUT: simulation exceeded {_timeout_ms(process)} ms"
    if verdict == "crash" and not reason:
        reason = f"simulator process terminated by signal {-process.returncode}"
    if inconclusive and not reason:
        reason = _NO_WAVEFORM if attempt.trace_requested and trace is None else _NO_SENTINEL
    return SimulationTestOutcome(
        name=name,
        verdict=verdict,
        passed=passed,
        elapsed_s=item.elapsed_s if item and item.elapsed_s else process.duration_s,
        cycles=cycles,
        cycle_status=cycle_status,
        inconclusive=inconclusive,
        reason=reason,
        sva_errors=sva_errors,
        error_tail="" if passed else reason or _output_tail(process, build.design_failed),
        timed_out=verdict == "timeout",
        crashed=verdict == "crash",
        build=build,
        artifacts=tuple(item for item in (log, trace) if item is not None),
        run_log_path=log.path if log is not None else "",
        workload_snapshot=_workload_snapshot(handle, attempt, name),
    )


def _timeout_ms(process: SubprocessResult) -> int:
    return max(1, round(process.duration_s * 1000))


def _build_failure_test(
    handle: TargetHandle,
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    log: SimulationArtifactEvidence | None,
) -> SimulationTestOutcome:
    name = attempt.test_names[0] if len(attempt.test_names) == 1 else handle.selector
    return SimulationTestOutcome(
        name=name,
        verdict="elab_error",
        passed=False,
        elapsed_s=process.duration_s,
        error_tail=_output_tail(process, True),
        elab_failed=True,
        build=build,
        artifacts=(log,) if log is not None else (),
        run_log_path=log.path if log is not None else "",
        workload_snapshot=_workload_snapshot(handle, attempt, name),
    )


def _with_workload_inputs(handle: TargetHandle, attempt: _Attempt) -> _Attempt:
    inputs = capture_workload_inputs(handle.project_root, attempt.prepared.resolved)
    return replace(attempt, workload_inputs=inputs)


def _workload_snapshot(
    handle: TargetHandle,
    attempt: _Attempt,
    name: str,
) -> Mapping[str, Any]:
    controls = {
        "cycle_sentinels": list(attempt.cycle_sentinels),
        "pre_run_commands": list(attempt.pre_sim_commands),
        "run_cwd": attempt.work.run_cwd,
        "environment": dict(attempt.simulator_environment),
    }
    return build_workload_snapshot(
        handle.project_root,
        handle.selector,
        name,
        attempt.prepared.resolved,
        controls=controls,
        inputs=attempt.workload_inputs,
    )


def _run_log_for(
    artifacts: tuple[SimulationArtifactEvidence, ...],
    name: str,
) -> SimulationArtifactEvidence | None:
    archive = next(
        (item for item in artifacts if item.kind == "run_log" and item.test_names == (name,)),
        None,
    )
    if archive is not None:
        return archive
    return next(
        (item for item in artifacts if item.kind == "live_run_log" and name in item.test_names),
        None,
    )


def _persist_run_logs(
    handle: TargetHandle,
    attempt: _Attempt,
    output: str,
    artifact_root: Path | None,
) -> tuple[SimulationArtifactEvidence, ...]:
    build_root = attempt.prepared.build_root
    try:
        live_path = write_run_log(build_root, output)
        live = validate_fresh_artifact(live_path, roots=(build_root,), before=None)
        archives = _archive_run_logs(handle, attempt, output, artifact_root)
    except (OSError, ArtifactValidationError) as exc:
        raise SimulationArtifactPersistenceError(f"could not preserve run log: {exc}") from exc
    live_evidence = SimulationArtifactEvidence(
        "live_run_log", str(live.path), live.size, attempt.test_names
    )
    return (live_evidence, *archives)


def _archive_run_logs(
    handle: TargetHandle,
    attempt: _Attempt,
    output: str,
    artifact_root: Path | None,
) -> tuple[SimulationArtifactEvidence, ...]:
    if artifact_root is None or not output or attempt.adapter == "cocotb":
        return ()
    names = attempt.test_names or (handle.selector,)
    target = artifact_path_component(f"sim_{handle.selector}")
    evidence: list[SimulationArtifactEvidence] = []
    for name in names:
        test = artifact_path_component(name)
        directory = artifact_root / "artifacts" / target / "tests" / test
        directory.mkdir(parents=True, exist_ok=True)
        path = write_run_log(directory, output, max_bytes=None)
        validated = validate_fresh_artifact(path, roots=(artifact_root,), before=None)
        evidence.append(
            SimulationArtifactEvidence("run_log", str(validated.path), validated.size, (name,))
        )
    return tuple(evidence)


def _trace_artifact(
    attempt: _Attempt,
    adapter: AdapterResult | None,
    trace_policy: TraceArtifactPolicy | None,
) -> SimulationArtifactEvidence | None:
    trace = adapter.trace if adapter is not None else None
    if trace_policy is None or trace is None or trace.status != "ok":
        return None
    try:
        evidence = trace_policy.validate_reported(trace.path)
    except ArtifactValidationError:
        return None
    return SimulationArtifactEvidence(
        "trace",
        str(evidence.path),
        evidence.size,
        attempt.test_names,
        trace.top_scope,
        trace.signal_count,
        trace.total_ticks,
    )


def _compatibility_artifacts(
    attempt: _Attempt,
    policy: CompatibilityArtifactPolicy,
) -> tuple[SimulationArtifactEvidence, ...]:
    return tuple(
        SimulationArtifactEvidence(kind, str(item.path), item.size, attempt.test_names)
        for kind, item in policy.current()
    )


def _trace_artifact_policy(
    handle: TargetHandle,
    attempt: _Attempt,
) -> TraceArtifactPolicy:
    run_cwd = Path(attempt.work.run_cwd)
    if not run_cwd.is_absolute():
        run_cwd = handle.project_root / run_cwd
    return TraceArtifactPolicy.capture(
        run_cwd=run_cwd,
        build_root=attempt.prepared.build_root,
        patterns=attempt.work.trace_files,
    )


def _missing_trace_result(adapter: AdapterResult) -> AdapterResult:
    tests = tuple(
        replace(test, verdict="inconclusive", detail=_NO_WAVEFORM)
        if test.verdict == "pass"
        else test
        for test in adapter.test_results
    )
    return replace(
        adapter,
        passed=False,
        inconclusive=True,
        failure_kind="artifact",
        detail=_NO_WAVEFORM,
        test_results=tests,
    )


def _cycle_observation(
    output: str,
    name: str,
    configured_sentinels: tuple[str, ...],
) -> tuple[str, int | None]:
    sentinels = configured_sentinels or (_DEFAULT_CYCLE_SENTINEL,)
    records = _cycle_records(output, sentinels)
    named = [parts for parts in records if len(parts) >= 2 and " ".join(parts[:-1]) == name]
    legacy = [parts[0] for parts in records if len(parts) == 1]
    if len(named) == 1 and named[0][-1].isdigit():
        return "observed", int(named[0][-1])
    if len(named) > 1 or len(legacy) > 1:
        return "duplicate", None
    if len(legacy) == 1 and legacy[0].isdigit():
        return "legacy", int(legacy[0])
    return ("wrong_test" if records else "missing"), None


def _cycle_records(output: str, sentinels: tuple[str, ...]) -> list[list[str]]:
    records = []
    for line in output.splitlines():
        sentinel = next((value for value in sentinels if value in line), None)
        if sentinel is not None:
            records.append(line.split(sentinel, 1)[1].strip().split())
    return records


def _attach_group_telemetry(
    tests: tuple[SimulationTestOutcome, ...],
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    pre_sim: PreSimEvidence | None,
    output: str,
    result_processing_s: float,
) -> tuple[SimulationTestOutcome, ...]:
    if not tests:
        return tests
    build_s = parse_build_seconds(output)
    run_s = parse_run_seconds(output)
    if run_s is None:
        run_s = max(0.0, process.duration_s - build_s) if build.passed else 0.0
    phases = {
        "setup": round(attempt.setup_s, 3),
        "pre_sim": round(pre_sim.elapsed_s if pre_sim else 0.0, 3),
        "build": round(build_s, 3),
        "run": round(run_s, 3),
        "result_processing": round(result_processing_s, 3),
    }
    first = replace(
        tests[0],
        build_s=build_s,
        phase_timings_s=phases,
        resources=process_resources(output, process),
    )
    return (first, *tests[1:])


def _output_tail(process: SubprocessResult, build_failed: bool) -> str:
    combined = process.stdout + ("\n" + process.stderr if process.stderr else "")
    source = combined if build_failed or not process.stdout.strip() else process.stdout
    return "\n".join(source.strip().splitlines()[-50:])


__all__ = ["PreparedOrdinaryGroup", "SimulationExecution"]
