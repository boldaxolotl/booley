from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import pytest

SCANNER = Path(__file__).parents[2] / ".github/scripts/confidential_content_guard.py"
SAFE_IDENT = "Safe User <safe@example.test>"
SENTINEL = "quokka-sentinel-987"
SUBPROCESS_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class _DestinationScenario:
    repo: Path
    origin: Path
    remote_topic: str
    destination_main: str
    head: str


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def _commit(
    repo: Path, message: str, *, name: str = "Safe User", email: str = "safe@example.test"
) -> str:
    _git(repo, "add", ".")
    return _commit_index(repo, message, name=name, email=email)


def _identity_env(name: str = "Safe User", email: str = "safe@example.test") -> dict[str, str]:
    return os.environ | {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def _commit_index(
    repo: Path, message: str, *, name: str = "Safe User", email: str = "safe@example.test"
) -> str:
    _git(repo, "commit", "-m", message, env=_identity_env(name, email))
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    return repo, _commit(repo, "initial commit")


def _bare_remote(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    return origin


def _existing_topic_with_destination_main(
    tmp_path: Path,
) -> _DestinationScenario:
    repo, initial = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", f"{initial}:refs/heads/main")

    _git(repo, "checkout", "-b", "topic")
    (repo / "topic.txt").write_text("topic\n", encoding="utf-8")
    remote_topic = _commit(repo, "add topic")
    _git(repo, "push", "origin", f"{remote_topic}:refs/heads/topic")

    _git(repo, "checkout", "main")
    (repo / "upstream.txt").write_text("destination history\n", encoding="utf-8")
    destination_main = _commit(
        repo,
        "add upstream change",
        name="Destination User",
        email="destination@example.test",
    )
    _git(repo, "push", "origin", f"{destination_main}:refs/heads/main")

    _git(repo, "checkout", "topic")
    _git(repo, "merge", "--no-ff", "main", "-m", "merge main", env=_identity_env())
    head = _git(repo, "rev-parse", "HEAD")
    return _DestinationScenario(repo, origin, remote_topic, destination_main, head)


def _encoded_config() -> str:
    document = f'''[guard]
allowed_authors = ["{SAFE_IDENT}"]

[private]
words = ["{SENTINEL}"]
'''
    return base64.b64encode(document.encode()).decode()


def _sealed_fixture(repo: Path, config: str) -> dict[str, str]:
    """Seal a private fixture with a key isolated in this temporary Git directory."""
    env = os.environ | {"BOOLEY_LEAK_GUARD_KEY_DIR": str(repo / ".git/guard-keys")}
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ".git/booley-leak-guard.toml").write_bytes(base64.b64decode(config))
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "seal-config"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
    return env


def _scan(
    repo: Path,
    base: str,
    head: str,
    *,
    config: str | None,
    destination: tuple[str, Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    record = f"refs/heads/topic {head} refs/heads/main {base}\n"
    return _scan_records(repo, record, config=config, destination=destination)


def _scan_records(
    repo: Path,
    records: str,
    *,
    config: str | None,
    destination: tuple[str, Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _sealed_fixture(repo, config) if config is not None else os.environ.copy()
    command = [sys.executable, str(SCANNER), "--repo", str(repo), "pre-push"]
    if destination is not None:
        command.extend((destination[0], str(destination[1])))
    return subprocess.run(
        command,
        input=records,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _scan_pull_request(repo: Path, event: dict) -> subprocess.CompletedProcess[str]:
    event_path = repo / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    env = _sealed_fixture(repo, _encoded_config())
    return subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo",
            str(repo),
            "pull-request",
            "--event",
            str(event_path),
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _scan_pr_text(
    repo: Path,
    *,
    files: tuple[Path, ...] = (),
    stdin: str | None = None,
    config: str | None = None,
    env_config: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _sealed_fixture(repo, config) if config is not None else os.environ.copy()
    env["BOOLEY_LEAK_GUARD_KEY_DIR"] = str(repo / ".git/guard-keys")
    if env_config is not None:
        env["BOOLEY_LEAK_GUARD_CONFIG_B64"] = env_config
    command = [sys.executable, str(SCANNER), "--repo", str(repo), "pr-text"]
    for path in files:
        command.extend(("--file", str(path)))
    if stdin is not None:
        command.append("--stdin")
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _sync_ci_key(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "sync-ci-key"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _publish_pr(
    repo: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "publish-pr", *args],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _recording_gh(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        '#!/bin/sh\ntest -z "${GH_REPO+x}" || exit 2\n'
        'printf "%s\\n" "$@" > "$CAPTURE_ARGS"\ncat > "$CAPTURE_STDIN"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    captured_args = tmp_path / "gh-args"
    captured_stdin = tmp_path / "gh-stdin"
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_REPO": "somewhere/else",
        "CAPTURE_ARGS": str(captured_args),
        "CAPTURE_STDIN": str(captured_stdin),
    }
    return env, captured_args, captured_stdin


def test_clean_commit_passes(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "clean.txt").write_text("ordinary public content\n", encoding="utf-8")
    head = _commit(repo, "add public fixture")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 0, result.stderr


def test_new_ref_does_not_rescan_history_already_on_destination(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "historical.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    _commit(repo, "add historical fixture")
    (repo / "historical.txt").unlink()
    destination_head = _commit(repo, "remove historical fixture")
    _git(repo, "push", "origin", f"{destination_head}:refs/heads/main")
    (repo / "README.md").write_text("documentation only\n", encoding="utf-8")
    head = _commit(repo, "docs only")

    result = _scan(
        repo,
        "0" * 40,
        head,
        config=_encoded_config(),
        destination=("origin", origin),
    )

    assert result.returncode == 0, result.stderr


def test_existing_ref_does_not_rescan_other_destination_ref(tmp_path: Path) -> None:
    scenario = _existing_topic_with_destination_main(tmp_path)
    records = f"refs/heads/topic {scenario.head} refs/heads/topic {scenario.remote_topic}\n"

    result = _scan_records(
        scenario.repo,
        records,
        config=_encoded_config(),
        destination=("origin", scenario.origin),
    )

    assert result.returncode == 0, result.stderr


def test_existing_ref_still_scans_commit_absent_from_destination(tmp_path: Path) -> None:
    scenario = _existing_topic_with_destination_main(tmp_path)
    (scenario.repo / "local-only.txt").write_text("not on destination\n", encoding="utf-8")
    local_only = _commit(
        scenario.repo,
        "add local-only change",
        name="Local User",
        email="local@example.test",
    )
    records = f"refs/heads/topic {local_only} refs/heads/topic {scenario.remote_topic}\n"

    result = _scan_records(
        scenario.repo,
        records,
        config=_encoded_config(),
        destination=("origin", scenario.origin),
    )

    assert result.returncode == 1
    assert f"commit {local_only[:12]} author identity" in result.stderr
    assert scenario.destination_main[:12] not in result.stderr


def test_mixed_new_and_existing_updates_apply_destination_exclusions_to_both(
    tmp_path: Path,
) -> None:
    scenario = _existing_topic_with_destination_main(tmp_path)
    zero = "0" * 40
    records = (
        f"refs/heads/topic {scenario.head} refs/heads/topic {scenario.remote_topic}\n"
        f"refs/heads/review {scenario.head} refs/heads/review {zero}\n"
    )

    result = _scan_records(
        scenario.repo,
        records,
        config=_encoded_config(),
        destination=("origin", scenario.origin),
    )

    assert result.returncode == 0, result.stderr


def test_existing_ref_destination_lookup_failure_uses_remote_sha_fallback(
    tmp_path: Path,
) -> None:
    repo, _initial = _repository(tmp_path)
    (repo / "destination.txt").write_text("destination history\n", encoding="utf-8")
    remote_topic = _commit(
        repo,
        "add destination change",
        name="Destination User",
        email="destination@example.test",
    )
    (repo / "README.md").write_text("safe child\n", encoding="utf-8")
    head = _commit(repo, "update documentation")
    records = f"refs/heads/topic {head} refs/heads/topic {remote_topic}\n"

    result = _scan_records(
        repo,
        records,
        config=_encoded_config(),
        destination=("origin", tmp_path / "missing.git"),
    )

    assert result.returncode == 0, result.stderr


def test_new_ref_destination_lookup_failure_scans_full_ancestry(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    (repo / "historical.txt").write_text("not on destination\n", encoding="utf-8")
    bad_ancestor = _commit(
        repo,
        "add local-only ancestor",
        name="Local User",
        email="local@example.test",
    )
    (repo / "README.md").write_text("safe tip\n", encoding="utf-8")
    head = _commit(repo, "update documentation")

    result = _scan(
        repo,
        "0" * 40,
        head,
        config=_encoded_config(),
        destination=("origin", tmp_path / "missing.git"),
    )

    assert result.returncode == 1
    assert f"commit {bad_ancestor[:12]} author identity" in result.stderr


def test_docs_commit_does_not_rescan_unchanged_baseline_blob(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    (repo / "baseline.gz").write_bytes(b"\x1f\x8bnot-a-valid-gzip-stream")
    base = _commit(repo, "add destination baseline")
    (repo / "README.md").write_text("documentation only\n", encoding="utf-8")
    head = _commit(repo, "docs only")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 0, result.stderr


def test_pre_push_rejects_revision_option_instead_of_changing_scan_scope(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)

    result = _scan(repo, "0" * 40, "--all", config=_encoded_config())

    assert result.returncode == 1
    assert "could not complete" in result.stderr
    assert "--all" not in result.stderr


def test_blob_added_then_deleted_in_outgoing_history_is_blocked(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "temporary.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    _commit(repo, "add temporary fixture")
    (repo / "temporary.txt").unlink()
    head = _commit(repo, "remove temporary fixture")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert SENTINEL not in result.stderr


def test_new_ref_does_not_trust_history_reachable_only_from_another_remote(
    tmp_path: Path,
) -> None:
    repo, origin_head = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", f"{origin_head}:refs/heads/main")
    (repo / "private.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    private_head = _commit(repo, "add private fixture")
    _git(repo, "update-ref", "refs/remotes/private/topic", private_head)
    (repo / "README.md").write_text("documentation only\n", encoding="utf-8")
    head = _commit(repo, "docs only")

    result = _scan(
        repo,
        "0" * 40,
        head,
        config=_encoded_config(),
        destination=("origin", origin),
    )

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert SENTINEL not in result.stderr


def test_new_ref_to_empty_destination_scans_full_history(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    (repo / "payload.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    head = _commit(repo, "add fixture")

    result = _scan(
        repo,
        "0" * 40,
        head,
        config=_encoded_config(),
        destination=("origin", origin),
    )

    assert result.returncode == 1
    assert "confidential term" in result.stderr


def test_audit_finds_blob_deleted_later_in_history(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    (repo / "temporary.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    _commit(repo, "add temporary fixture")
    (repo / "temporary.txt").unlink()
    head = _commit(repo, "remove temporary fixture")
    env = _sealed_fixture(repo, _encoded_config())

    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo",
            str(repo),
            "audit",
            "--rev",
            head,
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert SENTINEL not in result.stderr


def test_audit_accepts_glob_escaped_mergify_bot_identity(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    (repo / "bot.txt").write_text("clean bot-authored content\n", encoding="utf-8")
    head = _commit(
        repo,
        "add bot-authored fixture",
        name="mergify[bot]",
        email="37929162+mergify[bot]@users.noreply.github.com",
    )
    env = _sealed_fixture(repo, _encoded_config()) | {
        "BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS": "mergify[[]bot[]]",
    }

    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo",
            str(repo),
            "audit",
            "--rev",
            head,
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_merge_introduced_blob_deleted_later_is_blocked(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("clean side\n", encoding="utf-8")
    _commit(repo, "add clean side")
    _git(repo, "checkout", "main")
    (repo / "main.txt").write_text("clean main\n", encoding="utf-8")
    _commit(repo, "add clean main")
    _git(repo, "merge", "--no-ff", "--no-commit", "side", env=_identity_env())
    (repo / "merge-only.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    _commit(repo, "merge side with fixture")
    (repo / "merge-only.txt").unlink()
    head = _commit(repo, "remove merge fixture")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 1
    assert "confidential term" in result.stderr


def test_gitlink_update_scans_path_without_reading_commit_as_blob(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    linked_commit = _git(repo, "rev-parse", "HEAD")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{linked_commit},vendor/example",
    )
    head = _commit_index(repo, "add submodule entry")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 0, result.stderr


def test_confidential_root_commit_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "payload.txt").write_text(f"contains {SENTINEL}\n", encoding="utf-8")
    head = _commit(repo, "root fixture")

    result = _scan(repo, "0" * 40, head, config=_encoded_config())

    assert result.returncode == 1
    assert "confidential term" in result.stderr


def test_renamed_blob_is_inspected_at_its_new_path(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "baseline.gz").write_bytes(b"\x1f\x8bnot-a-valid-gzip-stream")
    base = _commit(repo, "add baseline fixture")
    (repo / "baseline.gz").rename(repo / "renamed.gz")
    head = _commit(repo, "rename fixture")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 1
    assert "could not complete" in result.stderr


def test_regular_file_changed_to_symlink_is_inspected(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    path = repo / "link"
    path.write_text("ordinary content\n", encoding="utf-8")
    base = _commit(repo, "add ordinary file")
    path.unlink()
    path.symlink_to(SENTINEL)
    head = _commit(repo, "change file to symlink")

    result = _scan(repo, base, head, config=_encoded_config())

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert SENTINEL not in result.stderr


def test_multiple_updates_are_scanned_and_deletions_are_skipped(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "clean.txt").write_text("ordinary public content\n", encoding="utf-8")
    head = _commit(repo, "add public fixture")
    zero = "0" * 40
    records = (
        f"refs/heads/topic {head} refs/heads/topic {base}\n"
        f"refs/heads/second {head} refs/heads/second {base}\n"
        f"(delete) {zero} refs/heads/old {base}\n"
    )

    result = _scan_records(repo, records, config=_encoded_config())

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


def test_clone_recovers_vocabulary_with_separately_restored_key(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    env = _sealed_fixture(repo, _encoded_config())
    encrypted = repo / ".github/confidential-vocabulary.enc"
    assert SENTINEL.encode() not in encrypted.read_bytes()
    _commit(repo, "track encrypted vocabulary")

    clone = tmp_path / "restored"
    subprocess.run(
        ["git", "clone", "--quiet", str(repo), str(clone)],
        check=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    restored_keys = tmp_path / "restored-keys"
    restored_env = os.environ | {"BOOLEY_LEAK_GUARD_KEY_DIR": str(restored_keys)}
    key_path = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(clone), "key-path"],
        capture_output=True,
        check=False,
        env=restored_env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert key_path.returncode == 0, key_path.stderr
    missing = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(clone), "pr-text", "--stdin"],
        input="public title",
        capture_output=True,
        check=False,
        env=restored_env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert missing.returncode == 1

    source_keys = Path(env["BOOLEY_LEAK_GUARD_KEY_DIR"])
    restored_keys.mkdir()
    shutil.copyfile(next(source_keys.glob("*.key")), Path(key_path.stdout.strip()))
    Path(key_path.stdout.strip()).chmod(0o600)
    detected = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(clone), "pr-text", "--stdin"],
        input=SENTINEL,
        capture_output=True,
        check=False,
        env=restored_env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert detected.returncode == 1
    assert "confidential term" in detected.stderr
    assert SENTINEL not in detected.stderr
    restored = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(clone), "restore-draft"],
        capture_output=True,
        check=False,
        env=restored_env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert restored.returncode == 0, restored.stderr
    draft = clone / ".git/booley-leak-guard.toml"
    assert draft.read_bytes() == base64.b64decode(_encoded_config())
    assert SENTINEL not in restored.stdout + restored.stderr


def test_restore_draft_refuses_plaintext_inside_worktree(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    env = _sealed_fixture(repo, _encoded_config()) | {
        "BOOLEY_LEAK_GUARD_CONFIG": "private.toml",
    }
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "restore-draft"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 1
    assert not (repo / "private.toml").exists()
    assert SENTINEL not in result.stderr


def test_candidate_encrypted_vocabulary_is_validated_against_trusted_key(
    tmp_path: Path,
) -> None:
    repo, _initial = _repository(tmp_path)
    env = _sealed_fixture(repo, _encoded_config())
    trusted = _commit(repo, "add valid encrypted vocabulary")
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {"pull_request": {"title": "public", "body": "public", "head": {"ref": "topic"}}}
        ),
        encoding="utf-8",
    )
    valid = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo",
            str(repo),
            "verify-candidate",
            "--rev",
            trusted,
            "--base",
            trusted,
            "--event",
            str(event_path),
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert valid.returncode == 0, valid.stderr

    encrypted = repo / ".github/confidential-vocabulary.enc"
    encrypted.write_bytes(b"corrupt\n")
    broken = _commit(repo, "corrupt encrypted vocabulary")
    _git(repo, "checkout", trusted)
    invalid = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo",
            str(repo),
            "verify-candidate",
            "--rev",
            broken,
            "--base",
            trusted,
            "--event",
            str(event_path),
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert invalid.returncode == 1
    assert "could not complete" in invalid.stderr


def test_candidate_scan_blocks_new_term_in_same_pr(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    env = _sealed_fixture(repo, _encoded_config())
    trusted = _commit(repo, "add initial encrypted vocabulary")
    new_term = "new-secret-654"
    changed_config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n'
        f'[private]\nwords = ["{SENTINEL}", "{new_term}"]\n'.encode()
    ).decode()
    _sealed_fixture(repo, changed_config)
    candidate = _commit(repo, "add new vocabulary term")
    _git(repo, "checkout", trusted)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {"pull_request": {"title": "public", "body": new_term, "head": {"ref": "topic"}}}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo",
            str(repo),
            "verify-candidate",
            "--rev",
            candidate,
            "--base",
            trusted,
            "--event",
            str(event_path),
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert new_term not in result.stderr


def test_tampered_encrypted_vocabulary_fails_closed(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    env = _sealed_fixture(repo, _encoded_config())
    encrypted = repo / ".github/confidential-vocabulary.enc"
    header, locator, encoded = encrypted.read_bytes().splitlines()
    payload = bytearray(base64.b64decode(encoded, validate=True))
    payload[-1] ^= 1
    encrypted.write_bytes(b"\n".join((header, locator, base64.b64encode(payload), b"")))

    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "pr-text", "--stdin"],
        input="public title",
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 1
    assert "could not complete" in result.stderr


def test_pull_request_metadata_is_blocked_without_echoing_term(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    event = {
        "pull_request": {
            "title": "ordinary title",
            "body": f"private context: {SENTINEL}",
            "head": {"ref": "topic"},
        }
    }

    result = _scan_pull_request(repo, event)

    assert result.returncode == 1
    assert "pull request body" in result.stderr
    assert SENTINEL not in result.stderr


def test_pull_request_metadata_clean_event_passes(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    event = {
        "pull_request": {
            "title": "ordinary title",
            "body": "ordinary description",
            "head": {"ref": "topic"},
        }
    }

    result = _scan_pull_request(repo, event)

    assert result.returncode == 0, result.stderr


def test_proposed_pr_text_blocks_file_and_stdin_without_echoing_terms(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    draft = tmp_path / "body.md"
    draft.write_text(f"private detail: {SENTINEL}\n", encoding="utf-8")

    file_result = _scan_pr_text(repo, files=(draft,), config=_encoded_config())
    stdin_result = _scan_pr_text(repo, stdin=f"title {SENTINEL}", config=_encoded_config())

    for result in (file_result, stdin_result):
        assert result.returncode == 1
        assert "confidential term" in result.stderr
        assert SENTINEL not in result.stderr


def test_proposed_pr_text_accepts_clean_title_and_body(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    draft = tmp_path / "body.md"
    draft.write_text("public description\n", encoding="utf-8")

    result = _scan_pr_text(repo, files=(draft,), stdin="public title", config=_encoded_config())

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("prefix", ["", "prefixsynthetic-term-000suffix"])
def test_large_nonmatching_pr_text_scans_promptly(tmp_path: Path, prefix: str) -> None:
    repo, _base = _repository(tmp_path)
    words = ", ".join(f'"synthetic-term-{index:03d}"' for index in range(128))
    config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n[private]\nwords = [{words}]\n'.encode()
    ).decode()
    env = _sealed_fixture(repo, config)
    start = perf_counter()
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "pr-text", "--stdin"],
        input=prefix + "~" * 1_000_000,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    elapsed = perf_counter() - start

    assert result.returncode == 0, result.stderr
    assert elapsed < 3.0, f"nonmatching scan took {elapsed:.2f} seconds"


@pytest.mark.parametrize("text", ["f\u0130le", "f\u0131le", "\u017fecret", "\u212aey"])
def test_ascii_prefilter_preserves_unicode_ignorecase_matches(tmp_path: Path, text: str) -> None:
    repo, _base = _repository(tmp_path)
    draft = tmp_path / "unicode-pr-text.txt"
    draft.write_text(text, encoding="utf-8")
    config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n'
        '[private]\nwords = ["file", "secret", "key"]\n'.encode()
    ).decode()

    result = _scan_pr_text(repo, files=(draft,), config=config)

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert text not in result.stderr


def test_prefilter_detects_term_across_large_blob_chunk_boundary(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    marker = "boundary-marker-321"
    boundary = 1024 * 1024
    (repo / "large.txt").write_text("~" * (boundary - 3) + marker, encoding="utf-8")
    head = _commit(repo, "add large fixture")
    config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n[private]\nwords = ["{marker}"]\n'.encode()
    ).decode()

    result = _scan(repo, base, head, config=config)

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert marker not in result.stderr


def test_non_ascii_vocabulary_still_uses_full_unicode_matching(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n[private]\nwords = ["caf\u00e9"]\n'.encode()
    ).decode()

    result = _scan_pr_text(repo, stdin="CAF\u00c9", config=config)

    assert result.returncode == 1
    assert "confidential term" in result.stderr


def test_proposed_pr_text_uses_sealed_file_over_private_draft_and_stale_ci_env(
    tmp_path: Path,
) -> None:
    repo, _base = _repository(tmp_path)
    _sealed_fixture(repo, _encoded_config())
    (repo / ".git/booley-leak-guard.toml").write_text("[private]\nwords = ['other']\n")
    stale_config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n[private]\nwords = ["other"]\n'.encode()
    ).decode()

    result = _scan_pr_text(repo, stdin=SENTINEL, env_config=stale_config)

    assert result.returncode == 1
    assert "confidential term" in result.stderr


def test_proposed_pr_text_fails_closed_for_missing_input_or_config(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)

    missing_config = _scan_pr_text(repo, stdin="public title")
    missing_input = _scan_pr_text(repo, config=_encoded_config())

    assert missing_input.returncode == 1
    assert "could not complete" in missing_input.stderr
    assert missing_config.returncode == 1
    assert "could not complete" in missing_config.stderr


def test_proposed_pr_text_requires_sealed_file_even_with_old_ci_env(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)

    result = _scan_pr_text(repo, stdin="public title", env_config=_encoded_config())

    assert result.returncode == 1
    assert "could not complete" in result.stderr


def test_overlapping_banned_terms_are_detected(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    config = base64.b64encode(
        f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n'
        '[private]\nwords = ["Acme", "AcmeCloud", "abc", "abc-def"]\n'.encode()
    ).decode()

    for text in ("AcmeCloud", "abc-defg"):
        result = _scan_pr_text(repo, stdin=text, config=config)
        assert result.returncode == 1
        assert "confidential term" in result.stderr
        assert text not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX shell script")
def test_publish_pr_create_scans_then_sends_exact_drafts(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    sealed_env = _sealed_fixture(repo, _encoded_config())
    title = tmp_path / "title.txt"
    body = tmp_path / "body.md"
    title.write_text("Public title\n", encoding="utf-8")
    body.write_text("Public description\n", encoding="utf-8")
    env, captured_args, captured_stdin = _recording_gh(tmp_path)
    env["BOOLEY_LEAK_GUARD_KEY_DIR"] = sealed_env["BOOLEY_LEAK_GUARD_KEY_DIR"]

    result = _publish_pr(
        repo,
        [
            "create",
            "--base",
            "main",
            "--head",
            "topic",
            "--title-file",
            str(title),
            "--body-file",
            str(body),
            "--repo",
            "owner/repo",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    assert captured_args.read_text(encoding="utf-8").splitlines() == [
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        "topic",
        "--title",
        "Public title",
        "--body-file",
        "-",
        "--repo",
        "owner/repo",
    ]
    assert captured_stdin.read_text(encoding="utf-8") == body.read_text(encoding="utf-8")


def test_publish_pr_blocks_confidential_drafts_before_gh(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    sealed_env = _sealed_fixture(repo, _encoded_config())
    title = tmp_path / "title.txt"
    body = tmp_path / "body.md"
    title.write_text("Public title", encoding="utf-8")
    body.write_text(f"Sensitive context: {SENTINEL}", encoding="utf-8")
    env, captured_args, _captured_stdin = _recording_gh(tmp_path)
    env["BOOLEY_LEAK_GUARD_KEY_DIR"] = sealed_env["BOOLEY_LEAK_GUARD_KEY_DIR"]

    result = _publish_pr(
        repo,
        [
            "create",
            "--base",
            "main",
            "--head",
            "topic",
            "--title-file",
            str(title),
            "--body-file",
            str(body),
        ],
        env,
    )

    assert result.returncode == 1
    assert "confidential term" in result.stderr
    assert SENTINEL not in result.stderr
    assert not captured_args.exists()


def test_publish_pr_requires_sealed_file_even_with_old_ci_env(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    body = tmp_path / "body.md"
    body.write_text("Public comment", encoding="utf-8")
    env, captured_args, _captured_stdin = _recording_gh(tmp_path)
    env["BOOLEY_LEAK_GUARD_CONFIG_B64"] = _encoded_config()

    result = _publish_pr(repo, ["comment", "--pr", "123", "--body-file", str(body)], env)

    assert result.returncode == 1
    assert "could not complete" in result.stderr
    assert not captured_args.exists()


@pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX shell script")
def test_publish_pr_supports_edit_comment_and_review(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    sealed_env = _sealed_fixture(repo, _encoded_config())
    title = tmp_path / "title.txt"
    body = tmp_path / "body.md"
    title.write_text("Updated title", encoding="utf-8")
    body.write_text("Public response", encoding="utf-8")
    env, captured_args, captured_stdin = _recording_gh(tmp_path)
    env["BOOLEY_LEAK_GUARD_KEY_DIR"] = sealed_env["BOOLEY_LEAK_GUARD_KEY_DIR"]
    cases = (
        (
            ["edit", "--pr", "123", "--title-file", str(title)],
            ["pr", "edit", "123", "--title", "Updated title"],
            "",
        ),
        (
            ["comment", "--pr", "123", "--body-file", str(body), "--edit-last"],
            ["pr", "comment", "123", "--edit-last", "--body-file", "-"],
            "Public response",
        ),
        (
            ["review", "--pr", "123", "--verdict", "request-changes", "--body-file", str(body)],
            ["pr", "review", "123", "--request-changes", "--body-file", "-"],
            "Public response",
        ),
    )

    for arguments, expected_args, expected_stdin in cases:
        result = _publish_pr(repo, arguments, env)
        assert result.returncode == 0, result.stderr
        assert captured_args.read_text(encoding="utf-8").splitlines() == expected_args
        assert captured_stdin.read_text(encoding="utf-8") == expected_stdin


def test_proposed_pr_text_fails_closed_when_draft_cannot_be_read(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    missing = tmp_path / "missing.md"

    result = _scan_pr_text(repo, files=(missing,), config=_encoded_config())

    assert result.returncode == 1
    assert "could not complete" in result.stderr
    assert str(missing) not in result.stderr


def test_proposed_pr_text_fails_closed_when_draft_is_too_large(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    draft = tmp_path / "body.md"
    draft.write_bytes(b"x" * (1024 * 1024 + 1))

    result = _scan_pr_text(repo, files=(draft,), config=_encoded_config())

    assert result.returncode == 1
    assert "exceeds the inspection size limit" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX shell script")
def test_sync_ci_key_uses_sealed_vocabulary_and_not_stale_ci_env(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    sealed_env = _sealed_fixture(repo, _encoded_config())
    (repo / ".git/booley-leak-guard.toml").write_text("[private]\nwords = ['other']\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        '#!/bin/sh\ntest -z "${BOOLEY_LEAK_GUARD_KEY_B64+x}" || exit 2\n'
        'test -z "${GH_REPO+x}" || exit 3\n'
        'printf "%s\\n" "$*" > "$CAPTURE_ARGS"\ncat > "$CAPTURE_STDIN"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    captured_args = tmp_path / "args"
    captured_stdin = tmp_path / "stdin"
    env = sealed_env | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BOOLEY_LEAK_GUARD_KEY_B64": base64.b64encode(b"stale key").decode(),
        "GH_REPO": "somewhere/else",
        "CAPTURE_ARGS": str(captured_args),
        "CAPTURE_STDIN": str(captured_stdin),
    }

    result = _sync_ci_key(repo, env)

    assert result.returncode == 0, result.stderr
    assert captured_args.read_text(encoding="utf-8").strip() == (
        "secret set BOOLEY_LEAK_GUARD_KEY_B64 --app actions"
    )
    assert len(base64.b64decode(captured_stdin.read_bytes())) == 32
    assert SENTINEL not in result.stdout + result.stderr


def test_sync_ci_key_fails_when_sealed_file_is_missing(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    env = os.environ | {"BOOLEY_LEAK_GUARD_CONFIG_B64": _encoded_config()}

    result = _sync_ci_key(repo, env)

    assert result.returncode == 1
    assert "could not complete" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX shell script")
def test_sync_ci_key_does_not_echo_gh_failure_output(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    sealed_env = _sealed_fixture(repo, _encoded_config())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(f'#!/bin/sh\necho "{SENTINEL}" >&2\nexit 1\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    env = sealed_env | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    result = _sync_ci_key(repo, env)

    assert result.returncode == 1
    assert "could not complete" in result.stderr
    assert SENTINEL not in result.stderr


def test_workflow_scans_metadata_on_pr_edits() -> None:
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )

    assert "types: [opened, synchronize, reopened, ready_for_review, edited]" in workflow
    assert "pull-request --event" in workflow
    assert 'verify-candidate --rev "${HEAD_SHA}"' in workflow
    assert '--base "${BASE_SHA}" --event "${GITHUB_EVENT_PATH}"' in workflow


def test_workflow_trusts_mergify_identity_for_pr_updates_and_main_history() -> None:
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )
    pr_scan = workflow.split("- name: Scan pull-request commits", 1)[1].split(
        "- name: Scan complete main history", 1
    )[0]
    main_scan = workflow.split("- name: Scan complete main history", 1)[1].split(
        "- name: Publish scan status", 1
    )[0]

    assert 'BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: "mergify[[]bot[]]"' in pr_scan
    assert 'BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: "mergify[[]bot[]]"' in main_scan
