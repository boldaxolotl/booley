"""Run README installation on distro Python; exempt only full Host Bootstrap.

Fixture-only root preparation installs sudo, creates an unprivileged user with
/home storage, and writes a direct-wheel constraint. Artifacts are read-only;
no source, Project, host configuration, or Docker socket is mounted. A fresh
login shell represents reopening the terminal after pipx ensurepath.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

IMAGE = "ubuntu:26.04"
COMMANDS = {
    "prepare": ("sudo apt-get update", "sudo apt-get install -y pipx", "pipx ensurepath"),
    "install": ("pipx install booley-rtl", "booley bootstrap"),
    "upgrade": ("pipx upgrade booley-rtl", "booley bootstrap --update"),
}
BOOTSTRAP_EXEMPTION = "booley bootstrap"


def read_commands(readme: str) -> dict[str, tuple[str, ...]]:
    """Reject ambiguous blocks and command drift before executing documentation."""
    markers = re.findall(r"<!-- booley-smoke:([^>]+) -->", readme)
    endings = re.findall(r"<!-- /booley-smoke:([^>]+) -->", readme)
    if sorted(markers) != sorted(COMMANDS) or sorted(endings) != sorted(COMMANDS):
        raise ValueError("missing, duplicate, or unexpected README smoke block")
    result = {}
    for name, expected in COMMANDS.items():
        pattern = (
            rf"<!-- booley-smoke:{name} -->\n```bash\n(.*?)\n```\n<!-- /booley-smoke:{name} -->"
        )
        matches = re.findall(pattern, readme, re.DOTALL)
        if len(matches) != 1 or tuple(matches[0].splitlines()) != expected:
            raise ValueError(f"unexpected README commands in {name}")
        result[name] = tuple(matches[0].splitlines())
    return result


def run(command: list[str], *, timeout: int = 600) -> None:
    """Run a bounded smoke operation with visible evidence and propagated errors."""
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, check=True, timeout=timeout)


def user_command(command: str) -> None:
    run(
        [
            "sudo",
            "-H",
            "-u",
            "smoke",
            "env",
            "PIP_CONSTRAINT=/home/smoke/constraint.txt",
            "bash",
            "-lc",
            command,
        ]
    )


def verify_wheel(wheel: Path) -> None:
    """Prove installed distribution origin, version, and bytes match this artifact."""
    import importlib.metadata

    import booley

    distribution = importlib.metadata.distribution("booley-rtl")
    origin = json.loads(distribution.read_text("direct_url.json"))
    assert origin["url"] == wheel.as_uri(), origin
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        version = re.search(r"^Version: (.+)$", archive.read(metadata).decode(), re.MULTILINE)[1]
        assert distribution.version == version
        package = Path(booley.__file__).resolve().parent
        payloads = [
            name
            for name in archive.namelist()
            if name.startswith("booley/") and not name.endswith("/")
        ]
        assert payloads
        for name in payloads:
            assert (package.parent / name).read_bytes() == archive.read(name), name
    print(
        f"Verified local wheel: {origin['url']}; version={version}; payload files={len(payloads)}",
        flush=True,
    )


def probe(wheel: Path, launcher: Path, operation: str) -> None:
    """Exercise real installed identity/eligibility, isolated to the fixture user."""
    from booley.runtime.host_install import (
        HostInstallationError,
        current_host_installation,
        host_install_error,
        host_installation_path,
        load_host_installation,
        register_host_installation,
    )
    from booley.runtime.paths import skills_dir

    assert sys.prefix != sys.base_prefix
    assert os.geteuid() != 0
    assert Path(sys.prefix).is_relative_to(Path.home())
    print(f"Isolated interpreter: prefix={sys.prefix}; base_prefix={sys.base_prefix}", flush=True)
    assert not os.environ.get("BOOLEY_CONTAINER")
    assert Path.home() == Path("/home/smoke")
    assert host_installation_path().is_relative_to(Path.home())
    sys.argv[0] = str(launcher.resolve())
    verify_wheel(wheel)
    source = skills_dir()
    identity = current_host_installation(source)
    if operation == "refuse":
        original = host_installation_path().read_bytes()
        assert "--update" in host_install_error(source)
        try:
            register_host_installation(source)
        except HostInstallationError as exc:
            assert "--update" in str(exc)
        else:
            raise AssertionError("different installation implicitly claimed authority")
        assert host_installation_path().read_bytes() == original
    else:
        assert register_host_installation(source, update=operation == "update") == identity
        assert load_host_installation() == identity
        original = host_installation_path().read_bytes()
        assert register_host_installation(source) == identity
        assert host_installation_path().read_bytes() == original
        assert host_install_error(source) is None
    print(f"Identity {operation} passed: {identity}", flush=True)


def installed_probe(wheel: Path, launcher: str, operation: str) -> None:
    # Resolve the pipx symlink before locating its environment's interpreter.
    command = (
        f"launcher=$(readlink -f {shlex.quote(launcher)}); "
        '"$(dirname "$launcher")/python" /smoke/runner.py --probe '
        f'--wheel {shlex.quote(str(wheel))} --launcher "$launcher" --operation {operation}'
    )
    user_command(command)


def inside(wheel: Path, commands: dict[str, tuple[str, ...]]) -> None:
    assert Path(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED").is_file()
    assert not os.environ.get("BOOLEY_CONTAINER")
    print("Distro EXTERNALLY-MANAGED marker verified", flush=True)
    run(["apt-get", "update"])
    run(["apt-get", "install", "-y", "sudo"])
    run(["useradd", "--create-home", "--shell", "/bin/bash", "smoke"])
    Path("/etc/sudoers.d/smoke").write_text("smoke ALL=(ALL) NOPASSWD: ALL\n")
    Path("/etc/sudoers.d/smoke").chmod(0o440)
    Path("/home/smoke/constraint.txt").write_text(f"booley-rtl @ {wheel.as_uri()}\n")
    for command in commands["prepare"]:
        user_command(command)
    user_command('test "$(command -v booley || true)" = ""')
    for command in commands["install"]:
        if command == BOOTSTRAP_EXEMPTION:
            print(
                "Full Host Bootstrap exempted: Docker and host prerequisites; probing installed API instead",
                flush=True,
            )
            installed_probe(wheel, "/home/smoke/.local/bin/booley", "claim")
        else:
            user_command(command)
            user_command(
                'test "$(command -v booley)" = "/home/smoke/.local/bin/booley" && booley --version && pipx list'
            )
    user_command("python3 -m venv /home/smoke/second")
    user_command(f"/home/smoke/second/bin/python -m pip install {shlex.quote(str(wheel))}")
    second = "/home/smoke/second/bin/booley"
    installed_probe(wheel, second, "refuse")
    installed_probe(wheel, second, "update")
    installed_probe(wheel, "/home/smoke/.local/bin/booley", "refuse")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--operation", choices=("claim", "refuse", "update"))
    args = parser.parse_args()
    if args.probe and (args.inside or args.launcher is None or args.operation is None):
        parser.error("--probe requires --launcher and --operation and excludes --inside")
    if not args.probe and args.readme is None:
        parser.error("--readme is required for the installation smoke")
    if args.probe:
        probe(args.wheel, args.launcher, args.operation)
        return
    commands = read_commands(args.readme.read_text())
    if args.inside:
        inside(args.wheel, commands)
        return
    wheel = args.wheel.resolve(strict=True)
    run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,src={wheel.parent},dst=/artifacts,readonly",
            "--mount",
            f"type=bind,src={Path(__file__).resolve()},dst=/smoke/runner.py,readonly",
            "--mount",
            f"type=bind,src={args.readme.resolve()},dst=/README.md,readonly",
            IMAGE,
            "bash",
            "-c",
            'apt-get update && apt-get install -y python3 && exec python3 /smoke/runner.py "$@"',
            "smoke",
            "--inside",
            "--readme",
            "/README.md",
            "--wheel",
            f"/artifacts/{wheel.name}",
        ],
        timeout=1200,
    )


if __name__ == "__main__":
    main()
