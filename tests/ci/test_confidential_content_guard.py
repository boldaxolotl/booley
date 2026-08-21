from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

SCANNER = Path(__file__).parents[2] / ".github/scripts/confidential_content_guard.py"
SAFE_IDENT = "Safe User <safe@example.test>"
SENTINEL = "quokka-sentinel-987"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=env,
        text=True,
    )
    return result.stdout.strip()


def _commit(
    repo: Path, message: str, *, name: str = "Safe User", email: str = "safe@example.test"
) -> str:
    env = os.environ | {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    return repo, _commit(repo, "initial commit")


def _encoded_config() -> str:
    document = f'''[guard]
allowed_authors = ["{SAFE_IDENT}"]

[private]
words = ["{SENTINEL}"]
'''
    return base64.b64encode(document.encode()).decode()


def _scan(
    repo: Path, base: str, head: str, *, config: str | None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if config is not None:
        env["BOOLEY_LEAK_GUARD_CONFIG_B64"] = config
    record = f"refs/heads/topic {head} refs/heads/main {base}\n"
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "pre-push"],
        input=record,
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def test_clean_commit_passes(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "clean.txt").write_text("ordinary public content\n", encoding="utf-8")
    head = _commit(repo, "add public fixture")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 0, result.stderr


def test_term_and_unapproved_identity_are_blocked_without_echoing_term(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "payload.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    head = _commit(repo, "add fixture", name="Unexpected User", email="unexpected@example.test")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert "identity not allowed" in result.stderr
    assert SENTINEL not in result.stderr


def test_missing_secret_fails_closed(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "clean.txt").write_text("ordinary public content\n", encoding="utf-8")
    head = _commit(repo, "add public fixture")

    result = _scan(repo, base, head, config=None)

    assert result.returncode == 1
    assert "could not complete" in result.stderr
