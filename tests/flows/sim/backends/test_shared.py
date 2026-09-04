"""Focused tests for shared simulator-backend telemetry."""

from booley.flows.sim.backends.shared import child_cpu_marker


def test_child_cpu_marker_reports_non_negative_deltas() -> None:
    assert child_cpu_marker((1.0, 2.0), (1.25, 2.125)) == (
        "BOOLEY_SIM_CPU_SECONDS: user=0.250000 system=0.125000"
    )


def test_child_cpu_marker_is_absent_when_platform_cannot_measure() -> None:
    assert child_cpu_marker(None, (1.0, 2.0)) == ""
