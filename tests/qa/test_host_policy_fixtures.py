"""Validate authored fault bytes through the product's existing configuration boundary."""

from pathlib import Path

import pytest

from booley.config.host_config import HostConfigError, load_host_policy

ROOT = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/fixtures/host-policy"


@pytest.mark.parametrize("variant", ["scheme", "path", "port", "ip", "wildcard", "key"])
def test_policy_fault_rejects_without_mutation_then_recovers(tmp_path, variant):
    path = tmp_path / "config.toml"
    original = (ROOT / f"invalid-{variant}.toml").read_bytes()
    path.write_bytes(original)
    with pytest.raises(HostConfigError):
        load_host_policy(path)
    assert path.read_bytes() == original
    restored = (ROOT / "recovery.toml").read_bytes()
    path.write_bytes(restored)
    assert load_host_policy(path).max_sessions == 4
    assert path.read_bytes() == restored


def test_policy_positive_controls_have_distinct_documented_values():
    assert load_host_policy(ROOT / "idle.toml").idle_timeout_seconds == 30
    assert load_host_policy(ROOT / "cap.toml").max_sessions == 1
    assert load_host_policy(ROOT / "egress.toml").egress_allowlist == (
        "qa-allowed.example.invalid",
    )
