"""Tests for host skill reconciliation during ``booley init``."""

from pathlib import Path

from booley.harness.setup import skills as init_skills


def test_skill_targets_always_include_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert init_skills._find_skill_targets() == [tmp_path / ".agents" / "skills"]


def test_skill_targets_add_existing_distinct_claude_directory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert init_skills._find_skill_targets() == [
        tmp_path / ".agents" / "skills",
        tmp_path / ".claude" / "skills",
    ]


def test_skill_targets_do_not_duplicate_aliased_agent_roots(tmp_path: Path, monkeypatch) -> None:
    agents = tmp_path / ".agents"
    claude = tmp_path / ".claude"
    agents.mkdir()
    claude.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    real_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs) -> Path:
        if path in {agents, claude}:
            return tmp_path / "shared-agent-root"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert init_skills._find_skill_targets() == [tmp_path / ".agents" / "skills"]


def test_reconciliation_creates_agents_target_when_only_claude_exists(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "packaged"
    skill = source / "booley-setup"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Setup\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    pending = init_skills.reconcile_host_skills(
        source,
        dry_run=True,
        allow_retarget=True,
    )

    assert [item.target for item in pending] == [
        tmp_path / ".agents" / "skills",
        tmp_path / ".claude" / "skills",
    ]
    assert [item.report.count("created") for item in pending] == [1, 1]
    assert not (tmp_path / ".agents").exists()

    changed = init_skills.reconcile_host_skills(
        source,
        dry_run=False,
        allow_retarget=True,
    )

    assert [item.report.count("created") for item in changed] == [1, 1]
    assert (tmp_path / ".agents" / "skills" / "booley-setup").is_dir()
    assert (tmp_path / ".claude" / "skills" / "booley-setup").is_dir()
