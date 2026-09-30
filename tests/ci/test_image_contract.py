from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/image_contract.py"
CONTRACT = Path(__file__).parents[2] / ".github/contracts/session-runtime.toml"
SPEC = importlib.util.spec_from_file_location("image_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
image_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(image_contract)


def test_repository_contract_distinguishes_standard_and_riscv_capabilities() -> None:
    standard = image_contract.load_contract(CONTRACT, "standard")
    riscv = image_contract.load_contract(CONTRACT, "riscv")

    assert "gcc" in standard["required_commands"]
    assert "rustc" not in standard["required_commands"]
    assert "/usr/local/cargo" in standard["absent_paths"]
    assert "riscv-none-elf-gcc" not in standard["required_commands"]
    assert "riscv-none-elf-gcc" in riscv["required_commands"]
    assert "riscv32-unknown-elf-g++" in riscv["required_commands"]
    assert "riscv64-unknown-elf-g++" in riscv["required_commands"]
    assert len(riscv["probes"]) > len(standard["probes"])

    multilib_probe = next(
        probe for probe in riscv["probes"] if probe["name"] == "RISC-V multilib C and C++ links"
    )
    for architecture in (
        "rv32i ilp32",
        "rv32im ilp32",
        "rv32imc ilp32",
        "rv32e ilp32e",
        "rv32imaf ilp32f",
        "rv32imafd ilp32d",
        "rv64gc lp64d",
    ):
        assert architecture in multilib_probe["command"]
    assert "riscv32-unknown-elf riscv64-unknown-elf" in multilib_probe["command"]


def test_repository_contract_preserves_slimmed_image_payload() -> None:
    standard = image_contract.load_contract(CONTRACT, "standard")
    riscv = image_contract.load_contract(CONTRACT, "riscv")

    assert "/OpenROAD" in standard["absent_paths"]
    assert (
        "/usr/local/share/doc/openroad/OpenROAD-a9147cf3aebe65e058bb3fa89c1f9e524488dbb8.tar.gz"
        in standard["required_paths"]
    )
    assert (
        "/opt/agent-clis/node_modules/@anthropic-ai/claude-code-linux-x64-musl"
        in standard["absent_paths"]
    )
    for path in (
        "/usr/local/bin/iverilog",
        "/usr/local/bin/verilator_bin",
        "/usr/local/bin/verilator_bin_dbg",
        "/usr/local/bin/verible-verilog-lint",
    ):
        assert path in standard["stripped_elf"]
    for path in (
        "/opt/riscv/bin/spike",
        "/opt/riscv/bin/spike-log-parser",
        "/opt/riscv/bin/termios-xspike",
        "/opt/riscv/bin/xspike",
        "/opt/riscv/lib/libcustomext.so",
        "/opt/riscv/lib/libriscv.so",
    ):
        assert path in riscv["stripped_elf"]
    assert any(probe["name"] == "xPack duplicate files share storage" for probe in riscv["probes"])
    spike_probe = next(
        probe
        for probe in riscv["probes"]
        if probe["name"] == "Spike arithmetic reference and extlib loading"
    )
    assert "SPIKE_EXTLIB_MARKER" in spike_probe["command"]
    assert "--extlib=" in spike_probe["command"]
    assert "li t1, 61" in spike_probe["command"]


def test_contract_rejects_single_path_hard_link_group(tmp_path: Path) -> None:
    contract = tmp_path / "contract.toml"
    contract.write_text(
        'schema = 1\n[common]\nhard_link_groups = [["/one"]]\n[standard]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at least two paths"):
        image_contract.load_contract(contract, "standard")


def test_contract_rejects_boolean_schema_version(tmp_path: Path) -> None:
    contract = tmp_path / "contract.toml"
    contract.write_text("schema = true\n[common]\n[standard]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime contract schema"):
        image_contract.load_contract(contract, "standard")


@pytest.mark.parametrize("timeout", [True, 0, -1, "5"])
def test_contract_rejects_invalid_probe_timeout(tmp_path: Path, timeout: object) -> None:
    contract = tmp_path / "contract.toml"
    contract.write_text(
        "schema = 1\n[common]\n[[common.probe]]\n"
        f'name = "probe"\ncommand = "true"\ntimeout_seconds = {json.dumps(timeout)}\n'
        "[standard]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        image_contract.load_contract(contract, "standard")


@pytest.mark.skipif(sys.platform == "win32", reason="Sandbox Image probe requires Linux bash")
def test_container_probe_records_absence_hard_links_and_behavior(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("same bytes", encoding="utf-8")
    os.link(first, second)
    contract = {
        "required_commands": ["sh"],
        "required_paths": [str(first)],
        "absent_paths": [str(tmp_path / "absent")],
        "stripped_elf": [],
        "hard_link_groups": [[str(first), str(second)]],
        "probes": [{"name": "shell", "command": "test 2 -eq 2", "timeout_seconds": 5}],
    }

    result = subprocess.run(
        [sys.executable, "-c", image_contract._CONTAINER_PROBE],
        input=json.dumps(contract),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert report["errors"] == []
    assert report["hard_links"][0]["same_inode"] is True
    assert report["absent_paths"] == [{"path": str(tmp_path / "absent"), "absent": True}]
    assert report["probes"][0]["returncode"] == 0


def test_container_probe_reports_missing_hard_link_without_losing_evidence(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.write_text("bytes", encoding="utf-8")
    missing = tmp_path / "missing"
    contract = {
        "required_commands": [],
        "required_paths": [],
        "absent_paths": [],
        "stripped_elf": [],
        "hard_link_groups": [[str(missing), str(first)]],
        "probes": [],
    }

    result = subprocess.run(
        [sys.executable, "-c", image_contract._CONTAINER_PROBE],
        input=json.dumps(contract),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert report["hard_links"][0]["same_inode"] is False
    assert any("hard-link path is unavailable" in error for error in report["errors"])


def test_layer_contract_requires_exact_base_diff_id_prefix() -> None:
    base = {
        "reference": "base",
        "image_id": "sha256:base",
        "rootfs_diff_ids": ["one", "two"],
    }
    child = {"rootfs_diff_ids": ["one", "two", "three"]}
    wrong = {"rootfs_diff_ids": ["one", "different", "three"]}

    assert image_contract._layer_contract(child, base) == {
        "base_reference": "base",
        "base_image_id": "sha256:base",
        "prefix_match": True,
        "additional_layer_count": 1,
    }
    assert image_contract._layer_contract(wrong, base)["prefix_match"] is False


def test_validate_keeps_probe_and_layer_failures_in_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identities = {
        "candidate": {
            "reference": "candidate",
            "image_id": "sha256:candidate",
            "os": "linux",
            "architecture": "amd64",
            "rootfs_diff_ids": ["wrong", "child"],
        },
        "base": {
            "reference": "base",
            "image_id": "sha256:base",
            "os": "linux",
            "architecture": "amd64",
            "rootfs_diff_ids": ["base"],
        },
    }
    monkeypatch.setattr(
        image_contract, "load_contract", lambda *_args: {"hard_link_groups": [["/a", "/b"]]}
    )
    monkeypatch.setattr(image_contract, "_image_identity", identities.__getitem__)
    monkeypatch.setattr(
        image_contract,
        "_probe_image",
        lambda *_args: {"errors": ["missing command"]},
    )
    monkeypatch.setattr(
        image_contract,
        "_layer_link_audit",
        lambda *_args: ([{"path": "/a"}], ["hard-link group is carried by 2 exported layers"]),
    )
    contract = tmp_path / "contract.toml"
    contract.write_text("schema = 1\n", encoding="utf-8")

    evidence = image_contract.validate("candidate", "riscv", contract, "base")

    assert evidence["layer_links"] == [{"path": "/a"}]
    assert evidence["errors"] == [
        "missing command",
        "hard-link group is carried by 2 exported layers",
        "derived image RootFS layers do not prefix-match the standard image",
    ]


@pytest.mark.skipif(
    sys.platform != "linux", reason="Coreutils contract requires Linux GNU commands"
)
@pytest.mark.parametrize("command", ["stat", "date", "sort", "cp"])
def test_coreutils_contract_rejects_non_gnu_default(tmp_path: Path, command: str) -> None:
    contract = image_contract.load_contract(CONTRACT, "standard")
    probe = next(
        probe
        for probe in contract["probes"]
        if probe["name"] == "GNU coreutils defaults and shell compatibility"
    )
    # An explicit environment excludes inherited BASH_ENV and other startup hooks.
    default_path = f"/usr/local/bin{os.pathsep}{os.defpath}"
    environment = {"PATH": default_path, "HOME": str(tmp_path), "LANG": "C", "LC_ALL": "C"}
    identity = subprocess.run(
        ["stat", "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if "GNU coreutils" not in identity.stdout:
        pytest.skip("GNU coreutils host prerequisite unavailable; run in the Sandbox Image")
    marker = f"non-GNU replacement: {command}"
    for replace_command in (False, True):
        if replace_command:
            replacement = tmp_path / command
            replacement.write_text(
                f"#!/bin/sh\nprintf '%s\\n' '{marker}' >&2\n"
                "printf '%s\\n' 'uutils coreutils 0.8.0'\n",
                encoding="utf-8",
            )
            replacement.chmod(0o755)
            environment["PATH"] = f"{tmp_path}{os.pathsep}{default_path}"
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", probe["command"]],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if replace_command:
            assert result.returncode != 0, result.stdout
            assert marker in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stderr


# --- exported-layer hard-link audit (issue #828) ---------------------------------

LIBEXEC = "usr/local/libexec/booley/bwave"
PACKAGE = "usr/lib/python3/dist-packages/booley/data/bin/bwave"
GROUP = [[f"/{LIBEXEC}", f"/{PACKAGE}"]]

# An entry is (name, kind, linkname); kind is file, hardlink or symlink.
Entry = tuple[str, str, str]
FILE_LIBEXEC: Entry = (LIBEXEC, "file", "")
FILE_PACKAGE: Entry = (PACKAGE, "file", "")
LINK_PACKAGE: Entry = (PACKAGE, "hardlink", LIBEXEC)


def _layer_tar(entries: list[Entry], compression: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, kind, linkname in entries:
            info = tarfile.TarInfo(f"./{name}" if kind != "symlink" else name)
            if kind == "file":
                data = b"payload"
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.type = tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE
                info.linkname = linkname
                tar.addfile(info)
    raw = buffer.getvalue()
    if compression == "gzip":
        return gzip.compress(raw)
    if compression == "zstd":
        from compression import zstd

        return zstd.compress(raw)
    return raw


def _archive(layers: list[tuple[list[Entry], str]], *, legacy: bool = False) -> io.BytesIO:
    """A `docker save` stream: layers are (entries, compression) lowest first."""
    outer = io.BytesIO()
    names: list[str] = []
    with tarfile.open(fileobj=outer, mode="w") as tar:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        for index, (entries, compression) in enumerate(layers):
            name = f"{index}/layer.tar" if legacy else f"blobs/sha256/{index:064x}"
            add(name, _layer_tar(entries, compression))
            names.append(name)
        if legacy:
            # Legacy docker-archive dedupe: a manifest layer that is a symlink.
            alias = tarfile.TarInfo("alias/layer.tar")
            alias.type = tarfile.SYMTYPE
            alias.linkname = "../0/layer.tar"
            tar.addfile(alias)
            names[0] = "alias/layer.tar"
        add("blobs/sha256/" + "f" * 64, b'{"config": "not a layer"}')
        add("manifest.json", json.dumps([{"Config": "cfg", "Layers": names}]).encode())
    outer.seek(0)
    return outer


def _audit(layers: list[tuple[list[Entry], str]], **kwargs: bool) -> tuple[list[dict], list[str]]:
    return image_contract.audit_layer_links(_archive(layers, **kwargs), GROUP)


def test_layer_audit_accepts_group_carried_by_one_layer() -> None:
    rows, errors = _audit(
        [([("etc/os-release", "file", "")], "plain"), ([FILE_LIBEXEC, LINK_PACKAGE], "gzip")]
    )

    assert errors == []
    assert {row["type"] for row in rows} == {"file", "hardlink"}
    assert {row["layer"] for row in rows} == {rows[0]["layer"]}


def test_layer_audit_rejects_containerd_split_group() -> None:
    _, errors = _audit([([FILE_LIBEXEC], "gzip"), ([FILE_PACKAGE], "gzip")])

    assert len(errors) == 1
    assert "carried by 2 exported layers (expected 1)" in errors[0]


def test_layer_audit_rejects_classic_copy_up_split_group() -> None:
    _, errors = _audit([([FILE_LIBEXEC], "plain"), ([FILE_LIBEXEC, LINK_PACKAGE], "plain")])

    assert len(errors) == 1
    assert "carried by 2 exported layers (expected 1)" in errors[0]


def test_layer_audit_rejects_hardlink_to_a_different_file() -> None:
    other: Entry = (PACKAGE, "hardlink", "usr/bin/other")

    _, errors = _audit([([FILE_LIBEXEC, other], "gzip")])

    assert len(errors) == 1
    assert "not one tar hard-link set in exported layer 0" in errors[0]


def test_layer_audit_rejects_symlink_at_group_path() -> None:
    _, errors = _audit([([FILE_LIBEXEC, (PACKAGE, "symlink", LIBEXEC)], "gzip")])

    assert len(errors) == 1
    assert "not one tar hard-link set" in errors[0]


def test_layer_audit_rejects_two_regular_files_in_one_layer() -> None:
    _, errors = _audit([([FILE_LIBEXEC, FILE_PACKAGE], "gzip")])

    assert len(errors) == 1
    assert "not one tar hard-link set" in errors[0]


def test_layer_audit_rejects_group_missing_everywhere() -> None:
    _, errors = _audit([([("etc/os-release", "file", "")], "gzip")])

    assert len(errors) == 1
    assert "carried by 0 exported layers (expected 1)" in errors[0]


def test_layer_audit_fails_closed_on_unreadable_layer() -> None:
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w") as tar:
        for name, data in (
            ("blobs/sha256/aa", b"this is not a tar archive" * 50),
            ("manifest.json", json.dumps([{"Layers": ["blobs/sha256/aa"]}]).encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    outer.seek(0)

    _, errors = image_contract.audit_layer_links(outer, GROUP)

    assert "exported layer is unreadable: blobs/sha256/aa" in errors


def test_layer_audit_resolves_legacy_symlinked_layer_member() -> None:
    _, errors = _audit([([FILE_LIBEXEC, LINK_PACKAGE], "plain")], legacy=True)

    assert errors == []


@pytest.mark.skipif(sys.version_info < (3, 14), reason="zstd needs compression.zstd")
def test_layer_audit_decodes_zstd_layers() -> None:
    _, errors = _audit([([FILE_LIBEXEC, LINK_PACKAGE], "zstd")])

    assert errors == []


def test_layer_audit_names_zstd_when_it_cannot_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_contract, "_zstd", None)
    outer = io.BytesIO()
    layer = b"\x28\xb5\x2f\xfd" + b"\0" * 64
    with tarfile.open(fileobj=outer, mode="w") as tar:
        for name, data in (
            ("blobs/sha256/bb", layer),
            ("manifest.json", json.dumps([{"Layers": ["blobs/sha256/bb"]}]).encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    outer.seek(0)

    _, errors = image_contract.audit_layer_links(outer, GROUP)

    assert errors == [
        "exported layer blobs/sha256/bb is zstd-compressed; auditing it needs Python >= 3.14"
    ]


def test_layer_audit_requires_exactly_one_manifest_entry() -> None:
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w") as tar:
        data = b"[]"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    outer.seek(0)

    _, errors = image_contract.audit_layer_links(outer, GROUP)

    assert errors == ["exported archive must describe exactly one image (manifest.json has 0)"]


# The adapter runs a fake `docker` from PATH so no daemon is involved.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake docker is a shell script")


def _fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    script = tmp_path / "bin" / "docker"
    script.parent.mkdir()
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")


def _non_daemon_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]


@posix_only
def test_layer_link_audit_returns_promptly_and_leaves_no_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "save.tar"
    fixture.write_bytes(_archive([([FILE_LIBEXEC, LINK_PACKAGE], "gzip")]).getvalue())
    _fake_docker(tmp_path, monkeypatch, f'cat "{fixture}"')
    monkeypatch.setattr(image_contract, "_DOCKER_SAVE_DEADLINE_SECONDS", 30)
    before = _non_daemon_threads()

    started = time.monotonic()
    rows, errors = image_contract._layer_link_audit("img", GROUP)

    assert errors == []
    assert rows
    assert time.monotonic() - started < 10
    assert _non_daemon_threads() == before


@posix_only
def test_layer_link_audit_reports_docker_failure_with_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_docker(tmp_path, monkeypatch, "echo 'no such image' >&2; exit 3")

    with pytest.raises(RuntimeError, match="no such image"):
        image_contract._layer_link_audit("img", GROUP)


@posix_only
def test_layer_link_audit_kills_a_hung_docker_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_docker(tmp_path, monkeypatch, "exec sleep 60")
    monkeypatch.setattr(image_contract, "_DOCKER_SAVE_DEADLINE_SECONDS", 1)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="deadline"):
        image_contract._layer_link_audit("img", GROUP)

    assert time.monotonic() - started < 30


@posix_only
def test_layer_link_audit_names_a_docker_save_that_closes_output_but_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "save.tar"
    fixture.write_bytes(_archive([([FILE_LIBEXEC, LINK_PACKAGE], "gzip")]).getvalue())
    _fake_docker(tmp_path, monkeypatch, f'cat "{fixture}"; exec 1>&-; exec sleep 60')
    monkeypatch.setattr(image_contract, "_DOCKER_SAVE_DEADLINE_SECONDS", 60)
    monkeypatch.setattr(image_contract, "_DOCKER_SAVE_EXIT_GRACE_SECONDS", 1)

    with pytest.raises(RuntimeError, match="did not exit"):
        image_contract._layer_link_audit("img", GROUP)


@posix_only
def test_layer_link_audit_reaps_child_when_parser_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pid"
    _fake_docker(
        tmp_path,
        monkeypatch,
        f'echo $$ > "{pidfile}"; head -c 4096 /dev/zero | tr "\\0" "x"; exec sleep 60',
    )
    monkeypatch.setattr(image_contract, "_DOCKER_SAVE_DEADLINE_SECONDS", 60)

    with pytest.raises(tarfile.TarError):
        image_contract._layer_link_audit("img", GROUP)

    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
