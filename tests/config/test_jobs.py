"""``[jobs]`` cap parsing."""

import logging

import pytest

from booley.config.jobs import parse_caps


def test_parse_caps_accepts_memory_budget_without_unknown_key_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="booley.config.jobs"):
        parse_caps({"jobs": {"memory_budget": "24g", "max_heavy": 2}})

    assert "unknown key" not in caplog.text
    assert "memory_budget" not in caplog.text


def test_parse_caps_accepts_the_seats_table_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from booley.config.jobs import parse_seat_caps

    data = {"jobs": {"seats": {"vivado": 2}, "max_heavy": 2}}
    with caplog.at_level(logging.WARNING, logger="booley.config.jobs"):
        parse_caps(data)

    assert "unknown key" not in caplog.text
    assert parse_seat_caps(data) == {"vivado": 2}


def test_malformed_seat_caps_are_config_errors_naming_the_key() -> None:
    from booley.config.jobs import SeatCapError, parse_seat_caps

    cases = {
        "zero": ({"jobs": {"seats": {"vivado": 0}}}, "[jobs.seats].vivado = 0"),
        "text": ({"jobs": {"seats": {"vivado": "two"}}}, "[jobs.seats].vivado = 'two'"),
        "bool": ({"jobs": {"seats": {"vivado": True}}}, "[jobs.seats].vivado = True"),
        "not-a-table": ({"jobs": {"seats": 2}}, "[jobs].seats"),
        "open-source-tool": ({"jobs": {"seats": {"verilator": 1}}}, "[jobs.seats].verilator"),
    }
    for name, (data, key) in cases.items():
        with pytest.raises(SeatCapError) as raised:
            parse_seat_caps(data)
        assert key in str(raised.value), name
    # An open-source tool's error lists the tools a Seat Cap applies to.
    with pytest.raises(SeatCapError, match="apply to: vivado"):
        parse_seat_caps({"jobs": {"seats": {"verilator": 1}}})


@pytest.mark.parametrize("count", [-1, 2.0])
def test_negative_and_fractional_seat_counts_are_config_errors(count: object) -> None:
    from booley.config.jobs import SeatCapError, parse_seat_caps

    with pytest.raises(SeatCapError, match=r"\[jobs\.seats\]\.vivado"):
        parse_seat_caps({"jobs": {"seats": {"vivado": count}}})


def test_absent_or_unreadable_jobs_table_has_no_seat_caps() -> None:
    from booley.config.jobs import parse_seat_caps

    assert parse_seat_caps({}) == {}
    assert parse_seat_caps({"jobs": {"max_heavy": 2}}) == {}
    # parse_caps already warns about a non-table [jobs]; the seats parser stays quiet.
    assert parse_seat_caps({"jobs": "many"}) == {}


def test_required_seats_pairs_each_tool_with_its_cap_or_names_the_fix() -> None:
    from booley.config.jobs import SeatCapError, required_seats, seat_cap_fix

    data = {"jobs": {"seats": {"vivado": 3}}}
    assert required_seats(("vivado",), data) == (("vivado", 3),)
    # A Job that uses no licensed tool never reads the table, so a bad table refuses nothing.
    assert required_seats((), {"jobs": {"seats": {"vivado": 0}}}) == ()
    with pytest.raises(SeatCapError) as raised:
        required_seats(("vivado",), {})
    assert seat_cap_fix("vivado") in str(raised.value)
    assert seat_cap_fix("vivado") == "[jobs.seats] vivado = <seats you own>"
