"""Contracts for the principle 18 test-ratio CI check."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from tests.git_support import commit_files, git, repository

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

import ci_test_ratio

_EXEMPT = "ci\ntest-ratio-exempt"


def _lines(count: int, prefix: str = "x") -> str:
    return "".join(f"{prefix}{number} = {number}\n" for number in range(count))


def _run(repo: Path, base: str, head: str, labels: str = "") -> int:
    return ci_test_ratio.main(
        ["--repo", str(repo), "--base", base, "--head", head, "--labels", labels]
    )


def _pull_request(
    tmp_path: Path, files: dict[str, str | None], seed: dict[str, str] | None = None
) -> tuple[Path, str, str]:
    repo = repository(tmp_path / "repo")
    base = commit_files(repo, {"README.txt": "base\n", **(seed or {})})
    return repo, base, commit_files(repo, files)


@pytest.mark.parametrize(
    ("tests", "code"),
    [pytest.param(21, 1, id="21-test-lines"), pytest.param(20, 0, id="20-test-lines")],
)
def test_test_lines_over_twice_the_source_lines_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tests: int, code: int
) -> None:
    repo, base, head = _pull_request(
        tmp_path, {"src/pkg/mod.py": _lines(10), "tests/test_mod.py": _lines(tests)}
    )

    assert _run(repo, base, head) == code

    streams = capsys.readouterr()
    assert (
        f"{tests} test lines added, 0 removed; 10 source lines changed; limit 20." in streams.out
    )
    if code:
        for text in (
            f"{tests} test lines added for 10 changed source lines",
            "limit of 20",
            "tests/test_mod.py",
            "test-ratio-exempt",
        ):
            assert text in streams.err


def test_rust_inline_test_items_count_as_test_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = (
        '#[cfg(test)]\nmod tests {\n    const F: &str = "\nmodule m;\n}\n";\n\n'
        "    #[test]\n    fn t() {\n        assert!(true);\n    }\n}\n"
    )
    repo = repository(tmp_path / "repo")
    base = commit_files(repo, {"crates/k/src/lib.rs": "pub fn a() {}\n" + module})
    added = "".join(f"        assert_eq!({n}, {n});\n" for n in range(7))
    head_text = "pub fn a() {}\npub fn b() {}\npub fn c() {}\npub fn d() {}\n" + module.replace(
        "    }\n}\n", f"    }}\n{added}}}\n"
    )
    head = commit_files(repo, {"crates/k/src/lib.rs": head_text})

    assert _run(repo, base, head) == 1

    assert "7 test lines added, 0 removed; 3 source lines changed" in capsys.readouterr().out


def test_rust_test_regions_survive_nesting_and_removal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = (
        "mod a {\n    #[cfg(test)]\n    mod tests {\n        /* outer /* inner */ } */\n"
        "        fn t() {}\n" + "        fn u() {}\n" * 3 + "    }\n}\n"
    )
    gone = "pub fn f() {}\n#[cfg(test)]\nmod tests {\n" + "    fn t() {}\n" * 30 + "}\n"
    repo = repository(tmp_path / "repo")
    base = commit_files(repo, {"crates/k/gone.rs": gone, "crates/k/lib.rs": "mod a {\n}\n"})
    head = commit_files(repo, {"crates/k/gone.rs": None, "crates/k/lib.rs": nested})

    assert _run(repo, base, head) == 0

    assert "8 test lines added, 33 removed; 0 source lines changed" in capsys.readouterr().out


def test_counts_against_the_merge_base(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = repository(tmp_path / "repo")
    start = commit_files(repo, {"README.txt": "base\n"})
    git(repo, "switch", "-q", "-c", "topic")
    commit_files(repo, {"src/m.py": _lines(10), "tests/test_m.py": _lines(20)})
    topic = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    git(repo, "reset", "-q", "--hard", start)
    tip = commit_files(repo, {"src/m.py": _lines(10)})

    assert _run(repo, tip, topic) == 0

    assert "20 test lines added, 0 removed; 10 source lines changed" in capsys.readouterr().out


def test_removing_more_test_lines_than_added_passes(tmp_path: Path) -> None:
    repo, base, head = _pull_request(
        tmp_path,
        {"tests/test_old.py": None, "tests/test_new.py": _lines(5), "src/mod.py": "y = 1\n"},
        {"tests/test_old.py": _lines(30)},
    )

    assert _run(repo, base, head) == 0


def test_exemption_label_decides_each_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = _pull_request(tmp_path, {"tests/test_new.py": _lines(3)})

    assert _run(repo, base, head) == 1
    assert _run(repo, base, head, _EXEMPT) == 0
    assert "exempt by the test-ratio-exempt label" in capsys.readouterr().out
    assert _run(repo, base, head, "ci") == 1


def test_data_files_under_test_trees_do_not_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = _pull_request(
        tmp_path,
        {
            "tests/fixtures/wave.vcd": _lines(40),
            "tests/golden/out.json": _lines(40),
            "crates/k/tests/fixtures/t.vcd": _lines(40),
            "src/mod.py": "y = 1\n",
        },
    )

    assert _run(repo, base, head) == 0

    assert "0 test lines added, 0 removed; 1 source lines changed" in capsys.readouterr().out


def test_git_failure_fails_the_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _, head = _pull_request(tmp_path, {"src/mod.py": "y = 1\n"})

    assert _run(repo, "no-such-revision", head) == 2
    assert "error:" in capsys.readouterr().err


def test_malformed_hunk_header_is_rejected() -> None:
    with pytest.raises(ValueError, match="malformed hunk header"):
        ci_test_ratio.parse_diff("--- a/x\n+++ b/x\n@@ nonsense @@\n")


def test_workflow_reads_labels_live_on_every_pull_request() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["test-ratio"]
    run = "\n".join(step.get("run", "") for step in job["steps"])

    assert "gh api" in run
    assert "/issues/${PR_NUMBER}/labels" in run
    assert "pull_request.labels" not in yaml.safe_dump(job)
    steps = [step for step in job["steps"] if "ci_patch_count.py" in step.get("run", "")]
    assert len(steps) == 1, "no step runs the patch count"
    assert "gh api" in steps[0]["run"]
    assert "/issues/${PR_NUMBER}/labels" in steps[0]["run"]
    assert "--labels" in steps[0]["run"]
    assert "!cancelled()" in steps[0]["if"]
    assert job["needs"] == "changes"
    assert "github.event_name == 'pull_request'" in job["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    trigger = workflow[True]["pull_request"]
    assert "paths" not in trigger
    assert "paths-ignore" not in trigger
