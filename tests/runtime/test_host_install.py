import json
import os
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.path_tool_fake import PathToolFake
from tests.time_budget import scaled

import booley
from booley.core.user_paths import config_dir
from booley.runtime import host_install
from booley.runtime.host_install import HostInstallationError, HostInstallationIdentity

# An installed wheel's resource on every host: a ``site-packages`` path outside the
# temporary folder and outside the ephemeral folders.
_SOURCE = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")
_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="POSIX launcher stand-ins and links for booley"
)
_ONE_INSTALL_TEXT = (
    "this Booley process does not match the canonical host installation "
    "(revision abc123 -> def456); run the canonical host `booley bootstrap "
    "--update` if this upgrade is intentional"
)


@pytest.fixture(autouse=True)
def _empty_path(tmp_path: Path, monkeypatch) -> None:
    """Keep mismatch messages from scanning the developer's real PATH."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


@pytest.fixture
def running(monkeypatch):
    """Make the running process the given identity, or raise the given exception.

    ``current_host_installation`` describes the imported package, which in a test process
    is a source checkout: no public entry point can turn it into an installed wheel.
    """

    def install(outcome: HostInstallationIdentity | Exception) -> None:
        def current(_source):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(host_install, "current_host_installation", current)

    return install


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(identity)), encoding="utf-8")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (json.dumps({"schema_version": 2}), "unsupported host installation schema"),
        ("{", "cannot read host installation identity"),
    ],
    ids=["schema 2", "malformed JSON"],
)
def test_load_rejects_unreadable_state(tmp_path: Path, content: str, message: str) -> None:
    state = tmp_path / "host-installation.json"
    state.write_text(content, encoding="utf-8")

    with pytest.raises(HostInstallationError, match=message):
        host_install.load_host_installation(state)
    assert message in (host_install.host_install_error(_SOURCE, path=state) or "")


@pytest.mark.parametrize("argv", ["on PATH", "absolute"])
def test_current_installation_describes_running_executable(tmp_path, monkeypatch, argv) -> None:
    executable = tmp_path / "bin" / ("booley.exe" if os.name == "nt" else "booley")
    executable.parent.mkdir()
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(sys, "argv", [str(executable) if argv == "absolute" else "booley"])
    package_root = Path(booley.__file__).resolve().parent

    identity = host_install.current_host_installation(package_root / "data")

    assert identity.executable == str(executable.resolve())
    assert identity.distribution_root == str(package_root.parent)


def test_current_installation_hashes_the_package_without_a_build_fingerprint(monkeypatch) -> None:
    # The existing public patch: the real build metadata of a source checkout has no digest.
    monkeypatch.setattr(
        host_install,
        "current_build_metadata",
        lambda: SimpleNamespace(version="1.2.3", revision="abc123", payload_fingerprint=""),
    )
    package_root = Path(booley.__file__).resolve().parent

    identity = host_install.current_host_installation(package_root / "data")

    assert re.fullmatch("[0-9a-f]{64}", identity.payload_fingerprint)
    assert identity.revision == "abc123"


def test_tree_fingerprint_ignores_bytecode_and_cache_directories(tmp_path: Path) -> None:
    # Private on purpose: the public path hashes the imported package, which a test may not edit.
    fingerprint = host_install._tree_fingerprint
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "data.bin").write_bytes(b"one")
    (tmp_path / "module.pyc").write_bytes(b"one")
    first = fingerprint(tmp_path)

    (cache / "data.bin").write_bytes(b"two")
    (tmp_path / "module.pyc").write_bytes(b"two")
    assert fingerprint(tmp_path) == first

    (tmp_path / "module.py").write_text("value = 2\n", encoding="utf-8")
    assert fingerprint(tmp_path) != first


def test_rejects_resource_outside_imported_distribution() -> None:
    with pytest.raises(HostInstallationError, match="not owned"):
        host_install.current_host_installation(
            Path("/opt/other/lib/python3.14/site-packages/booley/data/skills")
        )


@pytest.mark.parametrize(
    ("kind", "resource", "message"),
    [
        ("plain", "/work/.worktrees/lib/site-packages/booley/data/skills", "ephemeral workspace"),
        ("plain", "/work/.runtime/lib/site-packages/booley/data/skills", "ephemeral workspace"),
        ("plain", "/work/qa-runs/lib/site-packages/booley/data/skills", "ephemeral workspace"),
        (
            "plain",
            "/work/Booley/.runtime/qa-runs/run/operator-venv/lib/python3.14/site-packages"
            "/booley/data/skills",
            "ephemeral workspace",
        ),
        ("plain", "/work/Booley/src/booley/data/skills", "not from an installed wheel"),
        (
            "temporary",
            "/tmp/booley-qa/run/lib/python3.14/site-packages/booley/data/skills",
            "temporary filesystem state",
        ),
        ("link", "src/booley/data/skills", "not from an installed wheel"),
        ("link", ".runtime/site-packages/booley/data/skills", "ephemeral workspace"),
    ],
    ids=[
        ".worktrees",
        ".runtime",
        "qa-runs",
        "qa runtime with site-packages",
        "source checkout",
        "temporary",
        "symlink to source",
        "symlink to ephemeral",
    ],
)
def test_ineligible_resource_is_refused(tmp_path, monkeypatch, kind, resource, message) -> None:
    source = Path(resource)
    if kind == "temporary":
        monkeypatch.setattr(host_install.tempfile, "gettempdir", lambda: "/tmp/booley-qa")
    elif kind == "link":
        # tmp_path is inside the real temporary folder, which would refuse it first.
        monkeypatch.setattr(host_install.tempfile, "gettempdir", lambda: "/unrelated-temp")
        actual = tmp_path / resource
        actual.mkdir(parents=True)
        source = tmp_path / "site-packages/booley/data/skills"
        source.parent.mkdir(parents=True)
        try:
            source.symlink_to(actual, target_is_directory=True)
        except OSError:
            pytest.skip("this host cannot create symbolic links")

    assert message in (host_install.host_install_error(source) or "")
    assert message in (host_install.inspect_host_installation(source).error or "")
    with pytest.raises(HostInstallationError, match=message):
        host_install.register_host_installation(source)


@pytest.mark.parametrize(
    "root", ["/home/user/.local/share/pipx/venvs/booley-rtl", "/work/Booley/.venv"]
)
def test_persistent_wheel_venv_claims_and_retains_authority(
    tmp_path, monkeypatch, running, root
) -> None:
    # The persistent virtual environment of #1237: the venv prefix differs from the base one.
    monkeypatch.setattr(sys, "prefix", root)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    identity = _identity(root + "/lib/python3.14/site-packages")
    running(identity)
    source = Path(identity.distribution_root) / "booley/data/skills"
    state = tmp_path / "host-installation.json"

    assert host_install.register_host_installation(source, path=state) == identity
    original = state.read_bytes()
    assert host_install.register_host_installation(source, path=state) == identity
    assert state.read_bytes() == original
    assert host_install.host_install_error(source, path=state) is None


@pytest.mark.parametrize(
    "fields",
    [
        ("executable",),
        ("interpreter",),
        ("distribution_root",),
        ("version",),
        ("revision",),
        ("payload_fingerprint",),
        ("revision", "payload_fingerprint"),
    ],
    ids="+".join,
)
def test_registration_records_once_and_requires_update(running, fields) -> None:
    original = _identity()
    candidate = replace(original, **{field: f"changed-{field}" for field in fields})
    differences = [f"{field} {getattr(original, field)} -> changed-{field}" for field in fields]
    state = host_install.host_installation_path()
    running(original)
    assert "booley bootstrap" in (host_install.host_install_error(_SOURCE) or "")

    assert host_install.register_host_installation(_SOURCE) == original
    assert state == config_dir() / "host-installation.json"
    assert not list(state.parent.glob(".*.tmp"))
    recorded = state.read_bytes()
    assert host_install.register_host_installation(_SOURCE) == original
    assert state.read_bytes() == recorded
    assert host_install.host_install_error(_SOURCE) is None

    running(candidate)
    error = host_install.host_install_error(_SOURCE) or ""
    with pytest.raises(HostInstallationError, match="--update") as caught:
        host_install.register_host_installation(_SOURCE)
    for message in (error, str(caught.value)):
        assert "--update" in message
        assert all(difference in message for difference in differences)
    assert state.read_bytes() == recorded
    assert host_install.register_host_installation(_SOURCE, update=True) == candidate
    assert host_install.host_install_error(_SOURCE) is None

    running(original)
    assert "--update" in (host_install.host_install_error(_SOURCE) or "")
    with pytest.raises(HostInstallationError, match="--update"):
        host_install.register_host_installation(_SOURCE)
    assert host_install.load_host_installation() == candidate


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
def test_invalid_candidate_preserves_state(tmp_path, running, existing, field) -> None:
    state = tmp_path / "config" / "identity.json"
    if existing:
        _write(state, _identity())
    before = state.read_bytes() if existing else None
    running(replace(_identity(), **{field: 2 if field == "schema_version" else ""}))

    with pytest.raises(HostInstallationError) as caught:
        host_install.register_host_installation(_SOURCE, path=state, update=True)

    assert ("schema" if field == "schema_version" else field) in str(caught.value)
    assert ("complete source checkout" if field == "revision" else "valid build") in str(
        caught.value
    )
    if existing:
        assert state.read_bytes() == before
    else:
        assert not state.parent.exists()


@pytest.mark.parametrize("update", [False, True])
def test_valid_build_can_explicitly_repair_invalid_record(tmp_path, running, update) -> None:
    state = tmp_path / "identity.json"
    _write(state, replace(_identity(), revision=""))
    before = state.read_bytes()
    running(_identity())
    if update:
        assert (
            host_install.register_host_installation(_SOURCE, path=state, update=True)
            == _identity()
        )
        assert host_install.load_host_installation(state) == _identity()
    else:
        with pytest.raises(HostInstallationError, match="revision"):
            host_install.register_host_installation(_SOURCE, path=state)
        assert state.read_bytes() == before


@pytest.mark.parametrize(
    "failure", ["temporary-validation", "write", "replace", "readback", "mismatch"]
)
def test_registration_cannot_succeed_without_persisted_validation(
    tmp_path, monkeypatch, running, failure
):
    state = tmp_path / "identity.json"
    _write(state, _identity())
    before = state.read_bytes()
    candidate = replace(_identity(), revision="new")
    running(candidate)
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

    # The write path has no public parameter that makes the loader, the temporary write
    # or the final replacement fail at one chosen step.
    monkeypatch.setattr(host_install, "load_host_installation", load)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(Path, "replace", replace_path)
    with pytest.raises(HostInstallationError):
        host_install.register_host_installation(_SOURCE, path=state, update=True)
    if failure in {"temporary-validation", "write", "replace"}:
        assert state.read_bytes() == before
    else:
        assert original_load(state) == candidate
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "condition",
    [
        "missing",
        "current",
        "conflict",
        "corrupt",
        "ineligible",
        "invalid",
        "unreadable",
        "inaccessible",
    ],
)
def test_read_only_inspection_distinguishes_registration_work(monkeypatch, running, condition):
    state = host_install.host_installation_path()
    identity = _identity()
    source = Path("/work/Booley/src/booley/data/skills") if condition == "ineligible" else _SOURCE
    if condition == "inaccessible":
        running(PermissionError("package inaccessible"))
    else:
        running(replace(identity, revision="") if condition == "invalid" else identity)
    if condition in {"current", "conflict", "unreadable"}:
        _write(state, replace(identity, version="other") if condition == "conflict" else identity)
    elif condition == "corrupt":
        state.parent.mkdir(parents=True)
        state.write_text("{invalid", encoding="utf-8")
    if condition == "unreadable":
        original = Path.read_text

        def denied(path, *args, **kwargs):
            if path == state:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)

    result = host_install.inspect_host_installation(source)

    assert result.pending is (condition == "missing")
    assert bool(result.error) is (condition not in {"missing", "current"})
    if condition == "missing":
        assert not state.exists()
        assert host_install.host_install_error(source) is not None
    if condition == "conflict":
        assert "--update" in result.error
    if condition == "inaccessible":
        assert str(source) in result.error
        assert "restore package read access" in result.error


def _two_installs(
    tmp_path: Path, monkeypatch, running, old_answer: dict
) -> tuple[PathToolFake, Path, Path]:
    """Two `booley` launchers on PATH, the recorded older one first, the running one second."""
    new = PathToolFake.install(tmp_path / "new", "booley", monkeypatch)
    old = PathToolFake.install(tmp_path / "old", "booley", monkeypatch)
    old.answer("--version", **old_answer)
    old_path, new_path = old.executable.resolve(), new.executable.resolve()
    _write(
        host_install.host_installation_path(),
        replace(_identity(), executable=str(old_path), version="0.9.0"),
    )
    running(replace(_identity(), executable=str(new_path), revision="def456"))
    return old, old_path, new_path


@_POSIX_ONLY
@pytest.mark.parametrize("entry", ["refusal", "inspection", "registration"])
def test_mismatch_lists_shadowing_older_install(tmp_path, monkeypatch, running, entry) -> None:
    _, old, new = _two_installs(
        tmp_path, monkeypatch, running, {"stdout": "booley 0.9.0 (abc1234, x)\n"}
    )

    if entry == "refusal":
        message = host_install.host_install_error(_SOURCE)
    elif entry == "inspection":
        message = host_install.inspect_host_installation(_SOURCE).error
    else:
        with pytest.raises(HostInstallationError) as caught:
            host_install.register_host_installation(_SOURCE)
        message = str(caught.value)

    assert message is not None
    assert message.endswith(
        f"; `booley` executables on PATH, first one runs: {old} (v0.9.0, recorded), "
        f"{new} (v1.2.3, running); remove or upgrade the installs listed before the one "
        "you want, or run `booley bootstrap --update` to record the running one"
    )


@_POSIX_ONLY
def test_mismatch_message_unchanged_with_one_distinct_install(
    tmp_path, monkeypatch, running
) -> None:
    real = PathToolFake.install(tmp_path / "dir", "booley", monkeypatch).executable.resolve()
    (tmp_path / "link_dir").mkdir()
    (tmp_path / "link_dir" / "booley").symlink_to(real)
    (tmp_path / "hard_dir").mkdir()
    os.link(real, tmp_path / "hard_dir" / "booley")
    locked = tmp_path / "locked"
    locked.mkdir()
    entries = [locked, real.parent, real.parent, tmp_path / "link_dir", tmp_path / "hard_dir"]
    monkeypatch.setenv("PATH", os.pathsep.join(str(item) for item in entries))
    _write(host_install.host_installation_path(), _identity())
    running(replace(_identity(), revision="def456"))
    locked.chmod(0)
    try:
        message = host_install.host_install_error(_SOURCE)
    finally:
        locked.chmod(0o755)

    assert message == _ONE_INSTALL_TEXT


@_POSIX_ONLY
@pytest.mark.parametrize(
    "answer",
    [
        {"returncode": 3},
        {"hang_s": 60},
        {"stdout": "other 1.0\n"},
        {"stdout": "booley\n"},
    ],
    ids=["failing", "hanging", "other tool", "bare name"],
)
def test_mismatch_lists_unreadable_version_as_unknown(
    tmp_path, monkeypatch, running, answer
) -> None:
    # No public parameter bounds the probe: only the module's timeout keeps a hang short.
    monkeypatch.setattr(host_install, "_VERSION_TIMEOUT_S", scaled(1.0))
    probed, old, _new = _two_installs(tmp_path, monkeypatch, running, answer)

    message = host_install.host_install_error(_SOURCE)

    assert message is not None
    assert f"{old} (version unknown, recorded)" in message
    assert [call.args for call in probed.calls()] == [["--version"]]


def test_mismatch_lists_platform_executable_name(tmp_path, monkeypatch, running) -> None:
    name = "booley.exe" if os.name == "nt" else "booley"
    decoy_name = "booley" if os.name == "nt" else "booley.exe"
    paths: dict[str, Path] = {}
    for directory, file_name in (("decoy", decoy_name), ("first", name), ("second", name)):
        (tmp_path / directory).mkdir()
        candidate = tmp_path / directory / file_name
        candidate.write_bytes(b"")
        candidate.chmod(0o755)
        paths[directory] = candidate.resolve()
    monkeypatch.setenv(
        "PATH", os.pathsep.join(str(tmp_path / part) for part in ("decoy", "first", "second"))
    )
    _write(
        host_install.host_installation_path(), replace(_identity(), executable=str(paths["first"]))
    )
    running(replace(_identity(), executable=str(paths["second"]), revision="def456"))

    message = host_install.host_install_error(_SOURCE)

    assert message is not None
    assert message.endswith(
        f"; `booley` executables on PATH, first one runs: {paths['first']} "
        f"(version unknown, recorded), {paths['second']} (v1.2.3, running); "
        "remove or upgrade the installs listed before the one you want, or run "
        "`booley bootstrap --update` to record the running one"
    )


def test_mismatch_marks_launcher_whose_identity_lacks_exe(tmp_path, monkeypatch) -> None:
    launchers = {}
    for part in ("first", "second"):
        launcher = tmp_path / part / "booley.exe"
        launcher.parent.mkdir()
        launcher.write_bytes(b"")  # empty: it cannot run, so its version stays unknown
        launcher.chmod(0o755)
        launchers[part] = launcher.resolve()
    if os.name == "nt":
        folders = [tmp_path / "first", tmp_path / "second"]
    else:  # `booley` links to the `.exe`: the search resolves them to it
        folders = [tmp_path / "link-first", tmp_path / "link-second"]
        for folder, part in zip(folders, launchers, strict=True):
            folder.mkdir()
            (folder / "booley").symlink_to(launchers[part])
    monkeypatch.setenv("PATH", os.pathsep.join(str(folder) for folder in folders))
    recorded = replace(_identity(), executable=str(tmp_path / "first" / "booley"))
    running = replace(
        _identity(), executable=str(tmp_path / "second" / "booley"), revision="def456"
    )

    listing = host_install.shadowing_installs(recorded, running)

    assert listing is not None
    assert f"{launchers['first']} (version unknown, recorded)" in listing
    assert f"{launchers['second']} (v1.2.3, running)" in listing
