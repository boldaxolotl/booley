import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from booley.runtime import host_install
from booley.runtime.host_install import HostInstallationError, HostInstallationIdentity


def _identity(root: str = "/opt/python/lib/python3.14/site-packages") -> HostInstallationIdentity:
    return HostInstallationIdentity(
        schema_version=1,
        executable="/usr/local/bin/booley",
        interpreter="/usr/local/bin/python3",
        distribution_root=root,
        version="1.2.3",
        revision="abc123",
        payload_fingerprint="f" * 64,
    )


def _write(path: Path, identity: HostInstallationIdentity) -> None:
    path.write_text(json.dumps(asdict(identity)), encoding="utf-8")


def test_accepts_recorded_base_interpreter_wheel(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    identity = _identity()
    _write(state, identity)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")

    assert (
        host_install.host_install_error(
            source, prefix=Path("/usr"), base_prefix=Path("/usr"), path=state
        )
        is None
    )


def test_rejects_virtual_environment_wheel() -> None:
    source = Path("/project/.venv/lib/python3.14/site-packages/booley/data/skills")
    error = host_install.host_install_error(
        source, prefix=Path("/project/.venv"), base_prefix=Path("/usr")
    )
    assert error is not None
    assert "virtual environment" in error


def test_rejects_source_checkout() -> None:
    source = Path("/work/Booley/src/booley/data/skills")
    error = host_install.host_install_error(
        source, prefix=Path("/usr"), base_prefix=Path("/usr")
    )
    assert error is not None
    assert "not from an installed wheel" in error


def test_rejects_qa_runtime_even_when_it_contains_site_packages() -> None:
    source = Path(
        "/work/Booley/.runtime/qa-runs/run/operator-venv/"
        "lib/python3.14/site-packages/booley/data/skills"
    )
    error = host_install.host_install_error(
        source, prefix=Path("/usr"), base_prefix=Path("/usr")
    )
    assert error is not None
    assert "ephemeral workspace state" in error


def test_rejects_arbitrary_temporary_qa_path(monkeypatch) -> None:
    monkeypatch.setattr(host_install.tempfile, "gettempdir", lambda: "/tmp/booley-qa")
    source = Path("/tmp/booley-qa/run/lib/python3.14/site-packages/booley/data/skills")
    error = host_install.host_install_error(
        source, prefix=Path("/usr"), base_prefix=Path("/usr")
    )
    assert error is not None
    assert "temporary filesystem state" in error


def test_rejects_resource_outside_imported_distribution() -> None:
    with pytest.raises(HostInstallationError, match="not owned"):
        host_install.current_host_installation(
            Path("/opt/other/lib/python3.14/site-packages/booley/data/skills")
        )


def test_unrecorded_installed_wheel_requires_explicit_adoption(tmp_path: Path) -> None:
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")
    error = host_install.host_install_error(
        source,
        prefix=Path("/usr"),
        base_prefix=Path("/usr"),
        path=tmp_path / "missing.json",
    )
    assert error is not None
    assert "--adopt-installation" in error


def test_mismatched_recorded_identity_is_rejected(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    _write(state, _identity())
    monkeypatch.setattr(
        host_install,
        "current_host_installation",
        lambda _source: replace(_identity(), distribution_root="/other/site-packages"),
    )
    error = host_install.host_install_error(
        Path("/other/site-packages/booley/data/skills"),
        prefix=Path("/usr"),
        base_prefix=Path("/usr"),
        path=state,
    )
    assert error is not None
    assert "does not match" in error


def test_adoption_is_atomic_and_refuses_implicit_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "host-installation.json"
    candidate = _identity()
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: candidate)

    assert host_install.adopt_host_installation(Path("/skills"), path=state) == candidate
    assert host_install.load_host_installation(state) == candidate
    assert not list(tmp_path.glob("*.tmp"))

    replacement = replace(candidate, version="2.0.0")
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: replacement)
    with pytest.raises(HostInstallationError, match="different canonical"):
        host_install.adopt_host_installation(Path("/skills"), path=state)
    assert host_install.load_host_installation(state) == candidate

    assert host_install.adopt_host_installation(
        Path("/skills"), replace=True, path=state
    ) == replacement
    assert host_install.load_host_installation(state) == replacement
