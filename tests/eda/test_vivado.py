"""Built-in Vivado installation policy tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.eda import vivado

_REAL_HOST_ARCHITECTURE = vivado._host_architecture


@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "Xilinx" / "2025.2"
    launcher = root / "Vivado" / "bin" / "vivado"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    (root / "tps").mkdir()
    monkeypatch.setattr(vivado, "_detect_version", lambda _launcher: "2025.2")
    monkeypatch.setattr(vivado, "_host_architecture", lambda: "linux-x86_64")
    return root


def test_inspection_uses_observed_identity_and_exact_layout(release: Path) -> None:
    observed = vivado.inspect_installation(release)
    assert observed == vivado.Inspection(release.resolve(), "2025.2", "linux-x86_64")


@pytest.mark.parametrize("bad", [Path("relative/2025.2"), Path("/"), Path("/tmp/with,comma")])
def test_rejects_relative_broad_and_mount_grammar_sources(bad: Path) -> None:
    with pytest.raises(vivado.VivadoPolicyError):
        vivado.inspect_installation(bad)


def test_rejects_missing_release_sibling(release: Path) -> None:
    (release / "tps").rmdir()
    with pytest.raises(vivado.VivadoPolicyError, match="tps"):
        vivado.inspect_installation(release)


def test_rejects_non_executable_launcher(release: Path) -> None:
    launcher = release / "Vivado" / "bin" / "vivado"
    launcher.chmod(0o644)
    with pytest.raises(vivado.VivadoPolicyError, match="executable"):
        vivado.inspect_installation(release)


def test_rejects_project_overlap(release: Path) -> None:
    project = release / "project"
    project.mkdir()
    with pytest.raises(vivado.VivadoPolicyError, match="overlaps"):
        vivado.inspect_installation(release, project_root=project)


def test_rejects_unvalidated_version(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vivado, "_detect_version", lambda _launcher: "2024.2")
    with pytest.raises(vivado.VivadoPolicyError, match="not validated"):
        vivado.inspect_installation(release)


def test_rejects_non_linux_x86_64_with_future_support_message(
    release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vivado.sys, "platform", "win32")
    monkeypatch.setattr(vivado.platform, "machine", lambda: "AMD64")
    with pytest.raises(vivado.VivadoPolicyError, match=r"Linux x86-64 only.*future"):
        _REAL_HOST_ARCHITECTURE()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit policy")
def test_wrapper_is_shell_owned_and_scopes_preload() -> None:
    content = vivado.wrapper_path().read_text(encoding="utf-8")
    assert "settings64.sh" not in content
    assert "exec env LD_PRELOAD=/lib/x86_64-linux-gnu/libudev.so.1" in content
    assert f"{vivado.CONTAINER_TARGET}/Vivado/bin/vivado" in content
