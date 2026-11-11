"""Where a resumed `booley chat` conversation reopens: the worktree it ran in (ADR 0082, Resume).

Claude Code and Codex store a conversation per working directory, so a resume started from the
main checkout cannot find a conversation that ran in worktree `s3`. This module recognizes the
resume forms in the agent arguments, looks the conversation up in the records the lifecycle
hook keeps, and says where the launch must start and which earlier handle it keeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.harness import chat_checkout
from booley.runtime import agent_transcripts, conversation_records
from booley.runtime.agent_transcripts import RecencyUnknownError
from booley.runtime.conversation_records import ConversationRecord


@dataclass(frozen=True)
class ResumeRequest:
    """A resume form in the agent arguments.

    *conversation_id* is set for the id form; ``None`` is the latest form (`-c`, `--last`),
    whose deciding token sits at *index*.
    """

    conversation_id: str | None
    index: int = 0


@dataclass(frozen=True)
class ResumeTarget:
    """Where the resumed launch starts.

    *worktree* is the recorded worktree and *cwd* the directory inside it to start in; both
    are ``None`` for the main checkout. *predecessors* are the process keys whose handle the
    new process keeps.
    """

    worktree: Path | None
    cwd: Path | None
    predecessors: tuple[str, ...]
    agent_args: tuple[str, ...]
    conversation_id: str | None = None
    notice: str | None = None


class ResumeRefusedError(Exception):
    """The checkout the resume must reopen in is in use; a second agent must not start there."""


def _before_separator(agent_args: list[str]) -> list[str]:
    """The user's own options: everything before a `--`."""
    return agent_args[: agent_args.index("--")] if "--" in agent_args else agent_args


def _claude_request(args: list[str]) -> ResumeRequest | None:
    """`--resume <id>`, `--resume=<id>`, `-r <id>` or `-c`/`--continue`; a bare `--resume` is the picker."""
    for index, token in enumerate(args):
        if token in ("-c", "--continue"):
            return ResumeRequest(None, index)
        if token.startswith("--resume=") and token != "--resume=":
            return ResumeRequest(token.split("=", 1)[1])
        if token in ("-r", "--resume"):
            value = args[index + 1] if index + 1 < len(args) else "-"
            return None if value.startswith("-") else ResumeRequest(value)
    return None


def _codex_request(args: list[str]) -> ResumeRequest | None:
    """`resume <id>` or `resume --last`; `--all` lifts the directory filter, so it is left alone.

    With `--last` the first positional is the prompt, not an id.
    """
    if args[:1] != ["resume"]:
        return None
    rest = args[1:]
    if "--last" in rest:
        return None if "--all" in rest else ResumeRequest(None, rest.index("--last") + 1)
    if rest and not rest[0].startswith("-"):
        return ResumeRequest(rest[0])
    return None


def request(provider: str, agent_args: list[str]) -> ResumeRequest | None:
    """The resume form in *agent_args* for *provider*, else ``None`` (launch as before)."""
    args = _before_separator(agent_args)
    if provider == "claude":
        return _claude_request(args)
    if provider == "codex":
        return _codex_request(args)
    return None


def _main(
    agent_args: list[str], conversation_id: str | None, notice: str | None = None
) -> ResumeTarget:
    return ResumeTarget(None, None, (), tuple(agent_args), conversation_id, notice)


def _start_directory(worktree: Path, recorded: str) -> Path:
    """The recorded directory when it still lies inside *worktree*, else the worktree root."""
    candidate = Path(recorded)
    if candidate.is_dir() and candidate.resolve().is_relative_to(worktree.resolve()):
        return candidate
    return worktree


def _worktree_target(
    common: Path,
    provider: str,
    record: ConversationRecord,
    agent_args: list[str],
    occupied: dict[str, str],
) -> ResumeTarget:
    """Reopen *record*'s worktree under its old handle, or refuse when a session runs there."""
    if (session := occupied.get(record.worktree)) is not None:
        raise ResumeRefusedError(
            f"conversation {record.conversation_id} belongs to worktree {record.worktree}, "
            f"where session {session} is still running; end that session first."
        )
    names = [record.process_key] if record.process_key else []
    if provider == "codex":
        names.append("codex:" + record.conversation_id)
    worktree = common / "worktrees" / record.worktree
    cwd = _start_directory(worktree, record.cwd)
    return ResumeTarget(worktree, cwd, tuple(names), tuple(agent_args), record.conversation_id)


def _occupied(common: Path, live: list[ConversationRecord] | None = None) -> dict[str, str]:
    """Map each worktree with a running session to its handle.

    The records name the sessions `booley chat` started; the registry also sees an agent
    started by hand, whose record carries no process key.
    """
    records = conversation_records.live(common) if live is None else live
    return {
        **chat_checkout.worktree_sessions(common),
        **conversation_records.occupants(records),
    }


def _by_id(
    common: Path, provider: str, conversation_id: str, agent_args: list[str]
) -> ResumeTarget:
    """The id form: the conversation's own worktree, else the main checkout."""
    record = conversation_records.find(common, provider, conversation_id)
    if record is None:
        return _main(agent_args, conversation_id)
    if not conversation_records.is_live(common, record):
        return _main(
            agent_args,
            conversation_id,
            f"NOTE: conversation {conversation_id} ran in worktree {record.worktree}, "
            "which no longer exists; resuming in the main checkout.",
        )
    return _worktree_target(common, provider, record, agent_args, _occupied(common))


def _rewritten(
    provider: str, resume: ResumeRequest, conversation_id: str, agent_args: list[str]
) -> list[str]:
    """Replace the latest form with the id form, so the CLI resumes the aliased conversation.

    Codex takes the id right after `resume`; its `--last` is dropped.
    """
    if provider == "claude":
        return [
            *agent_args[: resume.index],
            "--resume",
            conversation_id,
            *agent_args[resume.index + 1 :],
        ]
    rest = [*agent_args[: resume.index], *agent_args[resume.index + 1 :]]
    return [rest[0], conversation_id, *rest[1:]]


def _latest(
    common: Path,
    provider: str,
    resume: ResumeRequest,
    launch_cwd: Path,
    agent_args: list[str],
    main_busy: bool,
) -> ResumeTarget:
    """The latest form: the most recent conversation among main and the free worktrees.

    A worktree conversation is chosen only when strictly newer than the main checkout's;
    when main's recency cannot be determined the CLI keeps its own pick, as before. A main
    checkout in use is skipped like an occupied worktree: its newest conversation is the
    running session's.
    """
    live = conversation_records.live(common)
    occupied = _occupied(common, live)
    candidates: list[tuple[float, str, ConversationRecord]] = []
    for record in live:
        if record.client != provider or record.worktree in occupied:
            continue
        stamp = agent_transcripts.last_active(provider, record.cwd, record.conversation_id)
        if stamp is not None:
            candidates.append((stamp, record.conversation_id, record))
    if not candidates:
        return _main(agent_args, None)
    stamp, _, winner = max(candidates, key=lambda candidate: candidate[:2])
    if main_busy:
        args = _rewritten(provider, resume, winner.conversation_id, agent_args)
        return _worktree_target(common, provider, winner, args, occupied)
    try:
        main_stamp = agent_transcripts.latest_in(provider, launch_cwd, stamp)
    except RecencyUnknownError:
        return _main(agent_args, None)
    if main_stamp is not None and main_stamp >= stamp:
        return _main(agent_args, None)
    args = _rewritten(provider, resume, winner.conversation_id, agent_args)
    return _worktree_target(common, provider, winner, args, occupied)


def resolve(
    common: Path,
    provider: str,
    resume: ResumeRequest,
    launch_cwd: Path,
    agent_args: list[str],
    main_busy: bool,
) -> ResumeTarget:
    """Decide where the resume starts; raises `ResumeRefusedError` for an occupied worktree.

    *main_busy* says the main checkout is in use, so the latest form must not pick it.
    """
    if resume.conversation_id is not None:
        return _by_id(common, provider, resume.conversation_id, agent_args)
    return _latest(common, provider, resume, launch_cwd, agent_args, main_busy)
