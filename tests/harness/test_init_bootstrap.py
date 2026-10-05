from __future__ import annotations

import argparse
import inspect
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config.host_config import host_config_path
from booley.harness import bootstrap, bootstrap_cli, init_cmd
from booley.runtime import issuance_invalidation, session_refresh
from booley.runtime.image_lifecycle import Intent, LifecycleResult, Status
from tests.lifecycle_lock_support import held_lifecycle_lock, observe_lifecycle_contention


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


def _host_setup_run(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invoked: list[str],
) -> Callable[[], int]:
    if operation == "init":
        monkeypatch.setattr(
            init_cmd,
            "_run_init_unlocked",
            lambda *_args: invoked.append(operation) or 0,
        )
        return lambda: init_cmd.run_init(_args(), tmp_path)
    monkeypatch.setattr(
        bootstrap_cli,
        "register_host_installation",
        lambda *_args, **_kwargs: SimpleNamespace(
            version="1.0",
            payload_fingerprint="abcdef123456",
        ),
    )
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda *_args, **_kwargs: (
            invoked.append(operation) or bootstrap.BootstrapResult(Intent.ENSURE, ())
        ),
    )
    return lambda: bootstrap_cli.run_bootstrap(_args())


@pytest.mark.parametrize("operation", ["init", "bootstrap"])
def test_host_setup_waits_for_lifecycle_lock(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lock_path = tmp_path / "host-config" / "locks" / "docker-lifecycle.lock"
    waiting = observe_lifecycle_contention(monkeypatch)
    invoked: list[str] = []
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kw: False)
    run = _host_setup_run(operation, tmp_path, monkeypatch, invoked)

    with (
        held_lifecycle_lock(lock_path) as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        result = executor.submit(run)
        assert waiting.wait(2)
        assert not result.done()
        holder.release()
        assert result.result(timeout=5) == 0

    assert invoked == [operation]
    assert "host Docker lifecycle is busy" in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
@pytest.mark.parametrize("check_only", [True, False])
def test_init_reports_unsafe_private_store_before_project_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check_only: bool,
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    project = tmp_path / "project"
    project.mkdir()
    pending = issuance_invalidation.prepare(
        str(project.resolve()),
        cleanup_resources=False,
    )
    issuance_invalidation.cancel(pending)
    booley_config = config_root / "booley"
    booley_config.chmod(0o755)
    monkeypatch.setattr(
        init_cmd,
        "_run_init_unlocked",
        lambda *_a, **_kw: pytest.fail("unsafe recovery must precede Project work"),
    )

    assert init_cmd.run_init(_args(check_only=check_only), project) == 2

    captured = capsys.readouterr()
    assert str(booley_config) in captured.out
    assert "0755" in captured.out
    assert f"chmod 700 {booley_config}" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


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


def test_init_force_observes_host_bootstrap_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = []

    def reconcile(intent, **kwargs):
        observed.append((intent, kwargs))
        return init_cmd.BootstrapResult(intent, ())

    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", reconcile)
    ctx = init_cmd.InitContext(project_root=tmp_path, force=True)

    assert init_cmd._reconcile_init_bootstrap(ctx, _args(force=True)) is not None
    assert observed == [(Intent.CHECK, {"verbose": False})]


def test_init_without_a_usable_bootstrap_base_defers_to_project_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = SimpleNamespace()
    observed = []
    monkeypatch.setattr(
        init_cmd,
        "_step_image_lifecycle",
        lambda ctx, **kwargs: observed.append((ctx, kwargs)) or expected,
    )
    ctx = init_cmd.InitContext(project_root=tmp_path)
    bootstrap_result = init_cmd.BootstrapResult(Intent.ENSURE, ())

    assert init_cmd._reconcile_initialized_image(ctx, bootstrap_result) is expected
    assert observed == [(ctx, {})]


def test_init_managed_project_uses_planned_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = LifecycleResult("booley-sandbox", "sha256:image", Status.CURRENT)
    observed = []
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "reconcile_planned",
        lambda _scope, intent, **_kwargs: observed.append(intent) or expected,
    )
    monkeypatch.setattr(
        init_cmd,
        "reconcile_images",
        lambda *_args, **_kwargs: pytest.fail("managed init must use the aggregate plan"),
    )
    ctx = init_cmd.InitContext(project_root=tmp_path, force=True)

    assert init_cmd._step_image_lifecycle(ctx) is expected
    assert observed == [Intent.REFRESH]


def test_init_external_image_uses_explicit_plan_result(tmp_path, monkeypatch):
    expected = LifecycleResult("external/image", "sha256:image", Status.EXTERNAL)
    monkeypatch.setattr(
        init_cmd.image_lifecycle, "reconcile_planned", lambda *_args, **_kwargs: expected
    )
    monkeypatch.setattr(
        init_cmd,
        "reconcile_images",
        lambda *_args, **_kwargs: pytest.fail("legacy fallback invoked"),
    )
    ctx = init_cmd.InitContext(project_root=tmp_path)
    assert init_cmd._step_image_lifecycle(ctx) is expected
    assert ctx.results[-1].detail == "user-managed image"


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


@pytest.mark.parametrize("table", ["interactive", "sandbox"])
def test_retired_project_policy_fails_before_project_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], table: str
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    config = project_dir / "booley.toml"
    config.write_text(
        f"[{table}]\nidle_timeout_seconds = 600\nmax_sessions = 2\n",
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


def _image_command_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text("[sandbox]\nimage = 'booley-sandbox'\n")
    spec = tmp_path / ".devcontainer/devcontainer.json"
    spec.parent.mkdir()
    spec.write_text('{"image":"sha256:prior"}\n')
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kw: init_cmd.BootstrapResult(intent, ()),
    )
    monkeypatch.setattr(
        init_cmd,
        "_resolve_agent_selection",
        lambda *_args: init_cmd.AgentSelection("claude", "auto", True, True),
    )
    monkeypatch.setattr(init_cmd, "_plan_existing_guidance", lambda _ctx: (None, True))
    monkeypatch.setattr(init_cmd, "_step_agent_config", lambda *_args: True)
    return spec


def test_init_capacity_refusal_returns_two_without_image_or_spec_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.docker_capacity import DockerCapacityError

    spec = _image_command_project(tmp_path, monkeypatch)
    before = spec.read_bytes()
    intents = []

    def refuse(
        _scope: init_cmd.ProjectImageScope, intent: Intent, **_kwargs: object
    ) -> LifecycleResult:
        intents.append(intent)
        raise DockerCapacityError("Project overlay needs 10 GiB; only 1 GiB available")

    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", refuse)

    # Isolate unrelated setup resources while retaining the CLI, image step,
    # diagnostic recording and aggregate exit-status boundaries.
    def image_steps(ctx: init_cmd.InitContext, *_args: object, **_kwargs: object) -> int:
        init_cmd._step_image_lifecycle(ctx)
        return init_cmd._print_summary(ctx)

    monkeypatch.setattr(init_cmd, "_run_project_init_steps", image_steps)
    assert init_cmd.run_init(_args(force=True), tmp_path) == 2
    captured = capsys.readouterr()
    assert "[XX] image-capacity:" in captured.out
    assert "only 1 GiB available" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert intents == [Intent.REFRESH]
    assert spec.read_bytes() == before


@pytest.mark.parametrize("failure", ["stale", "unavailable"])
def test_seed_refuses_uncurrent_images_without_repair_or_spec_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    from booley.runtime.image_lifecycle import Diagnostic, ImageLifecycleError

    spec = _image_command_project(tmp_path, monkeypatch)
    before = spec.read_bytes()
    intents = []

    def observe(
        _scope: init_cmd.ProjectImageScope, intent: Intent, **_kwargs: object
    ) -> LifecycleResult:
        intents.append(intent)
        if failure == "unavailable":
            raise ImageLifecycleError("runtime base missing; run booley bootstrap")
        return LifecycleResult(
            "project-image",
            "sha256:prior",
            Status.STALE,
            diagnostics=(Diagnostic("stale", "Project wheel is stale"),),
        )

    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", observe)
    monkeypatch.setattr(
        init_cmd,
        "_step_interactive",
        lambda *_args, **_kwargs: pytest.fail("refused seed must not apply a spec"),
    )
    assert init_cmd.run_init(_args(seed=True, force=True), tmp_path) == 2
    captured = capsys.readouterr()
    repair = (
        "run booley bootstrap" if failure == "unavailable" else "run booley init before seeding"
    )
    assert repair in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert intents == [Intent.CHECK]
    assert spec.read_bytes() == before


@pytest.mark.parametrize("both_tables", [False, True])
def test_init_renders_host_table_alias_and_preserves_priority(
    tmp_path, monkeypatch, capsys, both_tables
):
    path = host_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "[interactive]\nmax_sessions = 2\n"
    if both_tables:
        text += "[sandbox]\nmax_sessions = 7\n"
    path.write_text(text)
    monkeypatch.setattr(
        bootstrap,
        "host_install_error",
        lambda _skills: "isolated fixture stops before infrastructure",
    )
    policies = []

    def reconcile(intent, **kwargs):
        result = bootstrap.reconcile_bootstrap(intent, **kwargs)
        policies.append(result.policy)
        return result

    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", reconcile)
    assert init_cmd.run_init(_args(), tmp_path) != 0
    output = capsys.readouterr().out
    assert "[interactive] is deprecated" in output
    assert "[sandbox]" in output
    assert ("Remove [interactive]" if both_tables else "replace it with") in output
    assert policies[0].max_sessions == (7 if both_tables else 2)
    assert path.read_text() == text
