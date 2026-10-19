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


def test_registration_rejects_ineligible_resource(monkeypatch) -> None:
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: "not eligible")

    with pytest.raises(HostInstallationError, match="not eligible"):
        host_install.register_host_installation(Path("/skills"))


def test_registration_is_idempotent_for_existing_identity(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    identity = _identity()
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    _write(state, identity)

    assert host_install.register_host_installation(Path("/skills"), path=state) == identity


def test_host_install_error_reports_invalid_state(tmp_path: Path) -> None:
    state = tmp_path / "host-installation.json"
    state.write_text("{", encoding="utf-8")
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")

    error = host_install.host_install_error(source, path=state)

    assert error is not None
    assert "cannot read host installation identity" in error


def test_accepts_recorded_base_interpreter_wheel(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    identity = _identity()
    _write(state, identity)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")

    assert host_install.host_install_error(source, path=state) is None


@pytest.mark.parametrize(
    "root", ["/home/user/.local/share/pipx/venvs/booley-rtl", "/work/Booley/.venv"]
)
def test_persistent_wheel_venv_claims_and_retains_authority(tmp_path, monkeypatch, root):
    monkeypatch.setattr(sys, "prefix", root)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    identity = _identity(root + "/lib/python3.14/site-packages")
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    source = Path(identity.distribution_root) / "booley/data/skills"
    state = tmp_path / "host-installation.json"
    assert host_install.register_host_installation(source, path=state) == identity
    original = state.read_bytes()
    assert host_install.register_host_installation(source, path=state) == identity
    assert state.read_bytes() == original
    assert host_install.host_install_error(source, path=state) is None


@pytest.mark.parametrize("component", [".worktrees", ".runtime", "qa-runs"])
def test_isolated_wheel_in_ephemeral_component_is_ineligible(monkeypatch, component):
    monkeypatch.setattr(sys, "prefix", "/home/user/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    source = Path("/work") / component / "lib/site-packages/booley/data/skills"
    assert "ephemeral workspace state" in host_install.host_install_error(source)
    with pytest.raises(HostInstallationError, match="ephemeral workspace state"):
        host_install.register_host_installation(source)


@pytest.mark.parametrize(
    "target", ["src/booley/data/skills", ".runtime/site-packages/booley/data/skills"]
)
def test_symlink_cannot_disguise_ineligible_resource(tmp_path, monkeypatch, target):
    monkeypatch.setattr(sys, "prefix", "/home/user/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(host_install.tempfile, "gettempdir", lambda: "/unrelated-temp")
    actual = tmp_path / target
    actual.mkdir(parents=True)
    link = tmp_path / "site-packages/booley/data/skills"
    link.parent.mkdir(parents=True)
    link.symlink_to(actual, target_is_directory=True)
    error = host_install.host_install_error(link)
    assert "not from an installed wheel" in error or "ephemeral workspace state" in error


def test_rejects_source_checkout(monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/work/Booley/.venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    source = Path("/work/Booley/src/booley/data/skills")
    error = host_install.host_install_error(
        source,
    )
    assert error is not None
    assert "not from an installed wheel" in error

    with pytest.raises(HostInstallationError, match="not from an installed wheel"):
        host_install.register_host_installation(source)


def test_rejects_qa_runtime_even_when_it_contains_site_packages() -> None:
    source = Path(
        "/work/Booley/.runtime/qa-runs/run/operator-venv/"
        "lib/python3.14/site-packages/booley/data/skills"
    )
    error = host_install.host_install_error(
        source,
    )
    assert error is not None
    assert "ephemeral workspace state" in error


def test_rejects_arbitrary_temporary_qa_path(monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/tmp/booley-qa/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(host_install.tempfile, "gettempdir", lambda: "/tmp/booley-qa")
    source = Path("/tmp/booley-qa/run/lib/python3.14/site-packages/booley/data/skills")
    error = host_install.host_install_error(
        source,
    )
    assert error is not None
    assert "temporary filesystem state" in error


def test_rejects_resource_outside_imported_distribution() -> None:
    with pytest.raises(HostInstallationError, match="not owned"):
        host_install.current_host_installation(
            Path("/opt/other/lib/python3.14/site-packages/booley/data/skills")
        )


def test_unrecorded_installed_wheel_directs_user_to_bootstrap(tmp_path: Path) -> None:
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")
    error = host_install.host_install_error(
        source,
        path=tmp_path / "missing.json",
    )
    assert error is not None
    assert "booley bootstrap" in error


def test_mismatched_recorded_identity_is_rejected(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    _write(state, _identity())
    monkeypatch.setattr(
        host_install,
        "current_host_installation",
        lambda _source: replace(_identity(), revision="def456"),
    )
    error = host_install.host_install_error(
        Path("/other/site-packages/booley/data/skills"),
        path=state,
    )
    assert error is not None
    assert "does not match" in error
    assert "revision abc123 -> def456" in error
    assert _identity().distribution_root not in error


def test_registration_is_atomic_and_refuses_implicit_update(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "host-installation.json"
    candidate = _identity()
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: candidate)

    assert host_install.register_host_installation(Path("/skills"), path=state) == candidate
    assert host_install.load_host_installation(state) == candidate
    assert not list(tmp_path.glob("*.tmp"))

    replacement = replace(candidate, revision="def456", payload_fingerprint="e" * 64)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: replacement)
    with pytest.raises(HostInstallationError, match="different canonical") as caught:
        host_install.register_host_installation(Path("/skills"), path=state)
    assert "revision abc123 -> def456" in str(caught.value)
    assert f"payload_fingerprint {'f' * 64} -> {'e' * 64}" in str(caught.value)
    assert host_install.load_host_installation(state) == candidate

    assert (
        host_install.register_host_installation(Path("/skills"), update=True, path=state)
        == replacement
    )
    assert host_install.load_host_installation(state) == replacement


@pytest.mark.parametrize(
    "field",
    [
        "executable",
        "interpreter",
        "distribution_root",
        "version",
        "revision",
        "payload_fingerprint",
    ],
)
def test_each_identity_difference_requires_explicit_update(tmp_path, monkeypatch, field):
    source = Path("/home/user/venv/lib/site-packages/booley/data/skills")
    monkeypatch.setattr(sys, "prefix", "/home/user/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    state = tmp_path / "identity.json"
    original = _identity()
    candidate = replace(original, **{field: "different"})
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: original)
    host_install.register_host_installation(source, path=state)
    recorded = state.read_bytes()
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: candidate)
    assert "--update" in host_install.host_install_error(source, path=state)
    with pytest.raises(HostInstallationError, match="--update"):
        host_install.register_host_installation(source, path=state)
    assert state.read_bytes() == recorded
    assert host_install.register_host_installation(source, path=state, update=True) == candidate
    assert host_install.host_install_error(source, path=state) is None
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: original)
    assert "--update" in host_install.host_install_error(source, path=state)

    with pytest.raises(HostInstallationError, match="--update"):
        host_install.register_host_installation(source, path=state)
    assert host_install.load_host_installation(state) == candidate


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "executable",
        "interpreter",
        "distribution_root",
        "version",
        "revision",
        "payload_fingerprint",
    ],
)
def test_invalid_candidate_preserves_state(tmp_path, monkeypatch, update, existing, field):
    state = tmp_path / "config" / "identity.json"
    if existing:
        state.parent.mkdir()
        _write(state, _identity())
    before = state.read_bytes() if existing else None
    candidate = replace(_identity(), **{field: 2 if field == "schema_version" else ""})
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: candidate)
    with pytest.raises(HostInstallationError) as caught:
        host_install.register_host_installation(Path("/skills"), path=state, update=update)
    assert ("schema" if field == "schema_version" else field) in str(caught.value)
    assert ("complete source checkout" if field == "revision" else "valid build") in str(
        caught.value
    )
    if existing:
        assert state.read_bytes() == before
        assert host_install.load_host_installation(state) == _identity()
    else:
        assert not state.parent.exists()


@pytest.mark.parametrize("update", [False, True])
def test_valid_build_can_explicitly_repair_invalid_record(tmp_path, monkeypatch, update):
    state = tmp_path / "identity.json"
    _write(state, replace(_identity(), revision=""))
    before = state.read_bytes()
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: _identity())
    if update:
        assert (
            host_install.register_host_installation(Path("/skills"), path=state, update=True)
            == _identity()
        )
        assert host_install.load_host_installation(state) == _identity()
    else:
        with pytest.raises(HostInstallationError, match="revision"):
            host_install.register_host_installation(Path("/skills"), path=state)
        assert state.read_bytes() == before


@pytest.mark.parametrize(
    "failure", ["temporary-validation", "write", "replace", "readback", "mismatch"]
)
def test_registration_cannot_succeed_without_persisted_validation(tmp_path, monkeypatch, failure):
    state = tmp_path / "identity.json"
    _write(state, _identity())
    before = state.read_bytes()
    candidate = replace(_identity(), revision="new")
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: candidate)
    original_load = host_install.load_host_installation
    original_write = Path.write_text
    original_replace = Path.replace

    def load(path=None):
        if failure == "temporary-validation" and path != state:
            raise HostInstallationError("temporary validation denied")
        if path == state and failure == "readback":
            raise HostInstallationError("persisted readback denied")
        if path == state and failure == "mismatch":
            return _identity()
        return original_load(path)

    def write(path, *args, **kwargs):
        if failure == "write":
            raise OSError("temporary write denied")
        return original_write(path, *args, **kwargs)

    def replace_path(path, *args, **kwargs):
        if failure == "replace":
            raise OSError("atomic replacement denied")
        return original_replace(path, *args, **kwargs)

    monkeypatch.setattr(host_install, "load_host_installation", load)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(Path, "replace", replace_path)
    with pytest.raises(HostInstallationError):
        host_install.register_host_installation(Path("/skills"), path=state, update=True)
    if failure in {"temporary-validation", "write", "replace"}:
        assert state.read_bytes() == before
    else:
        assert original_load(state) == candidate
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "condition",
    ["missing", "current", "conflict", "corrupt", "ineligible", "invalid", "unreadable"],
)
def test_read_only_installation_distinguishes_registration_work(tmp_path, monkeypatch, condition):
    path = tmp_path / "identity.json"
    identity = _identity()
    monkeypatch.setattr(host_install, "host_installation_path", lambda: path)
    monkeypatch.setattr(
        host_install,
        "_eligibility_error",
        lambda _source: "not an installed wheel" if condition == "ineligible" else None,
    )
    monkeypatch.setattr(
        host_install,
        "current_host_installation",
        lambda _source: replace(identity, revision="") if condition == "invalid" else identity,
    )
    if condition in {"current", "conflict", "unreadable"}:
        _write(path, replace(identity, version="other") if condition == "conflict" else identity)
    elif condition == "corrupt":
        path.write_text("{invalid", encoding="utf-8")
    if condition == "unreadable":
        original = Path.read_text

        def denied(source, *args, **kwargs):
            if source == path:
                raise PermissionError("denied")
            return original(source, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    result = host_install.inspect_host_installation(tmp_path)
    assert result.pending is (condition == "missing")
    assert bool(result.error) is (condition not in {"missing", "current"})
    if condition == "missing":
        assert not path.exists()
        assert host_install.host_install_error(tmp_path) is not None
    if condition == "conflict":
        assert "--update" in result.error


def test_read_only_installation_contains_unreadable_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(host_install, "_eligibility_error", lambda _source: None)

    def denied(_source):
        raise PermissionError("package inaccessible")

    monkeypatch.setattr(host_install, "current_host_installation", denied)
    result = host_install.inspect_host_installation(tmp_path)
    assert not result.pending
    assert str(tmp_path) in result.error
    assert "restore package read access" in result.error
