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
from booley.runtime import issuance_invalidation
from booley.runtime.docker_capacity import DockerCapacityError
from booley.runtime.image_lifecycle import (
    Diagnostic,
    ImageLifecycleError,
    Intent,
    LifecycleResult,
    Status,
)
from tests.harness.init_agent_support import (
    empty_host,
    login,
    project_config,
    seed_init,
    stub_host_images,
)
from tests.lifecycle_lock_support import held_lifecycle_lock, observe_lifecycle_contention
from tests.time_budget import hang_guard_s


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


# private: a function's length is source structure, not public behavior.
@pytest.mark.parametrize("coordinator", ["_run_project_init_steps", "_run_init_unlocked"])
def test_init_coordinators_fit_on_a_screen(coordinator: str) -> None:
    source = inspect.getsourcelines(getattr(init_cmd, coordinator))[0]
    assert len(source) <= 50, f"{coordinator} exceeds the 50-line architecture limit"


def _host_setup_run(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invoked: list[str],
) -> Callable[[], int]:
    if operation == "init":
        # private: stands for everything after the lock, which needs Docker-backed
        # Host Bootstrap; the test only asks whether `run_init` waits for the lock.
        monkeypatch.setattr(
            init_cmd,
            "_run_init_unlocked",  # private: see above
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
    monkeypatch.setattr(
        issuance_invalidation, "shared_recovery_blocks_command", lambda **_kw: False
    )
    run = _host_setup_run(operation, tmp_path, monkeypatch, invoked)

    with (
        held_lifecycle_lock(lock_path) as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        result = executor.submit(run)
        assert waiting.wait(hang_guard_s())
        assert not result.done()
        holder.release()
        assert result.result(timeout=hang_guard_s()) == 0

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
    issuance_invalidation.prepare(str(project.resolve()))
    issuance_invalidation.cancel(str(project.resolve()))
    booley_config = config_root / "booley"
    booley_config.chmod(0o755)
    # private: the guard stands for everything after the lock, which needs
    # Docker-backed Host Bootstrap; it proves recovery refuses before that work.
    monkeypatch.setattr(
        init_cmd,
        "_run_init_unlocked",  # private: see above
        lambda *_a, **_kw: pytest.fail("unsafe recovery must precede Project work"),
    )

    assert init_cmd.run_init(_args(check_only=check_only), project) == 2

    captured = capsys.readouterr()
    assert str(booley_config) in captured.out
    assert "0755" in captured.out
    assert f"chmod 700 {booley_config}" in captured.out
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("force", [False, True])
def test_bootstrap_failure_precedes_every_project_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    """`--force` observes Host Bootstrap (CHECK) and never refreshes it."""
    observed: list[tuple[Intent, dict[str, object]]] = []

    def reconcile(intent: Intent, **kwargs: object) -> init_cmd.BootstrapResult:
        observed.append((intent, kwargs))
        error = _finding(init_cmd.BootstrapState.ERROR, "docker")
        return init_cmd.BootstrapResult(intent, (error,))

    # Host Bootstrap inspects and builds Docker-backed host resources.
    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", reconcile)

    assert init_cmd.run_init(_args(force=force), tmp_path) == 2
    assert observed == [(Intent.CHECK, {"verbose": False})]
    assert not (tmp_path / ".booley").exists()
    assert not (tmp_path / ".devcontainer").exists()


def test_source_checkout_refusal_precedes_bootstrap_and_project_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_bytes(b"[tool.booley]\nsource_checkout = true\n")

    def unexpected_bootstrap(*_args: object, **_kwargs: object) -> None:
        pytest.fail("source refusal must precede Host Bootstrap")

    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", unexpected_bootstrap)

    assert init_cmd.run_init(_args(), tmp_path) == 2
    assert "cannot be initialized or used as a Project" in capsys.readouterr().out
    assert not (tmp_path / ".booley").exists()
    assert not (tmp_path / ".devcontainer").exists()


@pytest.mark.parametrize("check_only", [False, True])
def test_pending_bootstrap_stops_seed_but_not_its_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check_only: bool,
) -> None:
    login(empty_host(tmp_path, monkeypatch), "claude")
    reached = stub_host_images(monkeypatch)
    # Host Bootstrap inspects and builds Docker-backed host resources.
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(
            intent, (_finding(init_cmd.BootstrapState.PENDING),)
        ),
    )

    code = seed_init(tmp_path, check_only=check_only)

    output = capsys.readouterr().out
    assert not (tmp_path / ".booley").exists()
    if not check_only:
        assert code == 2
        assert "booley bootstrap" in output
        assert reached == []
        return
    assert reached == [tmp_path]
    assert "would record [agent] claude/subscription" in output
    # A pending finding is a warning row: an `err` row would turn the check into a failure.
    row = next(line for line in output.splitlines() if "bootstrap.resource" in line)
    assert "pending" in row and "!" in row and "✗" not in row


@pytest.mark.parametrize(("resource", "pending"), [("vscode", False), ("host-agent-cli", True)])
def test_optional_editor_warning_is_not_pending_init_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resource: str,
    pending: bool,
) -> None:
    login(empty_host(tmp_path, monkeypatch), "claude")
    stub_host_images(monkeypatch)
    finding = _finding(init_cmd.BootstrapState.WARNING, resource)
    # Host Bootstrap inspects and builds Docker-backed host resources.
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(intent, (finding,)),
    )

    seed_init(tmp_path, check_only=True)

    output = capsys.readouterr().out
    assert f"{resource}: warning" in output
    verdict = output[output.index("Checked initialization:") :]
    assert (f"bootstrap.{resource}" in verdict) is pending


@pytest.mark.parametrize("table", ["interactive", "sandbox"])
def test_retired_project_policy_fails_before_project_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], table: str
) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    config = booley_dir / "booley.toml"
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


@pytest.mark.parametrize(
    ("force", "outcome"),
    [(True, "current"), (False, "external"), (True, "capacity")],
)
def test_init_image_step_records_each_reconciliation_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    force: bool,
    outcome: str,
) -> None:
    intents: list[Intent] = []
    results = {
        "current": LifecycleResult("booley-sandbox", "sha256:image", Status.CURRENT),
        "external": LifecycleResult("external/image", "sha256:image", Status.EXTERNAL),
    }

    def reconcile(
        _scope: init_cmd.ProjectImageScope, intent: Intent, **_kwargs: object
    ) -> LifecycleResult:
        intents.append(intent)
        if outcome == "capacity":
            raise DockerCapacityError("Project overlay needs 10 GiB; only 1 GiB available")
        return results[outcome]

    # The Sandbox Image chain plans, pulls and builds images through Docker.
    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", reconcile)
    ctx = init_cmd.InitContext(project_root=tmp_path, force=force)

    # private: full init reaches the image step only inside host preparation and then
    # runs Sandbox Issuance, and `--seed` passes an explicit CHECK, so no public route
    # chooses the intent from `--force` without Docker-backed stubs.
    result = init_cmd._step_image_lifecycle(ctx)
    code = init_cmd._print_summary(ctx)  # private: the closing step of the same run

    output = capsys.readouterr()
    assert intents == [Intent.REFRESH if force else Intent.ENSURE]
    if outcome == "capacity":
        assert result is None
        assert code == 2
        assert "✗  image-capacity:" in output.out
        assert "only 1 GiB available" in output.out
        assert "Traceback" not in output.out + output.err
        return
    assert result is not None
    assert code == 0
    if outcome == "external":
        assert ctx.results[-1].detail == "user-managed image"


@pytest.mark.parametrize(
    ("failure", "repair"),
    [("stale", "run booley init before seeding"), ("unavailable", "run booley bootstrap")],
)
def test_seed_refuses_uncurrent_images_without_repair_or_spec_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    repair: str,
) -> None:
    empty_host(tmp_path, monkeypatch)
    project_config(tmp_path, b'[agent]\ndefault = "claude"\n')
    spec = tmp_path / ".devcontainer" / "devcontainer.json"
    spec.parent.mkdir()
    spec.write_bytes(b'{"image":"sha256:prior"}\n')
    stub_host_images(monkeypatch)
    intents: list[Intent] = []

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

    # The Sandbox Image chain plans, pulls and builds images through Docker.
    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", observe)

    assert seed_init(tmp_path, force=True) == 2

    captured = capsys.readouterr()
    assert repair in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert intents == [Intent.CHECK]
    assert spec.read_bytes() == b'{"image":"sha256:prior"}\n'


@pytest.mark.parametrize("both_tables", [False, True])
def test_init_renders_host_table_alias_and_preserves_priority(
    tmp_path, monkeypatch, capsys, both_tables
):
    config_root = tmp_path / "config"
    config_root.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    path = host_config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = "[interactive]\nmax_sessions = 2\n"
    if both_tables:
        text += "[sandbox]\nmax_sessions = 7\n"
    path.write_text(text)
    monkeypatch.setattr(
        bootstrap,
        "host_install_error",
        lambda _skills: "isolated fixture stops before infrastructure",
    )
    monkeypatch.setattr(
        bootstrap,
        "inspect_bootstrap",
        lambda **_kw: pytest.fail("Init must use legacy reconciliation"),
    )
    # private: the host probe runs commands; this guard proves the fixture stops before it.
    monkeypatch.setattr(
        bootstrap, "_prerequisite_findings", lambda: pytest.fail("fixture must not probe the host")
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_a, **_kw: pytest.fail("fixture must not run external commands"),
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


@pytest.mark.parametrize(
    ("sidecar", "role"),
    [("pending", None), ("error", None), (None, "RUNTIME_BASE"), (None, "PROJECT_OVERLAY")],
)
def test_auth_reseed_names_the_command_that_repairs_each_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sidecar: str | None,
    role: str | None,
) -> None:
    from booley.harness import host_sidecars, image_lifecycle
    from booley.runtime.image_lifecycle import ImageRole, PlanAction

    empty_host(tmp_path, monkeypatch)
    project_config(tmp_path, b'[agent]\ndefault = "claude"\n')
    full: list[bool] = []
    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", lambda *a, **k: full.append(True))
    state = host_sidecars.SidecarState(sidecar) if sidecar else None
    found = (
        (host_sidecars.SidecarFinding("proxy", state, "synthetic sidecar error"),) if state else ()
    )
    steps = (SimpleNamespace(role=ImageRole[role], action=PlanAction.BUILD),) if role else ()
    # Reconciling sidecars and planning images inspect and build Docker resources.
    monkeypatch.setattr(
        host_sidecars, "reconcile_sidecars", lambda *a, **k: host_sidecars.SidecarResult(found)
    )
    monkeypatch.setattr(image_lifecycle, "plan", lambda *a, **k: SimpleNamespace(steps=steps))

    assert init_cmd.reseed_auth_spec(tmp_path) == 2

    output = capsys.readouterr().out
    assert not full
    if sidecar == "pending":
        assert output.count("booley bootstrap") == 1
        assert "init --seed" in output
    elif sidecar == "error":
        assert "synthetic sidecar error" in output
        assert "booley bootstrap" not in output
    else:
        assert ("booley bootstrap" in output) == (role == "RUNTIME_BASE")
        assert "booley init" in output


def test_seed_accepts_absent_proxy_and_network_under_unproxied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.config.host_config import SandboxHostPolicy
    from booley.harness import host_sidecars
    from tests.harness.test_bootstrap import _wire_current
    from tests.harness.test_host_sidecars import _add_sidecar, _egress_docker, _mutations

    reconcile_sidecars = host_sidecars.reconcile_sidecars
    _wire_current(monkeypatch)
    monkeypatch.setattr(host_sidecars, "reconcile_sidecars", reconcile_sidecars)
    policy = SandboxHostPolicy(egress="unproxied")
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: policy)
    docker = _egress_docker(tmp_path / "docker", monkeypatch)
    _add_sidecar(
        docker,
        host_sidecars.legacy.REAPER_CONTAINER,
        "reaper",
        host_sidecars.legacy.REAPER_IMAGE,
        policy,
    )
    ctx = init_cmd.InitContext(project_root=tmp_path)

    # private: past Host Bootstrap, `run_init --seed` goes on to the Sandbox Image
    # chain, which the borrowed `_egress_docker` fake does not serve.
    result = init_cmd._reconcile_init_bootstrap(ctx, _args(seed=True))

    assert result is not None, "absent proxy and network are current under unproxied"
    assert "run `booley bootstrap`" not in capsys.readouterr().out
    assert _mutations(docker) == []
