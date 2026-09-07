"""Test-only construction boundary for selected Target values."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import _HANDLE_FACTORY_KEY, TargetHandle, UnknownTargetError


def make_target_handle(
    project_root: Path | str,
    selector: str,
    *,
    vlnv: str = "::test:0",
    flow: str | None = None,
    eda_tool: str | None = None,
    drivable_by: tuple[str, ...] = (),
    core_file: Path | None = None,
    doctor_flows: tuple[str, ...] = (),
    cocotb_module: str | None = None,
    declared_toplevel: str = "",
) -> TargetHandle:
    """Create a handle for layer tests that intentionally omit a real catalog."""
    root = Path(project_root).resolve()
    return TargetHandle(
        identity=f"{vlnv}#{selector}",
        selector=selector,
        name=selector,
        vlnv=vlnv,
        core_file=core_file or root / "test.core",
        flow=flow,
        eda_tool=eda_tool,
        drivable_by=drivable_by,
        project_root=root,
        doctor_private=False,
        doctor_flows=doctor_flows,
        cocotb_module=cocotb_module,
        declared_toplevel=declared_toplevel,
        _factory_key=_HANDLE_FACTORY_KEY,
    )


class _LenientCatalog:
    """Test-only catalog that preserves pre-catalog pass-through behavior."""

    def __init__(
        self,
        project_root: Path | str,
        real_catalog_build: Callable[[Path | str], TargetCatalog],
        fallback_handle: Callable[[Path, str], TargetHandle],
    ) -> None:
        self.project_root = Path(project_root)
        self._real_catalog_build = real_catalog_build
        self._fallback_handle = fallback_handle

    def select(self, token: str, *, for_flow: str | None = None) -> TargetHandle:
        try:
            return self._real_catalog_build(self.project_root).select(token, for_flow=for_flow)
        except UnknownTargetError:
            return self._fallback_handle(self.project_root, token)

    def select_many(
        self,
        target_arg: str | None,
        *,
        for_flow: str | None = None,
    ) -> tuple[TargetHandle, ...]:
        return tuple(
            self.select(token.strip(), for_flow=for_flow)
            for token in (target_arg or "").split(",")
            if token.strip()
        )

    def inspect(self, handle: TargetHandle):
        catalog = self._real_catalog_build(self.project_root)
        return catalog.inspect(catalog.select(handle.selector))

    def core_closure(self, handles):
        # Intentional test-adapter seam: production consumers cannot reach this
        # registry walker; the fake mirrors TargetCatalog behavior.
        return fusesoc_registry._selectable_core_closure_for_refs(  # pyright: ignore[reportPrivateUsage]
            self.project_root, handles
        )


def install_lenient_target_catalog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    real_catalog_build: Callable[[Path | str], TargetCatalog],
    fallback_handle: Callable[[Path, str], TargetHandle],
) -> None:
    """Install the shared legacy-selection adapter for layer-focused Flow tests."""
    monkeypatch.setattr(
        TargetCatalog,
        "build",
        classmethod(lambda _cls, root: _LenientCatalog(root, real_catalog_build, fallback_handle)),
    )

    def resolve_handle(handle: TargetHandle, **kwargs):
        return fusesoc_registry._resolve_target(
            handle.selector,
            project_root=handle.project_root,
            vlnv=handle.vlnv,
            **kwargs,
        )

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve_handle)

    def setup_handle(handle: TargetHandle, **kwargs):
        return fusesoc_registry._setup_command(
            handle.selector,
            project_root=handle.project_root,
            vlnv=handle.vlnv,
            **kwargs,
        )

    monkeypatch.setattr(fusesoc_registry, "setup_command_for_handle", setup_handle)
