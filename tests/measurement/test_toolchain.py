"""The toolchain fingerprint: recorded on release and development installs, never keyed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

DIGEST = "sha256:" + "ab" * 32


def _fake_tools(monkeypatch: pytest.MonkeyPatch, found: dict[str, str]) -> None:
    from booley.measurement import toolchain

    monkeypatch.setattr(toolchain.shutil, "which", found.get)

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/bin/broken":
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0, f"\n{command[0]} 1.2.3\nsecond\n", "")

    monkeypatch.setattr(toolchain.subprocess, "run", run)


def test_fingerprint_on_release_and_development_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import toolchain
    from booley.runtime.execution_identity import IdentityError

    _fake_tools(monkeypatch, {"yosys": "/bin/yosys", "verilator": "/bin/broken"})
    environ = {"BOOLEY_SANDBOX_IMAGE_ID": "sha256:image"}
    monkeypatch.setattr(toolchain, "booley_build_identity", lambda: DIGEST)

    release = toolchain.collect_toolchain_fingerprint(environ)

    assert (release.image, release.booley, release.booley_kind, release.booley_note) == (
        "sha256:image",
        DIGEST,
        "build",
        None,
    )
    assert release.tools == {"yosys": "/bin/yosys 1.2.3", "verilator": "unknown"}

    package = tmp_path / "site" / "booley"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"VALUE = 1\n")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.pyc").write_bytes(b"noise")
    sources: list[str] = [str(tmp_path / "site")]

    def refuse() -> str:
        raise IdentityError("no RECORD for booley")

    monkeypatch.setattr(toolchain, "booley_build_identity", refuse)
    monkeypatch.setattr(
        toolchain,
        "booley_source_identity",
        lambda: __import__(
            "booley.runtime.execution_identity", fromlist=["x"]
        ).booley_source_identity(sources),
    )

    development = toolchain.collect_toolchain_fingerprint({})
    (package / "__pycache__" / "x.pyc").write_bytes(b"other noise")
    assert toolchain.collect_toolchain_fingerprint({}).booley == development.booley
    (package / "__init__.py").write_bytes(b"VALUE = 2\n")
    changed = toolchain.collect_toolchain_fingerprint({})

    assert development.image is None
    assert development.booley_kind == "source"
    assert development.booley_note == "no RECORD for booley"
    assert development.booley.startswith("sha256:")
    assert changed.booley != development.booley


def test_source_identity_reports_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.execution_identity import IdentityError, booley_source_identity

    package = tmp_path / "site" / "booley"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"x = 1\n")

    def unreadable(_self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    with pytest.raises(IdentityError, match="cannot hash booley sources"):
        booley_source_identity([str(tmp_path / "site")])
