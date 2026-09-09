from booley.evidence.timing import (
    ClockTiming,
    make_clock_timing,
    per_clock_from_json,
    per_clock_to_json,
    worst_clock,
    worst_fmax_from_json,
)


def test_clock_timing_round_trip_preserves_derived_values() -> None:
    timing = make_clock_timing("clk", 10.0, -2.0, 0.1)

    assert timing.critical_path_ps == 12_000.0
    assert timing.fmax_mhz == 1_000_000.0 / 12_000.0
    assert per_clock_from_json(per_clock_to_json({"clk": timing})) == {"clk": timing}


def test_timing_parser_tolerates_malformed_entries() -> None:
    assert per_clock_from_json(None) == {}
    assert per_clock_from_json({"bad": "value", "clk": {"fmax_mhz": True}}) == {
        "clk": ClockTiming(clock="clk")
    }


def test_worst_clock_uses_fmax_then_critical_path_then_slack() -> None:
    clocks = {
        "fast": ClockTiming(clock="fast", fmax_mhz=200.0),
        "slow": ClockTiming(clock="slow", fmax_mhz=100.0),
    }

    assert worst_clock(clocks) is clocks["slow"]
    assert worst_fmax_from_json(per_clock_to_json(clocks)) == 100.0
