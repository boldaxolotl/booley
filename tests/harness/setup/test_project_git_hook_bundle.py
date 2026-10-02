from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import warnings
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
            "booley_commit_policy.py",
            "booley_commit_validation.py",
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


def test_normalized_source_rejects_nonregular_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a regular file"):
        bundle_module._normalized_source(tmp_path)


def test_source_inventory_rejects_duplicate_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bundle_module,
        "_SOURCE_INVENTORY",
        (("duplicate.py", Path("one.py")), ("duplicate.py", Path("two.py"))),
    )

    with pytest.raises(ValueError, match="duplicate source members"):
        bundle_module._read_source_members()


def test_archive_validation_rejects_duplicate_members() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", "pass")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("__main__.py", "pass")

    with pytest.raises(ValueError, match="duplicate archive members"):
        bundle_module._validate_archive(buffer.getvalue(), {"hook.py": b"pass"})


@pytest.mark.parametrize(
    ("manifest", "expected_error"),
    [
        (
            {"schema": 999, "members": ["__main__.py", "hook.py", "manifest.json"]},
            "unsupported schema",
        ),
        ({"schema": 1, "members": ["__main__.py", "hook.py"]}, "does not match"),
    ],
)
def test_archive_validation_rejects_invalid_manifest(
    manifest: dict[str, object], expected_error: str
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", "pass")
        archive.writestr("hook.py", "pass")
        archive.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(ValueError, match=expected_error):
        bundle_module._validate_archive(buffer.getvalue(), {"hook.py": b"pass"})


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


def test_missing_canonical_source_aborts_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bundle_module, "_source_package_root", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="canonical hook source"):
        build_project_git_hook_bundle()


@pytest.mark.parametrize("minimum", [False, True], ids=["current", "minimum-2.37.2"])
def test_bundled_hook_accepts_verified_import_and_blocks_new_identity(tmp_path, minimum):
    env = _bundled_hook_environment(minimum)
    project = tmp_path / "project"
    project.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(project), *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Real Dev")
    git("config", "user.email", "dev@example.com")
    git("commit", "--allow-empty", "--no-verify", "-m", "fix(core): generated by claude")
    base = git("rev-parse", "HEAD")
    upstream = tmp_path / "upstream.git"
    destination = tmp_path / "destination.git"
    git("clone", "--bare", str(project), str(upstream))
    git("init", "--bare", str(destination))
    managed = project / ".booley_project" / ".managed"
    managed.mkdir(parents=True)
    (managed.parent / "booley.toml").write_text(
        f'[stealth]\nupstream_repository = "{upstream.as_posix()}"\nupstream_base = "{base}"\n'
    )
    bundle = managed / BUNDLE_NAME
    bundle.write_bytes(build_project_git_hook_bundle().content)

    def push(sha):
        return subprocess.run(
            [sys.executable, "-I", "-S", str(bundle), "pre-push", "destination", str(destination)],
            input=f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n",
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    result = push(base)
    assert result.returncode == 0, result.stderr
    git("commit", "--allow-empty", "--no-verify", "-m", "fix(core): claude fresh identity")
    result = push(git("rev-parse", "HEAD"))
    assert result.returncode == 1 and "banned terms: claude" in result.stderr


def _bundled_hook_environment(minimum):
    env = os.environ.copy()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    if minimum:
        executable = env.get("BOOLEY_TEST_MIN_GIT")
        if not executable:
            if env.get("BOOLEY_TEST_REQUIRE_MIN_GIT") == "1":
                pytest.fail("required minimum Git bundle executable absent")
            pytest.skip("optional bundled minimum Git execution needs BOOLEY_TEST_MIN_GIT")
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=True, timeout=10
        )
        assert re.fullmatch(r"git version 2\.37\.2(?:\.windows\.\d+)?", version.stdout.strip())
        env["PATH"] = str(Path(executable).resolve().parent) + os.pathsep + env["PATH"]
    return env


def test_bundle_identifier_tiers_refresh_from_current_config(tmp_path):
    project = tmp_path / "project"
    directory = project / ".booley_project"
    directory.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, timeout=30)
    bundle = directory / BUNDLE_NAME
    first = build_project_git_hook_bundle()
    bundle.write_bytes(first.content)
    assert first.content == build_project_git_hook_bundle().content
    config = directory / "booley.toml"
    message = project / "message.txt"
    for setting, expected in (
        (
            'banned_words = ["booley", "agent"]\nbanned_substrings = ["quokka"]',
            "fix: redacted_config redactedRunner myredactedfile axi_redacted reagent precursor\n",
        ),
        (
            "banned_words = []\nbanned_substrings = []",
            "fix: booley_config BooleyRunner myquokkafile axi_agent reagent precursor\n",
        ),
    ):
        config.write_text("[stealth]\n" + setting + "\n")
        message.write_text(
            "fix: booley_config BooleyRunner myquokkafile axi_agent reagent precursor\n"
        )
        result = subprocess.run(
            ["python3", "-I", "-S", str(bundle), "commit-msg", str(message)],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert message.read_text() == expected
