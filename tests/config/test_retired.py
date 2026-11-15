"""The retired-configuration refusal covers a ``[goals]`` section without touching kept tables."""

from __future__ import annotations

import tomllib
from pathlib import Path

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
