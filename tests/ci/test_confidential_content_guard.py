from __future__ import annotations

import base64
import json
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


def _encoded_config() -> str:
    document = f'''[guard]
allowed_authors = ["{SAFE_IDENT}"]

[private]
words = ["{SENTINEL}"]
'''
    return base64.b64encode(document.encode()).decode()


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
    env = os.environ.copy()
    if config is not None:
        env["BOOLEY_LEAK_GUARD_CONFIG_B64"] = config
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
    )


def _scan_pull_request(repo: Path, event: dict) -> subprocess.CompletedProcess[str]:
    event_path = repo / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    env = os.environ | {"BOOLEY_LEAK_GUARD_CONFIG_B64": _encoded_config()}
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
    )


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
    env = os.environ | {"BOOLEY_LEAK_GUARD_CONFIG_B64": _encoded_config()}

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
    env = os.environ | {
        "BOOLEY_LEAK_GUARD_CONFIG_B64": _encoded_config(),
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


def test_workflow_scans_metadata_on_pr_edits() -> None:
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )

    assert "types: [opened, synchronize, reopened, ready_for_review, edited]" in workflow
    assert "pull-request --event" in workflow


def test_workflow_trusts_mergify_identity_only_for_main_history() -> None:
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )
    main_scan = workflow.split("- name: Scan complete main history", 1)[1].split(
        "- name: Publish scan status", 1
    )[0]

    assert 'BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: "mergify[[]bot[]]"' in main_scan
