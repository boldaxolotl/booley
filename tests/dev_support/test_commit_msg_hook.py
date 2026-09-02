"""Tests for commit_msg_hook — sanitize_message() and main() entry point."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support.commit_msg_hook import main, sanitize_message


@pytest.fixture(autouse=True)
def _project_hook_has_no_ambient_source_policy():
    """Unit-test the vendored Project hook, not this source checkout's role."""
    with patch("validate_commit_msg._current_repo_root", return_value=None):
        yield


# ---------------------------------------------------------------------------
# sanitize_message
# ---------------------------------------------------------------------------


class TestSanitizeMessage:
    """Bodies survive — redacted, never truncated (F-11); trailers are dropped."""

    def test_keeps_body_on_normal_commit(self):
        """The body is authored work: redact it, don't throw it away (F-11)."""
        msg = "fix(core): repair widget\n\nThis is the body.\nMore details."
        assert sanitize_message(msg) == (
            "fix(core): repair widget\n\nThis is the body.\nMore details.\n"
        )

    def test_body_banned_words_redacted_in_place(self):
        """A body line naming a tool keeps its prose; only the word is scrubbed."""
        msg = "fix(core): repair widget\n\nRan the docker build to check the fix.\n"
        result = sanitize_message(msg)
        assert "docker" not in result
        assert "Ran the redacted build to check the fix." in result

    def test_attribution_trailers_dropped_not_redacted(self):
        """Redacting a trailer leaves debris; drop it, keep the rationale above."""
        msg = (
            "fix(core): repair widget\n\n"
            "The widget dropped every other frame.\n\n"
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        )
        result = sanitize_message(msg)
        assert "The widget dropped every other frame." in result
        assert "Co-Authored-By" not in result
        assert "🤖" not in result
        assert "noreply" not in result

    def test_body_separated_from_subject_by_one_blank_line(self):
        """Git convention, regardless of how the author spaced the original."""
        assert sanitize_message("fix(a): x\nBody\n") == "fix(a): x\n\nBody\n"

    def test_honest_prose_is_redacted_not_deleted(self):
        """Only the 🤖 footer and Co-Authored-By: are droppable. A sentence that
        merely trips the banned list keeps its shape — a mangled word is
        recoverable, a deleted line is not. ('generated' is on the list, so an
        attribution-shaped rule would have eaten this real sentence.)"""
        msg = "fix(core): repair widget\n\nGenerated with care by the whole team.\n"
        result = sanitize_message(msg)
        assert "with care by the whole team." in result
        assert "care by the whole team" in result.split("\n\n")[1]

    def test_preserves_subject_only(self):
        msg = "feat(ui): add button\n"
        assert sanitize_message(msg) == "feat(ui): add button\n"

    def test_strips_comment_lines(self):
        msg = "fix(ci): tweak pipeline\n# This is a git comment\n"
        assert sanitize_message(msg) == "fix(ci): tweak pipeline\n"

    def test_merge_commit_keeps_body(self):
        msg = "Merge branch 'feature'\n\nSome merge details.\n"
        result = sanitize_message(msg)
        assert result.startswith("Merge branch 'feature'\n")
        assert "Some merge details." in result

    def test_merge_commit_strips_banned_lines(self):
        msg = "Merge branch 'dev'\n\nClean line\nCo-Authored-By: bot\nAnother clean line\n"
        result = sanitize_message(msg)
        assert "Co-Authored-By" not in result
        assert "Clean line" in result
        assert "Another clean line" in result

    def test_merge_scope_syntax(self):
        """merge(scope): ... is treated as a merge commit."""
        msg = "merge(main): reconcile\n\nBody text\n"
        result = sanitize_message(msg)
        assert "Body text" in result

    def test_crlf_normalised(self):
        msg = "fix(a): msg\r\nBody\r\n"
        result = sanitize_message(msg)
        assert "\r" not in result
        assert result == "fix(a): msg\n\nBody\n"

    def test_cr_only_normalised(self):
        msg = "fix(b): msg\rBody\r"
        result = sanitize_message(msg)
        assert "\r" not in result

    def test_empty_message(self):
        result = sanitize_message("")
        assert result == "\n"

    def test_whitespace_only(self):
        result = sanitize_message("   \n\n")
        assert result.strip() == ""

    def test_merge_no_body(self):
        msg = "Merge branch 'main'\n"
        result = sanitize_message(msg)
        assert result == "Merge branch 'main'\n"

    def test_multiple_comment_lines(self):
        """Git's own ``#`` lines go; the author's body line stays."""
        msg = "feat(x): thing\n# comment1\n# comment2\nBody line\n"
        result = sanitize_message(msg)
        assert result == "feat(x): thing\n\nBody line\n"

    def test_comment_only_body(self):
        msg = "fix(y): stuff\n# only comments here\n"
        result = sanitize_message(msg)
        assert result == "fix(y): stuff\n"

    def test_redacts_banned_word_in_subject(self):
        """Banned word in the summary is scrubbed in place, not left to leak."""
        result = sanitize_message("fix(sim): rebuild the docker base image\n")
        assert "docker" not in result
        assert result == "fix(sim): rebuild the redacted base image\n"

    def test_redacts_banned_scope_keeping_format(self):
        """A banned scope (e.g. the framework name) redacts to a valid scope."""
        result = sanitize_message("feat(booley): add elaboration cache\n")
        assert "booley" not in result
        assert result == "feat(redacted): add elaboration cache\n"

    def test_merge_subject_also_redacted(self):
        """Redaction covers the merge path's subject, not just its body lines."""
        result = sanitize_message("Merge branch 'claude-fix'\n\nClean line\n")
        assert "claude" not in result
        assert "Clean line" in result


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


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

    def test_valid_message_written_back(self, tmp_path: Path):
        """main() sanitises, writes back, and validates a good message."""
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("fix(core): repair edge case\n\nThe body.\n", encoding="utf-8")

        with (
            patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(msg_file)]),
            patch("validate_commit_msg.validate_message", return_value=[]),
        ):
            rc = main()

        assert rc == 0
        # The body reaches history intact — the hook redacts, it does not truncate.
        written = msg_file.read_text(encoding="utf-8")
        assert written == "fix(core): repair edge case\n\nThe body.\n"

    def test_validation_failure(self, tmp_path: Path):
        """main() returns 1 when validate_message reports errors."""
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("bad message\n", encoding="utf-8")

        with (
            patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(msg_file)]),
            patch(
                "validate_commit_msg.validate_message",
                return_value=["Subject doesn't match format"],
            ),
        ):
            rc = main()

        assert rc == 1

    def test_skip_env_bypasses_convention_but_still_sanitizes(self, tmp_path: Path, monkeypatch):
        """SETUP-10: BOOLEY_SKIP_COMMIT_VALIDATION skips the convention check,
        but the IP-leak scrub still runs — the banned word goes, the prose stays."""
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text(
            "Upstream style summary\n\nrebuilt the docker image\n", encoding="utf-8"
        )
        monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")

        with (
            patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(msg_file)]),
            patch(
                "validate_commit_msg.validate_message",
                return_value=["Subject doesn't match format"],
            ),
        ):
            rc = main()

        assert rc == 0
        written = msg_file.read_text(encoding="utf-8")
        assert "docker" not in written
        assert written == "Upstream style summary\n\nrebuilt the redacted image\n"

    def test_standalone_hook_leaves_source_checkout_message_unchanged(self, tmp_path: Path):
        """A vendored hook cannot depend on the installed ``booley`` package."""
        root = tmp_path / "source"
        hooks = root / "stale-hooks"
        hooks.mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
        support = Path(__file__).resolve().parents[2] / "src" / "booley" / "dev_support"
        for name in ("commit_msg_hook.py", "commit_msg_utils.py", "validate_commit_msg.py"):
            shutil.copy2(support / name, hooks / name)
        package = support.parent
        shutil.copy2(package / "runtime" / "checkout_role.py", hooks / "checkout_role.py")
        shutil.copy2(package / "core" / "boundary.py", hooks / "boundary.py")
        message = root / "COMMIT_EDITMSG"
        original = "docs: explain Booley architecture\n"
        message.write_text(original, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "-S", str(hooks / "commit_msg_hook.py"), str(message)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr
        assert message.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Redaction notice (F-8)
# ---------------------------------------------------------------------------


class TestRedactionNotice:
    """Sanitization is stealth-by-design, but it was also stealth from the
    author: a scope silently became `(redacted)` with nothing said."""

    def _run(self, tmp_path: Path, msg: str) -> str:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text(msg, encoding="utf-8")
        with (
            patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(msg_file)]),
            patch("validate_commit_msg.validate_message", return_value=[]),
        ):
            assert main() == 0
        return msg_file.read_text(encoding="utf-8")

    def test_redacted_subject_is_announced(self, tmp_path: Path, capsys):
        self._run(tmp_path, "feat(booley): wire the thing\n")
        err = capsys.readouterr().err
        # The notice must name its cause — "stealth-mode redaction" — so a
        # first-time user isn't left guessing why their scope changed (F-15).
        assert "stealth-mode redaction" in err
        assert "feat(redacted): wire the thing" in err

    def test_notice_does_not_echo_the_banned_phrase(self, tmp_path: Path, capsys):
        self._run(tmp_path, "feat(booley): wire the thing\n")
        assert "booley" not in capsys.readouterr().err

    def test_rewritten_body_is_announced(self, tmp_path: Path, capsys):
        written = self._run(tmp_path, "fix(core): repair edge case\n\nchecked the docker image\n")
        err = capsys.readouterr().err
        assert "rewrote the commit body" in err
        # And it says so *because* it edited the body, not because it dropped it.
        assert "checked the redacted image" in written

    def test_untouched_body_draws_no_notice(self, tmp_path: Path, capsys):
        """A clean body is kept verbatim, so there is nothing to announce."""
        written = self._run(tmp_path, "fix(core): repair edge case\n\nA clean body.\n")
        assert written == "fix(core): repair edge case\n\nA clean body.\n"
        assert capsys.readouterr().err == ""

    def test_redacted_subject_does_not_mask_the_body_notice(self, tmp_path: Path, capsys):
        # Both notices must fire independently: an `elif` here once let a
        # redacted subject swallow the body notice, so body edits (trailers
        # included) happened without a word.
        written = self._run(
            tmp_path,
            "feat(booley): wire the thing\n\nLong body.\n\nCo-Authored-By: Someone\n",
        )
        err = capsys.readouterr().err
        assert "stealth-mode redaction" in err
        assert "rewrote the commit body" in err
        # The prose survives; only the trailer is gone.
        assert "Long body." in written
        assert "Co-Authored-By" not in written

    def test_sanitization_notice_names_the_opt_out(self, tmp_path: Path, capsys):
        self._run(tmp_path, "fix(core): repair edge case\n\nbuilt the docker image\n")
        err = capsys.readouterr().err
        assert "[stealth]" in err and "enabled = false" in err

    def test_clean_single_line_message_is_silent(self, tmp_path: Path, capsys):
        self._run(tmp_path, "fix(core): repair edge case\n")
        assert capsys.readouterr().err == ""

    def test_merge_commit_body_survives_and_is_not_announced(self, tmp_path: Path, capsys):
        self._run(tmp_path, "Merge branch 'dev'\n\nSome detail\n")
        assert "body dropped" not in capsys.readouterr().err

    def test_comment_only_body_is_not_reported_as_dropped(self, tmp_path: Path, capsys):
        # git's own '# ...' lines are not the author's body.
        self._run(tmp_path, "fix(core): repair edge case\n# please enter a message\n")
        assert capsys.readouterr().err == ""
