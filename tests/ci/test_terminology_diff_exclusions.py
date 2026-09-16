"""Keep changed-line coverage strict when code behavior changes."""

import runpy
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / ".github/scripts/terminology_diff_exclusions.py"
MODULE = runpy.run_path(str(SCRIPT))
terminology_only = MODULE["terminology_only"]


def test_renamed_diagnostic_without_code_change_is_excluded() -> None:
    before = '''def check(value):
    """Check the Session Runtime."""
    if not value:
        raise ValueError("Session Runtime unavailable")
    return value
'''
    after = before.replace("Session Runtime", "Sandbox")
    assert terminology_only(before, after)


def test_changed_branch_is_covered_even_with_renamed_diagnostic() -> None:
    before = 'def check(value):\n    if value:\n        raise ValueError("Session Runtime")\n'
    after = 'def check(value):\n    if not value:\n        raise ValueError("Sandbox")\n'
    assert not terminology_only(before, after)


def test_persisted_path_change_is_covered() -> None:
    before = 'PATH = ".runtime/session"\n'
    after = 'PATH = ".sandbox/session"\n'
    assert not terminology_only(before, after)


def test_unrelated_text_change_is_covered() -> None:
    before = 'MESSAGE = "Session Runtime unavailable; retry"\n'
    after = 'MESSAGE = "Sandbox unavailable; ignore the failure"\n'
    assert not terminology_only(before, after)


def test_exclusion_path_is_exact_and_absolute(tmp_path: Path) -> None:
    path = tmp_path / "src" / "example.py"
    assert MODULE["diff_cover_path"](path) == str(path.resolve())
