from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from booley.config.host_config import HostConfigError, InteractiveHostPolicy
from booley.harness import bootstrap, bootstrap_cli
from booley.harness.image_lifecycle import Intent, LifecycleResult, Status


def _current(resource: str) -> bootstrap.BootstrapFinding:
    return bootstrap.BootstrapFinding(resource, bootstrap.BootstrapState.CURRENT, "current")


def _wire_current(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "load_host_policy", InteractiveHostPolicy)
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (_current("git"), _current("docker"), _current("vscode")),
    )
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_skills",
        lambda _intent: calls.append("skills") or _current("skills"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_nangate",
        lambda _intent: calls.append("nangate45") or _current("nangate45"),
    )
    base = LifecycleResult("booley-sandbox", "sha256:base", Status.CURRENT)
    monkeypatch.setattr(
        bootstrap,
        "_reconcile_base_image",
        lambda _intent, **_kwargs: (
            calls.append("base-image") or base,
            _current("base-image"),
        ),
    )
    sidecar_findings = tuple(
        bootstrap.host_sidecars.SidecarFinding(
            resource,
            bootstrap.host_sidecars.SidecarState.CURRENT,
            "current",
        )
        for resource in ("proxy-image", "reaper-image", "network", "proxy", "reaper")
    )
    monkeypatch.setattr(
        bootstrap.host_sidecars,
        "reconcile_sidecars",
        lambda _policy, _intent: (
            calls.append("sidecars") or bootstrap.host_sidecars.SidecarResult(sidecar_findings)
        ),
    )
    return calls


def test_bootstrap_reconciles_resources_in_fixed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_current(monkeypatch)
    result = bootstrap.reconcile_bootstrap(Intent.CHECK)
    assert calls == ["skills", "nangate45", "base-image", "sidecars"]
    assert [finding.resource for finding in result.findings] == [
        "host-config",
        "git",
        "docker",
        "vscode",
        "skills",
        "nangate45",
        "base-image",
        "proxy-image",
        "reaper-image",
        "network",
        "proxy",
        "reaper",
    ]
    assert result.ready
    assert result.exit_status == 0


def test_invalid_config_stops_before_any_other_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def invalid():
        raise HostConfigError(tmp_path / "config.toml", "interactive.max_sessions", "bad")

    monkeypatch.setattr(bootstrap, "load_host_policy", invalid)
    monkeypatch.setattr(
        bootstrap,
        "_prerequisite_findings",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)
    assert result.exit_status == 2
    assert [finding.resource for finding in result.findings] == ["host-config"]


def test_check_only_pending_is_exit_one_but_mutating_pending_is_failure() -> None:
    pending = bootstrap.BootstrapFinding("resource", bootstrap.BootstrapState.PENDING, "work")
    assert bootstrap.BootstrapResult(Intent.CHECK, (pending,)).exit_status == 1
    assert bootstrap.BootstrapResult(Intent.ENSURE, (pending,)).exit_status == 2


def test_public_adapter_uses_refresh_for_force(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Intent] = []
    monkeypatch.setattr(
        bootstrap_cli,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: seen.append(intent) or bootstrap.BootstrapResult(intent, ()),
    )
    assert (
        bootstrap_cli.run_bootstrap(SimpleNamespace(force=True, check_only=False, verbose=False))
        == 0
    )
    assert seen == [Intent.REFRESH]


def test_vscode_requires_an_executable_or_installed_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from booley.config import editor

    (tmp_path / ".config" / "Code").mkdir(parents=True)
    monkeypatch.setattr(editor, "resolve_editor_command", lambda: None)
    monkeypatch.setattr(editor, "resolve_editor_install", lambda: None)

    finding = bootstrap._vscode_finding()

    assert finding.state is bootstrap.BootstrapState.ERROR


def test_vscode_accepts_a_proven_gui_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from booley.config import editor

    application = tmp_path / "Visual Studio Code.app"
    executable = application / "Contents" / "MacOS" / "Electron"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(editor, "resolve_editor_command", lambda: None)
    monkeypatch.setattr(editor, "resolve_editor_install", lambda: application)

    finding = bootstrap._vscode_finding()

    assert finding.state is bootstrap.BootstrapState.CURRENT
    assert application.name in finding.detail


@pytest.mark.parametrize("failed_resource", ["git", "skills", "nangate45", "base-image"])
def test_bootstrap_stops_after_each_failed_dependency(
    monkeypatch: pytest.MonkeyPatch,
    failed_resource: str,
) -> None:
    calls = _wire_current(monkeypatch)
    error = bootstrap.BootstrapFinding(
        failed_resource,
        bootstrap.BootstrapState.ERROR,
        "failed",
    )
    if failed_resource == "git":
        monkeypatch.setattr(
            bootstrap,
            "_prerequisite_findings",
            lambda: (error, _current("docker"), _current("vscode")),
        )
        expected_calls: list[str] = []
    elif failed_resource == "skills":
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_skills",
            lambda _intent: calls.append("skills") or error,
        )
        expected_calls = ["skills"]
    elif failed_resource == "nangate45":
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_nangate",
            lambda _intent: calls.append("nangate45") or error,
        )
        expected_calls = ["skills", "nangate45"]
    else:
        monkeypatch.setattr(
            bootstrap,
            "_reconcile_base_image",
            lambda _intent, **_kwargs: (calls.append("base-image") or None, error),
        )
        expected_calls = ["skills", "nangate45", "base-image"]

    result = bootstrap.reconcile_bootstrap(Intent.ENSURE)

    assert result.exit_status == 2
    assert calls == expected_calls
    assert error in result.findings


def test_prerequisites_replace_a_valid_docker_probe_with_daemon_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "_tool_finding", lambda name, _arg: _current(name))
    monkeypatch.setattr(bootstrap, "_docker_daemon_error", lambda: "daemon unavailable")
    monkeypatch.setattr(bootstrap, "_vscode_finding", lambda: _current("vscode"))

    findings = bootstrap._prerequisite_findings()

    assert findings[1] == bootstrap.BootstrapFinding(
        "docker", bootstrap.BootstrapState.ERROR, "daemon unavailable"
    )


def test_tool_probe_reports_missing_failure_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    assert bootstrap._tool_finding("git", "--version").state is bootstrap.BootstrapState.ERROR

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "bad"),
    )
    assert "probe failed" in bootstrap._tool_finding("git", "--version").detail

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", "tool 1.2\nmore"),
    )
    assert bootstrap._tool_finding("git", "--version").detail == "tool 1.2"


def test_tool_probe_wraps_execution_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/tool")

    def fail(*_args, **_kwargs):
        raise OSError("denied")

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    assert "cannot run git" in bootstrap._tool_finding("git", "--version").detail


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (subprocess.TimeoutExpired("docker", 10), "did not respond"),
        (OSError("denied"), "cannot contact"),
    ],
)
def test_docker_daemon_wraps_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
    outcome: BaseException,
    expected: str,
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")

    def fail(*_args, **_kwargs):
        raise outcome

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    assert expected in (bootstrap._docker_daemon_error() or "")


def test_docker_daemon_reports_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    assert bootstrap._docker_daemon_error() is None

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "daemon stopped\nmore"),
    )
    assert bootstrap._docker_daemon_error() == (
        "Docker daemon is not running or accessible: daemon stopped"
    )


def test_vscode_accepts_a_path_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from booley.config import editor

    monkeypatch.setattr(editor, "resolve_editor_command", lambda: "/usr/bin/codium")

    finding = bootstrap._vscode_finding()

    assert finding.state is bootstrap.BootstrapState.CURRENT
    assert finding.detail == "codium available"


def test_skill_reconciliation_reports_missing_pending_changed_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: missing)
    assert bootstrap._reconcile_skills(Intent.CHECK).state is bootstrap.BootstrapState.ERROR

    source = tmp_path / "skills"
    source.mkdir()
    monkeypatch.setattr(bootstrap, "skills_dir", lambda: source)
    changed_event = SimpleNamespace(changed=True, failed=False, detail="", name="linked")
    report = SimpleNamespace(events=(changed_event,), diagnostics=(), fatal=None)
    reconciliation = SimpleNamespace(report=report)
    monkeypatch.setattr(
        bootstrap, "reconcile_host_skills", lambda *_args, **_kwargs: (reconciliation,)
    )
    assert bootstrap._reconcile_skills(Intent.CHECK).state is bootstrap.BootstrapState.PENDING
    assert bootstrap._reconcile_skills(Intent.ENSURE).state is bootstrap.BootstrapState.CHANGED

    failed_event = SimpleNamespace(changed=False, failed=True, detail="broken", name="link")
    failed_report = SimpleNamespace(
        events=(failed_event,), diagnostics=("diagnostic",), fatal="fatal"
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_host_skills",
        lambda *_args, **_kwargs: (SimpleNamespace(report=failed_report),),
    )
    finding = bootstrap._reconcile_skills(Intent.ENSURE)
    assert finding.state is bootstrap.BootstrapState.ERROR
    assert finding.detail == "broken; diagnostic; fatal"


def test_nangate_reconciliation_covers_current_check_fetch_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(bootstrap.nangate_pdk, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.nangate_pdk, "validation_errors", lambda _root: ())
    assert bootstrap._reconcile_nangate(Intent.CHECK).state is bootstrap.BootstrapState.CURRENT

    monkeypatch.setattr(
        bootstrap.nangate_pdk, "validation_errors", lambda _root: ("missing archive",)
    )
    pending = bootstrap._reconcile_nangate(Intent.CHECK)
    assert pending.state is bootstrap.BootstrapState.PENDING
    assert "missing archive" in pending.detail

    monkeypatch.setattr(bootstrap.nangate_pdk, "fetch", lambda _root: None)
    assert bootstrap._reconcile_nangate(Intent.ENSURE).state is bootstrap.BootstrapState.CHANGED

    def fail(_root):
        raise bootstrap.nangate_pdk.NangatePdkError("download failed")

    monkeypatch.setattr(bootstrap.nangate_pdk, "fetch", fail)
    assert bootstrap._reconcile_nangate(Intent.ENSURE).state is bootstrap.BootstrapState.ERROR


@pytest.mark.parametrize(
    ("image_status", "bootstrap_state"),
    [
        (Status.CURRENT, bootstrap.BootstrapState.CURRENT),
        (Status.STALE, bootstrap.BootstrapState.PENDING),
        (Status.CHANGED, bootstrap.BootstrapState.CHANGED),
        (Status.EXTERNAL, bootstrap.BootstrapState.ERROR),
    ],
)
def test_base_image_status_mapping(
    monkeypatch: pytest.MonkeyPatch,
    image_status: Status,
    bootstrap_state: bootstrap.BootstrapState,
) -> None:
    result = LifecycleResult("base", "sha256:id", image_status)
    monkeypatch.setattr(bootstrap, "reconcile_images", lambda *_args, **_kwargs: result)
    actual, finding = bootstrap._reconcile_base_image(Intent.CHECK, verbose=False)
    assert actual is result
    assert finding.state is bootstrap_state


def test_base_image_failure_becomes_typed_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise bootstrap.ImageLifecycleError("inspect failed")

    monkeypatch.setattr(bootstrap, "reconcile_images", fail)
    result, finding = bootstrap._reconcile_base_image(Intent.CHECK, verbose=False)
    assert result is None
    assert finding == bootstrap.BootstrapFinding(
        "base-image", bootstrap.BootstrapState.ERROR, "inspect failed"
    )


@pytest.mark.parametrize(
    ("sidecar_state", "bootstrap_state"),
    tuple(zip(bootstrap.host_sidecars.SidecarState, bootstrap.BootstrapState, strict=True)),
)
def test_sidecar_state_mapping(sidecar_state, bootstrap_state) -> None:
    finding = bootstrap._sidecar_finding(
        bootstrap.host_sidecars.SidecarFinding("proxy", sidecar_state, "detail")
    )
    assert finding == bootstrap.BootstrapFinding("proxy", bootstrap_state, "detail")


@pytest.mark.parametrize(
    ("state", "expected_message"),
    [
        (bootstrap.BootstrapState.CURRENT, "is current"),
        (bootstrap.BootstrapState.PENDING, "pending work"),
        (bootstrap.BootstrapState.ERROR, "is incomplete"),
    ],
)
def test_cli_renders_each_exit_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: bootstrap.BootstrapState,
    expected_message: str,
) -> None:
    result = bootstrap.BootstrapResult(
        Intent.CHECK,
        (bootstrap.BootstrapFinding("resource", state, "detail"),),
    )
    monkeypatch.setattr(bootstrap_cli, "reconcile_bootstrap", lambda *_args, **_kwargs: result)
    status = bootstrap_cli.run_bootstrap(
        SimpleNamespace(force=False, check_only=True, verbose=True)
    )
    output = capsys.readouterr().out
    assert status == result.exit_status
    assert expected_message in output
