from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
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

    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
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


def test_lf_and_crlf_canonical_sources_build_identical_bundles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = build_project_git_hook_bundle().content
    root = bundle_module._source_package_root()
    for label, newline in (("lf", b"\n"), ("crlf", b"\r\n"), ("cr", b"\r")):
        copy = tmp_path / label
        for _name, relative in bundle_module._SOURCE_INVENTORY:
            source = root / relative
            (copy / relative).parent.mkdir(parents=True, exist_ok=True)
            lines = source.read_bytes().replace(b"\r\n", b"\n").split(b"\n")
            (copy / relative).write_bytes(newline.join(lines))
        monkeypatch.setattr(bundle_module, "_source_package_root", lambda copy=copy: copy)

        assert build_project_git_hook_bundle().content == expected, label


def test_bundle_runs_without_ambient_booley_package(tmp_path: Path) -> None:
    project = tmp_path / "project"
    managed = project / ".booley" / ".managed"
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


def test_unreadable_canonical_source_aborts_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source path that cannot be read as a file gets the same clear error."""
    (tmp_path / bundle_module._SOURCE_INVENTORY[0][1]).mkdir(parents=True)
    monkeypatch.setattr(bundle_module, "_source_package_root", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="canonical hook source"):
        build_project_git_hook_bundle()


@pytest.mark.parametrize("minimum", [False, True], ids=["current", "minimum-2.37.2"])
def test_bundled_hook_accepts_destination_history_and_blocks_new_identity(
    tmp_path: Path, minimum: bool
) -> None:
    env = _bundled_hook_environment(minimum)
    project = tmp_path / "project"
    project.mkdir()

    def git(*args: str) -> str:
        return _bundled_git(project, env, *args)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Real Dev")
    git("config", "user.email", "dev@example.com")
    git("commit", "--allow-empty", "--no-verify", "-m", "fix(core): generated by claude")
    base = git("rev-parse", "HEAD")
    destination = tmp_path / "destination.git"
    git("clone", "--bare", str(project), str(destination))
    managed = project / ".booley" / ".managed"
    managed.mkdir(parents=True)
    bundle = managed / BUNDLE_NAME
    bundle.write_bytes(build_project_git_hook_bundle().content)

    def push(sha: str) -> subprocess.CompletedProcess[str]:
        return _bundled_push(project, bundle, destination, env, sha)

    result = push(base)
    assert result.returncode == 0, result.stderr
    assert "destination advertisement unavailable" not in result.stderr
    git("commit", "--allow-empty", "--no-verify", "-m", "fix(core): claude fresh identity")
    result = push(git("rev-parse", "HEAD"))
    assert result.returncode == 1 and "banned terms: claude" in result.stderr
    empty = tmp_path / "empty.git"
    git("init", "--bare", str(empty))
    destination = empty
    assert push(base).returncode == 1
    git("checkout", "--orphan", "ordinary")
    git(
        "commit",
        "--allow-empty",
        "--no-verify",
        "-m",
        "fix(core): generated docker verification agent",
    )
    result = push(git("rev-parse", "HEAD"))
    assert result.returncode == 0, result.stderr


def _bundled_git(project: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(project), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _bundled_push(
    project: Path, bundle: Path, destination: Path, env: dict[str, str], sha: str
) -> subprocess.CompletedProcess[str]:
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
    directory = project / ".booley"
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
