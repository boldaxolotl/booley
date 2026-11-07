"""Flow configuration helpers resolve Targets under the checkout's Booley Directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from booley.flows import flow_config
from booley.fusesoc import fusesoc_registry
from tests.fusesoc_test_support import relocated_project, write_core


@pytest.mark.parametrize("directory", [".booley", "state"])
def test_payload_build_root_uses_the_checkout_booley_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    project = tmp_path / "proj"
    if directory == ".booley":
        project.mkdir()
    else:
        relocated_project(project, directory)
    write_core(project / "ip")
    captured: dict[str, Path] = {}

    def capture(_handle: object, *, build_root: Path) -> Any:
        captured["build_root"] = build_root
        return object()

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", capture)

    assert flow_config._maybe_resolve("sim", project) is not None
    assert (
        captured["build_root"] == project / directory / ".runtime" / "edalize" / "payload" / "sim"
    )
