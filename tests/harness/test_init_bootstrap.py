from __future__ import annotations

import argparse
import inspect
from pathlib import Path

import pytest

from booley.config.host_config import host_config_path
from booley.harness import bootstrap, init_cmd


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "seed": False,
        "check_only": False,
        "force": False,
        "verbose": False,
        "provider": None,
        "auth": None,
        "skip_credentials": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _finding(
    state: init_cmd.BootstrapState,
    resource: str = "resource",
) -> bootstrap.BootstrapFinding:
    return bootstrap.BootstrapFinding(resource, state, state.value)


def test_project_init_coordinator_fits_on_a_screen() -> None:
    assert len(inspect.getsourcelines(init_cmd._run_project_init_steps)[0]) <= 50


def test_project_init_preflight_coordinator_fits_on_a_screen() -> None:
    assert len(inspect.getsourcelines(init_cmd._run_init_unlocked)[0]) <= 50


def test_bootstrap_failure_precedes_every_project_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(
            intent,
            (_finding(init_cmd.BootstrapState.ERROR, "docker"),),
        ),
    )
    assert init_cmd.run_init(_args(), tmp_path) == 2
    assert not (tmp_path / ".booley_project").exists()
    assert not (tmp_path / ".devcontainer").exists()


def test_source_checkout_refusal_precedes_bootstrap_and_project_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n",
        encoding="utf-8",
    )

    def unexpected_bootstrap(*_args: object, **_kwargs: object) -> None:
        pytest.fail("source refusal must precede Host Bootstrap")

    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", unexpected_bootstrap)

    assert init_cmd.run_init(_args(), tmp_path) == 2
    assert "cannot be initialized or used as a Project" in capsys.readouterr().out
    assert not (tmp_path / ".booley_project").exists()
    assert not (tmp_path / ".devcontainer").exists()


def test_seed_only_checks_bootstrap_and_names_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(
            intent,
            (_finding(init_cmd.BootstrapState.PENDING),),
        ),
    )
    assert init_cmd.run_init(_args(seed=True), tmp_path) == 2
    assert "booley bootstrap" in capsys.readouterr().out
    assert not (tmp_path / ".booley_project").exists()


def test_check_only_continues_project_planning_and_returns_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(
            intent,
            (_finding(init_cmd.BootstrapState.PENDING),),
        ),
    )
    selection = init_cmd.AgentSelection("claude", "auto", True, True)
    monkeypatch.setattr(
        init_cmd,
        "_resolve_agent_selection",
        lambda _ctx, _args, _path: selection,
    )
    monkeypatch.setattr(init_cmd, "_plan_existing_guidance", lambda _ctx: (None, True))
    planned: list[bool] = []

    def project_steps(ctx, *_args, **_kwargs):
        planned.append(True)
        return init_cmd._print_summary(ctx)

    monkeypatch.setattr(init_cmd, "_run_project_init_steps", project_steps)
    assert init_cmd.run_init(_args(check_only=True), tmp_path) == 1
    assert planned == [True]
    assert not (tmp_path / ".booley_project").exists()


def test_retired_project_policy_fails_before_project_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    config = project_dir / "booley.toml"
    config.write_text(
        "[interactive]\nidle_timeout_seconds = 600\nmax_sessions = 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(intent, ()),
    )
    before = config.read_bytes()
    assert init_cmd.run_init(_args(scaffold="demo"), tmp_path) == 2
    output = capsys.readouterr().out
    assert str(host_config_path()) in output
    assert "idle_timeout_seconds = 600" in output
    assert config.read_bytes() == before
    assert not (tmp_path / "rtl").exists()
