"""Per-Project Docker leftovers, observed through a fake Docker CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.config.sandbox import project_image_name_for_identity
from booley.core.project_identity import project_label_id, project_name_id
from booley.projects.image_keepers import Leftover
from booley.runtime import image_lifecycle, interactive_docker, project_resources
from booley.runtime.session_issuance import keeper_image_for_identity


def _id(char: str) -> str:
    return "sha256:" + char * 64


class FakeHost:
    """The Docker objects one host holds, answering only the commands Booley issues."""

    def __init__(self) -> None:
        self.images: list[tuple[str, str, str]] = []  # (ID, repository:tag, size)
        self.containers: dict[str, tuple[str, tuple[str, ...], str]] = {}  # image, volumes, label
        self.volumes: dict[str, str] = {}
        self.networks: dict[str, str] = {}  # name -> project label
        self.df_fails = False

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, self._answer(args), "")

    def _answer(self, args: list[str]) -> str:  # noqa: PLR0911 -- one branch per Docker command
        if args[:3] == ["image", "ls", "--all"]:
            return "".join(f"{i}\t{ref}\t{size}\n" for i, ref, size in self.images)
        if args == ["container", "ls", "--all", "--quiet", "--no-trunc"]:
            return "".join(f"{name}\n" for name in self.containers)
        if args[:2] == ["container", "inspect"]:
            return self.containers[args[2]][0] + "\n"
        if args[:2] == ["volume", "ls"]:
            return "".join(f"{name}\n" for name in self.volumes)
        if args[:3] == ["container", "ls", "--all"] and "--filter" in args:
            key, value = args[args.index("--filter") + 1].split("=", 1)
            return "".join(
                f"{name}\n"
                for name, (_image, volumes, label) in self.containers.items()
                if (value in volumes if key == "volume" else value.endswith(f"={label}"))
            )
        if args[:2] == ["network", "ls"]:
            return "".join(
                f"{name}\n"
                for name, label in self.networks.items()
                if args[3].endswith(f"={label}")
            )
        if args[:2] == ["system", "df"]:
            if self.df_fails:
                raise subprocess.TimeoutExpired(args, 60)
            rows = [{"Name": name, "Size": size} for name, size in self.volumes.items()]
            return json.dumps({"Images": [], "Volumes": rows})
        raise AssertionError(args)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = FakeHost()
    monkeypatch.setattr(interactive_docker, "_run_docker", fake)
    return fake


@pytest.mark.parametrize("df_fails", [False, True])
def test_leftovers_lists_only_this_projects_resources(
    tmp_path: Path, host: FakeHost, df_fails: bool
) -> None:
    root, other = str(tmp_path / "chip"), str(tmp_path / "other")
    family = project_image_name_for_identity(root)
    keeper = keeper_image_for_identity(root)
    host.images = [
        (_id("1"), f"{family}:latest", "5.1GB"),
        (_id("1"), keeper, "5.1GB"),
        (_id("2"), f"{family}-wheel:latest", "1.2GB"),
        (_id("2"), "mirror/wheel:copy", "1.2GB"),
        (_id("3"), f"{project_image_name_for_identity(other)}:latest", "5GB"),
        (_id("4"), "ubuntu:24.04", "80MB"),
        (_id("5"), "<none>:<none>", "4.9GB"),
        (_id("6"), "<none>:<none>", "4.8GB"),
        (_id("7"), f"{family}:<none>", "4.7GB"),
    ]
    image_lifecycle.record_owned_images(Path(root), "t1", {}, (_id("5"),))
    used = f"booley-codex-state-{project_name_id(root)}"
    idle = f"booley-claude-state-{project_name_id(root)}-subscription"
    host.volumes = {
        used: "300MB",
        idle: "N/A",
        f"booley-codex-state-{project_name_id(other)}": "1MB",
    }
    host.containers = {
        "c" * 64: (_id("1"), (used,), project_label_id(root)),
        "d" * 64: (_id("3"), (), project_label_id(other)),
    }
    host.networks = {"chip-flexnet": project_name_id(root), "other-net": project_label_id(other)}
    host.df_fails = df_fails

    found = project_resources.leftovers(root)

    volume_size = (lambda size: None) if df_fails else (lambda size: size)
    assert found == (
        Leftover(
            "image", _id("1"), "in-use", "5.1GB", tuple(sorted((f"{family}:latest", keeper)))
        ),
        Leftover(
            "image", _id("2"), "shared", "1.2GB", (f"{family}-wheel:latest", "mirror/wheel:copy")
        ),
        Leftover("image", _id("5"), "unused", "4.9GB"),
        Leftover("image", _id("7"), "unrecorded", "4.7GB"),
        Leftover("volume", idle, "unused"),
        Leftover("volume", used, "in-use", volume_size("300MB")),
        Leftover("container", "c" * 64, "labelled"),
        Leftover("network", "chip-flexnet", "labelled"),
    )


def test_leftovers_fails_loud_when_docker_cannot_list_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        interactive_docker,
        "_run_docker",
        lambda args, **_: subprocess.CompletedProcess(args, 1, "", "daemon down"),
    )
    with pytest.raises(RuntimeError, match="daemon down"):
        project_resources.leftovers(str(tmp_path / "chip"))
