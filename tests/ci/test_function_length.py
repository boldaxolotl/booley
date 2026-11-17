"""CI's function length check: limit, growth rule, docstrings, nesting, and wiring."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest
from tests.subprocess_budget import run_bounded_child

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _SOURCE_ROOT / ".github/scripts/check_function_length.py"
_MODULE = "src/booley/mod.py"


def _gate() -> ModuleType:
    # Imported in the test body so a missing module fails the test, not collection.
    return importlib.import_module("booley.dev_support.function_length")


def _body(name: str, lines: int) -> str:
    """A function that counts exactly ``lines`` lines."""
    return f"def {name}():\n" + "    x = 1\n" * (lines - 1)


def _git(repo: Path, *args: str) -> str:
    return run_bounded_child(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, isolated_git_attributes: None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, {"README.md": "base\n", "src/booley/old.py": _body("f", 60) + "y = 1\n"})
    _git(root, "switch", "-q", "-c", "codex/feature")
    return root


def _run(repo: Path) -> tuple[int, str, str]:
    result = run_bounded_child([sys.executable, "-B", str(_LAUNCHER)], cwd=repo)
    return result.returncode, result.stdout, result.stderr


def _exit_codes(repo: Path, cases: dict[str, tuple[dict[str, str], int]]) -> None:
    """Commit each case's files on a fresh branch tip and check the exit code."""
    start = _git(repo, "rev-parse", "HEAD")
    for case, (files, expected) in cases.items():
        _commit(repo, files)
        code = _run(repo)[0]
        _git(repo, "reset", "-q", "--hard", start)
        assert code == expected, case


def test_new_function_over_limit_fails(repo: Path) -> None:
    _exit_codes(
        repo,
        {
            "51 lines fails": ({"src/booley/mod.py": _body("f", 51)}, 1),
            "50 lines passes": ({"src/booley/mod.py": _body("f", 50)}, 0),
            "51 lines in a non-ASCII path fails": ({"src/booley/caf\u00e9.py": _body("f", 51)}, 1),
        },
    )


def test_oversize_base_function_may_not_grow(repo: Path) -> None:
    _exit_codes(
        repo,
        {
            "grown to 61 fails": ({"src/booley/old.py": _body("f", 61) + "y = 2\n"}, 1),
            "unchanged at 60 passes": ({"src/booley/old.py": _body("f", 60) + "y = 2\n"}, 0),
            "shrunk to 55 passes": ({"src/booley/old.py": _body("f", 55) + "y = 2\n"}, 0),
        },
    )


def test_docstring_and_blank_lines_do_not_count() -> None:
    source = 'def f():\n    """Doc\n\n    more\n    """\n\n' + "    x = 1\n\n" * 49
    assert _gate().function_lengths(source) == [("f", 50)]
    assert _gate().violations("a.py", source, None) == []
    assert _gate().violations("a.py", source + "    x = 1\n", None) != []
    # U+2028 inside a comment is not a Python line break: all 51 lines count.
    separator = "def f():\n    # \u2028\n" + "    x = 1\n" * 49
    assert _gate().function_lengths(separator) == [("f", 51)]


def test_nested_function_counts_alone_and_in_parent(repo: Path) -> None:
    source = (
        "def outer():\n    y = 1\n    def inner():\n        z = 1\n        return z\n    return y\n"
        "class Box:\n    def method(self):\n        return 1\n"
    )
    assert _gate().function_lengths(source) == [
        ("outer", 6),
        ("outer.<locals>.inner", 3),
        ("Box.method", 2),
    ]
    inner = "    def inner():\n" + "        z = 1\n" * 50
    _commit(repo, {_MODULE: "async def outer():\n" + inner + "    return 1\n"})
    code, _, stderr = _run(repo)
    assert code == 1
    assert "outer is 53 lines" in stderr
    assert "outer.<locals>.inner is 51 lines" in stderr


def test_failure_message_names_file_function_and_both_lengths(repo: Path) -> None:
    method = "".join("    " + line + "\n" for line in _body("f", 61).splitlines())
    _commit(
        repo,
        {
            "src/booley/old.py": _body("f", 61),
            "src/booley/new.py": f"class Box:\n{method}" + _body("g", 51),
        },
    )
    code, stdout, stderr = _run(repo)
    assert code == 1
    assert stderr.splitlines() == [
        "src/booley/new.py: Box.f is 61 lines (new; base: none); limit 50",
        "src/booley/new.py: g is 51 lines (new; base: none); limit 50",
        "src/booley/old.py: f is 61 lines (base: 60); limit 50, and it may not grow",
    ]
    assert "2 changed source files checked" in stdout


def test_duplicate_names_pair_by_occurrence() -> None:
    base = _body("f", 60) + _body("f", 10)
    assert _gate().violations("a.py", _body("f", 60) + _body("f", 9), base) == []
    assert len(_gate().violations("a.py", _body("f", 10) + _body("f", 61), base)) == 1
    assert len(_gate().violations("a.py", base + _body("f", 55), base)) == 1


def test_files_outside_the_package_are_ignored(repo: Path) -> None:
    _git(repo, "rm", "-q", "src/booley/old.py")
    _commit(repo, {"tests/test_x.py": _body("test_x", 60), "src/booley/notes.txt": "x\n"})
    assert _run(repo)[0] == 0


def test_unparseable_changed_file_exits_2(repo: Path) -> None:
    _commit(repo, {_MODULE: "def broken(:\n"})
    code, _, stderr = _run(repo)
    assert code == 2
    assert "src/booley/mod.py does not parse" in stderr


def test_unusable_git_state_exits_2(tmp_path: Path) -> None:
    code, _, stderr = _run(tmp_path)
    assert code == 2
    assert stderr.startswith("function length:")
