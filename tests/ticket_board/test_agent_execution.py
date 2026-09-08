"""Execution composition retains existing layout and notification policy in both modes."""

from pathlib import Path

import pytest

from booley.core.models import AgentCallParams
from booley.ticket_board.agent_execution import configure_agent_call, resolve_agent_artifacts
from booley.ticket_board.notifications import notify_rate_limit
from booley.ticket_board.paths import session_jobs_dir


@pytest.mark.parametrize("mode", ["ticket", "interactive"])
@pytest.mark.parametrize("explicit_runtime", [False, True])
def test_session_paths_follow_environment_precedence(
    tmp_path, monkeypatch, mode, explicit_runtime
):
    logs = tmp_path / ("tickets/logs/slug" if mode == "ticket" else ".interactive_logs/session")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    runtime = logs / ".runtime"
    if explicit_runtime:
        runtime = tmp_path / "runtime-override"
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
    assert session_jobs_dir() == runtime / "jobs"
    paths = resolve_agent_artifacts(None, label="reviewer/quality")
    assert paths.prompt_json.parent == runtime / "prompts"
    assert paths.prompt_json.name.startswith("reviewer_quality-")
    assert paths.transcript_markdown is None
    if not explicit_runtime:
        assert paths.prompt_markdown.parent == logs / "human-logs" / "prompts"
    else:
        assert paths.prompt_markdown.parent == runtime / "prompts"


def test_runtime_override_without_logs_does_not_enable_persistence(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    assert session_jobs_dir() is None
    paths = resolve_agent_artifacts(None)
    assert paths.prompt_json is None
    assert paths.prompt_markdown is None


def test_composition_does_not_mutate_params_and_resolves_each_attempt(tmp_path):
    original = AgentCallParams(prompt="work", model="test", cwd=tmp_path, label="reviewer")
    configured = configure_agent_call(original)
    assert original.artifact_paths is None and original.notify_rate_limit is None
    assert configured.notify_rate_limit is notify_rate_limit
    source = tmp_path / ".runtime" / "transcripts" / "reviewer-retry2.jsonl"
    paths = configured.artifact_paths(source)
    assert paths.prompt_json == source.with_suffix(".prompt.json")
    assert paths.prompt_markdown == tmp_path / "human-logs/transcripts/reviewer-retry2.prompt.md"
    assert paths.transcript_markdown == tmp_path / "human-logs/transcripts/reviewer-retry2.md"


@pytest.mark.parametrize("name", ["coder.jsonl", "coder", "developer.run_003-retry2.jsonl"])
def test_prompt_suffix_compatibility(tmp_path: Path, name: str):
    source = tmp_path / ".runtime" / name
    base = source.with_suffix("") if source.suffix else source
    paths = resolve_agent_artifacts(source)
    assert paths.prompt_json == base.with_suffix(".prompt.json")
    assert paths.prompt_markdown.name == base.with_suffix(".prompt.md").name
