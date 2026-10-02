"""Built-in Vivado installation policy tests."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from booley.eda.provisioning.policies import vivado

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


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX executable mode bit")
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
    assert "exec /usr/bin/env LD_PRELOAD=/lib/x86_64-linux-gnu/libudev.so.1" in content
    assert f"{vivado.CONTAINER_TARGET}/Vivado/bin/vivado" in content


@pytest.fixture
def wrapper_install(tmp_path: Path) -> tuple[Path, Path, Path]:
    if os.name == "nt":
        pytest.skip("POSIX shell wrapper")
    root = tmp_path / "installation with spaces" / "Vivado"
    (root / "bin").mkdir(parents=True)
    libraries = root / "lib" / "lnx64.o"
    for name in ("18", "20", "22", "24"):
        (libraries / "Ubuntu" / name).mkdir(parents=True)
    launcher = root / "bin" / "vivado"
    launcher.write_text(
        "#!/bin/sh\nexec /usr/bin/python3 -c "
        + shlex.quote(
            "import json,os,sys; print(json.dumps({"
            "'library':os.environ.get('LD_LIBRARY_PATH'),"
            "'preload':os.environ.get('LD_PRELOAD'),'argv':sys.argv[1:]}))"
        )
        + ' "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    selector = root / "bin" / "ldlibpath.sh"
    selector.write_text(
        '#!/bin/sh\nunset LD_LIBRARY_PATH\nprintf "%s/Ubuntu:%s\\n" "$1" "$1"\n', encoding="utf-8"
    )
    selector.chmod(0o755)
    wrapper = tmp_path / "wrapper"
    content = vivado.wrapper_path().read_text(encoding="utf-8")
    fixed = f"{vivado.CONTAINER_TARGET}/Vivado/bin/vivado"
    assert content.count(fixed) == 1
    wrapper.write_text(content.replace(fixed, shlex.quote(str(launcher))), encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper, root, libraries


def _run_wrapper(wrapper: Path, inherited: str | None = "/caller/libs", *args: str):
    environment = os.environ.copy()
    environment.pop("LD_LIBRARY_PATH", None)
    environment["LD_PRELOAD"] = "/caller/preload"
    if inherited is not None:
        environment["LD_LIBRARY_PATH"] = inherited
    return subprocess.run(
        [str(wrapper), *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_wrapper_adds_highest_bundled_ubuntu_library(
    wrapper_install: tuple[Path, Path, Path],
) -> None:
    wrapper, _root, libraries = wrapper_install
    result = _run_wrapper(wrapper)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["library"] == f"{libraries}/Ubuntu/24:/caller/libs"
    assert observed["preload"] == "/lib/x86_64-linux-gnu/libudev.so.1"


@pytest.mark.parametrize("inherited", [None, "", "/caller/libs::/last"])
@pytest.mark.parametrize(
    "selection", ["Ubuntu/24", "Ubuntu/26", "Rhel/9", "Default", "unknown", "failure", "missing"]
)
def test_wrapper_preserves_vendor_selection(
    wrapper_install: tuple[Path, Path, Path], inherited: str | None, selection: str
) -> None:
    wrapper, root, libraries = wrapper_install
    selector = root / "bin" / "ldlibpath.sh"
    if selection == "missing":
        selector.unlink()
    elif selection == "failure":
        selector.write_text("#!/bin/sh\nexit 7\n")
    else:
        selector.write_text(
            "#!/bin/sh\nprintf '%s\\n' "
            + shlex.quote(f"{libraries}/{selection}:{libraries}")
            + "\n"
        )
    observed = json.loads(_run_wrapper(wrapper, inherited).stdout)
    assert observed["library"] == inherited


def test_wrapper_numeric_candidates_and_idempotence(
    wrapper_install: tuple[Path, Path, Path],
) -> None:
    wrapper, _root, libraries = wrapper_install
    ubuntu = libraries / "Ubuntu"
    for name in ("9", "100", "024", "nonnumeric"):
        (ubuntu / name).mkdir()
    (ubuntu / "999").write_text("regular file")
    (ubuntu / "1000").symlink_to(ubuntu / "absent")
    selected = str(ubuntu / "100")
    for inherited, expected in (
        (None, selected),
        ("", selected),
        ("/first::/last", selected + ":/first::/last"),
        (f"/first:{selected}:/last", f"/first:{selected}:/last"),
    ):
        assert json.loads(_run_wrapper(wrapper, inherited).stdout)["library"] == expected
    for directory in ubuntu.iterdir():
        if directory.is_dir() and directory.name.isdecimal():
            directory.rmdir()
    assert json.loads(_run_wrapper(wrapper).stdout)["library"] == "/caller/libs"


def test_wrapper_resolves_launcher_symlink_and_preserves_arguments(
    wrapper_install: tuple[Path, Path, Path],
) -> None:
    wrapper, root, _libraries = wrapper_install
    resolved_root = root.with_name("resolved Vivado")
    root.rename(resolved_root)
    root.symlink_to(resolved_root, target_is_directory=True)
    launcher = root / "bin" / "vivado"
    actual = launcher.with_name("actual-vivado")
    launcher.rename(actual)
    launcher.symlink_to(actual)
    result = _run_wrapper(wrapper, None, "argument with spaces", "*.sv", "")
    assert json.loads(result.stdout)["argv"] == ["argument with spaces", "*.sv", ""]
    assert json.loads(result.stdout)["library"] == str(
        resolved_root / "lib" / "lnx64.o" / "Ubuntu" / "24"
    )
    actual.write_text("#!/bin/sh\nexit 37\n")
    assert _run_wrapper(wrapper).returncode == 37


def test_wrapper_refuses_self_resolution(wrapper_install: tuple[Path, Path, Path]) -> None:
    wrapper, root, _libraries = wrapper_install
    launcher = root / "bin" / "vivado"
    launcher.unlink()
    launcher.symlink_to(wrapper)
    result = _run_wrapper(wrapper)
    assert result.returncode != 0
    assert "resolves to wrapper itself" in result.stderr
