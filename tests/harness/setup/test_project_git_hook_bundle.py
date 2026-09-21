from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from booley.harness.setup import project_git_hook_bundle as bundle_module
from booley.harness.setup.project_git_hook_bundle import (
    BUNDLE_NAME,
    build_project_git_hook_bundle,
)


def test_bundle_is_deterministic_and_has_fixed_archive_metadata() -> None:
    first = build_project_git_hook_bundle()
    second = build_project_git_hook_bundle()

    assert first.content == second.content
    assert first.sha256 == second.sha256 == hashlib.sha256(first.content).hexdigest()

    with zipfile.ZipFile(__import__("io").BytesIO(first.content)) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(info.external_attr >> 16 & 0o111 == 0 for info in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["members"] == names
        assert set(manifest["source_sha256"]) == {
            "boundary.py",
            "checkout_role.py",
            "run_command.py",
            "commit_msg_utils.py",
            "validate_commit_msg.py",
            "commit_msg_hook.py",
            "pre_push_hook.py",
        }


def test_normalized_source_bytes_are_identical_for_lf_and_crlf(tmp_path: Path) -> None:
    source = tmp_path / "hook.py"
    source.write_bytes(b"one\r\ntwo\rthree\n")
    first = bundle_module._normalized_source(source)
    source.write_bytes(b"one\ntwo\nthree\n")

    assert first == bundle_module._normalized_source(source)


def test_bundle_runs_without_ambient_booley_package(tmp_path: Path) -> None:
    project = tmp_path / "project"
    managed = project / ".booley_project" / ".managed"
    managed.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    bundle = managed / BUNDLE_NAME
    bundle.write_bytes(build_project_git_hook_bundle().content)
    message = project / "message.txt"
    message.write_text("fix: clean message\n", encoding="utf-8")

    fake = tmp_path / "fake"
    (fake / "booley").mkdir(parents=True)
    (fake / "booley" / "__init__.py").write_text(
        "raise AssertionError('ambient package imported')\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake)
    result = subprocess.run(
        ["python3", "-I", "-S", str(bundle), "commit-msg", str(message)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert message.read_text(encoding="utf-8") == "fix: clean message\n"


@pytest.mark.parametrize("argv", [["unknown"], []])
def test_launcher_rejects_missing_or_unknown_commands(tmp_path: Path, argv: list[str]) -> None:
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(build_project_git_hook_bundle().content)

    result = subprocess.run(
        ["python3", "-I", "-S", str(bundle), *argv],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "project-git-hooks.pyz" in result.stderr
    assert "expected commit-msg or pre-push" in result.stderr


def test_missing_canonical_source_aborts_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bundle_module, "_source_package_root", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="canonical hook source"):
        build_project_git_hook_bundle()
