from __future__ import annotations

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
