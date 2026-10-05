"""Contracts for README-driven Ubuntu installation and its one exemption."""

import importlib.util
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import yaml

SCRIPT = Path(".github/scripts/isolated_host_install_smoke.py")
_spec = importlib.util.spec_from_file_location("isolated_host_install_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)
README = Path("README.md").read_text()


def test_readme_commands_are_the_smoke_input():
    commands = smoke.read_commands(README)
    assert commands["prepare"] == (
        "sudo apt-get update",
        "sudo apt-get install -y pipx",
        "pipx ensurepath",
    )
    assert commands["install"] == ("pipx install booley-rtl", "booley bootstrap")
    assert commands["upgrade"] == ("pipx upgrade booley-rtl", "booley bootstrap --update")


@pytest.mark.parametrize(
    "old,new",
    [
        ("pipx install booley-rtl", "pipx install other"),
        ("booley bootstrap\n```", "booley bootstrap --update\n```"),
        ("sudo apt-get update", "sudo apt-get update -q"),
        ("<!-- booley-smoke:prepare -->", "<!-- booley-smoke:unexpected -->"),
        ("<!-- booley-smoke:install -->", ""),
        ("<!-- /booley-smoke:install -->", ""),
    ],
)
def test_command_or_block_drift_fails_explicitly(old, new):
    with pytest.raises(ValueError):
        smoke.read_commands(README.replace(old, new))


def test_duplicate_blocks_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        smoke.read_commands(README + "\n<!-- booley-smoke:install -->")


def test_installed_probe_uses_resolved_launcher_interpreter(monkeypatch):
    commands = []
    monkeypatch.setattr(smoke, "user_command", commands.append)
    smoke.installed_probe(
        PurePosixPath("/artifacts/exact.whl"), "/home/smoke/.local/bin/booley", "claim"
    )
    assert "readlink -f /home/smoke/.local/bin/booley" in commands[0]
    assert '"$(dirname "$launcher")/python"' in commands[0]
    assert '--launcher "$launcher" --operation claim' in commands[0]


def test_container_mounts_only_fixture_inputs_and_bootstraps_distro_python(tmp_path, monkeypatch):
    wheel = tmp_path / "exact.whl"
    wheel.touch()
    calls = []
    monkeypatch.setattr(smoke, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(
        smoke.sys, "argv", ["smoke", "--readme", "README.md", "--wheel", str(wheel)]
    )
    smoke.main()
    command, bounds = calls[0]
    assert command[:3] == ["docker", "run", "--rm"]
    assert smoke.IMAGE == "ubuntu:26.04"
    assert smoke.IMAGE in command
    mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 3
    assert all(value.endswith(",readonly") for value in mounts)
    assert any("dst=/artifacts," in value for value in mounts)
    assert any("dst=/smoke/runner.py," in value for value in mounts)
    assert "apt-get install -y python3" in command[command.index("-c") + 1]
    assert "--inside" in command
    assert bounds["timeout"] == 1200


def test_inside_executes_readme_and_only_exempts_full_bootstrap(monkeypatch):
    # Replace only fixture filesystem preparation and external operations.
    commands, probes, writes = [], [], []
    monkeypatch.setattr(smoke, "run", commands.append)
    monkeypatch.setattr(smoke, "user_command", commands.append)
    monkeypatch.setattr(smoke, "installed_probe", lambda *args: probes.append(args))
    monkeypatch.setattr(
        smoke,
        "Path",
        lambda *args: SimpleNamespace(
            is_file=lambda: True, write_text=writes.append, chmod=lambda _mode: None
        ),
    )
    wheel = PurePosixPath("/artifacts/exact.whl")
    smoke.inside(wheel, smoke.read_commands(README))
    assert commands[:3] == [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "sudo"],
        ["useradd", "--create-home", "--shell", "/bin/bash", "smoke"],
    ]
    assert "booley-rtl @ file:///artifacts/exact.whl\n" in writes
    assert all(command in commands for command in smoke.COMMANDS["prepare"])
    assert "pipx install booley-rtl" in commands
    assert smoke.BOOTSTRAP_EXEMPTION == "booley bootstrap"
    assert "booley bootstrap" not in commands
    assert [item[2] for item in probes] == ["claim", "refuse", "update", "refuse"]
    assert probes[0][1] == probes[-1][1] == "/home/smoke/.local/bin/booley"
    assert any("booley --version" in item for item in commands if isinstance(item, str))


def test_workflow_runs_distro_smoke_after_local_wheel_build_and_is_required():
    jobs = yaml.safe_load(Path(".github/workflows/test.yml").read_text())["jobs"]
    package = jobs["package-artifacts"]
    names = [step.get("name") for step in package["steps"]]
    name = "Verify README isolated host install on Ubuntu 26.04"
    assert names.index(name) > names.index("Build wheel and source distribution")
    step = package["steps"][names.index(name)]
    assert ".github/scripts/isolated_host_install_smoke.py" in step["run"]
    assert '--readme README.md --wheel "${wheels[0]}"' in step["run"]
    assert "package-artifacts" in jobs["ci-required"]["needs"]


def test_wheel_verification_rejects_wrong_origin_version_and_payload(tmp_path, monkeypatch):
    import importlib.metadata
    import json
    import zipfile

    import booley

    wheel = tmp_path / "exact.whl"
    package = tmp_path / "installed/booley"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_bytes(b"# local wheel payload\n")
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("booley/__init__.py", module.read_bytes())
        archive.writestr("booley_rtl-1.2.3.dist-info/METADATA", "Version: 1.2.3\n")
    distribution = SimpleNamespace(
        version="1.2.3", read_text=lambda _name: json.dumps({"url": wheel.as_uri()})
    )
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: distribution)
    monkeypatch.setattr(booley, "__file__", str(module))
    smoke.verify_wheel(wheel)
    distribution.read_text = lambda _name: json.dumps({"url": "https://example.org/other.whl"})
    with pytest.raises(AssertionError):
        smoke.verify_wheel(wheel)
    distribution.read_text = lambda _name: json.dumps({"url": wheel.as_uri()})
    distribution.version = "different"
    with pytest.raises(AssertionError):
        smoke.verify_wheel(wheel)
    distribution.version = "1.2.3"
    module.write_bytes(b"# substituted payload\n")
    with pytest.raises(AssertionError, match=r"booley/__init__\.py"):
        smoke.verify_wheel(wheel)
