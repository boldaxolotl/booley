"""Contracts for the principle 14 patch-count CI check."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
from tests.git_support import commit_files, repository

_SCRIPTS = Path(__file__).parents[2] / ".github/scripts"
sys.path.insert(0, str(_SCRIPTS))

_SOURCE = "def helper(): ...\ndef _private(): ...\ndef run_captured_bounded(): ...\n"
_HEADER = "from booley import mod\nimport os\nimport shutil\nimport subprocess\nfrom pathlib import Path\n"
_PUBLIC = 'monkeypatch.setattr(mod, "helper", stub)'
_PRIVATE = 'monkeypatch.setattr(mod, "_private", stub)'
_COMMAND = 'monkeypatch.setattr(subprocess, "run", stub)'
_FIX = "mark a real boundary patch with '# boundary: <reason>'"


def _module(**tests: str) -> str:
    """A test module: patch statements start at line 7, two lines apart."""
    return _HEADER + "".join(
        f"def test_{name}(monkeypatch):\n    {body}\n" for name, body in tests.items()
    )


def _pull_request(
    tmp_path: Path, seed: dict[str, str], files: dict[str, str | None]
) -> tuple[Path, str, str]:
    repo = repository(tmp_path / "repo")
    base = commit_files(repo, {"src/booley/mod.py": _SOURCE, **seed})
    return repo, base, commit_files(repo, files)


def _run(repo: Path, base: str, head: str, labels: str = "") -> int:
    main = runpy.run_path(str(_SCRIPTS / "ci_patch_count.py"))["main"]
    return main(["--repo", str(repo), "--base", base, "--head", head, "--labels", labels])


def _added(statement: str, names: int, commands: int, *named: str, code: int = 1):
    """A case whose pull request adds one test module holding *statement*."""
    totals = (f"0 -> {names}", f"0 -> {commands}")
    return ({}, {"tests/test_a.py": _module(a=statement)}, code, totals, named)


@pytest.mark.parametrize(
    ("seed", "files", "code", "totals", "named"),
    [
        pytest.param(
            {"tests/test_a.py": _module(a=_PUBLIC, c=_PUBLIC)},
            {"tests/test_a.py": _module(a=_PUBLIC, b=_PUBLIC, c=_PUBLIC)},
            1,
            ("2 -> 3", "0 -> 0"),
            ("tests/test_a.py:9 mod.helper",),
            id="public-in-test-module",
        ),
        pytest.param(
            {},
            {"tests/support/fakes.py": _module(a=_PRIVATE)},
            1,
            ("0 -> 1", "0 -> 0"),
            ("tests/support/fakes.py:7 mod._private",),
            id="private-in-new-support-module",
        ),
        pytest.param(
            {"tests/test_a.py": _module(a=_PUBLIC, b=_PUBLIC)},
            {"tests/test_a.py": _module(c=_COMMAND)},
            1,
            ("2 -> 0", "0 -> 1"),
            ("command patches rose from 0 to 1", "tests/test_a.py:7 subprocess.run"),
            id="command-despite-removed-names",
        ),
        pytest.param(
            *_added(f"{_PUBLIC}  # boundary: runs git", 0, 0, code=0), id="boundary-with-reason"
        ),
        pytest.param(
            *_added(
                'monkeypatch.setattr(\n        mod, "helper", stub\n    )  # boundary: clock',
                0,
                0,
                code=0,
            ),
            id="boundary-on-wrapped-call",
        ),
        pytest.param(
            *_added(f"{_PUBLIC}  # boundary:", 1, 0, "tests/test_a.py:7"),
            id="boundary-empty-reason",
        ),
        pytest.param(
            *_added('monkeypatch.setattr(mod, "helper", "# boundary: x")', 1, 0),
            id="boundary-in-string",
        ),
        pytest.param(
            *_added('patch("booley.mod.helper")', 1, 0, "tests/test_a.py:7 booley.mod.helper"),
            id="string-target",
        ),
        pytest.param(
            *_added(
                'monkeypatch.setattr(mod, "run_captured_bounded", stub)',
                1,
                1,
                "Booley-name patches rose from 0 to 1",
                "command patches rose from 0 to 1",
            ),
            id="runner",
        ),
        pytest.param(
            *_added(
                'monkeypatch.setattr(os, "system", stub)\n'
                '    monkeypatch.setattr(shutil, "which", stub)',
                0,
                2,
                "tests/test_a.py:7 os.system",
                "tests/test_a.py:8 shutil.which",
            ),
            id="os-and-which",
        ),
        pytest.param(
            *_added('monkeypatch.setattr(Path, "home", stub)', 0, 0, code=0), id="external-only"
        ),
        pytest.param(
            *_added(
                'patch.object(mod, "helper", stub)\n'
                '    mock.patch.object(mod, "helper", stub)\n'
                '    monkeypatch.setattr("booley.mod.helper", stub)',
                3,
                0,
                "tests/test_a.py:7 mod.helper",
                "tests/test_a.py:8 mod.helper",
                "tests/test_a.py:9 booley.mod.helper",
            ),
            id="object-and-string-setattr",
        ),
        pytest.param(
            {},
            {".gitattributes": "*.py -diff\n", "tests/test_a.py": _module(a=_COMMAND)},
            1,
            ("0 -> 0", "0 -> 1"),
            ("tests/test_a.py:7 subprocess.run",),
            id="no-diff-attribute",
        ),
    ],
)
def test_patch_changes_decide_the_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    seed: dict[str, str],
    files: dict[str, str | None],
    code: int,
    totals: tuple[str, str],
    named: tuple[str, ...],
) -> None:
    repo, base, head = _pull_request(tmp_path, seed, files)

    assert _run(repo, base, head) == code

    streams = capsys.readouterr()
    assert f"Booley-name patches {totals[0]}, command patches {totals[1]} in 1 " in streams.out
    for text in (*named, _FIX) if code else ():
        assert text in streams.err
    assert "tests/test_a.py:11 " not in streams.err
    assert code or not streams.err


@pytest.mark.parametrize(
    ("seed", "files", "total"),
    [
        pytest.param(
            {"tests/test_old.py": _module(a=_PUBLIC, b=_PUBLIC)},
            {"tests/test_old.py": None, "tests/test_b.py": _module(a=_PUBLIC)},
            "2 -> 1",
            id="removed-and-moved",
        ),
        pytest.param(
            {"tests/test_a.py": _module(a=_PUBLIC, b=_PRIVATE)},
            {"tests/test_a.py": _module(a=_PUBLIC, b=_PUBLIC)},
            "2 -> 2",
            id="private-to-public",
        ),
    ],
)
def test_removed_moved_or_publicized_patches_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    seed: dict[str, str],
    files: dict[str, str | None],
    total: str,
) -> None:
    repo, base, head = _pull_request(tmp_path, seed, files)

    assert _run(repo, base, head) == 0

    assert f"Booley-name patches {total}," in capsys.readouterr().out


def test_exemption_label_decides_each_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = _pull_request(tmp_path, {}, {"tests/test_a.py": _module(a=_PUBLIC)})

    assert _run(repo, base, head) == 1
    assert _run(repo, base, head, "ci\npatch-count-exempt") == 0
    assert "exempt by the patch-count-exempt label" in capsys.readouterr().out
    assert _run(repo, base, head, "ci\ntest-ratio-exempt") == 1


@pytest.mark.parametrize(
    ("text", "base_override", "named"),
    [
        pytest.param("def broken(:\n", None, "tests/test_broken.py:1:", id="unparsable-file"),
        pytest.param("x = 1\n", "no-such-revision", "no-such-revision", id="git-failure"),
    ],
)
def test_unreadable_input_fails_the_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    base_override: str | None,
    named: str,
) -> None:
    repo, base, head = _pull_request(tmp_path, {}, {"tests/test_broken.py": text})

    assert _run(repo, base_override or base, head) == 2

    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert named in err
