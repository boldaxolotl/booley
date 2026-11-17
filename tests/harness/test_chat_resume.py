"""Resume forms in `booley chat` arguments and where each resolves (ADR 0082, Resume)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

from booley.harness import chat_cmd
from booley.harness.chat_resume import (
    ResumeRefusedError,
    ResumeRequest,
    ResumeTarget,
    _rewritten,
    request,
    resolve,
)

_STAMP = 1_000  # whole NTFS ticks (100 ns), so the stamp reads back unchanged


@pytest.mark.parametrize(
    ("provider", "agent_args", "expected"),
    [
        ("claude", ["--resume", "x"], ResumeRequest("x")),
        ("claude", ["--resume=x"], ResumeRequest("x")),
        ("claude", ["-r", "x", "--verbose"], ResumeRequest("x")),
        ("claude", ["-c"], ResumeRequest(None, 0)),
        ("claude", ["--model", "m", "--continue"], ResumeRequest(None, 2)),
        ("claude", ["--resume"], None),
        ("claude", ["--resume", "--verbose"], None),
        ("claude", ["--resume="], None),
        ("claude", ["--", "-c"], None),
        ("claude", ["-p", "hello"], None),
        ("claude", [], None),
        ("codex", ["resume", "x"], ResumeRequest("x")),
        ("codex", ["resume", "--last"], ResumeRequest(None, 1)),
        ("codex", ["resume", "-m", "y", "--last"], ResumeRequest(None, 3)),
        ("codex", ["resume", "--last", "--all"], None),
        ("codex", ["resume", "do it", "--last"], ResumeRequest(None, 2)),
        ("codex", ["resume"], None),
        ("codex", ["--last"], None),
        ("codex", ["exec", "resume", "x"], ResumeRequest("x", 0, 1)),
        ("codex", ["e", "resume", "x"], ResumeRequest("x", 0, 1)),
        ("codex", ["-m", "o3", "resume", "x"], ResumeRequest("x", 0, 2)),
        ("codex", ["resume", "-m", "y", "x"], ResumeRequest("x")),
        (
            "codex",
            ["-c", "k=v", "exec", "--json", "resume", "--last"],
            ResumeRequest(None, 5, 4),
        ),
        ("codex", ["exec", "resume", "--last", "--all"], None),
        ("codex", ["exec", "do it"], None),
        ("codex", ["-m", "resume"], None),
        ("codex", ["exec", "resume", "--", "x", "p"], ResumeRequest("x", 0, 1)),
        ("codex", ["-C", "/w", "resume", "x"], None),
        ("codex", ["resume", "--cd=/w", "--last"], None),
        ("codex", ["resume", "-C/w", "x"], None),
        ("codex", ["resume", "--worktree", "x"], None),
        ("gemini", ["--resume", "x"], None),
    ],
)
def test_resume_forms_are_recognized_before_any_separator(provider, agent_args, expected):
    # Bug caught: a form misread as a resume reroutes a launch that never asked for one.
    assert request(provider, agent_args) == expected


def _record(common: Path, conversation: str, worktree: str, cwd: Path, key: str | None) -> None:
    root = common / "worktrees" / worktree
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").write_bytes(b"gitdir: elsewhere\n")
    os.utime(root / ".git", ns=(_STAMP, _STAMP))
    record = {
        "schema": 1,
        "client": "claude",
        "conversation_id": conversation,
        "cwd": str(cwd),
        "worktree": worktree,
        "handle": "S1",
        "process_key": key,
        "at": "2026-11-11T10:00:00Z",
        "git_stamp": _STAMP,
    }
    digest = hashlib.sha256(f"claude:{conversation}".encode()).hexdigest()
    store = common / "worktrees" / ".conversations"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{digest}.json").write_bytes(json.dumps(record).encode())


def test_a_recorded_directory_outside_the_worktree_falls_back_to_its_root(tmp_path):
    # Bug caught: a record whose cwd is gone or points elsewhere starting the agent outside
    # the worktree it names.
    common = tmp_path / ".booley"
    _record(common, "outside", "s1", tmp_path, "pid:fixture:990001:7")
    _record(common, "missing", "s2", common / "worktrees" / "s2" / "gone", None)
    _record(common, "inside", "s3", common / "worktrees" / "s3", None)
    (common / "worktrees" / "s3" / "sub").mkdir()
    _record(common, "nested", "s4", common / "worktrees" / "s4" / "sub", None)
    (common / "worktrees" / "s4" / "sub").mkdir()

    def target(conversation: str) -> ResumeTarget:
        return resolve(
            common, "claude", ResumeRequest(conversation), tmp_path, ["--resume"], False
        )

    assert target("outside").cwd == common / "worktrees" / "s1"
    assert target("outside").predecessors == ("pid:fixture:990001:7",)
    assert target("missing").cwd == common / "worktrees" / "s2"
    assert target("missing").predecessors == ()
    assert target("nested").cwd == common / "worktrees" / "s4" / "sub"
    assert target("nested").worktree == common / "worktrees" / "s4"


@pytest.mark.skipif(sys.platform != "linux", reason="process keys are observed through procfs")
def test_a_running_session_in_the_worktree_refuses_the_resume(tmp_path):
    # Bug caught: the refusal missing, or naming the process key instead of the handle.
    from booley.runtime.pid import capture_process_identity
    from booley.runtime.session_activity import process_session_key

    identity = capture_process_identity(os.getpid())
    assert identity is not None
    common = tmp_path / ".booley"
    _record(common, "busy", "s1", common / "worktrees" / "s1", process_session_key(identity))

    with pytest.raises(
        ResumeRefusedError, match=r"worktree s1, where session S1 is still running"
    ):
        resolve(common, "claude", ResumeRequest("busy"), tmp_path, ["--resume", "busy"], False)


def test_the_banner_without_a_handle_still_names_the_conversation(tmp_path, monkeypatch, capsys):
    # Bug caught: "session None" in the banner when the handle could not be reserved.
    worktree = tmp_path / "s1"
    worktree.mkdir()
    monkeypatch.chdir(tmp_path)
    target = ResumeTarget(worktree, worktree, (), (), "conv-1")

    chat_cmd._enter_resumed_worktree(target, worktree, None)

    assert (
        capsys.readouterr().err == f"Booley resumes conversation conv-1 in worktree {worktree}.\n"
    )
    assert Path.cwd() == worktree.resolve()


@pytest.mark.parametrize(
    ("provider", "agent_args", "expected"),
    [
        ("claude", ["-c"], ["--resume", "c1"]),
        (
            "claude",
            ["--model", "m", "--continue", "-p", "hi"],
            ["--model", "m", "--resume", "c1", "-p", "hi"],
        ),
        ("codex", ["resume", "--last"], ["resume", "c1"]),
        ("codex", ["resume", "do it", "--last", "-m", "x"], ["resume", "c1", "do it", "-m", "x"]),
        ("codex", ["exec", "resume", "--last", "p"], ["exec", "resume", "c1", "p"]),
        ("codex", ["-m", "o3", "resume", "--last"], ["-m", "o3", "resume", "c1"]),
    ],
)
def test_the_latest_form_becomes_the_id_form_the_cli_reads(provider, agent_args, expected):
    # Bug caught: the id landing where Codex reads the prompt, or beside a stray `--continue`.
    found = request(provider, agent_args)
    assert found is not None

    assert _rewritten(provider, found, "c1", agent_args) == expected


def test_a_recorded_directory_under_a_former_mount_resumes_under_the_current_one(
    tmp_path, monkeypatch
):
    # Bug caught: a conversation recorded under a former Sandbox mount starting at the worktree
    # root, or its transcript lookup following the translated directory instead of the one the
    # CLI filed the conversation under.
    from booley.runtime.mount_history import MountHistoryError
    from tests.mount_change_support import simulate_mount_change

    common = tmp_path / "mount"
    former = Path(simulate_mount_change(monkeypatch, common, tmp_path / "former-mount"))
    old_cwd = former / "worktrees" / "s1" / "sub"
    _record(common, "c1", "s1", old_cwd, None)
    (common / "worktrees" / "s1" / "sub").mkdir()
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    project = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(old_cwd))
    project.mkdir(parents=True)
    (project / "c1.jsonl").write_bytes(b"{}\n")

    target = resolve(common, "claude", ResumeRequest(None, 0), tmp_path, ["-c"], True)

    assert target.conversation_id == "c1"
    assert target.agent_args == ("--resume", "c1")
    assert target.cwd == common / "worktrees" / "s1" / "sub"

    history = common / ".runtime" / "sandbox-mounts.json"
    history.write_bytes(b"{")
    with pytest.raises(MountHistoryError, match=r"saved conversation c1.*sandbox-mounts\.json"):
        resolve(common, "claude", ResumeRequest("c1"), tmp_path, ["--resume", "c1"], False)
