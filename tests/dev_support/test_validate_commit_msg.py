"""Tests for validate_commit_msg — message format validation and diff scanning."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from booley.dev_support.validate_commit_msg import (
    ALLOWED_TYPES,
    MAX_SUMMARY_LEN,
    SUBJECT_RE,
    _has_project_config,
    validate_diff,
    validate_message,
)


def _convention(enabled: bool = True):
    """Patch the [stealth] enforce_convention knob the validator reads."""
    return patch("booley.dev_support.validate_commit_msg.enforce_convention", return_value=enabled)


def test_stealth_disabled_skips_banned_phrase_validation():
    with (
        _convention(False),
        patch("booley.dev_support.validate_commit_msg.stealth_enabled", return_value=False),
    ):
        assert validate_message("mention Booley intentionally") == []


# ---------------------------------------------------------------------------
# SUBJECT_RE pattern
# ---------------------------------------------------------------------------


class TestSubjectRegex:
    """Verify the conventional-commit regex accepts/rejects expected formats."""

    def test_feat_with_scope(self):
        assert SUBJECT_RE.match("feat(core): add widget")

    def test_fix_no_scope(self):
        assert SUBJECT_RE.match("fix: repair the thing")

    def test_refactor_with_scope(self):
        assert SUBJECT_RE.match("refactor(parser): simplify AST walk")

    def test_test_type(self):
        assert SUBJECT_RE.match("test(sim): add coverage for edge case")

    def test_review_type(self):
        assert SUBJECT_RE.match("review(lint): address findings")

    def test_wip_type(self):
        assert SUBJECT_RE.match("wip: checkpoint")

    def test_docs_type(self):
        assert SUBJECT_RE.match("docs: update readme")

    def test_chore_type(self):
        """Housekeeping commits are first-class (F-15) — no more masquerading
        as fix/docs for gitignore tweaks and dep bumps."""
        assert SUBJECT_RE.match("chore(ci): update pipeline")

    def test_hyphenated_scope(self):
        assert SUBJECT_RE.match("feat(my-ticket-123): implement feature")

    def test_underscore_scope(self):
        assert SUBJECT_RE.match("fix(my_module): fix bug")

    def test_numeric_scope(self):
        assert SUBJECT_RE.match("feat(42): version bump")

    def test_rejects_unknown_type(self):
        assert SUBJECT_RE.match("perf(ci): speed up pipeline") is None

    def test_rejects_missing_colon_space(self):
        assert SUBJECT_RE.match("feat(core):missing space") is None

    def test_rejects_empty_summary(self):
        # "feat(core): " has an empty capture group for summary — regex
        # requires .+ so this should fail
        assert SUBJECT_RE.match("feat(core): ") is None

    def test_rejects_plain_text(self):
        assert SUBJECT_RE.match("just a random message") is None

    def test_rejects_uppercase_type(self):
        assert SUBJECT_RE.match("Fix(core): repair") is None


# ---------------------------------------------------------------------------
# Cross-module type-list consistency
# ---------------------------------------------------------------------------


class TestAllowedTypesConsistency:
    """ALLOWED_TYPES is the single source of truth for commit types. The
    auto-formatter once carried a hardcoded copy that drifted (missed `chore`),
    silently rewriting valid subjects — pin every consumer to the tuple."""

    def test_formatter_accepts_every_allowed_type(self):
        from booley.dev_support.commit_message_format import _CONVENTIONAL_RE

        for t in ALLOWED_TYPES:
            msg = f"{t}(scope): do the thing"
            assert _CONVENTIONAL_RE.match(msg), f"formatter rejects allowed type '{t}'"

    def test_formatter_preserves_valid_chore_message(self):
        """A valid `chore` subject must pass through the auto-formatter
        unchanged, not get rewritten as feat/fix."""
        from booley.dev_support.commit_message_format import _auto_format_commit_message

        msg = "chore(ci): update pipeline"
        assert _auto_format_commit_message(msg) == msg

    def test_specialist_guidance_lists_every_allowed_type(self):
        from booley.mcp_tools.specialist import Specialist

        for t in ALLOWED_TYPES:
            assert t in Specialist.COMMIT_MSG_GUIDANCE, (
                f"agent prompt guidance omits allowed type '{t}'"
            )


# ---------------------------------------------------------------------------
# validate_message
# ---------------------------------------------------------------------------


class TestValidateMessage:
    """Format/length assertions here run with the convention **enabled**. The
    opt-in default (off — a non-conforming subject is accepted) lives in
    TestConventionOptIn below."""

    @pytest.fixture(autouse=True)
    def _enable_convention(self):
        with _convention(True):
            yield

    def test_valid_message(self):
        assert validate_message("fix(core): handle edge case\n") == []

    def test_valid_no_scope(self):
        assert validate_message("feat: add new feature\n") == []

    def test_bad_format(self):
        errors = validate_message("bad message\n")
        assert len(errors) >= 1
        assert any("doesn't match" in e for e in errors)

    def test_bad_format_lists_allowed_types(self):
        """The format error must enumerate every allowed commit type so the
        user immediately sees what's valid (e.g. after typing 'perf')."""
        errors = validate_message("perf(ci): speed up pipeline\n")
        format_errors = [e for e in errors if "doesn't match" in e]
        assert format_errors, "expected a format error for unknown type 'perf'"
        err = format_errors[0]
        for t in ALLOWED_TYPES:
            assert t in err, f"allowed type '{t}' missing from error: {err!r}"

    def test_summary_too_long(self):
        long_summary = "x" * (MAX_SUMMARY_LEN + 1)
        msg = f"fix(a): {long_summary}\n"
        errors = validate_message(msg)
        assert any("chars" in e for e in errors)

    def test_summary_exactly_at_limit(self):
        summary = "x" * MAX_SUMMARY_LEN
        msg = f"fix(a): {summary}\n"
        errors = validate_message(msg)
        # Should pass — exactly at limit, not over
        assert not any("chars" in e for e in errors)

    def test_body_is_allowed(self):
        """F-11: a body is legitimate. The sanitizer redacts it, so the old
        "single line only" rule bought no safety and cost authored rationale."""
        msg = "fix(a): do thing\n\nBody text here.\nWith a second line."
        assert validate_message(msg) == []

    def test_banned_phrase_in_body_still_rejected(self):
        """Allowing a body must not open a leak path around the banned list."""
        msg = "fix(a): do thing\n\nPaired with claude on this one."
        errors = validate_message(msg)
        assert any("claude" in e for e in errors)

    def test_merge_commit_exempt_from_format(self):
        """Merge commits bypass format and body checks."""
        msg = "Merge branch 'feature'\n\nMerge body."
        errors = validate_message(msg)
        # No format or body errors for merge commits
        format_errors = [e for e in errors if "doesn't match" in e or "single line" in e]
        assert format_errors == []

    def test_merge_scope_syntax_exempt(self):
        msg = "merge(main): reconcile\n\nBody."
        errors = validate_message(msg)
        format_errors = [e for e in errors if "doesn't match" in e or "single line" in e]
        assert format_errors == []

    def test_banned_word_in_message(self):
        msg = "fix(core): claude-assisted repair\n"
        errors = validate_message(msg)
        assert any("Banned phrase" in e for e in errors)

    def test_banned_word_in_merge_commit(self):
        """Banned words are checked even in merge commits."""
        msg = "Merge branch 'feat'\n\nGenerated by claude.\n"
        errors = validate_message(msg)
        assert any("Banned phrase" in e for e in errors)

    def test_empty_message(self):
        errors = validate_message("")
        # Empty subject fails format check
        assert len(errors) >= 1


class TestConventionOptIn:
    """[stealth] enforce_convention — the type(scope): summary check is off by
    default (SETUP-10) and only rejects non-conforming subjects when a project
    opts in. Sanitization/banned-word checks are independent and always run."""

    def test_bad_subject_accepted_by_default(self):
        """Default (knob absent) → a non-conforming subject raises no format
        error. This is the opt-in behavior: upstream/human commits land as-is."""
        with _convention(False):
            assert validate_message("just a random message\n") == []

    def test_bad_subject_rejected_when_enabled(self):
        with _convention(True):
            errors = validate_message("just a random message\n")
            assert any("doesn't match" in e for e in errors)

    def test_summary_length_unchecked_by_default(self):
        """Summary length is part of the convention — skipped when off."""
        long_summary = "x" * (MAX_SUMMARY_LEN + 1)
        with _convention(False):
            assert validate_message(f"fix(a): {long_summary}\n") == []

    def test_empty_subject_accepted_by_default(self):
        with _convention(False):
            assert validate_message("") == []

    def test_banned_word_still_rejected_when_convention_off(self):
        """Turning the convention off must not open a leak path: the banned-word
        scrub is independent and always runs."""
        with _convention(False):
            errors = validate_message("literally anything with claude in it\n")
            assert any("Banned phrase" in e for e in errors)

    def test_body_cap_still_enforced_when_convention_off(self):
        """max_body_lines is its own knob, unaffected by the convention toggle."""
        with _convention(False), _cap(0):
            errors = validate_message("whatever subject\n\nA line of body.")
            assert any("max_body_lines" in e for e in errors)


def _cap(value: int | None):
    """Patch the [stealth] max_body_lines knob the validator reads."""
    return patch("booley.dev_support.validate_commit_msg.max_body_lines", return_value=value)


class TestBodyLineCap:
    """[stealth] max_body_lines — opt-in, off by default (F-11 stays the default)."""

    def test_unlimited_by_default(self):
        with _cap(None):
            msg = "fix(a): do thing\n\n" + "\n".join(f"line {i}" for i in range(50))
            assert validate_message(msg) == []

    def test_zero_rejects_any_body(self):
        with _cap(0):
            errors = validate_message("fix(a): do thing\n\nOne line of rationale.")
            assert any("max_body_lines" in e for e in errors)

    def test_zero_accepts_bare_subject(self):
        with _cap(0):
            assert validate_message("fix(a): do thing\n") == []

    def test_zero_accepts_subject_with_trailing_blank_lines(self):
        """Git's own editor leaves trailing newlines — those are not a body."""
        with _cap(0):
            assert validate_message("fix(a): do thing\n\n\n") == []

    def test_comment_lines_do_not_count(self):
        """`#` lines never reach history (git strips them), so they can't
        push a message over the cap."""
        with _cap(0):
            msg = "fix(a): do thing\n\n# Please enter the commit message\n#\n"
            assert validate_message(msg) == []

    def test_blank_lines_do_not_count(self):
        with _cap(2):
            msg = "fix(a): do thing\n\nfirst\n\n\nsecond\n"
            assert validate_message(msg) == []

    def test_exactly_at_cap_passes(self):
        with _cap(3):
            msg = "fix(a): do thing\n\none\ntwo\nthree\n"
            assert validate_message(msg) == []

    def test_one_over_cap_fails(self):
        with _cap(3):
            msg = "fix(a): do thing\n\none\ntwo\nthree\nfour\n"
            errors = validate_message(msg)
            assert any("Body is 4 line(s)" in e for e in errors)

    def test_merge_commits_are_capped_too(self):
        """The long body that motivated the knob rode in on a merge commit —
        exempting merges (as the format check does) would miss it entirely."""
        with _cap(0):
            errors = validate_message("Merge branch 'feature'\n\nA long merge narrative.")
            assert any("max_body_lines" in e for e in errors)

    def test_generated_merge_message_still_passes(self):
        """`git merge` on a conflict writes only `#` comment lines — no body."""
        with _cap(0):
            msg = "Merge branch 'feature'\n\n# Conflicts:\n#\tsrc/thing.sv\n"
            assert validate_message(msg) == []

    def test_crlf_body_is_counted(self):
        """A Windows-authored message must hit the same cap as a Unix one."""
        with _cap(0):
            errors = validate_message("fix(a): do thing\r\n\r\nBody line.\r\n")
            assert any("max_body_lines" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_diff
# ---------------------------------------------------------------------------


def _git_ok(stdout: str):
    """Patch run_command to return a successful CommandRun carrying *stdout*."""
    from booley.core.run_command import CommandRun

    return patch(
        "booley.dev_support.validate_commit_msg.run_command",
        return_value=CommandRun(argv=["git", "diff"], returncode=0, stdout=stdout),
    )


class TestValidateDiff:
    def test_clean_diff(self):
        diff_output = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "+# Normal code addition\n"
            "+x = 42\n"
        )
        with _git_ok(diff_output):
            errors = validate_diff()
        assert errors == []

    def test_banned_in_diff(self):
        diff_output = "diff --git a/foo.py b/foo.py\n+# Generated by Claude\n"
        with _git_ok(diff_output):
            errors = validate_diff()
        assert len(errors) > 0
        assert any("claude" in e.lower() for e in errors)

    def test_empty_diff(self):
        with _git_ok(""):
            errors = validate_diff()
        assert errors == []

    def test_git_failure(self):
        from booley.core.run_command import CommandRun

        failed = CommandRun(
            argv=["git", "diff"], returncode=128, stderr="fatal: not a git repository"
        )
        with patch("booley.dev_support.validate_commit_msg.run_command", return_value=failed):
            errors = validate_diff()
        assert len(errors) == 1
        assert "Failed" in errors[0]
        # Regression: git's own stderr must be surfaced, not swallowed.
        assert "not a git repository" in errors[0]

    def test_diff_header_lines_ignored(self):
        """Lines starting with +++ should not be scanned for banned words."""
        diff_output = "+++ b/claude.py\n"  # header — should be ignored
        with _git_ok(diff_output):
            errors = validate_diff()
        assert errors == []

    def test_deduplicates_errors(self):
        """Same banned phrase on multiple lines should produce one error."""
        diff_output = "+# claude wrote this\n+# claude also wrote that\n"
        with _git_ok(diff_output):
            errors = validate_diff()
        # Deduplicated — but phrase + truncated line makes each unique if
        # lines differ. The code deduplicates by full error string.
        # Two different display lines → two different errors.
        assert len(errors) >= 1

    def test_long_lines_truncated_in_error(self):
        long_line = "+" + "x" * 40 + " claude " + "y" * 60
        with _git_ok(long_line):
            errors = validate_diff()
        assert len(errors) >= 1
        # Error message should contain "..." for truncation
        assert any("..." in e for e in errors)


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def test_valid_message_exits_zero(self):
        with (
            patch("sys.argv", ["validate_commit_msg.py", "fix(x): good msg"]),
            patch(
                "booley.dev_support.validate_commit_msg._configured_project_repo",
                return_value=True,
            ),
            patch("booley.dev_support.validate_commit_msg.validate_diff", return_value=[]),
        ):
            from booley.dev_support.validate_commit_msg import main

            assert main() == 0

    def test_invalid_message_exits_one(self):
        with (
            patch("sys.argv", ["validate_commit_msg.py", "bad message"]),
            patch(
                "booley.dev_support.validate_commit_msg._configured_project_repo",
                return_value=True,
            ),
            patch("booley.dev_support.validate_commit_msg.validate_diff", return_value=[]),
            _convention(True),  # convention is opt-in; enable it to reject the bad subject
        ):
            from booley.dev_support.validate_commit_msg import main

            assert main() == 1

    def test_no_diff_flag_skips_diff(self):
        with (
            patch("sys.argv", ["validate_commit_msg.py", "--no-diff", "fix(x): good msg"]),
            patch(
                "booley.dev_support.validate_commit_msg._configured_project_repo",
                return_value=True,
            ),
        ):
            from booley.dev_support.validate_commit_msg import main

            # Should not call validate_diff at all
            with patch("booley.dev_support.validate_commit_msg.validate_diff") as mock_diff:
                rc = main()
                mock_diff.assert_not_called()
            assert rc == 0

    def test_non_project_repository_skips_all_checks(self):
        with (
            patch("sys.argv", ["validate_commit_msg.py", "bad message"]),
            patch(
                "booley.dev_support.validate_commit_msg._configured_project_repo",
                return_value=False,
            ),
            patch("booley.dev_support.validate_commit_msg.validate_message") as message_check,
            patch("booley.dev_support.validate_commit_msg.validate_diff") as diff_check,
        ):
            from booley.dev_support.validate_commit_msg import main

            assert main() == 0
            message_check.assert_not_called()
            diff_check.assert_not_called()

    def test_module_cli_honors_current_checkout_stealth_setting(self, tmp_path, monkeypatch):
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = false\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        with patch("sys.argv", ["validate_commit_msg.py", "mention Booley intentionally"]):
            from booley.dev_support.validate_commit_msg import main

            assert main() == 0


def test_project_config_detection(tmp_path):
    assert not _has_project_config(tmp_path)

    config = tmp_path / ".booley_project" / "booley.toml"
    config.parent.mkdir()
    config.write_text("[stealth]\nenabled = true\n", encoding="utf-8")

    assert _has_project_config(tmp_path)


def test_project_state_repository_config_detection(tmp_path):
    state_repo = tmp_path / ".booley_project"
    state_repo.mkdir()
    (state_repo / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")

    assert _has_project_config(state_repo)


# ---------------------------------------------------------------------------
# Vendored standalone import (SETUP-9): run as a script from a hooks dir that
# has no core/ package. The package-relative unit tests above never exercise
# the except-block import fallbacks, so drive them end-to-end as a subprocess.
# ---------------------------------------------------------------------------


class TestVendoredStandaloneImport:
    @staticmethod
    def _vendor(dst, *, include_runner: bool) -> None:
        import shutil

        from booley.paths import dev_support_dir

        src = dev_support_dir()
        core = src.parent / "core"
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("validate_commit_msg.py", "commit_msg_utils.py"):
            shutil.copy2(src / name, dst / name)
        if include_runner:
            shutil.copy2(core / "run_command.py", dst / "run_command.py")

    @staticmethod
    def _run(dst, msg: str):
        import subprocess
        import sys

        subprocess.run(["git", "init", "-q", str(dst)], capture_output=True, check=True)
        project_dir = dst / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(dst / "validate_commit_msg.py"), msg],
            cwd=str(dst),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_flat_run_tool_resolves(self, tmp_path):
        """init vendors run_command.py flat → imported by bare name, no crash."""
        self._vendor(tmp_path, include_runner=True)
        proc = self._run(tmp_path, "feat(x): ok")
        assert proc.returncode == 0, proc.stderr

    def test_shim_fallback_when_runner_absent(self, tmp_path):
        """Stale vendored dir (no run_command.py) → subprocess shim, still no crash
        (the original SETUP-9 symptom was ModuleNotFoundError here)."""
        self._vendor(tmp_path, include_runner=False)
        proc = self._run(tmp_path, "feat(x): ok")
        assert proc.returncode == 0, proc.stderr

    def test_flat_run_tool_actually_scans_staged_diff(self, tmp_path):
        """Prove the resolved run_command runs the git-diff scan (not just that the
        import succeeded): a banned term staged in the diff must be rejected."""
        import subprocess
        import sys

        self._vendor(tmp_path, include_runner=True)
        subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True, check=True)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        # A Co-Authored-By trailer is a banned attribution term the diff scan
        # flags. Stage it so `git diff --cached` (run via run_command) sees it.
        (tmp_path / "note.txt").write_text("Co-Authored-By: somebody <x@y.z>\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "note.txt"], capture_output=True, check=True
        )
        proc = subprocess.run(
            [sys.executable, str(tmp_path / "validate_commit_msg.py"), "feat(x): ok"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "co-authored-by" in proc.stderr.lower()
