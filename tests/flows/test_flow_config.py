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


def test_expected_count_malformed_values_resolve_to_zero(tmp_path: Path) -> None:
    """All four structural allowances share one rule: bad values never widen the gate."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    pairs = (
        ("synth", "expected_latches"),
        ("synth", "expected_comb_loops"),
        ("fpga", "expected_latches"),
        ("fpga", "expected_comb_loops"),
    )
    for flow_name, key in pairs:

        def resolved(literal: str | None, flow_name: str = flow_name, key: str = key) -> int:
            body = "" if literal is None else f"{key} = {literal}\n"
            (booley_dir / "booley.toml").write_text(
                f"[flows.{flow_name}]\n{body}", encoding="utf-8"
            )
            return flow_config.expected_count(flow_name, tmp_path, key)

        for literal in (None, "-1", "true", "1.5", '"one"'):
            assert resolved(literal) == 0, (flow_name, key, literal)
        assert resolved("0") == 0
        assert resolved("2") == 2, (flow_name, key)
        # No project config at all is the strict historical behavior.
        assert flow_config.expected_count(flow_name, tmp_path / "nowhere", key) == 0
