"""Flow configuration helpers read declared Flow policy."""

from __future__ import annotations

from pathlib import Path

from booley.flows import flow_config


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
