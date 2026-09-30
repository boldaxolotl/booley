"""Boundary tests for exporting the embedded base package inventory (#829).

A fake ``docker`` executable on PATH stands in for the daemon, so these tests
exercise the real subprocess, tar-stream, timeout, and cleanup paths.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/image_package_inventory.py"
SPEC = importlib.util.spec_from_file_location("image_package_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)
inventory = exporter.inventory

IMAGE_ID = "sha256:" + "a" * 64
BASENAME = "base-package-inventory.json"

_FAKE_DOCKER = r"""#!{python}
import json, os, sys, time
from pathlib import Path
state = Path(os.environ["FAKE_DOCKER_STATE"])
with (state / "calls.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
command = sys.argv[1]
if command == "image":
    sys.stdout.write((state / "inspect.json").read_text())
elif command == "create":
    print("container-123")
elif command == "cp":
    mode = (state / "cp-mode").read_text() if (state / "cp-mode").exists() else "ok"
    if mode == "fail":
        sys.stderr.write("no such file\n")
        sys.exit(1)
    if mode == "stall":
        time.sleep(30)
    sys.stdout.buffer.write((state / "archive.tar").read_bytes())
elif command == "rm":
    pass
elif command == "run":
    sys.exit(int((state / "run-rc").read_text()) if (state / "run-rc").exists() else 0)
"""


def _inventory_bytes(version: str = "5.2.21-2ubuntu4") -> bytes:
    document = inventory.build_inventory(
        [inventory.Package("bash", version, "amd64")], "3.13.15", "26.2.1"
    )
    return inventory.serialize(document)


def _tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for info, data in members:
            bundle.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _file(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _special(name: str, kind: bytes, target: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


class FakeDocker:
    def __init__(self, root: Path) -> None:
        self.state = root / "state"
        self.state.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(_FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.bin_dir = bin_dir
        self.inspect(label=inventory.INVENTORY_PATH)
        self.archive(_tar(_file(BASENAME, _inventory_bytes())))

    def inspect(self, *, label: str | None, image_id: str = IMAGE_ID) -> None:
        labels = {} if label is None else {inventory.LABEL: label}
        row = {
            "Id": image_id,
            "RepoDigests": ["ghcr.io/acme/base@sha256:" + "b" * 64],
            "Config": {"Labels": labels},
        }
        (self.state / "inspect.json").write_text(json.dumps([row]), encoding="utf-8")

    def archive(self, data: bytes) -> None:
        (self.state / "archive.tar").write_bytes(data)

    def set(self, name: str, value: str) -> None:
        (self.state / name).write_text(value, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        text = (self.state / "calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker(tmp_path)
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(fake.state))
    return fake


def _run(tmp_path: Path, *extra: str) -> tuple[int, dict[str, Any], Path]:
    output = tmp_path / "out" / "inventory.json"
    evidence = tmp_path / "out" / "evidence.json"
    status = exporter.main(
        ["--image", "booley-test", "--output", str(output), "--evidence", str(evidence), *extra]
    )
    return status, json.loads(evidence.read_text(encoding="utf-8")), output


def _assert_cleaned_up(docker: FakeDocker) -> None:
    assert ["rm", "--force", "container-123"] in docker.calls()


def test_exports_exact_bytes_bound_to_the_immutable_image_id(
    docker: FakeDocker, tmp_path: Path
) -> None:
    status, evidence, output = _run(tmp_path, "--base-reference", "ghcr.io/acme/base@sha256:c")

    assert status == 0, evidence["errors"]
    assert output.read_bytes() == _inventory_bytes()
    assert evidence["status"] == "passed"
    assert evidence["image_id"] == IMAGE_ID
    assert evidence["requested_reference"] == "booley-test"
    assert evidence["base_reference"] == "ghcr.io/acme/base@sha256:c"
    assert evidence["package_count"] == 1
    assert (evidence["python_version"], evidence["pip_version"]) == ("3.13.15", "26.2.1")
    calls = docker.calls()
    create = next(call for call in calls if call[0] == "create")
    assert create[-1] == IMAGE_ID  # never the mutable requested reference
    assert "--entrypoint" in create and "start" not in [call[0] for call in calls]
    assert ["cp", f"container-123:{inventory.INVENTORY_PATH}", "-"] in calls
    _assert_cleaned_up(docker)


def test_exported_bytes_are_not_reserialized(docker: FakeDocker, tmp_path: Path) -> None:
    compact = json.dumps(json.loads(_inventory_bytes()), sort_keys=True).encode()
    docker.archive(_tar(_file(BASENAME, compact)))

    status, _, output = _run(tmp_path)

    assert status == 0
    assert output.read_bytes() == compact


@pytest.mark.parametrize("label", [None, "/etc/passwd"])
def test_absent_or_wrong_label_fails_before_any_copy(
    docker: FakeDocker, tmp_path: Path, label: str | None
) -> None:
    docker.inspect(label=label)

    status, evidence, output = _run(tmp_path)

    assert status == 1
    assert inventory.LABEL in evidence["errors"][0]
    assert not output.exists()
    assert [call[0] for call in docker.calls()] == ["image"]


def test_corrupt_inventory_is_retained_for_diagnosis_and_fails(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.archive(_tar(_file(BASENAME, b'{"schema": true}')))

    status, evidence, output = _run(tmp_path)

    assert status == 1
    assert not output.exists()
    rejected = Path(evidence["rejected_output"])
    assert rejected.read_bytes() == b'{"schema": true}'
    assert evidence["sha256"] and evidence["status"] == "failed"
    _assert_cleaned_up(docker)


def test_copy_failure_yields_evidence_without_inventory(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.set("cp-mode", "fail")

    status, evidence, output = _run(tmp_path)

    assert status == 1
    assert "docker cp exited 1" in evidence["errors"][0]
    assert not output.exists() and "rejected_output" not in evidence
    _assert_cleaned_up(docker)


def test_stalled_copy_is_killed_and_cleaned_up(
    docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker.set("cp-mode", "stall")
    monkeypatch.setattr(exporter, "COPY_TIMEOUT_SECONDS", 1)

    status, evidence, _ = _run(tmp_path)

    assert status == 1
    assert "exceeded" in evidence["errors"][0]
    _assert_cleaned_up(docker)


def test_parent_inventory_mismatch_fails(docker: FakeDocker, tmp_path: Path) -> None:
    parent = tmp_path / "parent.json"
    parent.write_bytes(_inventory_bytes(version="5.2.21-2ubuntu5"))

    status, evidence, output = _run(tmp_path, "--expected-inventory", str(parent))

    assert status == 1
    assert evidence["expected_inventory"]["matches"] is False
    assert output.read_bytes() == _inventory_bytes()  # inherited bytes still retained


def test_parent_inventory_match_passes(docker: FakeDocker, tmp_path: Path) -> None:
    parent = tmp_path / "parent.json"
    parent.write_bytes(_inventory_bytes())

    status, evidence, _ = _run(tmp_path, "--expected-inventory", str(parent))

    assert status == 0
    assert evidence["expected_inventory"]["matches"] is True


@pytest.mark.parametrize("run_rc", ["0", "1"])
def test_current_state_verification_runs_the_installed_helper_offline(
    docker: FakeDocker, tmp_path: Path, run_rc: str
) -> None:
    docker.set("run-rc", run_rc)

    status, evidence, _ = _run(tmp_path, "--verify-current-state")

    run = next(call for call in docker.calls() if call[0] == "run")
    assert run[run.index("--network") + 1] == "none"
    assert IMAGE_ID in run and inventory.HELPER_PATH in run and "-I" in run
    assert status == int(run_rc)
    assert evidence.get("current_state_verified") is (True if run_rc == "0" else None)


# --- hostile archives -------------------------------------------------------


def _hostile_archives(tmp_path: Path) -> dict[str, bytes]:
    sentinel = tmp_path / "host-secret"
    sentinel.write_text("host secret", encoding="utf-8")
    good = _file(BASENAME, _inventory_bytes())
    return {
        "symlink": _tar(_special(BASENAME, tarfile.SYMTYPE, str(sentinel))),
        "hardlink": _tar(_special(BASENAME, tarfile.LNKTYPE, str(sentinel))),
        "directory": _tar(_special(BASENAME, tarfile.DIRTYPE)),
        "device": _tar(_special(BASENAME, tarfile.CHRTYPE)),
        "fifo": _tar(_special(BASENAME, tarfile.FIFOTYPE)),
        "extra-member": _tar(good, _file("other.json", b"{}")),
        "traversal": _tar(_file(f"../{BASENAME}", _inventory_bytes())),
        "nested": _tar(_file(f"booley/{BASENAME}", _inventory_bytes())),
        "empty": _tar(),
        "not-a-tar": b"definitely not a tar archive" * 40,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "hardlink",
        "directory",
        "device",
        "fifo",
        "extra-member",
        "traversal",
        "nested",
        "empty",
        "not-a-tar",
    ],
)
def test_hostile_archives_are_rejected_without_touching_host_paths(
    docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    docker.archive(_hostile_archives(tmp_path)[kind])
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    opened: list[str] = []
    real_open: Callable[..., Any] = io.open

    def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)

    status, evidence, output = _run(tmp_path)

    assert status == 1
    assert evidence["errors"]
    assert not output.exists()
    assert str(tmp_path / "host-secret") not in opened
    assert "host secret" not in json.dumps(evidence)
    assert not (tmp_path / BASENAME).exists()  # traversal target
    assert list(work.iterdir()) == []  # nothing extracted into the cwd
    _assert_cleaned_up(docker)


@pytest.mark.parametrize("member_bytes", [inventory.MAX_INVENTORY_BYTES + 1])
def test_oversized_member_is_rejected(
    docker: FakeDocker, tmp_path: Path, member_bytes: int
) -> None:
    docker.archive(_tar(_file(BASENAME, b" " * member_bytes)))

    status, evidence, _ = _run(tmp_path)

    assert status == 1
    assert "bytes" in evidence["errors"][0]


def test_oversized_archive_stream_is_cut_off(docker: FakeDocker, tmp_path: Path) -> None:
    docker.archive(b"\0" * (exporter.MAX_ARCHIVE_BYTES + 1024))

    status, evidence, _ = _run(tmp_path)

    assert status == 1
    assert f"exceeds {exporter.MAX_ARCHIVE_BYTES}" in evidence["errors"][0]
    _assert_cleaned_up(docker)
