"""Tests for commit_msg_hook: rejection of banned terms and attribution."""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.subprocess_budget import run_bounded_child

from booley.core.host_shell import quote_arg
from booley.dev_support import commit_msg_hook
from booley.dev_support.commit_msg_hook import main, message_lines

RETRY_HINT = "-v -e -F"
EMPTY_WORDS = "[stealth]\nenabled = true\nbanned_words = []\n"
BANNED_ONLY = '[stealth]\nenabled = true\nbanned_words = ["generated", "assistant-identity"]\n'
CAP_ZERO = "[stealth]\nenabled = true\nmax_body_lines = 0\n"


def test_bundled_hook_falls_back_to_the_host_shell_beside_it(monkeypatch: pytest.MonkeyPatch):
    """Outside the Booley package the hook imports the copy bundled next to it."""
    monkeypatch.setitem(sys.modules, "booley.core.host_shell", None)
    monkeypatch.setitem(sys.modules, "host_shell", type(sys)("host_shell"))
    sys.modules["host_shell"].quote_arg = quote_arg  # type: ignore[attr-defined]
    spec = importlib.util.spec_from_file_location("bundled_hook", commit_msg_hook.__file__)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


@pytest.fixture(autouse=True)
def project(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Unit-test the vendored Project hook, not this source checkout's role.

    The hook acts only in Stealth Mode, so the default Project enables it explicitly.
    It runs in a real Git repository of its own, and Git ignores the developer's own
    configuration, so a global ``core.commentChar`` cannot change what the hook skips.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    project_root = tmp_path_factory.mktemp("stealth-project")
    assert run_bounded_child(["git", "init", "-q", str(project_root)]).returncode == 0
    (project_root / ".booley").mkdir()
    _write_config(project_root, "[stealth]\nenabled = true\n")
    monkeypatch.chdir(project_root)
    return project_root


def _write_config(project_root: Path, text: str) -> None:
    (project_root / ".booley" / "booley.toml").write_text(text, encoding="utf-8")


def _git_config(project_root: Path, key: str, value: str) -> None:
    result = run_bounded_child(["git", "-C", str(project_root), "config", key, value])
    assert result.returncode == 0, result.stderr


def _run_hook(
    tmp_path: Path, raw: bytes | str, name: str = "COMMIT_EDITMSG"
) -> tuple[int, str, bytes]:
    """Run ``main()`` on a message file; return (rc, stderr text, file bytes after)."""
    msg_file = tmp_path / name
    msg_file.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    err = io.StringIO()
    with (
        patch("booley.dev_support.commit_msg_hook.sys.argv", ["commit-msg", str(msg_file)]),
        redirect_stderr(err),
    ):
        rc = main()
    return rc, err.getvalue(), msg_file.read_bytes()


# ---------------------------------------------------------------------------
# Banned terms: the message file is never rewritten
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "original", "expected"),
    [
        pytest.param(
            "[stealth]\nenabled = true\n",
            "fix(sim): rebuild the llm base image\n",
            "'llm' in the subject",
            id="word-in-subject",
        ),
        pytest.param(
            "[stealth]\nenabled = true\n",
            "fix(core): repair widget\n\nSplit the helper.\nRenamed BooleyRunner to Runner.\n",
            "'booley' on line 4",
            id="vendor-name-inside-identifier",
        ),
        pytest.param(
            '[stealth]\nenabled = true\nbanned_substrings = ["quokka"]\n',
            "fix(core): repair myquokkafile\n",
            "'quokka' in the subject",
            id="banned-substring",
        ),
    ],
)
def test_banned_term_rejects_and_leaves_the_file_unchanged(
    tmp_path: Path, project: Path, config: str, original: str, expected: str
) -> None:
    _write_config(project, config)
    rc, err, after = _run_hook(tmp_path, original)
    assert rc == 1
    assert after == original.encode()
    assert expected in err


def test_rejection_lists_every_match_once_and_both_ways_out(tmp_path: Path) -> None:
    original = "fix(llm): tune the claude build llm LLM\n\nRan booley twice.\nbooley again.\n"
    rc, err, _ = _run_hook(tmp_path, original)
    assert rc == 1
    assert err.count("'llm'") == 1  # one entry per term per line
    assert "'claude' in the subject" in err
    assert "'booley' on line 3" in err
    assert "'booley' on line 4" in err
    assert "Reword the message" in err
    assert "banned_words" in err and "banned_substrings" in err
    assert RETRY_HINT in err


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
def test_ignored_lines_do_not_reject(tmp_path: Path, raw: str) -> None:
    rc, err, after = _run_hook(tmp_path, raw)
    assert rc == 0, err
    assert after == raw.encode()


@pytest.mark.parametrize(
    ("key", "value", "raw", "rejected_line"),
    [
        pytest.param("core.commentChar", ";", "; booley here\n", None, id="configured-char"),
        pytest.param("core.commentChar", ";", "# booley here\n", 2, id="hash-is-text-then"),
        pytest.param("core.commentChar", "auto", "# booley here\n", None, id="auto-hash"),
        pytest.param(
            "core.commentChar",
            "auto",
            "\n; On branch booley/x\n; ------------------------ >8 ------------------------\n"
            "+import booley\n",
            None,
            id="auto-skips-any-char-git-may-pick",
        ),
        # A ``core.commentString`` of ``"# "`` leaves ``#booley`` as message text.
        pytest.param(
            "core.commentString", "# ", "# comment\n#booley\n", 3, id="comment-string-space"
        ),
    ],
)
def test_configured_comment_prefix_is_honored(
    tmp_path: Path, project: Path, key: str, value: str, raw: str, rejected_line: int | None
) -> None:
    _git_config(project, key, value)
    rc, err, _ = _run_hook(tmp_path, "fix(core): repair widget\n" + raw)
    if rejected_line is None:
        assert rc == 0, err
    else:
        assert rc == 1
        assert f"'booley' on line {rejected_line}" in err


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\n# c\nb\n", [(1, "a"), (3, "b"), (4, "")]),
        # Git cuts only at the exact scissors line, so text after it on the line is kept.
        ("a\n# ------------------------ >8 ------------------------ booley\nb\n", None),
    ],
)
def test_message_lines_keeps_file_line_numbers(
    raw: str, expected: list[tuple[int, str]] | None
) -> None:
    kept = message_lines(raw, ("#",))
    assert kept == expected if expected is not None else [t for _, t in kept] == ["a", "b", ""]


def test_merge_commit_message_file_gets_merge_hint(tmp_path: Path) -> None:
    folder = tmp_path / "it's $HOME"
    folder.mkdir()
    rc, err, _ = _run_hook(folder, "Merge branch 'llm-fix'\n", name="MERGE_MSG")
    assert rc == 1
    assert "git commit -m" in err and "git merge" in err
    assert f"git commit -v -e -F {quote_arg(str((folder / 'MERGE_MSG').resolve()))}" in err
    _, plain_err, _ = _run_hook(tmp_path, "fix(core): llm\n")
    assert "merge commit" not in plain_err


# ---------------------------------------------------------------------------
# Attribution footers are rejected without echoing the identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "footer"),
    [
        ("[stealth]\nenabled = true\n", "  co-authored-by  : Person <person@example.test>"),
        ("[stealth]\nenabled = true\n", "  \U0001f916 Generated with private-identity"),
        (BANNED_ONLY, "Generated with assistant-identity"),
        (BANNED_ONLY, "  GENERATED   WITH   assistant-identity  "),
        (
            BANNED_ONLY,
            "Generated with [assistant-identity](https://example.test/assistant-identity)",
        ),
    ],
)
def test_attribution_footer_rejected_without_echoing_the_identity(
    tmp_path: Path, project: Path, config: str, footer: str
) -> None:
    """Structural footers always reject; plain ``Generated with`` uses the Project vocabulary."""
    _write_config(project, config)
    original = f"fix(core): repair edge case\n\nUseful rationale.\n\n{footer}\n"
    rc, err, after = _run_hook(tmp_path, original)
    assert rc == 1
    assert after == original.encode()
    assert "identity" not in err and footer.strip() not in err
    assert "remove" in err.lower() and "retry" in err.lower()
    assert RETRY_HINT in err


def test_generated_prose_with_banned_term_is_a_term_rejection(
    tmp_path: Path, project: Path
) -> None:
    _write_config(project, '[stealth]\nenabled = true\nbanned_words = ["generated", "docker"]\n')
    rc, err, _ = _run_hook(
        tmp_path, "fix(core): repair widget\n\nGenerated with Docker for reproducibility.\n"
    )
    assert rc == 1
    assert "'generated' on line 3" in err
    assert "attribution footer" not in err


@pytest.mark.parametrize(
    ("config", "raw", "rc"),
    [
        # With an empty vocabulary the plain shape is prose; the structural shape still rejects.
        (EMPTY_WORDS, "Generated with assistant-identity", 0),
        (EMPTY_WORDS, "Co-Authored-By: Person <person@example.test>", 1),
        # Git comments are not part of the message; merge commits are checked like any other.
        (BANNED_ONLY, "# Generated with assistant-identity", 0),
        ("[stealth]\nenabled = true\n", "Clean line\nCo-Authored-By: bot\nAnother clean line", 1),
    ],
)
def test_attribution_verdicts(
    tmp_path: Path, project: Path, config: str, raw: str, rc: int
) -> None:
    _write_config(project, config)
    original = f"Merge branch 'dev'\n\n{raw}\n"
    code, err, after = _run_hook(tmp_path, original)
    assert (code, after) == (rc, original.encode()), err


def test_attribution_payload_equality_is_not_broadened_by_substrings(
    tmp_path: Path, project: Path
) -> None:
    _write_config(
        project, '[stealth]\nenabled = true\nbanned_words = []\nbanned_substrings = ["quokka"]\n'
    )
    rc, err, _ = _run_hook(tmp_path, "fix: repair\n\nGenerated with QuokkaRunner\n")
    assert rc == 1
    assert "attribution footer" not in err
    assert "'quokka' on line 3" in err
    rc, err, _ = _run_hook(tmp_path, "fix: repair\n\nGenerated with quokka\n")
    assert rc == 1
    assert "attribution footer" in err


# ---------------------------------------------------------------------------
# BOOLEY_SKIP_COMMIT_VALIDATION skips convention checks only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("fix(core): repair the llm path\n", id="banned"),
        pytest.param("fix(core): repair edge case\n\nCo-Authored-By: bot\n", id="attribution"),
    ],
)
def test_skip_still_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")
    rc, err, after = _run_hook(tmp_path, raw)
    assert rc == 1
    assert after == raw.encode()
    assert RETRY_HINT in err


def test_skip_skips_body_cap(tmp_path: Path, project: Path, monkeypatch) -> None:
    _write_config(project, CAP_ZERO)
    raw = "Upstream style summary\n\nA body line.\n"
    rc, err, _ = _run_hook(tmp_path, raw)
    assert rc == 1
    assert "Commit message validation FAILED" in err
    assert "max_body_lines = 0" in err
    assert "banned terms and attribution footers are still rejected" in err
    monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")
    rc, err, after = _run_hook(tmp_path, raw)
    assert rc == 0, err
    assert after == raw.encode()


@pytest.mark.parametrize("skip", [False, True])
def test_body_cap_errors_are_listed_with_banned_terms_unless_skipped(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, skip: bool
) -> None:
    _write_config(project, CAP_ZERO)
    if skip:
        monkeypatch.setenv("BOOLEY_SKIP_COMMIT_VALIDATION", "1")
    _, err, _ = _run_hook(tmp_path, "Update llm\n\nA body line.\n")
    assert "'llm' in the subject" in err
    assert ("max_body_lines" in err) is not skip


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["commit-msg"], ["commit-msg", "no_such_file"]])
def test_missing_argument_or_file_fails(argv: list[str]) -> None:
    with patch("booley.dev_support.commit_msg_hook.sys.argv", argv):
        assert main() == 1


@pytest.mark.parametrize(
    "raw",
    [
        # Booley enforces no ``type(scope): summary`` convention or subject length.
        "Update ALU\n",
        "just a random message\n",
        "x" * 200 + "\n",
        "fix(core): repair widget\n\nGenerated with care by the whole team\n",
        b"fix(core): repair widget\r\n\r\n# a comment\r\nBody line.\r\n\r\n\r\n",
    ],
)
def test_accepted_message_is_never_written(tmp_path: Path, raw: str | bytes) -> None:
    rc, err, after = _run_hook(tmp_path, raw)
    assert rc == 0, err
    assert err == ""
    assert after == (raw if isinstance(raw, bytes) else raw.encode())


def test_stealth_disabled_leaves_message_unchanged(tmp_path: Path, project: Path) -> None:
    _write_config(project, BANNED_ONLY.replace("true", "false"))
    raw = "Generated with assistant-identity llm\n"
    assert _run_hook(tmp_path, raw) == (0, "", raw.encode())


def test_standalone_hook_leaves_source_checkout_message_unchanged(tmp_path: Path) -> None:
    """A standalone hook member cannot depend on installed ``booley``."""
    root = tmp_path / "source"
    hooks = root / "stale-hooks"
    hooks.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n",
        encoding="utf-8",
    )
    init = run_bounded_child(["git", "init", "-q", str(root)])
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
    )

    assert result.returncode == 0, result.stderr
    assert message.read_text(encoding="utf-8") == original
