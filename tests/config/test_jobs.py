"""``[jobs]`` Seat Cap and retired-key parsing."""

from pathlib import Path

import pytest


def test_malformed_seat_caps_are_config_errors_naming_the_key() -> None:
    from booley.config.jobs import SeatCapError, parse_seat_caps

    cases = {
        "zero": ({"jobs": {"seats": {"vivado": 0}}}, "[jobs.seats].vivado = 0"),
        "text": ({"jobs": {"seats": {"vivado": "two"}}}, "[jobs.seats].vivado = 'two'"),
        "bool": ({"jobs": {"seats": {"vivado": True}}}, "[jobs.seats].vivado = True"),
        "not-a-table": ({"jobs": {"seats": 2}}, "[jobs].seats"),
        "open-source-tool": ({"jobs": {"seats": {"verilator": 1}}}, "[jobs.seats].verilator"),
        "negative": ({"jobs": {"seats": {"vivado": -1}}}, "[jobs.seats].vivado"),
        "fractional": ({"jobs": {"seats": {"vivado": 2.0}}}, "[jobs.seats].vivado"),
    }
    for name, (data, key) in cases.items():
        with pytest.raises(SeatCapError) as raised:
            parse_seat_caps(data)
        assert key in str(raised.value), name
    # An open-source tool's error lists the tools a Seat Cap applies to.
    with pytest.raises(SeatCapError, match="apply to: vivado"):
        parse_seat_caps({"jobs": {"seats": {"verilator": 1}}})


def test_absent_seat_caps_are_empty_and_required_seats_pair_each_tool_with_its_cap() -> None:
    from booley.config.jobs import SeatCapError, parse_seat_caps, required_seats, seat_cap_fix

    assert parse_seat_caps({}) == {}
    assert parse_seat_caps({"jobs": {"max_light": 2}}) == {}
    assert parse_seat_caps({"jobs": "many"}) == {}
    data = {"jobs": {"seats": {"vivado": 3}}}
    assert required_seats(("vivado",), data) == (("vivado", 3),)
    # A Job that uses no licensed tool never reads the table, so a bad table refuses nothing.
    assert required_seats((), {"jobs": {"seats": {"vivado": 0}}}) == ()
    with pytest.raises(SeatCapError) as raised:
        required_seats(("vivado",), {})
    assert seat_cap_fix("vivado") in str(raised.value)
    assert seat_cap_fix("vivado") == "[jobs.seats] vivado = <seats you own>"


def test_retired_jobs_keys_are_named_with_their_lines(tmp_path: Path) -> None:
    from booley.config.jobs import (
        RetiredKey,
        retired_jobs_keys,
        retired_jobs_keys_fix,
        retired_jobs_keys_message,
    )

    forms = {
        "table": ('[jobs]\nmax_tickets = 2\nmax_heavy = 2\nheavy_memory = "8g"\n', (3, 4)),
        "dotted": ('jobs.max_heavy = 2\njobs.heavy_memory = "8g"\n', (1, 2)),
        "quoted": ('[ "jobs" ]\n"max_heavy" = 2\n\'heavy_memory\' = "8g"  # note\n', (2, 3)),
        "inline": ('title = 1\njobs = { max_heavy = 2, heavy_memory = "8g" }\n', (2, 2)),
    }
    for name, (text, lines) in forms.items():
        path = tmp_path / f"{name}.toml"
        path.write_bytes(text.encode())
        assert retired_jobs_keys(path) == (
            RetiredKey("max_heavy", lines[0]),
            RetiredKey("heavy_memory", lines[1]),
        ), name

    # Other tables, multi-line strings and clean, missing or broken files are no hit.
    quiet = {
        "other-table": "[sandbox]\nmax_heavy = 2\n",
        "multi-line-string": '[jobs]\nnote = """\nmax_heavy = 2\n"""\nmax_tickets = 1\n',
        "clean": "[jobs]\nmax_tickets = 2\n",
        "broken": "[jobs\nmax_heavy = 2\n",
    }
    for name, text in quiet.items():
        path = tmp_path / f"{name}.toml"
        path.write_bytes(text.encode())
        assert retired_jobs_keys(path) == (), name
    assert retired_jobs_keys(tmp_path / "missing.toml") == ()

    found = (RetiredKey("max_heavy", 12), RetiredKey("heavy_memory", 13))
    message = retired_jobs_keys_message(tmp_path / "booley.toml", found)
    assert "heavy_memory (line 13: memory admission replaced it)" in message
    assert "booley-heal" in message
    assert "max_heavy (line 12: memory admission replaced it)" in message
    assert retired_jobs_keys_fix(found[0]) == (
        "delete [jobs].max_heavy (line 12); memory admission replaced it"
    )


@pytest.mark.parametrize(
    ("text", "found", "fix", "fragments", "absent"),
    [
        (
            b"[jobs]\nmax_heavy = 2\nqueue_max = 8\n",
            (("max_heavy", 2), ("queue_max", 3)),
            "delete [jobs].queue_max (line 3); "
            "the Job queue has no limit: a Job waits until it runs or is cancelled",
            (
                "queue_max (line 3: the Job queue has no limit: a Job waits until it runs "
                "or is cancelled)",
                "max_heavy (line 2: memory admission replaced it)",
            ),
            None,
        ),
        (
            b'[jobs]\nmemory_budget = "24g"\n',
            (("memory_budget", 2),),
            "delete [jobs].memory_budget (line 2); "
            "the Memory Budget is computed from [sandbox].memory: set [sandbox].memory instead",
            ("memory_budget (line 2: ", "set [sandbox].memory instead"),
            "memory admission replaced it",
        ),
    ],
    ids=["queue_max", "memory_budget"],
)
def test_retired_keys_carry_their_own_reason_and_fix(
    tmp_path: Path,
    text: bytes,
    found: tuple[tuple[str, int], ...],
    fix: str,
    fragments: tuple[str, ...],
    absent: str | None,
) -> None:
    from booley.config.jobs import (
        RetiredKey,
        retired_jobs_keys,
        retired_jobs_keys_fix,
        retired_jobs_keys_message,
    )

    path = tmp_path / "booley.toml"
    path.write_bytes(text)
    keys = retired_jobs_keys(path)
    assert keys == tuple(RetiredKey(name, line) for name, line in found)

    assert retired_jobs_keys_fix(keys[-1]) == fix
    message = retired_jobs_keys_message(path, keys)
    for fragment in fragments:
        assert fragment in message
    assert absent is None or absent not in message
    assert message.count("Fix: delete these lines") == 1
