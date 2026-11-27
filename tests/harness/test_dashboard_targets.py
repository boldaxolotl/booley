"""The Flows a worktree's settings leave enabled, as the Run form lists them."""

from __future__ import annotations

import pytest

from booley.harness.dashboard.targets import enabled_flows


@pytest.mark.parametrize(
    "layout",
    [
        {".booley/booley.toml": "[flows.elab]\n"},
        {".booley/booley.toml": "", ".booley_project/booley.toml": ""},
    ],
    ids=["retired-key", "two-booley-directories"],
)
def test_enabled_flows_keeps_a_flow_whose_setting_cannot_be_read(tmp_path, layout):
    for name, text in layout.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_bytes(text.encode())
    assert enabled_flows(("fpga", "sim"), tmp_path) == ("fpga", "sim")
