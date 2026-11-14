"""``[jobs]`` cap parsing."""

import logging
from pathlib import Path

import pytest

from booley.config.jobs import parse_caps


def test_parse_caps_accepts_memory_budget_without_unknown_key_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="booley.config.jobs"):
        parse_caps({"jobs": {"memory_budget": "24g", "max_light": 2}})

    assert "unknown key" not in caplog.text
    assert "memory_budget" not in caplog.text


def test_parse_caps_accepts_the_seats_table_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from booley.config.jobs import parse_seat_caps

    data = {"jobs": {"seats": {"vivado": 2}, "max_light": 2}}
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
    assert parse_seat_caps({"jobs": {"max_light": 2}}) == {}
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


def test_retired_jobs_keys_are_named_with_their_lines(tmp_path: Path) -> None:
    from booley.config.jobs import (
        RetiredKey,
        retired_jobs_keys,
        retired_jobs_keys_fix,
        retired_jobs_keys_message,
    )

    forms = {
        "table": ('[jobs]\nmax_light = 2\nmax_heavy = 2\nheavy_memory = "8g"\n', (3, 4)),
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
        "multi-line-string": '[jobs]\nnote = """\nmax_heavy = 2\n"""\nmax_light = 1\n',
        "clean": '[jobs]\nmemory_budget = "8g"\n',
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


def test_queue_max_is_retired_with_its_own_reason(tmp_path: Path) -> None:
    from booley.config.jobs import (
        RetiredKey,
        retired_jobs_keys,
        retired_jobs_keys_fix,
        retired_jobs_keys_message,
    )

    path = tmp_path / "booley.toml"
    path.write_bytes(b"[jobs]\nmax_heavy = 2\nqueue_max = 8\n")
    found = retired_jobs_keys(path)
    assert found == (RetiredKey("max_heavy", 2), RetiredKey("queue_max", 3))

    reason = "the Job queue has no limit: a Job waits until it runs or is cancelled"
    assert retired_jobs_keys_fix(found[1]) == f"delete [jobs].queue_max (line 3); {reason}"
    message = retired_jobs_keys_message(path, found)
    assert f"queue_max (line 3: {reason})" in message
    assert "max_heavy (line 2: memory admission replaced it)" in message
    assert message.count("Fix: delete these lines") == 1


def test_parse_caps_reads_only_max_light(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="booley.config.jobs"):
        caps = parse_caps({"jobs": {"max_light": 2, "queue_max": 8}})

    assert caps.max_light == 2
    assert "unknown key" not in caplog.text  # the retirement refusal reports queue_max


def test_refuse_retired_jobs_keys_reads_the_main_booley_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.config.jobs import refuse_retired_jobs_keys
    from booley.runtime import booley_dir as booley_dir_module

    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    booley_dir_module.reset_cache()
    (booley_dir / "booley.toml").write_bytes(b"[jobs]\nmax_heavy = 1\n")

    with pytest.raises(ValueError, match=r"max_heavy \(line 2: "):
        refuse_retired_jobs_keys(tmp_path)

    (booley_dir / "booley.toml").write_bytes(b"[jobs]\nmax_light = 1\n")
    refuse_retired_jobs_keys(tmp_path)
    # A Project that cannot be resolved has nothing to refuse.
    monkeypatch.delenv("BOOLEY_DIR")
    booley_dir_module.reset_cache()
    refuse_retired_jobs_keys(tmp_path / "nowhere")
