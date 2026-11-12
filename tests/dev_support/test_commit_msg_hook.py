"""Tests for commit_msg_hook: rejection of banned terms and attribution."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.subprocess_budget import run_bounded_child

from booley.dev_support.commit_msg_hook import main


@pytest.fixture(autouse=True)
def project(tmp_path_factory) -> Path:
    """Unit-test the vendored Project hook, not this source checkout's role.

    The hook acts only in Stealth Mode, so the default Project enables it explicitly.
    """
    project_root = tmp_path_factory.mktemp("stealth-project")
    (project_root / ".booley").mkdir()
    _write_config(project_root, "[stealth]\nenabled = true\n")
    with patch("validate_commit_msg._current_repo_root", return_value=project_root):
        yield project_root


def _write_config(project_root: Path, text: str) -> None:
    (project_root / ".booley" / "booley.toml").write_text(text, encoding="utf-8")


def _run_hook(
    tmp_path: Path, raw: bytes | str, name: str = "COMMIT_EDITMSG"
) -> tuple[int, str, bytes]:
    """Run ``main()`` on a message file; return (rc, stderr text, file bytes after)."""
    msg_file = tmp_path / name
    msg_file.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with (
        patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(msg_file)]),
        redirect_stderr(err),
    ):
        rc = main()
    return rc, err.getvalue(), msg_file.read_bytes()


class TestBannedTermRejection:
    """A banned term rejects the commit; the message file is never rewritten."""

    def test_word_in_subject_rejected_file_unchanged(self, tmp_path: Path):
        original = "fix(sim): rebuild the llm base image\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 1
        assert after == original.encode()
        assert "'llm' in the subject" in err

    def test_vendor_name_inside_identifier_rejected(self, tmp_path: Path):
        original = (
            "fix(core): repair widget\n\nSplit the helper.\nRenamed BooleyRunner to Runner.\n"
        )
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 1
        assert after == original.encode()
        assert "'booley' on line 4" in err

    def test_banned_substring_rejected(self, tmp_path: Path, project: Path):
        _write_config(project, '[stealth]\nenabled = true\nbanned_substrings = ["quokka"]\n')
        original = "fix(core): repair myquokkafile\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 1
        assert after == original.encode()
        assert "'quokka' in the subject" in err

    def test_rejection_lists_every_match_and_both_ways_out(self, tmp_path: Path):
        original = "fix(llm): tune the claude build\n\nRan booley twice.\nbooley again.\n"
        rc, err, _ = _run_hook(tmp_path, original)
        assert rc == 1
        assert "'llm' in the subject" in err
        assert "'claude' in the subject" in err
        assert "'booley' on line 3" in err
        assert "'booley' on line 4" in err
        assert "Reword the message" in err
        assert "banned_words" in err and "banned_substrings" in err
        assert "-v -e -F" in err

    def test_one_entry_per_term_per_line(self, tmp_path: Path):
        _, err, _ = _run_hook(tmp_path, "fix(core): llm llm LLM\n")
        assert err.count("'llm'") == 1

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("fix(core): repair widget\n# booley is mentioned here\n", id="comment"),
            pytest.param(
                "fix(core): repair widget\n\n# ------------------------ >8 ------------------------\n"
                "diff --git a/booley.py b/booley.py\n+import booley\n",
                id="scissors",
            ),
        ],
    )
    def test_ignored_lines_do_not_reject(self, tmp_path: Path, raw: str):
        rc, err, after = _run_hook(tmp_path, raw)
        assert rc == 0, err
        assert after == raw.encode()

    def test_configured_comment_char_is_honored(self, tmp_path: Path, project: Path):
        for argv in (
            ["git", "init", "-q", str(project)],
            ["git", "-C", str(project), "config", "core.commentChar", ";"],
        ):
            assert run_bounded_child(argv, timeout=10).returncode == 0
        rc, err, _ = _run_hook(tmp_path, "fix(core): repair widget\n; booley here\n")
        assert rc == 0, err
        rc, err, _ = _run_hook(tmp_path, "fix(core): repair widget\n# booley here\n")
        assert rc == 1
        assert "'booley' on line 2" in err

    def test_comment_char_auto_falls_back_to_hash(self, tmp_path: Path, project: Path):
        for argv in (
            ["git", "init", "-q", str(project)],
            ["git", "-C", str(project), "config", "core.commentChar", "auto"],
        ):
            assert run_bounded_child(argv, timeout=10).returncode == 0
        rc, err, _ = _run_hook(tmp_path, "fix(core): repair widget\n# booley here\n")
        assert rc == 0, err

    def test_accepted_message_file_is_never_written(self, tmp_path: Path):
        original = b"fix(core): repair widget\r\n\r\n# a comment\r\nBody line.\r\n\r\n\r\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 0, err
        assert err == ""
        assert after == original

    def test_message_lines_keeps_file_line_numbers(self):
        from booley.dev_support.commit_msg_hook import message_lines

        assert message_lines("a\n# c\nb\n", "#") == [(1, "a"), (3, "b"), (4, "")]

    def test_merge_commit_message_file_gets_merge_hint(self, tmp_path: Path):
        rc, err, _ = _run_hook(tmp_path, "Merge branch 'llm-fix'\n", name="MERGE_MSG")
        assert rc == 1
        assert "git commit -m" in err and "git merge" in err

    def test_plain_rejection_has_no_merge_hint(self, tmp_path: Path):
        _, err, _ = _run_hook(tmp_path, "fix(core): llm\n")
        assert "merge commit" not in err

    def test_convention_errors_listed_with_banned_terms(self, tmp_path: Path, project: Path):
        _write_config(project, "[stealth]\nenabled = true\nenforce_convention = true\n")
        _, err, _ = _run_hook(tmp_path, "not conventional llm\n")
        assert "'llm' in the subject" in err
        assert "Subject doesn't match" in err


class TestAttributionRejection:
    """Attribution footers are rejected without echoing the identity."""

    @pytest.mark.parametrize(
        "footer",
        [
            "  co-authored-by  : Person <person@example.test>",
            "  \U0001f916 Generated with private-identity",
        ],
    )
    def test_structural_attribution_rejected_without_echo(self, tmp_path: Path, footer: str):
        original = f"fix(core): repair edge case\n\nUseful rationale.\n\n{footer}\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 1
        assert after == original.encode()
        assert footer.strip() not in err
        assert "remove" in err.lower() and "retry" in err.lower()
        assert "-v -e -F" in err

    @pytest.mark.parametrize(
        "footer",
        [
            "Generated with assistant-identity",
            "  GENERATED   WITH   assistant-identity  ",
            "Generated with [assistant-identity](https://example.test/assistant-identity)",
        ],
    )
    def test_plain_attribution_uses_project_vocabulary(
        self, tmp_path: Path, project: Path, footer: str
    ):
        _write_config(
            project,
            '[stealth]\nenabled = true\nbanned_words = ["generated", "assistant-identity"]\n',
        )
        original = f"fix(core): repair widget\n\nUseful rationale.\n\n{footer}\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 1
        assert after == original.encode()
        assert "assistant-identity" not in err

    def test_generated_prose_with_banned_term_is_a_term_rejection(
        self, tmp_path: Path, project: Path
    ):
        _write_config(
            project,
            '[stealth]\nenabled = true\nbanned_words = ["generated", "docker"]\n',
        )
        rc, err, _ = _run_hook(
            tmp_path, "fix(core): repair widget\n\nGenerated with Docker for reproducibility.\n"
        )
        assert rc == 1
        assert "'generated' on line 3" in err
        assert "attribution footer" not in err

    def test_empty_vocabulary_accepts_plain_shape_but_rejects_structural(
        self, tmp_path: Path, project: Path
    ):
        _write_config(project, "[stealth]\nenabled = true\nbanned_words = []\n")
        plain = "fix(core): repair widget\n\nGenerated with assistant-identity\n"
        rc, err, after = _run_hook(tmp_path, plain)
        assert rc == 0, err
        assert after == plain.encode()
        rc, _, _ = _run_hook(
            tmp_path, "fix(core): repair widget\n\nCo-Authored-By: Person <person@example.test>\n"
        )
        assert rc == 1

    def test_attribution_in_git_comment_is_ignored(self, tmp_path: Path, project: Path):
        _write_config(
            project,
            '[stealth]\nenabled = true\nbanned_words = ["generated", "assistant-identity"]\n',
        )
        rc, err, _ = _run_hook(
            tmp_path, "fix(core): repair widget\n# Generated with assistant-identity\n"
        )
        assert rc == 0, err

    def test_merge_commit_rejects_attribution(self, tmp_path: Path):
        original = "Merge branch 'dev'\n\nClean line\nCo-Authored-By: bot\nAnother clean line\n"
        rc, _, after = _run_hook(tmp_path, original)
        assert rc == 1
        assert after == original.encode()

    def test_default_generated_footer_is_accepted_verbatim(self, tmp_path: Path):
        original = "fix(core): repair widget\n\nGenerated with care by the whole team\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 0, err
        assert after == original.encode()

    def test_attribution_payload_equality_is_not_broadened_by_substrings(
        self, tmp_path: Path, project: Path
    ):
        _write_config(
            project,
            '[stealth]\nenabled = true\nbanned_words = []\nbanned_substrings = ["quokka"]\n',
        )
        rc, err, _ = _run_hook(tmp_path, "fix: repair\n\nGenerated with QuokkaRunner\n")
        assert rc == 1
        assert "attribution footer" not in err
        assert "'quokka' on line 3" in err
        rc, err, _ = _run_hook(tmp_path, "fix: repair\n\nGenerated with quokka\n")
        assert rc == 1
        assert "attribution footer" in err


class TestSkipVariable:
    """BOOLEY_SKIP_COMMIT_VALIDATION skips convention checks only."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("fix(core): repair the llm path\n", id="banned"),
            pytest.param("fix(core): repair edge case\n\nCo-Authored-By: bot\n", id="attribution"),
        ],
    )
    def test_skip_still_rejects(self, tmp_path: Path, monkeypatch, raw: str):
        monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")
        rc, err, after = _run_hook(tmp_path, raw)
        assert rc == 1
        assert after == raw.encode()
        assert "-v -e -F" in err

    def test_skip_skips_convention_and_body_checks(
        self, tmp_path: Path, project: Path, monkeypatch
    ):
        _write_config(
            project,
            "[stealth]\nenabled = true\nenforce_convention = true\nmax_body_lines = 0\n",
        )
        raw = "Upstream style summary\n\nA body line.\n"
        rc, err, _ = _run_hook(tmp_path, raw)
        assert rc == 1
        assert "Commit message validation FAILED" in err
        assert "banned terms and attribution footers are still rejected" in err
        monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")
        rc, err, after = _run_hook(tmp_path, raw)
        assert rc == 0, err
        assert after == raw.encode()

    def test_skip_omits_convention_errors_from_banned_rejection(
        self, tmp_path: Path, project: Path, monkeypatch
    ):
        _write_config(project, "[stealth]\nenabled = true\nenforce_convention = true\n")
        monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")
        _, err, _ = _run_hook(tmp_path, "not conventional llm\n")
        assert "'llm' in the subject" in err
        assert "Subject doesn't match" not in err


class TestMain:
    def test_no_args(self):
        """main() returns 1 when no message file argument is given."""
        with patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg"]):
            assert main() == 1

    def test_missing_file(self, tmp_path: Path):
        """main() returns 1 when the message file doesn't exist."""
        missing = tmp_path / "no_such_file"
        with patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(missing)]):
            assert main() == 1

    def test_validation_failure(self, tmp_path: Path):
        """main() returns 1 when the convention check reports errors."""
        with patch(
            "validate_commit_msg.convention_errors",
            return_value=["Subject doesn't match format"],
        ):
            rc, err, _ = _run_hook(tmp_path, "bad message\n")
        assert rc == 1
        assert "Subject doesn't match format" in err

    def test_stealth_disabled_leaves_message_unchanged(self, tmp_path: Path, project: Path):
        _write_config(
            project,
            '[stealth]\nenabled = false\nbanned_words = ["generated", "assistant-identity"]\n',
        )
        original = "fix(core): repair edge case\n\nGenerated with assistant-identity llm\n"
        rc, err, after = _run_hook(tmp_path, original)
        assert rc == 0
        assert after == original.encode()
        assert err == ""

    def test_standalone_hook_leaves_source_checkout_message_unchanged(self, tmp_path: Path):
        """A standalone hook member cannot depend on installed ``booley``."""
        root = tmp_path / "source"
        hooks = root / "stale-hooks"
        hooks.mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )
        init = run_bounded_child(["git", "init", "-q", str(root)], timeout=10)
        assert init.returncode == 0, init.stderr
        support = Path(__file__).resolve().parents[2] / "src" / "booley" / "dev_support"
        for name in ("commit_msg_hook.py", "commit_msg_utils.py", "validate_commit_msg.py"):
            shutil.copy2(support / name, hooks / name)
        package = support.parent
        shutil.copy2(package / "commit_policy" / "policy.py", hooks / "booley_commit_policy.py")
        shutil.copy2(
            package / "commit_policy" / "validation.py",
            hooks / "booley_commit_validation.py",
        )
        shutil.copy2(package / "core" / "checkout_role.py", hooks / "checkout_role.py")
        shutil.copy2(package / "core" / "boundary.py", hooks / "boundary.py")
        message = root / "COMMIT_EDITMSG"
        original = "docs: explain Booley architecture\n"
        message.write_text(original, encoding="utf-8")

        result = run_bounded_child(
            [sys.executable, "-S", str(hooks / "commit_msg_hook.py"), str(message)],
            cwd=root,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        assert message.read_text(encoding="utf-8") == original
