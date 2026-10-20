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
        self.layers: dict[str, int] = {}
        self.fail_volume_rm = False
        self.removals: list[str] = []

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
        if args[:2] in (["image", "rm"], ["volume", "rm"], ["image", "inspect"]):
            return self._remove_or_count_layers(args)
        if args[:2] == ["system", "df"]:
            if self.df_fails:
                raise subprocess.TimeoutExpired(args, 60)
            rows = [{"Name": name, "Size": size} for name, size in self.volumes.items()]
            return json.dumps({"Images": [], "Volumes": rows})
        raise AssertionError(args)

    def _remove_or_count_layers(self, args: list[str]) -> str:
        if args[:2] == ["image", "inspect"]:
            return f"{self.layers.get(args[-1], 1)}\n"
        self.removals.append(args[-1])
        if args[0] == "volume":
            if self.fail_volume_rm:
                raise subprocess.TimeoutExpired(args, 30)
            del self.volumes[args[-1]]
            return ""
        assert args[2] == "--no-prune"
        image = next(i for i, ref, _size in self.images if ref == args[-1])
        self.images = [row for row in self.images if row[1] != args[-1]]
        if all(ref.endswith("<none>") for i, ref, _size in self.images if i == image):
            # Docker reclaims the bytes once their last tag goes.
            self.images = [row for row in self.images if row[0] != image]
        return ""


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
    image_lifecycle.OwnedImages(Path(root)).record({}, (_id("5"),))
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


def test_leftovers_reads_records_of_a_root_since_replaced_by_a_symlink(
    tmp_path: Path, host: FakeHost
) -> None:
    root = tmp_path / "chip"
    root.mkdir()
    image_lifecycle.OwnedImages(root).record({}, (_id("5"),))
    root.rmdir()
    (tmp_path / "elsewhere").mkdir()
    root.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    host.images = [(_id("5"), "<none>:<none>", "4.9GB")]
    assert project_resources.leftovers(str(tmp_path / "chip")) == (
        Leftover("image", _id("5"), "unused", "4.9GB"),
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


def _purge_host(host: FakeHost, root: str, other: str, monkeypatch: pytest.MonkeyPatch) -> None:
    family = project_image_name_for_identity(root)
    host.images = [
        (_id("1"), f"{family}-standard-substrate:latest", "5GB"),
        (_id("2"), f"{family}:latest", "5.1GB"),
        (_id("2"), keeper_image_for_identity(root), "5.1GB"),
        (_id("3"), f"{family}-wheel:latest", "1GB"),
        (_id("3"), "mirror/wheel:copy", "1GB"),
        (_id("4"), f"{family}-layout-x:latest", "5.2GB"),
        (_id("5"), "<none>:<none>", "4.9GB"),
        (_id("6"), f"{family}:<none>", "4.8GB"),
        (_id("7"), f"{project_image_name_for_identity(other)}:latest", "5GB"),
    ]
    host.layers = {_id("1"): 10, _id("2"): 12, _id("3"): 3, _id("4"): 13}
    image_lifecycle.OwnedImages(Path(root)).record({}, (_id("5"),))
    used = f"booley-codex-state-{project_name_id(root)}"
    idle = f"booley-claude-state-{project_name_id(root)}"
    host.volumes = {
        used: "1MB",
        idle: "2MB",
        f"booley-codex-state-{project_name_id(other)}": "3MB",
    }
    host.containers = {"c" * 64: (_id("4"), (used,), project_label_id(root))}

    def cleanup(project: str) -> tuple[str, ...]:
        assert project == root  # the exact stored identity, never re-resolved
        host.images = [row for row in host.images if row[0] != _id("5")]
        return ()

    monkeypatch.setattr(project_resources, "cleanup_owned_images", cleanup)


def test_purge_removes_only_unused_resources_proven_to_be_the_projects(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, other = str(tmp_path / "chip"), str(tmp_path / "other")
    family = project_image_name_for_identity(root)
    _purge_host(host, root, other, monkeypatch)

    result = project_resources.purge(root)
    removed = result.removed

    # Children before parents; the in-use layout image and its volume stay.
    assert removed == (
        keeper_image_for_identity(root),
        f"{family}:latest",
        f"{family}-standard-substrate:latest",
        f"{family}-wheel:latest",
        _id("5"),
        f"booley-claude-state-{project_name_id(root)}",
    )
    assert host.removals == [*removed[:4], removed[5]]
    # The mirrored wheel bytes survive their Project tag and are named as kept.
    assert result.kept == (f"{_id('3')}: bytes kept (another image or tag still needs them)",)
    assert {row[0] for row in host.images} == {_id("3"), _id("4"), _id("6"), _id("7")}
    # The mirrored wheel bytes are no longer the Project's once its own tag is gone.
    assert [(item.name, item.reason) for item in project_resources.leftovers(root)] == [
        (_id("4"), "in-use"),
        (_id("6"), "unrecorded"),
        (f"booley-codex-state-{project_name_id(root)}", "in-use"),
        ("c" * 64, "labelled"),
    ]


def test_purge_failure_names_progress_and_a_rerun_finishes(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(tmp_path / "chip")
    _purge_host(host, root, str(tmp_path / "other"), monkeypatch)
    host.fail_volume_rm = True
    with pytest.raises(RuntimeError, match=r"already removed: .*sha256:5+; run the purge again"):
        project_resources.purge(root)
    host.fail_volume_rm = False
    assert project_resources.purge(root).removed == (
        f"booley-claude-state-{project_name_id(root)}",
    )


def test_purge_fails_when_a_recorded_image_removal_is_refused(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(tmp_path / "chip")
    _purge_host(host, root, str(tmp_path / "other"), monkeypatch)
    refusal = f"{_id('5')}: deferred (daemon refused)"
    monkeypatch.setattr(project_resources, "cleanup_owned_images", lambda _root: (refusal,))
    with pytest.raises(RuntimeError, match=r"daemon refused.*run the purge again"):
        project_resources.purge(root)


def test_retained_record_of_a_replaced_root_stays_readable(tmp_path: Path, host: FakeHost) -> None:
    from tests.harness.test_image_lifecycle import FakeDocker

    root = tmp_path / "chip"
    root.mkdir()
    image_lifecycle.OwnedImages(root).record({}, (_id("5"), _id("6")))
    root.rmdir()
    (tmp_path / "elsewhere").mkdir()
    root.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    docker = FakeDocker({_id("5"): (_id("5"), {}), _id("6"): (_id("6"), {})})
    docker.used_image_ids = frozenset({_id("6")})

    kept = image_lifecycle.cleanup_owned_images(str(root), docker=docker)

    assert kept == (f"{_id('6')}: retained (conflict: unable to delete {_id('6')})",)
    assert image_lifecycle.OwnedImages(str(root)).ids == [_id("6")]
