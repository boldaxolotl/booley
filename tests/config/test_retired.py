"""The retired-configuration refusal covers a ``[goals]`` section without touching kept tables."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

TABLES = (
    b'[project]\nname = "unit"\n'
    b"\n"
    b"[goals]\nquiet_after = 60\n"
    b"\n"
    b'[sandbox]\nmemory = "16g"\n'
    b"\n"
    b"[goals.extra]\nkey = 1\n"
    b"\n"
    b"[flows.sim]\n"
)
DOTTED = b'goals.quiet_after = 1\n[project]\nname = "unit"\n\n[sandbox]\nmemory = "16g"\n'


def _after_deleting(spans: tuple[tuple[int, int], ...], text: bytes) -> dict[str, object]:
    lines = text.splitlines(keepends=True)
    doomed = {number for first, last in spans for number in range(first, last + 1)}
    kept = b"".join(line for number, line in enumerate(lines, start=1) if number not in doomed)
    return tomllib.loads(kept.decode())


def test_goals_spans_never_cover_a_kept_table(tmp_path: Path) -> None:
    from booley.config.retired import goals_section_fix, goals_section_spans

    path = tmp_path / "booley.toml"
    path.write_bytes(TABLES)

    spans = goals_section_spans(path)

    assert spans == ((4, 5), (10, 11))
    assert goals_section_fix(path, spans).startswith(
        "delete the [goals] tables (lines 4-5, 10-11)"
    )
    data = _after_deleting(spans, TABLES)
    assert "goals" not in data
    assert data["sandbox"] == {"memory": "16g"}
    assert data["flows"] == {"sim": {}}

    path.write_bytes(DOTTED)
    spans = goals_section_spans(path)
    assert spans == ((1, 1),)
    data = _after_deleting(spans, DOTTED)
    assert "goals" not in data
    assert data["sandbox"] == {"memory": "16g"}


def test_a_missing_or_goalless_file_has_no_spans(tmp_path: Path) -> None:
    from booley.config.retired import goals_section_spans

    path = tmp_path / "booley.toml"
    assert goals_section_spans(path) == ()
    path.write_bytes(b'[project]\nname = "unit"\n')
    assert goals_section_spans(path) == ()
    path.write_bytes(b"[goals\n")
    assert goals_section_spans(path) == ()


COMMENT_QUOTES = b'[goals]\nquiet_after = 60 # """\n\n[sandbox]\nmemory = "16g"\n'
ESCAPED = b'["go\\u0061ls"]\nquiet_after = 60\n\n[sandbox]\nmemory = "16g"\n'


def test_a_triple_quote_in_a_comment_neither_hides_nor_widens_the_section(tmp_path: Path) -> None:
    from booley.config.retired import goals_section_spans, opens_multiline_string

    path = tmp_path / "booley.toml"
    path.write_bytes(COMMENT_QUOTES)
    spans = goals_section_spans(path)
    assert spans == ((1, 2),)
    assert _after_deleting(spans, COMMENT_QUOTES) == {"sandbox": {"memory": "16g"}}
    path.write_bytes(b'# """\n' + COMMENT_QUOTES)
    assert goals_section_spans(path) == ((2, 3),)
    assert opens_multiline_string('a = """x""" # """') is None
    assert opens_multiline_string("a = 'it''s' # c") is None
    assert opens_multiline_string('a = "x') is None
    assert opens_multiline_string("a = '''x") == "'''"


def test_an_escaped_goals_table_name_is_still_refused(tmp_path: Path) -> None:
    from booley.config.retired import (
        goals_section_fix,
        goals_section_location,
        goals_section_spans,
        has_goals_section,
    )

    path = tmp_path / "booley.toml"
    path.write_bytes(ESCAPED)
    spans = goals_section_spans(path)
    assert spans == ()
    assert has_goals_section(path)
    assert goals_section_location(spans) == ""
    assert goals_section_fix(path, spans).startswith("delete the [goals] tables from ")
    assert not has_goals_section(tmp_path / "absent.toml")


def test_the_refusal_names_an_escaped_section_without_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.config import retired
    from booley.runtime import booley_dir as booley_dir_module

    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_bytes(ESCAPED)
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    booley_dir_module.reset_cache()

    try:
        with pytest.raises(retired.RetiredConfigError) as caught:
            retired.refuse_retired_config(tmp_path)
    finally:
        booley_dir_module.reset_cache()

    assert str(caught.value).startswith("booley.toml has a [goals] section;")
    assert "booley-heal" in str(caught.value)
