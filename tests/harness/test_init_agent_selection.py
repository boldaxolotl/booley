"""Agent selection and defaulting contract for ``booley init`` and ``--seed``."""

from __future__ import annotations

import argparse

from booley.harness import init_cmd
from booley.harness.init_common import InitContext


def _args(**overrides) -> argparse.Namespace:
    values = {"seed": False, "provider": None, "auth": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_unattended_first_init_records_compatible_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    init_cmd.reset_cache()
    ctx = InitContext(project_root=tmp_path, interactive=False)

    resolved = init_cmd._resolve_agent_selection(ctx, _args())

    assert resolved == (
        init_cmd.AgentSelection("claude", "auto", True, True),
        tmp_path / init_cmd.PROJECT_DIR_NAME / "booley.toml",
    )


def test_agent_config_uses_resolved_project_directory(tmp_path, monkeypatch):
    project_dir = tmp_path / "project-data"
    project_dir.mkdir()
    monkeypatch.setattr(init_cmd, "resolve_checkout_project_dir", lambda _root: project_dir)

    assert init_cmd._agent_config_path(tmp_path) == project_dir / "booley.toml"


def test_terminal_prompt_accepts_documented_defaults(tmp_path, monkeypatch):
    answers = iter(("", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    ctx = InitContext(project_root=tmp_path, interactive=True)

    resolved = init_cmd._resolve_agent_selection(ctx, _args())

    assert resolved is not None
    selection, _ = resolved
    assert selection == init_cmd.AgentSelection("claude", "auto", True, True)


def test_run_init_persists_selection_before_seed(tmp_path, monkeypatch):
    project_dir = tmp_path / init_cmd.PROJECT_DIR_NAME
    project_dir.mkdir()
    monkeypatch.setattr(init_cmd, "_step_eda_tool_detection", lambda _ctx: True)

    def seed(_ctx, selection):
        config = (project_dir / "booley.toml").read_text(encoding="utf-8")
        assert selection == init_cmd.AgentSelection("codex", "subscription", True, True)
        assert '[agent]\nprovider = "codex"\nauth = "subscription"' in config
        return 0

    monkeypatch.setattr(init_cmd, "_run_seed", seed)

    assert (
        init_cmd.run_init(
            _args(seed=True, provider="codex", auth="subscription"),
            tmp_path,
        )
        == 0
    )


def test_terminal_prompt_stops_after_three_invalid_answers(monkeypatch):
    answers = iter(("", "other", "still-other"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert init_cmd._prompt_agent_choice("provider", ("claude", "codex")) is None


def test_persist_selection_preserves_existing_config_text(tmp_path):
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    config = project_dir / "booley.toml"
    original = "# user comment\n\n[flows.sim]\nenabled = false\n"
    config.write_text(original, encoding="utf-8")
    selection = init_cmd.AgentSelection("claude", "api_key", True, True)

    assert init_cmd._step_agent_config(InitContext(project_root=tmp_path), selection, config)

    updated = config.read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert updated.endswith('[agent]\nprovider = "claude"\nauth = "api_key"\n')


def test_existing_selection_is_authoritative_and_not_rewritten(tmp_path, monkeypatch):
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    config = project_dir / "booley.toml"
    body = '[agent]\nprovider = "codex"\nauth = "subscription"\n'
    config.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    ctx = InitContext(project_root=tmp_path, interactive=True)

    resolved = init_cmd._resolve_agent_selection(ctx, _args())

    assert resolved == (init_cmd.AgentSelection("codex", "subscription"), config)
    assert init_cmd._step_agent_config(ctx, resolved[0], config)
    assert config.read_text(encoding="utf-8") == body


def test_existing_provider_only_receives_and_persists_default_auth(tmp_path):
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    config = project_dir / "booley.toml"
    config.write_text('[agent]\nprovider = "claude"\n', encoding="utf-8")
    ctx = InitContext(project_root=tmp_path, interactive=False)

    resolved = init_cmd._resolve_agent_selection(ctx, _args(seed=True))

    assert resolved == (init_cmd.AgentSelection("claude", "auto", False, True), config)
    assert init_cmd._step_agent_config(ctx, resolved[0], config)
    assert config.read_text(encoding="utf-8") == ('[agent]\nprovider = "claude"\nauth = "auto"\n')


def test_check_only_reports_pending_selection_before_project_dir_exists(tmp_path):
    selection = init_cmd.AgentSelection("codex", "subscription", True, True)
    config = tmp_path / ".booley_project" / "booley.toml"
    ctx = InitContext(project_root=tmp_path, check_only=True, interactive=False)

    assert init_cmd._step_agent_config(ctx, selection, config)

    assert ctx.results[-1].status == "warn"
    assert not config.exists()


def test_seed_uses_resolved_provider_even_when_check_only_did_not_write(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        init_cmd,
        "_step_interactive",
        lambda _ctx, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(init_cmd, "_print_summary", lambda _ctx: 0)
    ctx = InitContext(project_root=tmp_path, check_only=True, interactive=False)

    assert init_cmd._run_seed(ctx, init_cmd.AgentSelection("codex", "api_key")) == 0

    assert len(calls) == 1
    assert calls[0]["agent_app"] == "codex"


def test_flag_cannot_silently_replace_existing_provider(tmp_path):
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(
        '[agent]\nprovider = "claude"\nauth = "auto"\n', encoding="utf-8"
    )
    ctx = InitContext(project_root=tmp_path, interactive=False)

    assert init_cmd._resolve_agent_selection(ctx, _args(provider="codex")) is None
    assert ctx.results[-1].status == "err"
