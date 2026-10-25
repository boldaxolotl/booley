"""Scoped, observational Simulation stages independent of execution authority."""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from booley.flows.progress_lifecycle import TERMINAL_PHASES, write_progress_json
from booley.flows.run_log import RunLogIdentity, current_run_token
from booley.flows.sim.campaign_reports import simulation_booley_dir
from booley.runtime.timefmt import utc_now_rfc3339

_STAGES = frozenset({"preparing", "pre_sim", "building", "executing", "postprocessing"})


@dataclass(frozen=True)
class AttemptScope:
    target: str
    key: str
    identity: str = ""
    tests: tuple[str, ...] = ()
    attempt_id: str = ""
    role: str = "candidate"
    revision: str = ""
    ephemeral_root: Path | None = None
    operation: str = "simulation"


_sink: ContextVar[LiveProgressSink | None] = ContextVar("simulation_live_progress", default=None)
_scope: ContextVar[AttemptScope | None] = ContextVar("simulation_live_attempt", default=None)


class LiveProgressSink:
    """Own the full checkpoint and serialize active attempts with terminal writes."""

    def __init__(self, path: Path, project_root: Path, run_id: str) -> None:
        self.path: Path = path
        self.project_root: Path = project_root.resolve()
        self.run_id: str = run_id
        self.run_token: str = current_run_token()
        self._lock = threading.RLock()
        self._base: dict[str, object] | None = None
        self._active: dict[str, dict[str, object]] = {}
        self._retired: set[str] = set()
        self._logs: dict[str, RunLogIdentity] = {}
        self._closed = False
        self.error: str | None = None

    def checkpoint(self, document: Mapping[str, object]) -> None:
        """Publish authoritative returned outcomes; allow terminal repair retries."""
        if document.get("run_id") != self.run_id:
            raise ValueError("checkpoint belongs to a different Simulation invocation")
        with self._lock:
            previous = self._base
            self._base = copy.deepcopy(dict(document))
            if document.get("phase") in TERMINAL_PHASES:
                self._closed = True
                self._active.clear()
            try:
                write_progress_json(self.path, self._document())
            except (OSError, ValueError, TypeError):
                self._base = previous
                raise

    def observe(
        self,
        scope: AttemptScope,
        stage: str,
        root: Path | None,
        token: str,
        log: RunLogIdentity | None,
    ) -> None:
        with self._lock:
            if self._closed or scope.key in self._retired or current_run_token() != self.run_token:
                return
            if log is not None:
                self._logs[scope.key] = log
            value: dict[str, object] = {
                "target": scope.target,
                "identity": scope.identity,
                "tests": list(scope.tests),
                "attempt_id": scope.attempt_id or token or scope.key,
                "role": scope.role,
                "stage": stage,
                "operation": scope.operation,
            }
            if scope.revision:
                value["revision"] = scope.revision
            log = self._logs.get(scope.key)
            if root is not None and log is not None and log.target == scope.target:
                pointer = log.pointer(root, token, self.project_root)
                if pointer is not None:
                    value["log"] = pointer
            self._active[scope.key] = value
            self._publish_active()

    def retire(self, key: str) -> None:
        with self._lock:
            self._retired.add(key)
            self._logs.pop(key, None)
            if self._closed:
                return
            if self._active.pop(key, None) is not None:
                self._publish_active()

    def _document(self) -> dict[str, object]:
        assert self._base is not None
        document = copy.deepcopy(self._base)
        document["timestamp"] = utc_now_rfc3339()
        document.pop("active", None)
        if self._active and not self._closed:
            document["active"] = list(self._active.values())
            candidate = [item for item in document["active"] if item["role"] == "candidate"]
            document["phase"] = (
                "running"
                if any(item["stage"] in {"executing", "postprocessing"} for item in candidate)
                else ("starting" if candidate else "baseline")
            )
        if self.error:
            document["observation_health"] = {"error": self.error}
        return document

    def _record_error(self, error: Exception) -> None:
        self.error = f"{type(error).__name__}: {error}"[-1024:]

    def _publish_active(self) -> None:
        if self._base is None:
            return
        try:
            write_progress_json(self.path, self._document(), lock_timeout_s=0.05)
        except (OSError, ValueError, TypeError) as error:
            self._record_error(error)


@contextmanager
def install_progress(sink: LiveProgressSink) -> Iterator[LiveProgressSink]:
    token = _sink.set(sink)
    try:
        yield sink
    finally:
        _sink.reset(token)


def publish_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
    """Route mandatory publication through the current invocation's snapshot."""
    sink = _sink.get()
    if sink is None or path != sink.path:
        write_progress_json(path, payload)
    else:
        sink.checkpoint(payload)


@contextmanager
def attempt_scope(
    target: str,
    *,
    identity: str = "",
    tests: tuple[str, ...] = (),
    attempt_id: str = "",
    role: str = "candidate",
    revision: str = "",
    ephemeral_root: Path | None = None,
    operation: str = "simulation",
) -> Iterator[None]:
    parent = _scope.get()
    nested = (
        parent is not None
        and parent.target == target
        and (not attempt_id or attempt_id == parent.attempt_id)
    )
    scope = AttemptScope(
        target,
        parent.key if nested else uuid.uuid4().hex,
        identity,
        tests,
        attempt_id or (parent.attempt_id if parent else ""),
        parent.role if parent else role,
        parent.revision if parent else revision,
        parent.ephemeral_root if parent else ephemeral_root.resolve() if ephemeral_root else None,
        operation,
    )
    token = _scope.set(scope)
    try:
        observe_stage(target, "preparing")
        yield
    finally:
        sink = _sink.get()
        if sink is not None and (parent is None or scope.key != parent.key):
            sink.retire(scope.key)
        _scope.reset(token)


def observe_stage(
    target: str,
    stage: str,
    *,
    evidence_root: Path | None = None,
    attempt_token: str = "",
    log: RunLogIdentity | None = None,
    tests: tuple[str, ...] = (),
) -> None:
    """Publish one real lifecycle transition, never infer a verdict."""
    if stage not in _STAGES:
        raise ValueError(f"invalid Simulation observation stage: {stage}")
    sink = _sink.get()
    if sink is None:
        return
    scope = _scope.get() or AttemptScope(target, f"{threading.get_ident()}:{target}", tests=tests)
    if scope.target != target:
        scope = replace(scope, target=target, identity="", tests=tests)
    elif tests:
        scope = replace(scope, tests=tests)
    if scope.role not in {"candidate", "baseline"} or not scope.target:
        raise ValueError("Simulation observations require a Target and valid attempt role")
    sink.observe(scope, stage, evidence_root, attempt_token, log)


def current_progress() -> LiveProgressSink | None:
    """Return the scoped invocation observer, when installed."""
    return _sink.get()


def prepare_attempt_log(
    root: Path, target: str, token: str, project: Path, *, required: bool = False
) -> RunLogIdentity | None:
    """Hand the writer its owned roots; preserve each caller's failure policy."""
    sink, scope = _sink.get(), _scope.get()
    allowed = [project.resolve()]
    if sink is not None:
        allowed.append(sink.path.parent.absolute())
    if scope is not None and scope.ephemeral_root is not None:
        allowed.append(scope.ephemeral_root)
    try:
        return RunLogIdentity.create(
            root, target, token, tuple(allowed), booley_dir=simulation_booley_dir(project)
        )
    except OSError as error:
        if required:
            raise
        if sink is not None:
            sink._record_error(error)
        return None
