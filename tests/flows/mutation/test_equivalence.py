"""The build-identity check: two Verilator builds are equal unless their C++ differs.

Every build tree here is made under ``tmp_path`` with real paths, so the tests hold on
Windows too.  The module under test is imported inside each test, so a missing module
fails the test itself.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

_EQUIVALENCE = "booley.flows.mutation.equivalence"


def _generated_cpp(generated: Path, private_root: Path, work_dir: Path) -> dict[str, str] | None:
    return importlib.import_module(_EQUIVALENCE).generated_cpp(generated, private_root, work_dir)


def _build(
    work_dir: Path, name: str, *, body: str = "x = 1;", spelling: str | None = None
) -> tuple[Path, Path]:
    """A Verilator build root shaped like the probe's, under ``builds/<name>``.

    The C++ names the build's private source copy the way ``VL_STOP_MT`` does; the other
    files hold what always differs between two builds (paths, inodes, timestamps).
    Returns the build's private root and its generated directory (the same here).
    """
    root = work_dir / "builds" / name
    (root / "src" / "x").mkdir(parents=True)
    source = f"{spelling if spelling is not None else root}/src/x/dut.sv"
    (root / "Vtb__ver.d").write_text(f"Vtb.cpp: {source} # {name}\n")
    (root / "Vtb__verFiles.dat").write_text(f"T {name}\n")
    (root / "Vtb.mk").write_text(f"VM_ROOT = {root}\n")
    (root / "Vtb___024root__0.cpp").write_text(
        f'void f() {{ VL_STOP_MT("{source}", 11, ""); {body} }}\n'
    )
    (root / "Vtb.h").write_text("class Vtb;\n")
    return root, root


def _work_dir(tmp_path: Path) -> Path:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    return work_dir


def test_private_copy_path_does_not_make_builds_differ(tmp_path: Path) -> None:
    work_dir = _work_dir(tmp_path)
    baseline, _ = _build(work_dir, "baseline")
    mutant, _ = _build(work_dir, "mutant_3")
    relative = _build(work_dir, "mutant_4", spelling="builds/mutant_4")[0]
    posix = _build(work_dir, "mutant_5", spelling=(work_dir / "builds" / "mutant_5").as_posix())[0]

    expected = _generated_cpp(baseline, baseline, work_dir)

    assert expected is not None
    assert set(expected) == {"Vtb___024root__0.cpp", "Vtb.h"}  # only .cpp and .h are read
    for other in (mutant, relative, posix):
        assert _generated_cpp(other, other, work_dir) == expected, other.name


def test_any_other_difference_keeps_the_mutant(tmp_path: Path) -> None:
    work_dir = _work_dir(tmp_path)
    baseline, _ = _build(work_dir, "baseline")
    expected = _generated_cpp(baseline, baseline, work_dir)

    changed_byte, _ = _build(work_dir, "mutant_1", body="x = 2;")
    extra_header, _ = _build(work_dir, "mutant_2")
    (extra_header / "Vtb__Syms.h").write_text("struct Syms;\n")
    nested, _ = _build(work_dir, "mutant_3")
    (nested / "Vblock").mkdir()
    (nested / "Vblock" / "Vblock.cpp").write_text("void block() {}\n")
    outside_path, _ = _build(work_dir, "mutant_4")
    cpp = outside_path / "Vtb___024root__0.cpp"
    cpp.write_text(cpp.read_text().replace("dut.sv", "dut.sv:other/").replace("x/dut", "y/dut"))
    other_root, _ = _build(work_dir, "mutant_5", spelling=str(tmp_path / "elsewhere"))

    for mutant in (changed_byte, extra_header, nested, outside_path, other_root):
        assert _generated_cpp(mutant, mutant, work_dir) != expected, mutant.name


def test_a_mutant_directory_with_the_same_prefix_is_not_folded(tmp_path: Path) -> None:
    work_dir = _work_dir(tmp_path)
    baseline, _ = _build(work_dir, "mutant_1")
    other, _ = _build(work_dir, "mutant_10")
    expected = _generated_cpp(baseline, baseline, work_dir)

    # `.../mutant_10/src` is not `.../mutant_1/src`: only whole directories are replaced.
    assert _generated_cpp(other, other, work_dir) == expected  # each folds its own root
    cpp = other / "Vtb___024root__0.cpp"
    cpp.write_text(cpp.read_text().replace(str(other), str(baseline)))
    assert _generated_cpp(other, other, work_dir) != expected  # a path to the baseline's copy


def test_unrecognized_layout_skips_the_check(tmp_path: Path) -> None:
    work_dir = _work_dir(tmp_path)
    root = work_dir / "builds" / "baseline"
    root.mkdir(parents=True)
    (root / "Vtb.cpp").write_text("void f() {}\n")  # C++, but no `*__ver.d`: not Verilator's

    assert _generated_cpp(root, root, work_dir) is None


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["verilator", "--version"], returncode, stdout, "")


def test_verilator_version_is_the_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, env=kwargs["env"], timeout=kwargs["timeout"])
        return _completed("Verilator 5.052 2025-01-01 rev v5.052\nextra\n")

    monkeypatch.setattr(f"{_EQUIVALENCE}.subprocess.run", fake_run)

    version = importlib.import_module(_EQUIVALENCE).verilator_version({"PATH": "/opt/bin"})

    assert version == "Verilator 5.052 2025-01-01 rev v5.052"
    assert seen["command"] == ["verilator", "--version"]
    assert seen["env"] == {"PATH": "/opt/bin"}
    assert seen["timeout"] > 0


@pytest.mark.parametrize(
    "outcome",
    [
        _completed("", returncode=2),
        _completed("Verilator 5\n", returncode=1),
        _completed(""),
        FileNotFoundError("verilator"),
        subprocess.TimeoutExpired("verilator", 30),
    ],
    ids=["failed", "failed-with-output", "silent", "missing", "timeout"],
)
def test_unreadable_verilator_version_is_unknown(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, subprocess.CompletedProcess)
        return outcome

    monkeypatch.setattr(f"{_EQUIVALENCE}.subprocess.run", fake_run)

    assert importlib.import_module(_EQUIVALENCE).verilator_version({}) == "unknown"
