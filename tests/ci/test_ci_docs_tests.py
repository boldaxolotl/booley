"""Contracts for the documentation content-test derivation behind ``docs-check``."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

_WORKFLOW = _ROOT / ".github/workflows/test.yml"

# Content tests the issue names explicitly; the derivation must never lose them.
_NAMED_CONTENT_TESTS = (
    "tests/test_readme_contract.py",
    "tests/test_site_contract.py",
    "tests/docs/test_links.py",
    "tests/docs/test_current_version_docs.py",
    "tests/runtime/test_docs_links.py",
    "tests/test_paths.py",
)

_BASE_FILES = {
    "README.md": "# readme\n",
    "docs/user/NEW.md": "new\n",
    "docs/user/X.md": "x\n",
    "qa/AREAS.md": "areas\n",
    # A non-documentation file keeps the ``qa`` directory from being a narrow
    # name by itself, so path-join readers must be found through the join.
    "qa/runner.py": "RUN = 1\n",
}


def _script() -> ModuleType:
    """Import the derivation script inside each test so a missing script fails the test body."""
    return importlib.import_module("ci_docs_tests")


def _tracked_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a Git repository whose index holds ``_BASE_FILES`` plus ``files``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    for relative, text in {**_BASE_FILES, **files}.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


def test_repository_derivation_includes_the_named_content_tests() -> None:
    derived = set(_script().derive(_ROOT))

    assert set(_NAMED_CONTENT_TESTS) <= derived
    # A reader found only through the derivation rules, not the named list.
    assert "tests/qa/test_missions.py" in derived


def test_docs_check_runs_the_derived_content_tests() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["docs-check"]

    assert "continue-on-error" not in job
    steps = job["steps"]
    runs = [step for step in steps if "ci_docs_tests.py" in step.get("run", "")]
    assert len(runs) == 1
    assert "pytest" in runs[0]["run"]
    assert "continue-on-error" not in runs[0]
    assert "if" not in runs[0]
    assert all("continue-on-error" not in step for step in steps)
    assert "docs-check" in workflow["jobs"]["ci-required"]["needs"]
    # The gate proves the content tests really ran, like the test legs do.
    evidence = [step for step in steps if step.get("uses", "").endswith("publish-pytest-evidence")]
    assert len(evidence) == 1
    assert evidence[0]["if"] == "always()"
    assert evidence[0]["with"]["label"] == "docs-check"


def test_derivation_fails_when_a_required_content_test_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _tracked_repo(tmp_path, {"tests/docs/test_other.py": "def test_x(): pass\n"})

    assert _script().main(["--repo", str(repo)]) == 2

    assert "tests/test_readme_contract.py" in capsys.readouterr().err


def test_derivation_fails_outside_a_git_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _script().main(["--repo", str(tmp_path)]) == 2
    error = capsys.readouterr().err
    assert error
    # A scratch directory inside some Git checkout would exit 2 through the
    # missing-test floor instead, which would not prove the Git failure.
    assert "not derived" not in error


def test_unparsable_python_is_reported_not_silently_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _tracked_repo(tmp_path, {"tests/test_broken.py": "def (:\n"})

    _script().derive(repo)

    assert "tests/test_broken.py" in capsys.readouterr().err


_READ_DOC = 'from pathlib import Path\nROOT = Path(__file__).parents[1]\nTEXT = (ROOT / "{}").read_text()\n'


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {"tests/test_x.py": _READ_DOC.format("docs/user/NEW.md")},
            "tests/test_x.py",
            id="test-literal",
        ),
        pytest.param(
            {
                "src/booley/reader.py": _READ_DOC.format("docs/user/X.md"),
                "tests/test_y.py": "from booley import reader\n",
            },
            "tests/test_y.py",
            id="src-reader-import",
        ),
        pytest.param(
            {
                "src/booley/reader.py": _READ_DOC.format("docs/user/X.md"),
                "tests/test_y.py": "import booley.reader\n",
            },
            "tests/test_y.py",
            id="src-reader-dotted-import",
        ),
        pytest.param(
            {
                "tests/support.py": _READ_DOC.format("README.md"),
                "tests/test_z.py": "from tests.support import TEXT\n",
            },
            "tests/test_z.py",
            id="support-module-import",
        ),
        pytest.param(
            {
                "tests/support.py": _READ_DOC.format("README.md"),
                "tests/middle.py": "from tests import support\n",
                "tests/test_z.py": "from tests.middle import support\n",
            },
            "tests/test_z.py",
            id="support-module-transitive",
        ),
        pytest.param(
            {
                "tests/qa/test_missions.py": (
                    "from pathlib import Path\n"
                    'QA_ROOT = Path(__file__).parents[2] / "qa"\n'
                    'AREAS = QA_ROOT / "AREAS.md"\n'
                ),
            },
            "tests/qa/test_missions.py",
            id="path-join",
        ),
        pytest.param(
            {"tests/docs/test_anything.py": "def test_x():\n    assert True\n"},
            "tests/docs/test_anything.py",
            id="tests-docs-directory",
        ),
        pytest.param(
            {
                "tests/area/conftest.py": _READ_DOC.format("docs/user/X.md"),
                "tests/area/test_a.py": "def test_a():\n    assert True\n",
            },
            "tests/area/test_a.py",
            id="reader-conftest",
        ),
    ],
)
def test_new_content_reader_is_derived(
    tmp_path: Path, files: dict[str, str], expected: str
) -> None:
    repo = _tracked_repo(tmp_path, files)

    assert expected in _script().derive(repo)


def test_unrelated_test_is_not_derived(tmp_path: Path) -> None:
    repo = _tracked_repo(
        tmp_path,
        {
            "tests/test_unrelated.py": 'def test_x():\n    assert "hello"\n',
            # A source module without __file__ is not repository-anchored, even
            # though it mentions the word "docs".
            "src/booley/words.py": 'BUCKET = "docs"\n',
            "tests/test_words.py": "from booley import words\n",
            # Synthetic fixture conftests write documentation into temporary
            # projects; they do not read this repository's documentation.
            "tests/goals/conftest.py": 'SPEC = "docs/user/X.md"\n',
            "tests/goals/test_goal.py": "def test_g():\n    assert True\n",
        },
    )

    derived = set(_script().derive(repo))

    assert "tests/test_unrelated.py" not in derived
    assert "tests/test_words.py" not in derived
    assert "tests/goals/test_goal.py" not in derived
