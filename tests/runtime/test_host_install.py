import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

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


def test_host_installation_path_is_under_config_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(host_install, "config_dir", lambda: tmp_path)

    assert host_install.host_installation_path() == tmp_path / "host-installation.json"


def test_resolved_executable_uses_path_lookup(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "booley"
    executable.touch()
    monkeypatch.setattr(sys, "argv", ["booley"])
    monkeypatch.setattr(host_install.shutil, "which", lambda _name: str(executable))

    assert host_install._resolved_executable() == executable.resolve()


def test_tree_fingerprint_ignores_bytecode_and_cache_directories(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"ignored")

    first = host_install._tree_fingerprint(tmp_path)
    (tmp_path / "module.py").write_text("value = 2\n", encoding="utf-8")

    assert first != host_install._tree_fingerprint(tmp_path)


def test_current_installation_falls_back_to_tree_fingerprint(monkeypatch) -> None:
    monkeypatch.setattr(
        host_install,
        "current_build_metadata",
        lambda: SimpleNamespace(version="1.2.3", revision="abc123", payload_fingerprint=""),
    )
    monkeypatch.setattr(host_install, "_tree_fingerprint", lambda _root: "tree-fingerprint")
    package_root = Path(host_install.__file__).resolve().parent.parent

    identity = host_install.current_host_installation(package_root / "data" / "skills")

    assert identity.payload_fingerprint == "tree-fingerprint"
    assert identity.revision == "abc123"


def test_load_rejects_unsupported_schema(tmp_path: Path) -> None:
    state = tmp_path / "host-installation.json"
    state.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")

    with pytest.raises(HostInstallationError, match="unsupported host installation schema"):
        host_install.load_host_installation(state)


def test_load_reports_malformed_state(tmp_path: Path) -> None:
    state = tmp_path / "host-installation.json"
    state.write_text("{", encoding="utf-8")

    with pytest.raises(HostInstallationError, match="cannot read host installation identity"):
        host_install.load_host_installation(state)


def test_adoption_rejects_ineligible_resource(monkeypatch) -> None:
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: "not eligible")

    with pytest.raises(HostInstallationError, match="not eligible"):
        host_install.adopt_host_installation(Path("/skills"))


def test_adoption_is_idempotent_for_existing_identity(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    identity = _identity()
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    _write(state, identity)

    assert host_install.adopt_host_installation(Path("/skills"), path=state) == identity


def test_host_install_error_reports_invalid_state(tmp_path: Path) -> None:
    state = tmp_path / "host-installation.json"
    state.write_text("{", encoding="utf-8")
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")

    error = host_install.host_install_error(
        source, prefix=Path("/usr"), base_prefix=Path("/usr"), path=state
    )

    assert error is not None
    assert "cannot read host installation identity" in error


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
    error = host_install.host_install_error(source, prefix=Path("/usr"), base_prefix=Path("/usr"))
    assert error is not None
    assert "not from an installed wheel" in error


def test_rejects_qa_runtime_even_when_it_contains_site_packages() -> None:
    source = Path(
        "/work/Booley/.runtime/qa-runs/run/operator-venv/"
        "lib/python3.14/site-packages/booley/data/skills"
    )
    error = host_install.host_install_error(source, prefix=Path("/usr"), base_prefix=Path("/usr"))
    assert error is not None
    assert "ephemeral workspace state" in error


def test_rejects_arbitrary_temporary_qa_path(monkeypatch) -> None:
    monkeypatch.setattr(host_install.tempfile, "gettempdir", lambda: "/tmp/booley-qa")
    source = Path("/tmp/booley-qa/run/lib/python3.14/site-packages/booley/data/skills")
    error = host_install.host_install_error(source, prefix=Path("/usr"), base_prefix=Path("/usr"))
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


def test_adoption_is_atomic_and_refuses_implicit_replacement(tmp_path: Path, monkeypatch) -> None:
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

    assert (
        host_install.adopt_host_installation(Path("/skills"), replace=True, path=state)
        == replacement
    )
    assert host_install.load_host_installation(state) == replacement
