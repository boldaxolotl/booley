"""Installed-distribution coverage for Interactive Mode sidecar setup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from booley.harness import init_cmd
from booley.harness.init_common import InitContext


def test_installed_wheel_builds_missing_interactive_sidecars(monkeypatch, tmp_path: Path) -> None:
    package_root = tmp_path / "site-packages" / "booley"
    docker_data = package_root / "data" / "docker"
    docker_source = package_root / "docker"
    docker_data.mkdir(parents=True)
    docker_source.mkdir(parents=True)
    (docker_data / "Dockerfile.egress-proxy").write_text("FROM scratch\n")
    (docker_data / "Dockerfile.reaper").write_text("FROM scratch\n")
    for source in ("egress_proxy.py", "proxy_entry.py", "reaper.py"):
        (docker_source / source).write_text("")

    monkeypatch.setattr(init_cmd, "docker_data_dir", lambda: docker_data)
    docker_ok = subprocess.CompletedProcess(args=["docker"], returncode=0)
    ctx = InitContext(project_root=tmp_path)

    with (
        patch.object(init_cmd.idk, "image_exists", return_value=False),
        patch.object(init_cmd.idk, "_run_docker", return_value=docker_ok) as run_docker,
        patch.object(init_cmd.idk, "ensure_egress_network", return_value=False),
        patch.object(init_cmd.idk, "ensure_egress_proxy", return_value="created"),
        patch.object(init_cmd.idk, "ensure_reaper", return_value="created"),
    ):
        notes = init_cmd._ensure_interactive_docker(ctx)

    assert "proxy:created" in notes
    assert "reaper:created" in notes
    assert "proxy:skipped" not in notes
    assert "reaper:skipped" not in notes
    build_calls = [call.args[0] for call in run_docker.call_args_list]
    assert len(build_calls) == 2
    assert all(call[0] == "build" for call in build_calls)
    assert all(call[-1] == str(docker_source) for call in build_calls)
    dockerfiles = {Path(call[call.index("-f") + 1]).name for call in build_calls}
    assert dockerfiles == {"Dockerfile.egress-proxy", "Dockerfile.reaper"}
