"""Scoped, observational Simulation stages independent of execution authority."""

from __future__ import annotations

import copy
import os
import stat
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from booley.flows.progress_lifecycle import TERMINAL_PHASES, write_progress_json
from booley.flows.run_log import RUN_LOG_HEADER_PREFIX, RUN_LOG_PENDING, current_run_token
from booley.runtime.regular_file import open_regular_nofollow
from booley.runtime.timefmt import utc_now_rfc3339

_STAGES = frozenset({"preparing", "pre_sim", "building", "executing", "postprocessing"})


def _absolute_nofollow(path: Path) -> Path:
    # Normalize dot components while preserving links for explicit rejection.
    return Path(os.path.abspath(path))  # noqa: PTH100 — resolve would hide symbolic links


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


@dataclass(frozen=True)
class StageObservation:
    """An immutable lifecycle event with its exact allocated evidence root."""

    scope: AttemptScope
    stage: str
    evidence_root: Path | None = None
    attempt_token: str = ""

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ValueError(f"invalid Simulation observation stage: {self.stage}")
        if self.scope.role not in {"candidate", "baseline"} or not self.scope.target:
            raise ValueError("Simulation observations require a Target and valid attempt role")


_sink: ContextVar[LiveProgressSink | None] = ContextVar("simulation_live_progress", default=None)
_scope: ContextVar[AttemptScope | None] = ContextVar("simulation_live_attempt", default=None)


class LiveProgressSink:
    """Own the full checkpoint and serialize active attempts with terminal writes."""

    def __init__(self, path: Path, project_root: Path, run_id: str) -> None:
        self.path = path
        self.project_root = _absolute_nofollow(project_root)
        self.run_id = run_id
        self.run_token = current_run_token()
        self._lock = threading.RLock()
        self._base: dict[str, object] | None = None
        self._active: dict[str, dict[str, object]] = {}
        self._initialized: dict[Path, tuple[str, str, str]] = {}
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

    def observe(self, observation: StageObservation) -> None:
        scope, stage = observation.scope, observation.stage
        root, token = observation.evidence_root, observation.attempt_token
        with self._lock:
            if self._closed or current_run_token() != self.run_token:
                return
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
            if root is not None:
                pointer = self._pointer(root, scope, token)
                if pointer is not None:
                    value["log"] = pointer
            self._active[scope.key] = value
            self._publish_active()

    def retire(self, key: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._initialized = {
                root: owned for root, owned in self._initialized.items() if owned[1] != key
            }
            if self._active.pop(key, None) is not None:
                self._publish_active()

    def initialize_log(self, root: Path, target: str, scope: AttemptScope, token: str) -> None:
        """Create an owned observation header without following planted links."""
        with self._lock:
            if self._closed:
                return
            root = _absolute_nofollow(root)
            self._initialized.pop(root, None)
            try:
                path = root / "run.log"
                self._validate_path(path, scope)
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                descriptor = os.open(path, flags, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("active run log must be regular")
                    stream.truncate(0)
                    stream.write(
                        f"{RUN_LOG_HEADER_PREFIX} run={self.run_token} flow=sim target={target} started={utc_now_rfc3339()}\n{RUN_LOG_PENDING}\n"
                    )
                self._initialized[root.absolute()] = (target, scope.key, token)
            except (OSError, ValueError) as error:
                self._record_error(error)

    def _validate_path(self, path: Path, scope: AttemptScope) -> None:
        containing = self.project_root
        if path.is_relative_to(self.path.parent.absolute()):
            containing = self.path.parent.absolute()
        if scope.ephemeral_root is not None and path.is_relative_to(
            scope.ephemeral_root.absolute()
        ):
            containing = scope.ephemeral_root.absolute()
        path.relative_to(containing)
        current = path
        while current != containing.parent:
            try:
                info = current.lstat()
            except FileNotFoundError:
                if current != path:
                    raise
            else:
                if current == path and not stat.S_ISREG(info.st_mode):
                    raise ValueError("active run log must be regular")
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("active evidence path contains a symbolic link")
            current = current.parent

    def _pointer(self, root: Path, scope: AttemptScope, token: str) -> dict[str, object] | None:
        root = _absolute_nofollow(root)
        owned = self._initialized.get(root)
        if (
            owned is None
            or owned[:2] != (scope.target, scope.key)
            or (token and token != owned[2])
        ):
            return None
        target = owned[0]
        path = root / "run.log"
        try:
            self._validate_path(path, scope)
            with os.fdopen(open_regular_nofollow(path), "r", encoding="utf-8") as stream:
                header = stream.readline(4096)
            fields = dict(part.split("=", 1) for part in header.split()[2:] if "=" in part)
            if (
                not header.startswith(RUN_LOG_HEADER_PREFIX)
                or fields.get("run") != self.run_token
                or fields.get("flow") != "sim"
                or fields.get("target") != target
            ):
                return None
            representation = (
                str(path.relative_to(self.project_root))
                if path.is_relative_to(self.project_root)
                else str(path)
            )
            return {"path": representation, "live": True, "complete": False}
        except (OSError, ValueError):
            return None

    def _document(self) -> dict[str, object]:
        assert self._base is not None
        document = copy.deepcopy(self._base)
        document["timestamp"] = utc_now_rfc3339()
        document.pop("active", None)
        if self._active and not self._closed:
            document["active"] = list(self._active.values())
            candidate = [item for item in self._active.values() if item["role"] == "candidate"]
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
    scope = AttemptScope(
        target,
        uuid.uuid4().hex,
        identity,
        tests,
        attempt_id,
        role,
        revision,
        ephemeral_root,
        operation,
    )
    if parent is not None:
        scope = replace(
            scope,
            key=parent.key
            if parent.target == target and (not attempt_id or attempt_id == parent.attempt_id)
            else scope.key,
            role=parent.role,
            revision=parent.revision,
            ephemeral_root=parent.ephemeral_root,
            attempt_id=attempt_id or parent.attempt_id,
        )
    token = _scope.set(scope)
    try:
        observe_stage(target, "preparing")
        yield
    finally:
        sink = _sink.get()
        if sink is not None:
            sink.retire(scope.key)
        _scope.reset(token)


def observe_stage(
    target: str,
    stage: str,
    *,
    evidence_root: Path | None = None,
    attempt_token: str = "",
    initialize_log: bool = False,
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
    if initialize_log and evidence_root is not None:
        sink.initialize_log(evidence_root, target, scope, attempt_token)
    sink.observe(StageObservation(scope, stage, evidence_root, attempt_token))


def current_progress() -> LiveProgressSink | None:
    """Return the scoped invocation observer, when installed."""
    return _sink.get()
