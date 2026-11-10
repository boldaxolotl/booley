"""Contracts for the Goal Mode unit-test import facts behind change classification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_goal_tests import goal_module_name, is_shared_goal_path, shared_goal_modules


def _tracked_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"],
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=SUBPROCESS_TIMEOUT_S)
    return repo


def test_module_names_follow_the_import_system() -> None:
    assert goal_module_name("tests/goals/a/b.py") == "tests.goals.a.b"
    assert goal_module_name("tests/goals/__init__.py") == "tests.goals"
    assert is_shared_goal_path("tests/goals/a.py", frozenset({"tests.goals.a"}))
    assert not is_shared_goal_path("tests/goals/fixtures/a.txt", frozenset({"tests.goals.a"}))


def test_shared_modules_close_over_helper_imports_including_relative_ones(tmp_path: Path) -> None:
    repo = _tracked_repo(
        tmp_path,
        {
            "tests/goals/__init__.py": "",
            "tests/goals/conftest.py": "from . import support\nfrom .sub import deep\n",
            "tests/goals/support.py": "import tests.goals.leaf\n",
            "tests/goals/leaf.py": "",
            "tests/goals/sub/__init__.py": "",
            "tests/goals/sub/deep.py": "from .. import unused_sibling\nfrom ..leaf import X\n",
            "tests/goals/unused.py": "",
            "tests/goals/test_own.py": "import tests.goals.unused\n",
            "tests/harness/test_x.py": "from tests.goals import conftest\n",
            "tests/fixtures/sample/test_y.py": "import tests.goals.unused\n",
        },
    )

    shared = shared_goal_modules(repo)

    assert {
        "tests.goals",
        "tests.goals.conftest",
        "tests.goals.support",
        "tests.goals.leaf",
        "tests.goals.sub.deep",
    } <= shared
    assert "tests.goals.unused" not in shared


def test_unparsable_test_file_is_an_error_not_a_skip(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path, {"tests/harness/test_bad.py": "def broken(:\n"})

    with pytest.raises(ValueError, match="cannot parse tests/harness/test_bad\\.py"):
        shared_goal_modules(repo)


def test_missing_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        shared_goal_modules(tmp_path / "absent")
