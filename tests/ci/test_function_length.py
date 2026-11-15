"""CI's function length check: limit, growth rule, docstrings, nesting, and wiring."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _SOURCE_ROOT / ".github/scripts/check_function_length.py"
_WORKFLOW = _SOURCE_ROOT / ".github/workflows/test.yml"
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


@pytest.mark.parametrize(
    ("name", "lines", "code"),
    [("mod.py", 51, 1), ("mod.py", 50, 0), ("café.py", 51, 1)],
    ids=["51-fails", "50-passes", "51-non-ascii-path-fails"],
)
def test_new_function_over_limit_fails(repo: Path, name: str, lines: int, code: int) -> None:
    _commit(repo, {f"src/booley/{name}": _body("f", lines)})
    assert _run(repo)[0] == code


@pytest.mark.parametrize(
    ("lines", "code"),
    [(61, 1), (60, 0), (55, 0)],
    ids=["grown-fails", "unchanged-passes", "shrunk-passes"],
)
def test_oversize_base_function_may_not_grow(repo: Path, lines: int, code: int) -> None:
    _commit(repo, {"src/booley/old.py": _body("f", lines) + "y = 2\n"})
    assert _run(repo)[0] == code


def test_docstring_and_blank_lines_do_not_count() -> None:
    source = 'def f():\n    """Doc\n\n    more\n    """\n\n' + "    x = 1\n\n" * 49
    assert _gate().function_lengths(source) == [("f", 50)]
    assert _gate().violations("a.py", source, None) == []
    assert _gate().violations("a.py", source + "    x = 1\n", None) != []


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


def test_lint_runs_function_length_check_on_pull_requests() -> None:
    lint = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["lint"]
    checkout = lint["steps"][0]
    assert checkout["with"]["fetch-depth"] == 2
    steps = [s for s in lint["steps"] if "check_function_length.py" in s.get("run", "")]
    assert len(steps) == 1
    step = steps[0]
    assert step["run"] == 'python .github/scripts/check_function_length.py --base "${BASE_SHA}"'
    assert step["env"] == {"BASE_SHA": "${{ needs.changes.outputs.diff_base }}"}
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "continue-on-error" not in step
