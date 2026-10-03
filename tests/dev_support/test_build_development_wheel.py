"""Contributor wheels select new artifacts and never leave source provenance behind."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime import build_stamp

ROOT = Path(__file__).resolve().parents[2]
WHEEL = "booley_rtl-0.2.15-py3-none-any.whl"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    for tree in build_stamp._DEVELOPMENT_CONTEXT_TREES:
        (root / tree).mkdir(parents=True, exist_ok=True)
    for name in build_stamp._DEVELOPMENT_CONTEXT_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n")
    (root / "src/booley/payload.py").write_text("VALUE = 1\n")
    (root / "VERSION").write_text("0.2.15\n")
    from booley.runtime import image_build_contracts

    monkeypatch.setattr(
        image_build_contracts,
        "source_image_build_contracts",
        lambda _root: SimpleNamespace(runtime_base="a" * 64, standard_substrate="b" * 64),
    )
    return root


def owner():
    return importlib.import_module("booley.dev_support.build_development_wheel")


def wire_frontend(monkeypatch, action):
    module = owner()
    original = subprocess.run

    def run(command, **kwargs):
        if command[1:5] != ["-P", "-m", "build", "--wheel"]:
            return original(command, **kwargs)
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 1800
        assert kwargs["stdout"] is sys.stderr
        assert command[5] == "--outdir"
        action(Path(kwargs["cwd"]), Path(command[6]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    return module


def assert_clean(root):
    assert not build_stamp.stamp_path(root).exists()
    assert not build_stamp.development_context_path(root).exists()
    assert not list((root / "dist").glob(".development-wheel-*"))


def test_explicit_profile_selects_only_new_wheel_and_prints_absolute_path(
    checkout, monkeypatch, capsys
):
    dist = checkout / "dist"
    dist.mkdir()
    stale = dist / "booley_rtl-0.1.0-py3-none-any.whl"
    stale.write_bytes(b"old")
    (dist / WHEEL).write_bytes(b"previous")
    (checkout / "build").mkdir()
    (checkout / "build/stale.py").write_text("stale")

    def build(root, staging):
        namespace = {}
        exec(build_stamp.stamp_path(root).read_text(), namespace)
        assert namespace["OFFICIAL_RELEASE"] is False
        assert len(namespace["DEVELOPMENT_CONTEXT_SHA256"]) == 64
        assert build_stamp.development_context_path(root).is_file()
        assert not (root / "build").exists()
        print("frontend log", file=sys.stderr)
        (staging / WHEEL).write_bytes(b"new")

    module = wire_frontend(monkeypatch, build)
    assert module.main(checkout) == 0
    out = capsys.readouterr()
    assert out.out == str((dist / WHEEL).resolve()) + "\n"
    assert "frontend log" in out.err
    assert (dist / WHEEL).read_bytes() == b"new"
    assert stale.read_bytes() == b"old"
    assert_clean(checkout)


@pytest.mark.parametrize("failure", ["stamp", "build", "timeout", "publish", "cleanup"])
def test_failures_report_no_success_path_and_clean_generated_provenance(
    checkout, monkeypatch, capsys, failure
):
    def build(root, staging):
        if failure == "build":
            raise subprocess.CalledProcessError(17, ["build"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(["build"], 1800)
        (staging / WHEEL).write_bytes(b"new")

    module = wire_frontend(monkeypatch, build)
    (checkout / "dist").mkdir()
    prior = checkout / "dist" / WHEEL
    prior.write_bytes(b"previous")
    if failure == "stamp":

        def partial_stamp(root, **_kwargs):
            build_stamp.stamp_path(root).write_text("partial")
            build_stamp.development_context_path(root).parent.mkdir(exist_ok=True)
            build_stamp.development_context_path(root).write_bytes(b"partial")
            raise OSError("stamp failed")

        monkeypatch.setattr(module, "write_build_stamp", partial_stamp)
    elif failure == "publish":
        original_replace = Path.replace

        def replace(source, target):
            if str(source).endswith(".whl"):
                raise OSError("publish failed")
            return original_replace(source, target)

        monkeypatch.setattr(Path, "replace", replace)
    elif failure == "cleanup":
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == build_stamp.stamp_path(checkout) and path.exists():
                raise PermissionError("cleanup failed")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
    assert module.main(checkout) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "development wheel" in captured.err.lower()
    if failure != "cleanup":
        assert_clean(checkout)
    else:
        assert not build_stamp.development_context_path(checkout).exists()
    assert prior.read_bytes() == b"previous"


@pytest.mark.parametrize("outputs", ["zero", "multiple", "symlink"])
def test_invalid_frontend_outputs_are_rejected(checkout, monkeypatch, outputs):
    def build(_root, staging):
        if outputs == "multiple":
            (staging / WHEEL).write_bytes(b"one")
            (staging / "booley_rtl-0.2.16-py3-none-any.whl").write_bytes(b"two")
        elif outputs == "symlink":
            (staging / WHEEL).symlink_to(checkout / "VERSION")

    module = wire_frontend(monkeypatch, build)
    with pytest.raises(ValueError, match="exactly one"):
        module.build_development_wheel(checkout)
    assert_clean(checkout)


@pytest.mark.parametrize("invalid", ["incomplete", "build-symlink", "dist-symlink"])
def test_validation_precedes_deletion(checkout, tmp_path, invalid):
    protected = tmp_path / "protected"
    protected.mkdir()
    sentinel = protected / "keep"
    sentinel.write_text("keep")
    if invalid == "incomplete":
        (checkout / "README.md").unlink()
        (checkout / "build").mkdir()
        (checkout / "build/keep").write_text("keep")
    else:
        (checkout / ("build" if invalid == "build-symlink" else "dist")).symlink_to(protected)
    with pytest.raises((OSError, ValueError)):
        owner().build_development_wheel(checkout)
    assert sentinel.read_text() == "keep"
    if invalid == "incomplete":
        assert (checkout / "build/keep").read_text() == "keep"


def test_source_launcher_runs_from_unrelated_cwd(tmp_path):
    root = tmp_path / "checkout"
    shutil.copytree(
        ROOT / "src/booley", root / "src/booley", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        ROOT / "crates/bwave", root / "crates/bwave", ignore=shutil.ignore_patterns("target")
    )
    shutil.copytree(ROOT / ".github/scripts", root / ".github/scripts")
    for name in build_stamp._DEVELOPMENT_CONTEXT_FILES:
        if (ROOT / name).is_file():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
    copied_owner = root / "src/booley/dev_support/build_development_wheel.py"
    owner_source = copied_owner.read_text()
    owner_source = owner_source.replace(
        "    try:\n        wheel = build_development_wheel(checkout)",
        '    (checkout.parent / "source-owner-evidence").write_text(str(Path(__file__).resolve()))\n'
        "    try:\n        wheel = build_development_wheel(checkout)",
    )
    copied_owner.write_text(owner_source)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "build.py").write_text(
        "import pathlib,sys\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--outdir')+1])\n"
        f"(out/{WHEEL!r}).write_bytes(b'new')\n"
        "print('stub frontend log')\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", str(root / ".github/scripts/build_development_wheel.py")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(frontend)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str((root / "dist" / WHEEL).resolve()) + "\n"
    assert "stub frontend log" in result.stderr
    assert (tmp_path / "source-owner-evidence").read_text() == str(copied_owner.resolve())
    assert_clean(root)
