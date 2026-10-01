"""Validate authored fault bytes through the product's existing configuration boundary."""

from pathlib import Path

import pytest

from booley.config.host_config import HostConfigError, load_host_policy

ROOT = Path(__file__).resolve().parents[2] / "qa/missions/uart/fixtures/host-policy"


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


@pytest.mark.parametrize(
    "name, cap, timeout, egress",
    [
        ("legacy", 2, 600, ("qa-allowed.example.invalid",)),
        ("both-tables", 1, 7200, ()),
    ],
)
def test_authored_migration_cases_have_expected_policy_and_warning(
    tmp_path: Path,
    name: str,
    cap: int,
    timeout: int,
    egress: tuple[str, ...],
) -> None:
    path = tmp_path / "config.toml"
    original = (ROOT / f"{name}.toml").read_bytes()
    path.write_bytes(original)
    messages: list[str] = []
    policy = load_host_policy(path, on_deprecation=messages.append)
    assert (policy.max_sessions, policy.idle_timeout_seconds, policy.egress_allowlist) == (
        cap,
        timeout,
        egress,
    )
    assert len(messages) == 1
    assert "[interactive] is deprecated" in messages[0]
    assert ("ignored" in messages[0]) == (name == "both-tables")
    assert path.read_bytes() == original
