"""`booley bootstrap --egress`: record the Sandbox egress mode, then bootstrap under it."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from booley.config.host_config import load_host_policy
from booley.harness import bootstrap, bootstrap_cli
from booley.runtime import session_admission
from booley.runtime.image_lifecycle import Intent
from tests.conftest import require_symlinks

_UNPROXIED = b'[sandbox]\negress = "unproxied"\n'


def _fail(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("must not be called")


def _current_result(intent: Intent = Intent.ENSURE) -> bootstrap.BootstrapResult:
    finding = bootstrap.BootstrapFinding("git", bootstrap.BootstrapState.CURRENT, "current")
    return bootstrap.BootstrapResult(intent, (finding,))


def _sandbox(name: str, egress: str, *, vscode: bool) -> session_admission.Sandbox:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return session_admission.Sandbox(
        container_id=f"id-{name}",
        name=name,
        project_root=f"/work/{name}",
        project_id=None,
        created_at=now,
        started_at=now,
        running=True,
        vscode=vscode,
        egress=egress,
    )


class _Host:
    """Stub every host collaborator; record what Bootstrap saw."""

    def __init__(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config = config
        self.registered = Mock()
        self.reconciled_egress: list[str] = []
        self.sandboxes: Callable[[], tuple[session_admission.Sandbox, ...]] = lambda: ()
        monkeypatch.setattr(bootstrap_cli, "host_lifecycle_lock", lambda _op: nullcontext())
        monkeypatch.setattr(bootstrap_cli, "register_host_installation", self.registered)
        monkeypatch.setattr(bootstrap_cli, "reconcile_bootstrap", self._reconcile)
        monkeypatch.setattr(
            bootstrap_cli, "inspect_bootstrap", lambda **_kw: _current_result(Intent.CHECK)
        )
        monkeypatch.setattr(
            "booley.runtime.session_refresh.shared_recovery_blocks_command",
            lambda **_kwargs: False,
        )
        monkeypatch.setattr(session_admission, "running_sandboxes", self._running_sandboxes)

    def _running_sandboxes(self) -> tuple[session_admission.Sandbox, ...]:
        # Late-bound so a test can swap `sandboxes` after construction.
        return self.sandboxes()

    def _reconcile(self, intent: Intent, **_kwargs: object) -> bootstrap.BootstrapResult:
        # Bootstrap reloads the host policy itself, so this is the mode it ran under.
        self.reconciled_egress.append(load_host_policy().egress)
        return _current_result(intent)

    def run(self, **flags: object) -> int:
        return bootstrap_cli.run_bootstrap(SimpleNamespace(**flags))


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # The booley/ directory is deliberately not created: --egress must create it.
    return _Host(tmp_path / "xdg" / "booley" / "config.toml", monkeypatch)


def test_egress_creates_config_then_bootstraps_under_it(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    assert host.run(egress="unproxied") == 0

    assert host.config.read_bytes() == _UNPROXIED
    assert host.reconciled_egress == ["unproxied"]
    assert 'recorded [sandbox] egress = "unproxied"' in capsys.readouterr().out


def test_invalid_egress_result_writes_nothing_and_skips_bootstrap(
    host: _Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = b'[sandbox]\negress_allowlist = ["example.com"]\n'
    host.config.parent.mkdir(parents=True)
    host.config.write_bytes(original)
    monkeypatch.setattr("booley.runtime.session_refresh.shared_recovery_blocks_command", _fail)

    assert host.run(egress="unproxied") == 2

    assert host.config.read_bytes() == original
    host.registered.assert_not_called()
    assert host.reconciled_egress == []
    assert "unproxied cannot be combined" in capsys.readouterr().out


def test_legacy_interactive_config_is_refused_with_migration_guidance(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    original = b"[interactive]\nmax_sessions = -1\n"
    host.config.parent.mkdir(parents=True)
    host.config.write_bytes(original)

    assert host.run(egress="unproxied") == 2

    assert host.config.read_bytes() == original
    host.registered.assert_not_called()
    assert host.reconciled_egress == []
    assert "rename it to [sandbox] first" in capsys.readouterr().out


def test_symlinked_config_is_refused_before_any_mutation(
    host: _Host, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    require_symlinks(tmp_path)
    target = tmp_path / "real.toml"
    target.write_bytes(b"[sandbox]\nmax_sessions = 2\n")
    host.config.parent.mkdir(parents=True)
    host.config.symlink_to(target)

    for check_only in (True, False):
        assert host.run(egress="unproxied", check_only=check_only) == 2

        assert target.read_bytes() == b"[sandbox]\nmax_sessions = 2\n"
        assert host.config.is_symlink()
        host.registered.assert_not_called()
        assert host.reconciled_egress == []
        assert "symlinked host config" in capsys.readouterr().out


def test_failed_egress_write_skips_bootstrap(
    host: _Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(_path: Path, _data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(bootstrap_cli, "replace_regular_file", refuse)

    assert host.run(egress="unproxied") == 2

    assert not host.config.exists()
    assert host.reconciled_egress == []
    assert "Cannot record host egress mode: disk full" in capsys.readouterr().out


def test_qa_enable_failure_leaves_config_unchanged(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_enable(_cwd: Path) -> None:
        raise OSError("no checkout")

    monkeypatch.setattr(bootstrap_cli, "enable_qa_skills", fail_enable)

    assert host.run(egress="unproxied", with_qa_skills=True) == 2

    assert not host.config.exists()
    assert host.reconciled_egress == []

    # The retry is not blocked by the first attempt and records the mode.
    monkeypatch.setattr(bootstrap_cli, "enable_qa_skills", lambda _cwd: None)
    assert host.run(egress="unproxied", with_qa_skills=True) == 0
    assert host.config.read_bytes() == _UNPROXIED


def test_check_only_egress_reports_pending_change_without_writing(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = b"# mine\n[sandbox]\nmax_sessions = 2\n"
    for content in (None, existing):  # missing file, then an existing one
        if content is not None:
            host.config.parent.mkdir(parents=True)
            host.config.write_bytes(content)

        assert host.run(egress="unproxied", check_only=True) == 1

        out = capsys.readouterr().out
        assert 'would set [sandbox] egress = "unproxied"' in out
        assert "booley bootstrap --egress unproxied" in out
        assert host.config.exists() == (content is not None)
        if content is not None:
            assert host.config.read_bytes() == content
        host.registered.assert_not_called()


def test_check_only_egress_already_recorded_is_current(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.config.parent.mkdir(parents=True)
    host.config.write_bytes(_UNPROXIED)

    assert host.run(egress="unproxied", check_only=True) == 0

    assert "egress is already unproxied" in capsys.readouterr().out


def test_mode_change_names_running_sandboxes_on_old_mode(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.sandboxes = lambda: (
        _sandbox("editor", "proxy", vscode=True),
        _sandbox("headless", "proxy", vscode=False),
        _sandbox("current", "unproxied", vscode=False),
    )

    assert host.run(egress="unproxied") == 0

    out = capsys.readouterr().out
    assert "Sandbox editor (Project /work/editor) still runs with egress 'proxy'" in out
    assert "Dev Containers: Rebuild Container" in out
    assert "Sandbox headless (Project /work/headless) still runs with egress 'proxy'" in out
    assert "booley session down" in out
    assert "Sandbox current" not in out


def test_sandbox_inventory_failure_warns_and_keeps_bootstrap_status(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable() -> tuple[session_admission.Sandbox, ...]:
        raise session_admission.AdmissionError("docker is down")

    host.sandboxes = unavailable

    assert host.run(egress="unproxied") == 0

    assert host.config.read_bytes() == _UNPROXIED
    out = capsys.readouterr().out
    assert "cannot list running Sandboxes to check the egress change: docker is down" in out
    assert "Stopped Sandboxes use the new mode on their next start" in out


def test_same_egress_mode_makes_no_edit(
    host: _Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host.config.parent.mkdir(parents=True)
    host.config.write_bytes(_UNPROXIED)
    monkeypatch.setattr(bootstrap_cli, "replace_regular_file", _fail)
    host.sandboxes = _fail

    assert host.run(egress="unproxied") == 0

    assert host.config.read_bytes() == _UNPROXIED
    assert host.reconciled_egress == ["unproxied"]
    assert "egress is already unproxied" in capsys.readouterr().out
