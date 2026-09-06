"""Immutable Project-scoped Target discovery, selection, and inspection."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.fusesoc.target_inspection import TargetSourceInspector
from booley.targets.domain import (
    _HANDLE_FACTORY_KEY,
    TARGET_AWARE_FLOWS,
    AmbiguousTargetError,
    ForeignTargetHandleError,
    IncompatibleTargetError,
    StaleTargetCatalogError,
    TargetHandle,
    TargetInspection,
    TargetRef,
    UnknownTargetError,
    flow_can_drive,
)


def _vlnv_key(vlnv: str) -> str:
    parts = vlnv.split(":")
    return ":".join(parts[:3]) if len(parts) >= 3 else vlnv


def _vlnv_matches(query: str, vlnv: str) -> bool:
    key_segments = _vlnv_key(vlnv).split(":")
    query_segments = _vlnv_key(query).split(":")
    return len(query_segments) <= len(key_segments) and (
        key_segments[-len(query_segments) :] == query_segments
    )


def _split_selector(token: str) -> tuple[str | None, str]:
    if "#" not in token:
        return None, token
    qualifier, _, name = token.rpartition("#")
    return qualifier or None, name


def _minimal_selector(ref: TargetRef, declaring: tuple[TargetRef, ...]) -> str:
    if len(declaring) <= 1:
        return ref.name
    segments = _vlnv_key(ref.vlnv).split(":")
    for length in range(1, len(segments) + 1):
        qualifier = ":".join(segments[-length:])
        if sum(_vlnv_matches(qualifier, candidate.vlnv) for candidate in declaring) == 1:
            return f"{qualifier}#{ref.name}"
    return f"{ref.vlnv}#{ref.name}"


def _resolve(declarations: dict[str, tuple[TargetRef, ...]], token: str) -> TargetRef:
    qualifier, name = _split_selector(token)
    bucket = declarations.get(name)
    if not bucket:
        known = ", ".join(sorted(declarations)) or "(none authored)"
        raise UnknownTargetError(f"Unknown target {token!r}; selectable Targets: {known}")
    if qualifier is None:
        if len(bucket) == 1:
            return bucket[0]
        candidates = sorted(ref.vlnv for ref in bucket)
        hint = f"{_vlnv_key(candidates[0]).split(':')[-1]}#{name}"
        raise AmbiguousTargetError(
            f"Target {name!r} is declared by {len(bucket)} cores: "
            f"{', '.join(candidates)}; qualify it as 'vlnv#name' (e.g. {hint!r})."
        )
    matches: tuple[TargetRef, ...] = tuple(
        ref for ref in bucket if _vlnv_matches(qualifier, ref.vlnv)
    )
    if not matches:
        candidates = ", ".join(sorted(ref.vlnv for ref in bucket))
        raise UnknownTargetError(
            f"no Target {name!r} in a core matching {qualifier!r}; "
            f"cores declaring {name!r}: {candidates}"
        )
    if len(matches) > 1:
        candidates = ", ".join(sorted(ref.vlnv for ref in matches))
        raise AmbiguousTargetError(
            f"{token!r} is ambiguous — {qualifier!r} matches {len(matches)} "
            f"cores: {candidates}; use a longer VLNV qualifier."
        )
    return next(iter(matches))


def _doctor_private_authority() -> bool:
    return os.environ.get(selftest_overlay.INTERNAL_KIND_ENV) == selftest_overlay.BAD_KIND


@dataclass
class _OperationalState:
    inspector: TargetSourceInspector | None = None
    documents: dict[Path, dict[str, Any]] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class TargetCatalog:
    """One immutable Target declaration snapshot for one Project checkout."""

    project_root: Path
    snapshot_id: str
    _declarations: tuple[tuple[str, tuple[TargetRef, ...]], ...] = field(repr=False)
    _doctor_private: bool = field(repr=False)
    _state: _OperationalState = field(default_factory=_OperationalState, repr=False, compare=False)
    _cache: ClassVar[dict[tuple[Path, bool], TargetCatalog]] = {}
    _cache_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def build(cls, project_root: Path | str) -> TargetCatalog:
        """Build a read-only declaration snapshot without preparing FuseSoC."""
        root = Path(project_root).resolve()
        doctor_private = _doctor_private_authority()
        snapshot_id = fusesoc_registry.target_snapshot_id(root)
        cache_key = (root, doctor_private)
        with cls._cache_lock:
            cached = cls._cache.get(cache_key)
            if cached is not None and cached.snapshot_id == snapshot_id:
                return cached
        declarations = fusesoc_registry.target_declarations(root)
        frozen = tuple((name, tuple(refs)) for name, refs in sorted(declarations.items()))
        catalog = cls(
            root,
            snapshot_id,
            frozen,
            doctor_private,
        )
        with cls._cache_lock:
            existing = cls._cache.get(cache_key)
            if existing is not None and existing.snapshot_id == snapshot_id:
                return existing
            cls._cache[cache_key] = catalog
        return catalog

    def list(self, *, for_flow: str | None = None) -> tuple[TargetHandle, ...]:
        """List visible Targets from this snapshot, optionally by compatible Flow."""
        for_flow = _canonical_flow(for_flow)
        handles: list[TargetHandle] = []
        for _name, refs in self._declarations:
            visible = self._visible(refs)
            for ref in visible:
                handle = self._handle(ref, visible)
                if for_flow is None or flow_can_drive(for_flow, handle):
                    handles.append(handle)
        return tuple(sorted(handles, key=lambda item: (item.vlnv, item.name)))

    def select(self, token: str, *, for_flow: str | None = None) -> TargetHandle:
        """Resolve one authored token once and return its canonical handle."""
        for_flow = _canonical_flow(for_flow)
        declarations = self._visible_declarations()
        ref = _resolve(declarations, token)
        if for_flow is not None and not flow_can_drive(for_flow, ref):
            raise IncompatibleTargetError(
                f"Target {token!r} cannot be driven by the {for_flow!r} Flow "
                f"(declared flow={ref.flow!r}, EDA tool={ref.eda_tool!r}). "
                f"Choose a compatible Target with `booley targets --for-flow {for_flow}`."
            )
        return self._handle(ref, declarations[ref.name])

    def select_many(
        self,
        target_arg: str | None,
        *,
        for_flow: str | None = None,
    ) -> tuple[TargetHandle, ...]:
        """Resolve a comma-separated endpoint Target argument."""
        tokens = [token.strip() for token in (target_arg or "").split(",") if token.strip()]
        return tuple(self.select(token, for_flow=for_flow) for token in tokens)

    def inspect(self, handle: TargetHandle) -> TargetInspection:
        """Inspect a handle through this snapshot's shared FuseSoC library view."""
        self._require_handle(handle)
        self._require_fresh()
        if self._state.inspector is None:
            self._state.inspector = TargetSourceInspector(self.project_root)
        return self._state.inspector.inspect_handle(handle)

    def _visible(self, refs: tuple[TargetRef, ...]) -> tuple[TargetRef, ...]:
        if self._doctor_private:
            return refs
        return tuple(ref for ref in refs if not ref.doctor_selftest)

    def _visible_declarations(self) -> dict[str, tuple[TargetRef, ...]]:
        return {
            name: visible for name, refs in self._declarations if (visible := self._visible(refs))
        }

    def _handle(self, ref: TargetRef, bucket: tuple[TargetRef, ...]) -> TargetHandle:
        core_file = ref.core_file.resolve()
        document = self._state.documents.get(core_file)
        if document is None:
            document = fusesoc_registry.read_core(core_file)
            self._state.documents[core_file] = document
        return TargetHandle(
            identity=f"{ref.vlnv}#{ref.name}",
            selector=_minimal_selector(ref, bucket),
            name=ref.name,
            vlnv=ref.vlnv,
            core_file=core_file,
            flow=ref.flow,
            eda_tool=ref.eda_tool,
            drivable_by=tuple(flow for flow in TARGET_AWARE_FLOWS if flow_can_drive(flow, ref)),
            project_root=self.project_root,
            doctor_private=ref.doctor_selftest,
            cocotb_module=ref.cocotb_module,
            doctor_flows=ref.doctor_flows,
            declared_toplevel=fusesoc_registry.core_target_toplevel(document, ref.name),
            snapshot_id=self.snapshot_id,
            _factory_key=_HANDLE_FACTORY_KEY,
        )

    def _require_handle(self, handle: TargetHandle) -> None:
        if handle.project_root != self.project_root:
            raise ForeignTargetHandleError(
                f"Target {handle.identity!r} was selected for Project "
                f"{handle.project_root}, not {self.project_root}"
            )
        if handle.snapshot_id != self.snapshot_id:
            raise ForeignTargetHandleError(
                f"Target {handle.identity!r} belongs to another Target catalog snapshot"
            )

    def _require_fresh(self) -> None:
        current = fusesoc_registry.target_snapshot_id(self.project_root)
        if current != self.snapshot_id:
            raise StaleTargetCatalogError(
                f"Target catalog for {self.project_root} is stale; build a new catalog"
            )


def _canonical_flow(flow: str | None) -> str | None:
    if flow is None:
        return None
    from booley.targets.flow_names import canonical

    result = canonical(flow)
    if result not in TARGET_AWARE_FLOWS:
        raise ValueError(
            f"{result!r} is not a target-aware Booley Flow; "
            f"choose one of: {', '.join(TARGET_AWARE_FLOWS)}"
        )
    return result


__all__ = ["TargetCatalog"]
