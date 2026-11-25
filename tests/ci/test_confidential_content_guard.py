from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import pytest
import yaml
from tests.path_tool_fake import PathToolFake
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child
from tests.time_budget import scaled

SCANNER = Path(__file__).parents[2] / ".github/scripts/confidential_content_guard.py"
SAFE_IDENT = "Safe User <safe@example.test>"
SENTINEL = "quokka-sentinel-987"


@dataclass(frozen=True)
class _DestinationScenario:
    repo: Path
    origin: Path
    remote_topic: str
    destination_main: str
    head: str


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = run_bounded_child(
        ["git", "-C", str(repo), *args],
        check=True,
        env=env,
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


KEY_DIR = "BOOLEY_LEAK_GUARD_KEY_DIR"
TERM_TEXT = f"contains {SENTINEL}\n"
_PROMPT_SCAN_S = scaled(3.0)
_NEW_REF = "0" * 40
_GH_BY_NAME = pytest.mark.skipif(
    os.name == "nt", reason="the guard runs gh by name, so Windows finds no .cmd launcher"
)


def _change(repo: Path, message: str, files: dict[str, str], **identity: str) -> str:
    for name, text in files.items():
        (repo / name).write_bytes(text.encode())
    return _commit(repo, message, **identity)


def _config(vocabulary: str | None = None) -> str:
    """Guard configuration text; the default vocabulary holds only the sentinel term."""
    vocabulary = vocabulary or _words(SENTINEL)
    return f'[guard]\nallowed_authors = ["{SAFE_IDENT}"]\n\n{vocabulary}'


def _words(*terms: str) -> str:
    return f"[private]\nwords = {json.dumps(list(terms))}\n"


def _guard(
    repo: Path, *args: str, stdin: str = "", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the guard CLI in `repo`; its key directory is isolated unless `env` names another."""
    # subprocess.run rather than run_bounded_child: the guard reads its input on stdin.
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), *args],
        input=stdin,
        capture_output=True,
        check=False,
        env=os.environ | {KEY_DIR: str(repo / ".git/guard-keys")} | (env or {}),
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT_S,
    )


def _seal(repo: Path, vocabulary: str | None = None) -> dict[str, str]:
    """Seal the guard configuration with a key kept in this repository's Git directory."""
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ".git/booley-leak-guard.toml").write_bytes(_config(vocabulary).encode())
    result = _guard(repo, "seal-config")
    assert result.returncode == 0, result.stderr
    return os.environ | {KEY_DIR: str(repo / ".git/guard-keys")}


def _update(head: str, base: str) -> str:
    return f"refs/heads/topic {head} refs/heads/main {base}\n"


def _push(
    repo: Path,
    records: str,
    *extra: str,
    vocabulary: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    _seal(repo, vocabulary)
    return _guard(repo, "pre-push", *extra, stdin=records, env=env)


def _audit(
    repo: Path,
    revs: tuple[str, ...] = ("HEAD",),
    *,
    owner: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `audit` over `revs`, optionally with --repository-owner."""
    args = [arg for rev in revs for arg in ("--rev", rev)]
    if owner is not None:
        args += ["--repository-owner", owner]
    _seal(repo)
    return _guard(repo, "audit", *args, env=env)


def _write_event(repo: Path, event: dict) -> Path:
    path = repo.parent / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return path


def _meta(title: str = "public", body: str = "public") -> dict:
    return {"pull_request": {"title": title, "body": body, "head": {"ref": "topic"}}}


def _verify(
    repo: Path, rev: str, base: str, event: dict, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    event_path = str(_write_event(repo, event))
    args = ["verify-candidate", "--rev", rev, "--base", base, "--event", event_path, *extra]
    return _guard(repo, *args, env=env)


def _expect_exit(result: subprocess.CompletedProcess[str], expected: int) -> None:
    assert result.returncode == expected, (
        f"expected exit {expected}, got {result.returncode}: {result.stderr[-400:]}"
    )


def _blocked(
    result: subprocess.CompletedProcess[str], *fragments: str, hidden: str = SENTINEL
) -> None:
    """The guard exited 1, said each fragment on stderr, and printed no plaintext term."""
    _expect_exit(result, 1)
    for fragment in fragments:
        assert fragment in result.stderr
    assert hidden not in result.stdout + result.stderr


def test_new_ref_does_not_rescan_history_already_on_destination(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _change(repo, "add historical fixture", {"historical.txt": TERM_TEXT})
    (repo / "historical.txt").unlink()
    destination_head = _commit(repo, "remove historical fixture")
    _git(repo, "push", "origin", f"{destination_head}:refs/heads/main")
    head = _change(repo, "docs only", {"README.md": "documentation only\n"})

    result = _push(repo, _update(head, _NEW_REF), "origin", str(origin))

    _expect_exit(result, 0)


def test_existing_ref_still_scans_commit_absent_from_destination(tmp_path: Path) -> None:
    scenario = _existing_topic_with_destination_main(tmp_path)
    local_only = _change(
        scenario.repo,
        "add local-only change",
        {"local-only.txt": "not on destination\n"},
        name="Local User",
        email="local@example.test",
    )
    records = f"refs/heads/topic {local_only} refs/heads/topic {scenario.remote_topic}\n"

    result = _push(scenario.repo, records, "origin", str(scenario.origin))

    _blocked(result, f"commit {local_only[:12]} author identity")
    assert scenario.destination_main[:12] not in result.stderr


def test_mixed_new_and_existing_updates_apply_destination_exclusions_to_both(
    tmp_path: Path,
) -> None:
    scenario = _existing_topic_with_destination_main(tmp_path)
    records = (
        f"refs/heads/topic {scenario.head} refs/heads/topic {scenario.remote_topic}\n"
        f"refs/heads/review {scenario.head} refs/heads/review {_NEW_REF}\n"
    )

    result = _push(scenario.repo, records, "origin", str(scenario.origin))

    _expect_exit(result, 0)


def test_existing_ref_destination_lookup_failure_uses_remote_sha_fallback(
    tmp_path: Path,
) -> None:
    repo, _initial = _repository(tmp_path)
    remote_topic = _change(
        repo,
        "add destination change",
        {"destination.txt": "destination history\n"},
        name="Destination User",
        email="destination@example.test",
    )
    head = _change(repo, "update documentation", {"README.md": "safe child\n"})
    records = f"refs/heads/topic {head} refs/heads/topic {remote_topic}\n"

    result = _push(repo, records, "origin", str(tmp_path / "missing.git"))

    _expect_exit(result, 0)


def test_new_ref_destination_lookup_failure_scans_full_ancestry(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    bad_ancestor = _change(
        repo,
        "add local-only ancestor",
        {"historical.txt": "not on destination\n"},
        name="Local User",
        email="local@example.test",
    )
    head = _change(repo, "update documentation", {"README.md": "safe tip\n"})

    result = _push(repo, _update(head, _NEW_REF), "origin", str(tmp_path / "missing.git"))

    _blocked(result, f"commit {bad_ancestor[:12]} author identity")


def test_docs_commit_does_not_rescan_unchanged_baseline_blob(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    (repo / "baseline.gz").write_bytes(b"\x1f\x8bnot-a-valid-gzip-stream")
    base = _commit(repo, "add destination baseline")
    head = _change(repo, "docs only", {"README.md": "documentation only\n"})

    _expect_exit(_push(repo, _update(head, base)), 0)


def test_pre_push_rejects_revision_option_instead_of_changing_scan_scope(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)

    result = _push(repo, _update("--all", _NEW_REF))

    _blocked(result, "could not complete")
    assert "--all" not in result.stderr


def test_blob_added_then_deleted_in_outgoing_history_is_blocked(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _change(repo, "add temporary fixture", {"temporary.txt": TERM_TEXT})
    (repo / "temporary.txt").unlink()
    head = _commit(repo, "remove temporary fixture")

    _blocked(_push(repo, _update(head, base)), "confidential term")


def test_new_ref_does_not_trust_history_reachable_only_from_another_remote(
    tmp_path: Path,
) -> None:
    repo, origin_head = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", f"{origin_head}:refs/heads/main")
    private_head = _change(repo, "add private fixture", {"private.txt": TERM_TEXT})
    _git(repo, "update-ref", "refs/remotes/private/topic", private_head)
    head = _change(repo, "docs only", {"README.md": "documentation only\n"})

    result = _push(repo, _update(head, _NEW_REF), "origin", str(origin))

    _blocked(result, "confidential term")


def test_new_ref_to_empty_destination_scans_full_history(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    head = _change(repo, "add fixture", {"payload.txt": TERM_TEXT})

    result = _push(repo, _update(head, _NEW_REF), "origin", str(_bare_remote(tmp_path)))

    _blocked(result, "confidential term")


def test_audit_finds_blob_deleted_later_in_history(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _change(repo, "add temporary fixture", {"temporary.txt": TERM_TEXT})
    (repo / "temporary.txt").unlink()
    head = _commit(repo, "remove temporary fixture")

    _blocked(_audit(repo, (head,)), "confidential term")


@pytest.mark.parametrize(
    "committer_email",
    [
        "37929162+mergify[bot]@users.noreply.github.com",
        "commit-signer@mergify.com",
        "untrusted@mergify.com",
    ],
)
def test_audit_checks_mergify_merge_commit_identities(
    tmp_path: Path, committer_email: str
) -> None:
    repo, _base = _repository(tmp_path)
    (repo / "bot.txt").write_text("clean bot-authored content\n", encoding="utf-8")
    _git(repo, "add", ".")
    bot = "37929162+mergify[bot]@users.noreply.github.com"
    _git(
        repo,
        "commit",
        "-m",
        "add bot-authored fixture",
        env=_identity_env("Mergify", bot) | {"GIT_COMMITTER_EMAIL": committer_email},
    )
    allowed = "37929162+mergify[[]bot[]]@users.noreply.github.com\ncommit-signer@mergify.com"

    result = _audit(repo, env={"BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS": allowed})

    if committer_email == "untrusted@mergify.com":
        _blocked(result, "committer identity")
    else:
        _expect_exit(result, 0)


def test_merge_introduced_blob_deleted_later_is_blocked(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _git(repo, "checkout", "-b", "side")
    _change(repo, "add clean side", {"side.txt": "clean side\n"})
    _git(repo, "checkout", "main")
    _change(repo, "add clean main", {"main.txt": "clean main\n"})
    _git(repo, "merge", "--no-ff", "--no-commit", "side", env=_identity_env())
    _change(repo, "merge side with fixture", {"merge-only.txt": TERM_TEXT})
    (repo / "merge-only.txt").unlink()
    head = _commit(repo, "remove merge fixture")

    _blocked(_push(repo, _update(head, base)), "confidential term")


def test_gitlink_update_scans_path_without_reading_commit_as_blob(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/example")
    head = _commit_index(repo, "add submodule entry")

    _expect_exit(_push(repo, _update(head, base)), 0)


def test_confidential_root_commit_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    head = _change(repo, "root fixture", {"payload.txt": TERM_TEXT})

    _blocked(_push(repo, _update(head, _NEW_REF)), "confidential term")


def test_renamed_blob_is_inspected_at_its_new_path(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    (repo / "baseline.gz").write_bytes(b"\x1f\x8bnot-a-valid-gzip-stream")
    base = _commit(repo, "add baseline fixture")
    (repo / "baseline.gz").rename(repo / "renamed.gz")
    head = _commit(repo, "rename fixture")

    _blocked(_push(repo, _update(head, base)), "could not complete")


def test_regular_file_changed_to_symlink_is_inspected(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    path = repo / "link"
    base = _change(repo, "add ordinary file", {"link": "ordinary content\n"})
    path.unlink()
    path.symlink_to(SENTINEL)
    head = _commit(repo, "change file to symlink")

    _blocked(_push(repo, _update(head, base)), "confidential term")


def test_multiple_updates_are_scanned_and_deletions_are_skipped(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    head = _change(repo, "add public fixture", {"clean.txt": "ordinary public content\n"})
    records = (
        f"refs/heads/topic {head} refs/heads/topic {base}\n"
        f"refs/heads/second {head} refs/heads/second {base}\n"
        f"(delete) {_NEW_REF} refs/heads/old {base}\n"
    )

    _expect_exit(_push(repo, records), 0)


def test_term_and_unapproved_identity_are_blocked_without_echoing_term(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    head = _change(
        repo, "add fixture", {"payload.txt": TERM_TEXT}, name="Unexpected User", email="u@e.test"
    )

    result = _push(repo, _update(head, base))

    _blocked(result, "confidential term", "identity not allowed")


def test_missing_secret_fails_closed(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    head = _change(repo, "add public fixture", {"clean.txt": "ordinary public content\n"})

    result = _guard(repo, "pre-push", stdin=_update(head, base))

    _blocked(result, "could not complete")


def test_clone_recovers_vocabulary_with_separately_restored_key(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    source_keys = Path(_seal(repo)[KEY_DIR])
    assert SENTINEL.encode() not in (repo / ".github/confidential-vocabulary.enc").read_bytes()
    _commit(repo, "track encrypted vocabulary")
    clone = tmp_path / "restored"
    _git(tmp_path, "clone", "--quiet", str(repo), str(clone))
    restored_keys = tmp_path / "restored-keys"
    restored = {KEY_DIR: str(restored_keys)}
    key_path = _guard(clone, "key-path", env=restored)
    assert key_path.returncode == 0, key_path.stderr
    assert _guard(clone, "pr-text", "--stdin", stdin="public title", env=restored).returncode == 1

    restored_keys.mkdir()
    key_file = Path(key_path.stdout.strip())
    shutil.copyfile(next(source_keys.glob("*.key")), key_file)
    key_file.chmod(0o600)
    detected = _guard(clone, "pr-text", "--stdin", stdin=SENTINEL, env=restored)
    _blocked(detected, "confidential term")
    draft = _guard(clone, "restore-draft", env=restored)
    assert draft.returncode == 0, draft.stderr
    assert (clone / ".git/booley-leak-guard.toml").read_bytes() == _config().encode()
    assert SENTINEL not in draft.stdout + draft.stderr


def test_restore_draft_refuses_plaintext_inside_worktree(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)

    result = _guard(repo, "restore-draft", env={"BOOLEY_LEAK_GUARD_CONFIG": "private.toml"})

    _blocked(result)
    assert not (repo / "private.toml").exists()


def test_candidate_encrypted_vocabulary_is_validated_against_trusted_key(
    tmp_path: Path,
) -> None:
    repo, _initial = _repository(tmp_path)
    _seal(repo)
    trusted = _commit(repo, "add valid encrypted vocabulary")
    _expect_exit(_verify(repo, trusted, trusted, _meta()), 0)

    (repo / ".github/confidential-vocabulary.enc").write_bytes(b"corrupt\n")
    broken = _commit(repo, "corrupt encrypted vocabulary")
    _git(repo, "checkout", trusted)

    _blocked(_verify(repo, broken, trusted, _meta()), "could not complete")


def test_candidate_scan_blocks_new_term_in_same_pr(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    _seal(repo)
    trusted = _commit(repo, "add initial encrypted vocabulary")
    new_term = "new-secret-654"
    _seal(repo, _words(SENTINEL, new_term))
    candidate = _commit(repo, "add new vocabulary term")
    _git(repo, "checkout", trusted)

    result = _verify(repo, candidate, trusted, _meta(body=new_term))

    _blocked(result, "confidential term", hidden=new_term)


@pytest.mark.parametrize(
    ("body", "blocked"), [(f"private context: {SENTINEL}", True), ("ordinary description", False)]
)
def test_pull_request_metadata_blocks_only_confidential_text(
    tmp_path: Path, body: str, blocked: bool
) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    event = _write_event(repo, _meta("ordinary title", body))

    result = _guard(repo, "pull-request", "--event", str(event))

    if blocked:
        _blocked(result, "pull request body")
    else:
        _expect_exit(result, 0)


def test_pr_text_blocks_terms_and_accepts_clean_title_and_body(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    draft = tmp_path / "body.md"
    draft.write_text(f"private detail: {SENTINEL}\n", encoding="utf-8")

    _blocked(_guard(repo, "pr-text", "--file", str(draft)), "confidential term")
    _blocked(_guard(repo, "pr-text", "--stdin", stdin=f"title {SENTINEL}"), "confidential term")
    draft.write_text("public description\n", encoding="utf-8")
    result = _guard(repo, "pr-text", "--file", str(draft), "--stdin", stdin="public title")
    _expect_exit(result, 0)


@pytest.mark.parametrize("prefix", ["", "prefixsynthetic-term-000suffix"])
def test_large_nonmatching_pr_text_scans_promptly(tmp_path: Path, prefix: str) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo, _words(*(f"synthetic-term-{index:03d}" for index in range(128))))
    start = perf_counter()

    result = _guard(repo, "pr-text", "--stdin", stdin=prefix + "~" * 1_000_000)

    elapsed = perf_counter() - start
    _expect_exit(result, 0)
    assert elapsed < _PROMPT_SCAN_S, f"nonmatching scan took {elapsed:.2f} seconds"


def test_prefilter_detects_term_across_large_blob_chunk_boundary(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    marker = "boundary-marker-321"
    boundary = 1024 * 1024
    head = _change(repo, "add large fixture", {"large.txt": "~" * (boundary - 3) + marker})

    result = _push(repo, _update(head, base), vocabulary=_words(marker))

    _blocked(result, "confidential term", hidden=marker)


_FILE_TERMS = _words("file", "secret", "key")
_STEALTH = (
    '[stealth]\nwords = ["quokka"]\nbanned_words = ["narwhal__"]\nbanned_substrings = ["booley"]\n'
)


@pytest.mark.parametrize(
    ("vocabulary", "text", "blocked"),
    [
        (_FILE_TERMS, "f\u0130le", True),  # the ASCII prefilter keeps Unicode case folding
        (_FILE_TERMS, "f\u0131le", True),
        (_FILE_TERMS, "\u017fecret", True),
        (_FILE_TERMS, "\u212aey", True),
        (_words("caf\u00e9"), "CAF\u00c9", True),  # a non-ASCII term uses full Unicode matching
        (_words("generated"), "ordinary generated prose", True),
        (_words("Acme", "AcmeCloud", "abc", "abc-def"), "AcmeCloud", True),  # overlapping terms
        (_words("Acme", "AcmeCloud", "abc", "abc-def"), "abc-defg", True),
        (_STEALTH, "path/quokka_file", True),  # a word is bounded by non-alphanumerics
        (_STEALTH, "myquokkafile QuokkaRunner", False),
        (_STEALTH, "narwhal__private_suffix", True),  # a `__` suffix makes the term a prefix
        (_STEALTH, "narwhal__", False),
        (_STEALTH, "booley here", False),  # banned_substrings are not vocabulary
    ],
)
def test_pr_text_matches_vocabulary(
    tmp_path: Path, vocabulary: str, text: str, blocked: bool
) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo, vocabulary)

    result = _guard(repo, "pr-text", "--stdin", stdin=text)

    _expect_exit(result, 1 if blocked else 0)
    assert ("confidential term" in result.stderr) is blocked
    shown = result.stdout + result.stderr
    assert text not in shown
    assert "quokka" not in shown
    assert "narwhal__" not in shown


def test_prefix_term_redacts_the_whole_identifier_in_a_path(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    head = _change(repo, "add prefixed path", {"narwhal__private_suffix.txt": "clean\n"})

    result = _push(repo, _update(head, base), vocabulary=_words("narwhal__"))

    _blocked(result, "[redacted].txt", hidden="narwhal__")
    assert "private_suffix" not in result.stderr


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-config", "could not complete"),
        ("old-ci-env", "could not complete"),
        ("missing-input", "could not complete"),
        ("unreadable-draft", "could not complete"),
        ("too-large", "exceeds the inspection size limit"),
        ("tampered", "could not complete"),
    ],
)
def test_pr_text_fails_closed(tmp_path: Path, case: str, message: str) -> None:
    repo, _base = _repository(tmp_path)
    draft = tmp_path / "body.md"
    args, stdin, env = ["--stdin"], "public title", {}
    if case not in {"missing-config", "old-ci-env"}:
        _seal(repo)
    if case == "old-ci-env":
        env = {"BOOLEY_LEAK_GUARD_CONFIG_B64": base64.b64encode(_config().encode()).decode()}
    if case == "missing-input":
        args, stdin = [], ""
    if case == "unreadable-draft":
        args = ["--file", str(tmp_path / "missing.md")]
    if case == "too-large":
        draft.write_bytes(b"x" * (1024 * 1024 + 1))
        args = ["--file", str(draft)]
    if case == "tampered":
        encrypted = repo / ".github/confidential-vocabulary.enc"
        header, locator, encoded = encrypted.read_bytes().splitlines()
        payload = bytearray(base64.b64decode(encoded, validate=True))
        payload[-1] ^= 1
        encrypted.write_bytes(b"\n".join((header, locator, base64.b64encode(payload), b"")))

    result = _guard(repo, "pr-text", *args, stdin=stdin, env=env)

    _blocked(result, message)
    assert "missing.md" not in result.stderr


def test_pr_text_uses_sealed_file_over_private_draft_and_stale_ci_env(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    (repo / ".git/booley-leak-guard.toml").write_text("[private]\nwords = ['other']\n")
    stale = base64.b64encode(_config(_words("other")).encode()).decode()

    result = _guard(
        repo, "pr-text", "--stdin", stdin=SENTINEL, env={"BOOLEY_LEAK_GUARD_CONFIG_B64": stale}
    )

    _blocked(result, "confidential term")


@_GH_BY_NAME
@pytest.mark.parametrize(
    ("arguments", "gh_args", "stdin"),
    [
        (
            "create --base main --head topic --title-file {title} --body-file {body} --repo owner/repo",
            "pr create --base main --head topic --title {name} --body-file - --repo owner/repo",
            "Public description\n",
        ),
        ("edit --pr 123 --title-file {title}", "pr edit 123 --title {name}", ""),
        (
            "comment --pr 123 --body-file {body} --edit-last",
            "pr comment 123 --edit-last --body-file -",
            "Public description\n",
        ),
        (
            "review --pr 123 --verdict request-changes --body-file {body}",
            "pr review 123 --request-changes --body-file -",
            "Public description\n",
        ),
    ],
)
def test_publish_pr_sends_exact_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: str, gh_args: str, stdin: str
) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    title, body = tmp_path / "title.txt", tmp_path / "body.md"
    title.write_text("Public-title\n", encoding="utf-8")
    body.write_text("Public description\n", encoding="utf-8")
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    gh.answer("pr", reads_input=True)
    command = arguments.format(title=title, body=body).split()

    result = _guard(repo, "publish-pr", *command, env={"GH_REPO": "somewhere/else"})

    _expect_exit(result, 0)
    (call,) = gh.calls()
    assert call.args == gh_args.format(name="Public-title").split()
    assert call.input == stdin
    assert "GH_REPO" not in call.env


@pytest.mark.parametrize(
    ("case", "message"),
    [("confidential", "confidential term"), ("old-ci-env", "could not complete")],
)
def test_publish_pr_stops_before_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, message: str
) -> None:
    repo, _base = _repository(tmp_path)
    title, body = tmp_path / "title.txt", tmp_path / "body.md"
    title.write_text("Public title", encoding="utf-8")
    body.write_text(f"Sensitive context: {SENTINEL}", encoding="utf-8")
    env = {}
    if case == "confidential":
        _seal(repo)
    else:
        env = {"BOOLEY_LEAK_GUARD_CONFIG_B64": base64.b64encode(_config().encode()).decode()}
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    args = ["create", "--base", "main", "--head", "topic", "--title-file", str(title)]

    result = _guard(repo, "publish-pr", *args, "--body-file", str(body), env=env)

    _blocked(result, message)
    assert gh.calls() == []


@_GH_BY_NAME
def test_publish_pr_reports_only_the_created_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    body = tmp_path / "body.md"
    body.write_text("Public response", encoding="utf-8")
    reference = "https://github.com/owner/name/pull/123#issuecomment-4567"
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    gh.answer("pr", stdout=f"unrelated {SENTINEL} output\n{reference}\n", reads_input=True)
    gh.answer("pr", stdout="no reference here\n", reads_input=True)
    arguments = ["publish-pr", "comment", "--pr", "123", "--body-file", str(body)]

    reported = _guard(repo, *arguments)
    silent = _guard(repo, *arguments)

    _expect_exit(reported, 0)
    assert f"GitHub reference: {reference}" in reported.stdout.splitlines()
    assert SENTINEL not in reported.stdout + reported.stderr
    _expect_exit(silent, 0)
    assert "GitHub reference" not in silent.stdout


@_GH_BY_NAME
def test_sync_ci_key_uses_sealed_vocabulary_and_not_stale_ci_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    (repo / ".git/booley-leak-guard.toml").write_text("[private]\nwords = ['other']\n")
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    gh.answer("secret", reads_input=True)
    stale = {"BOOLEY_LEAK_GUARD_KEY_B64": base64.b64encode(b"stale key").decode()}

    result = _guard(repo, "sync-ci-key", env=stale | {"GH_REPO": "somewhere/else"})

    _expect_exit(result, 0)
    (call,) = gh.calls()
    assert call.args == ["secret", "set", "BOOLEY_LEAK_GUARD_KEY_B64", "--app", "actions"]
    assert call.input is not None
    assert len(base64.b64decode(call.input)) == 32
    assert not {"GH_REPO", "BOOLEY_LEAK_GUARD_KEY_B64"} & set(call.env)
    assert SENTINEL not in result.stdout + result.stderr


def test_sync_ci_key_fails_when_sealed_file_is_missing(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    stale = base64.b64encode(_config().encode()).decode()

    result = _guard(repo, "sync-ci-key", env={"BOOLEY_LEAK_GUARD_CONFIG_B64": stale})

    _blocked(result, "could not complete")


@_GH_BY_NAME
def test_sync_ci_key_does_not_echo_gh_failure_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repository(tmp_path)
    _seal(repo)
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    gh.answer("secret", stderr=SENTINEL, returncode=1, reads_input=True)

    _blocked(_guard(repo, "sync-ci-key"), "could not complete")


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

    allowed_identity = (
        "BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: |\n"
        "            37929162+mergify[[]bot[]]@users.noreply.github.com\n"
        "            commit-signer@mergify.com"
    )
    assert allowed_identity in pr_scan
    assert allowed_identity in main_scan


RENOVATE_BOT_EMAIL = "353471265+booley-renovate[bot]@users.noreply.github.com"


def _workflow_allowed_authors(step_name: str, next_step_name: str) -> str:
    """Return one scan step's shipped BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS value."""
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )
    step = workflow.split(f"- name: {step_name}", 1)[1].split(f"- name: {next_step_name}", 1)[0]
    block = step.split("BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS: |\n", 1)[1]
    patterns = []
    for line in block.splitlines():
        # The block scalar ends at the first line that is not a bare pattern.
        if not line.startswith("            ") or ":" in line:
            break
        patterns.append(line.strip())
    return "\n".join(patterns)


@pytest.mark.parametrize(
    ("step_name", "next_step_name"),
    [
        ("Scan pull-request commits", "Scan complete main history"),
        ("Scan complete main history", "Publish scan status"),
    ],
)
@pytest.mark.parametrize(
    ("author_email", "allowed"),
    [
        (RENOVATE_BOT_EMAIL, True),
        # An unescaped "[bot]" glob is a character class; this lookalike would pass it.
        ("353471265+booley-renovateb@users.noreply.github.com", False),
        # Same App name with another account ID is a different identity.
        ("1+booley-renovate[bot]@users.noreply.github.com", False),
    ],
)
def test_workflow_trusts_only_the_exact_renovate_app_identity(
    tmp_path: Path, step_name: str, next_step_name: str, author_email: str, allowed: bool
) -> None:
    repo, _base = _repository(tmp_path)
    (repo / "deps.txt").write_text("clean dependency bump\n", encoding="utf-8")
    _git(repo, "add", ".")
    # Renovate commits locally with gitAuthor as both author and committer.
    _git(
        repo,
        "commit",
        "-m",
        "bump a dependency",
        env=_identity_env("booley-renovate[bot]", author_email),
    )
    head = _git(repo, "rev-parse", "HEAD")
    env = _seal(repo) | {
        "BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS": _workflow_allowed_authors(step_name, next_step_name),
    }

    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(repo), "audit", "--rev", head],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    if allowed:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert "author identity" in result.stderr


def _self_test(*, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``self-test`` in a subprocess, optionally shadowing imports via PYTHONPATH."""
    env = os.environ.copy()
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return run_bounded_child(
        [sys.executable, str(SCANNER), "self-test"],
        check=False,
        env=env,
    )


def test_self_test_passes_with_the_scanner_dependency() -> None:
    """The CI smoke job needs no key, repository, or network, only the dependency."""
    result = _self_test()

    assert result.returncode == 0, result.stderr
    assert "self-test" in result.stdout


def test_self_test_fails_when_the_dependency_is_broken(tmp_path: Path) -> None:
    """A dependency that cannot authenticate (or import) must fail the smoke job."""
    shadow = tmp_path / "cryptography"
    (shadow / "hazmat/primitives/ciphers").mkdir(parents=True)
    for package in (shadow, shadow / "hazmat", shadow / "hazmat/primitives"):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "hazmat/primitives/ciphers/__init__.py").write_text("", encoding="utf-8")
    (shadow / "hazmat/primitives/ciphers/aead.py").write_text(
        "class AESGCM:\n"
        "    def __init__(self, key):\n"
        "        pass\n"
        "    def encrypt(self, nonce, data, aad):\n"
        "        return data + bytes(16)\n"
        "    def decrypt(self, nonce, data, aad):\n"
        "        return data[:-16]\n",
        encoding="utf-8",
    )
    (shadow / "exceptions.py").write_text(
        "class InvalidTag(Exception):\n    pass\n", encoding="utf-8"
    )

    result = _self_test(pythonpath=tmp_path)

    assert result.returncode == 1
    assert result.stderr.strip()
    assert "Traceback" not in result.stderr


# --- Provenance-aware scanning (issue #3330) ---------------------------------

OUTSIDER = ("Outside Contributor", "outsider@example.test")
SAFE = ("Safe User", "safe@example.test")
OLD_DATE = "2020-01-01T00:00:00+0000"
NEW_DATE = "2021-01-01T00:00:00+0000"
REPOSITORY_OWNER = "boldaxolotl"


def _branch_commits(
    repo: Path,
    branch: str,
    commits: list[tuple[tuple[str, str], dict[str, str]]],
    *,
    start: str = "main",
    date: str | None = None,
) -> str:
    """Create `branch` at `start` with one commit per (identity, files), then return to main."""
    _git(repo, "checkout", "-b", branch, start)
    for index, (identity, files) in enumerate(commits):
        for name, text in files.items():
            (repo / name).write_bytes(text.encode())
        _git(repo, "add", ".")
        env = _identity_env(*identity)
        if date is not None:
            env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(repo, "commit", "-m", f"change {index}", env=env)
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    return tip


def _merge_pull_request(
    repo: Path,
    branch: str,
    *,
    owner: str,
    number: int,
    title: str,
    extra_file: tuple[str, str] | None = None,
    committer: tuple[str, str] | None = None,
    suffix: str = "",
) -> str:
    """Merge `branch` into the current branch with GitHub's merge-commit message."""
    _git(repo, "merge", "--no-ff", "--no-commit", branch, env=_identity_env())
    if extra_file is not None:
        (repo / extra_file[0]).write_bytes(extra_file[1].encode())
    _git(repo, "add", ".")
    env = _identity_env()
    if committer is not None:
        env |= {"GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
    message = f"Merge pull request #{number} from {owner}/{branch}{suffix}\n\n{title}"
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _outside_pull_request_history(repo: Path) -> None:
    """A fork PR carrying a term in identity, message, path, blob, branch and title."""
    branch = f"outsider-{SENTINEL}"
    _branch_commits(
        repo,
        branch,
        [
            (
                (f"Outsider {SENTINEL}", "outsider@example.test"),
                {f"{SENTINEL}.txt": TERM_TEXT},
            )
        ],
    )
    _merge_pull_request(repo, branch, owner="outsider", number=1, title=f"fix {SENTINEL}")
    (repo / "after.txt").write_bytes(b"clean maintainer follow-up\n")
    _commit(repo, "maintainer follow-up")


def test_audit_exempts_commits_merged_from_outside_pull_request(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _outside_pull_request_history(repo)

    result = _audit(repo, owner=REPOSITORY_OWNER)

    assert result.returncode == 0, result.stderr
    assert "advisory" in result.stderr.lower()
    assert "ERROR" not in result.stderr + result.stdout
    assert SENTINEL not in result.stderr + result.stdout


def _case_unlisted_identity(repo: Path) -> tuple[str, str]:
    (repo / "a.txt").write_bytes(b"clean\n")
    _commit(repo, "change", name="Unexpected User", email="unexpected@example.test")
    return REPOSITORY_OWNER, "identity not allowed"


def _case_unlisted_identity_before_outside_merge(repo: Path) -> tuple[str, str]:
    """An unattributable commit already on the first-parent line stays enforced."""
    (repo / "a.txt").write_bytes(b"clean\n")
    _commit(repo, "change", name="Unexpected User", email="unexpected@example.test")
    _outside_pull_request_history(repo)
    return REPOSITORY_OWNER, "identity not allowed"


def _case_term_in_first_parent_commit(repo: Path) -> tuple[str, str]:
    (repo / "a.txt").write_bytes(TERM_TEXT.encode())
    _commit(repo, "change")
    return REPOSITORY_OWNER, "confidential term"


def _case_inexact_merge_message(repo: Path) -> tuple[str, str]:
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, {"o.txt": "clean\n"})])
    _merge_pull_request(
        repo, "outsider-fix", owner="outsider", number=2, title="fix", suffix=" (edited)"
    )
    return REPOSITORY_OWNER, "identity not allowed"


def _case_octopus_merge_only_file(repo: Path) -> tuple[str, str]:
    _branch_commits(repo, "side-a", [(SAFE, {"a.txt": "a\n"})])
    _branch_commits(repo, "side-b", [(SAFE, {"b.txt": "b\n"})])
    _git(repo, "merge", "--no-commit", "side-a", "side-b", env=_identity_env())
    (repo / "octopus-only.txt").write_bytes(TERM_TEXT.encode())
    _commit(repo, "octopus merge")
    return REPOSITORY_OWNER, "octopus-only.txt"


def _case_allowlisted_author_in_outside_range(repo: Path) -> tuple[str, str]:
    _branch_commits(
        repo,
        "outsider-fix",
        [(OUTSIDER, {"o.txt": "clean\n"}), (SAFE, {"s.txt": TERM_TEXT})],
    )
    _merge_pull_request(repo, "outsider-fix", owner="outsider", number=3, title="fix")
    return REPOSITORY_OWNER, "confidential term"


def _case_attributing_merge_adds_term(repo: Path) -> tuple[str, str]:
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, {"o.txt": "clean\n"})])
    _merge_pull_request(
        repo,
        "outsider-fix",
        owner="outsider",
        number=4,
        title="fix",
        extra_file=("merge-only.txt", TERM_TEXT),
    )
    return REPOSITORY_OWNER, "merge-only.txt"


def _case_attributing_merge_unlisted_committer(repo: Path) -> tuple[str, str]:
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, {"o.txt": "clean\n"})])
    _merge_pull_request(
        repo, "outsider-fix", owner="outsider", number=5, title="fix", committer=OUTSIDER
    )
    return REPOSITORY_OWNER, "committer identity"


def _case_owner_with_trailing_space(repo: Path) -> tuple[str, str]:
    _outside_pull_request_history(repo)
    return f"{REPOSITORY_OWNER} ", "could not complete"


def _case_forged_merge_on_second_parent_side(repo: Path) -> tuple[str, str]:
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, {"o.txt": "clean\n"})])
    _git(repo, "checkout", "-b", "feature", "main")
    _merge_pull_request(repo, "outsider-fix", owner="outsider", number=6, title="forged")
    _git(repo, "checkout", "main")
    _merge_pull_request(repo, "feature", owner=REPOSITORY_OWNER, number=7, title="feature")
    return REPOSITORY_OWNER, "identity not allowed"


_ENFORCED_CASES = {
    "unlisted-identity": _case_unlisted_identity,
    "unlisted-identity-before-outside-merge": _case_unlisted_identity_before_outside_merge,
    "term-in-first-parent-commit": _case_term_in_first_parent_commit,
    "inexact-merge-message": _case_inexact_merge_message,
    "octopus-merge-only-file": _case_octopus_merge_only_file,
    "allowlisted-author-in-outside-range": _case_allowlisted_author_in_outside_range,
    "attributing-merge-adds-term": _case_attributing_merge_adds_term,
    "attributing-merge-unlisted-committer": _case_attributing_merge_unlisted_committer,
    "owner-with-trailing-space": _case_owner_with_trailing_space,
    "forged-merge-on-second-parent-side": _case_forged_merge_on_second_parent_side,
}


@pytest.mark.parametrize("case", sorted(_ENFORCED_CASES))
def test_audit_enforces_unattributed_or_maintainer_work(tmp_path: Path, case: str) -> None:
    repo, _base = _repository(tmp_path)
    owner, fragment = _ENFORCED_CASES[case](repo)

    result = _audit(repo, owner=owner)

    assert result.returncode == 1, result.stderr
    assert fragment in result.stderr
    assert SENTINEL not in result.stderr + result.stdout


def test_audit_without_owner_exempts_nothing(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    _outside_pull_request_history(repo)

    result = _audit(repo)

    assert result.returncode == 1
    assert "identity not allowed" in result.stderr


def test_audit_with_owner_refuses_multiple_revisions(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "a.txt").write_bytes(b"clean\n")
    head = _commit(repo, "change")

    result = _audit(repo, (head, base), owner=REPOSITORY_OWNER)

    assert result.returncode == 1
    assert "could not complete" in result.stderr


def test_pre_push_merge_of_main_skips_inherited_outside_content(tmp_path: Path) -> None:
    repo, initial = _repository(tmp_path)
    (repo / "outside.txt").write_bytes(TERM_TEXT.encode())
    main_tip = _commit(repo, "outside work", name=OUTSIDER[0], email=OUTSIDER[1])
    _git(repo, "checkout", "-b", "topic", initial)
    (repo / "topic.txt").write_bytes(b"clean topic\n")
    _commit(repo, "topic work")
    _git(repo, "merge", "--no-ff", "main", "-m", "merge main", env=_identity_env())
    head = _git(repo, "rev-parse", "HEAD")

    result = _push(repo, _update(head, main_tip))

    assert result.returncode == 0, result.stderr


def test_pre_push_merge_still_reports_content_only_the_merge_introduces(tmp_path: Path) -> None:
    repo, initial = _repository(tmp_path)
    _branch_commits(repo, "side", [(SAFE, {"side.txt": "clean side\n"})])
    main_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "topic", initial)
    (repo / "topic.txt").write_bytes(b"clean topic\n")
    _commit(repo, "topic work")
    _git(repo, "merge", "--no-ff", "--no-commit", "side", env=_identity_env())
    (repo / "merge-only.txt").write_bytes(TERM_TEXT.encode())
    head = _commit(repo, "evil merge")

    result = _push(repo, _update(head, main_tip))

    assert result.returncode == 1
    assert "merge-only.txt" in result.stderr


def _trusted_tip_repository(tmp_path: Path) -> tuple[Path, str]:
    """main's tip holds outside content; `topic` branches from before it."""
    repo, _initial = _repository(tmp_path)
    _seal(repo)
    vocabulary = _commit(repo, "add vocabulary")
    _git(repo, "branch", "topic", vocabulary)
    (repo / "outside.txt").write_bytes(TERM_TEXT.encode())
    (repo / f"{SENTINEL}-tip.txt").write_bytes(b"tip clean\n")
    tip = _commit(repo, "outside work", name=OUTSIDER[0], email=OUTSIDER[1])
    _git(repo, "checkout", "topic")
    return repo, tip


def _pr_scan(
    repo: Path, command: str, *, head: str, base: str, trusted_base: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the CI PR scan for one command; verify-candidate also needs an event."""
    trusted = ["--trusted-base", trusted_base] if trusted_base is not None else []
    if command == "pre-push":
        return _push(repo, _update(head, base), *trusted)
    _seal(repo)
    return _verify(repo, head, base, _meta(body=""), *trusted)


@pytest.mark.parametrize("command", ["pre-push", "verify-candidate"])
@pytest.mark.parametrize(
    ("case", "files", "blocked"),
    [
        ("identical-entry-restored", {"outside.txt": TERM_TEXT}, False),
        ("same-blob-at-new-path", {"copy.txt": TERM_TEXT}, True),
        ("edited-outside-file-keeps-term", {"outside.txt": TERM_TEXT + "more\n"}, True),
        ("term-named-tip-path-changed-content", {f"{SENTINEL}-tip.txt": "changed\n"}, True),
    ],
)
def test_trusted_base_skips_only_identical_entries(
    tmp_path: Path, command: str, case: str, files: dict[str, str], blocked: bool
) -> None:
    repo, tip = _trusted_tip_repository(tmp_path)
    for name, text in files.items():
        (repo / name).write_bytes(text.encode())
    head = _commit(repo, f"maintainer {case}")

    result = _pr_scan(repo, command, head=head, base=tip, trusted_base=tip)

    assert result.returncode == (1 if blocked else 0), result.stderr
    assert SENTINEL not in result.stderr


def test_identical_entry_is_scanned_without_trusted_base(tmp_path: Path) -> None:
    repo, tip = _trusted_tip_repository(tmp_path)
    (repo / "outside.txt").write_bytes(TERM_TEXT.encode())
    head = _commit(repo, "maintainer restores outside entry")

    result = _pr_scan(repo, "pre-push", head=head, base=tip, trusted_base=None)

    assert result.returncode == 1
    assert "confidential term" in result.stderr


@pytest.mark.parametrize("bad", ["not-an-oid", "tree", "missing"])
def test_trusted_base_refuses_a_value_that_is_not_a_commit_oid(tmp_path: Path, bad: str) -> None:
    repo, tip = _trusted_tip_repository(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    value = {
        "not-an-oid": "not-an-oid",
        "tree": _git(repo, "rev-parse", f"{tip}^{{tree}}"),
        "missing": "1" * 40,
    }[bad]

    result = _pr_scan(repo, "pre-push", head=head, base=tip, trusted_base=value)

    assert result.returncode == 1
    assert "could not complete" in result.stderr


def test_audit_maintainer_merge_of_main_skips_attributed_outside_content(tmp_path: Path) -> None:
    repo, initial = _repository(tmp_path)
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, {"outside.txt": TERM_TEXT})])
    _merge_pull_request(repo, "outsider-fix", owner="outsider", number=1, title="fix")
    _branch_commits(repo, "topic", [(SAFE, {"topic.txt": "clean topic\n"})], start=initial)
    _git(repo, "checkout", "topic")
    _git(repo, "merge", "--no-ff", "main", "-m", "merge main", env=_identity_env())
    _git(repo, "checkout", "main")
    _merge_pull_request(repo, "topic", owner=REPOSITORY_OWNER, number=2, title="topic")

    result = _audit(repo, owner=REPOSITORY_OWNER)

    assert result.returncode == 0, result.stderr


def test_audit_maintainer_edit_of_merged_outside_file_is_enforced(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, {"outside.txt": "clean\n"})])
    _merge_pull_request(repo, "outsider-fix", owner="outsider", number=1, title="fix")
    (repo / "outside.txt").write_bytes(TERM_TEXT.encode())
    _commit(repo, "maintainer edit keeping a term")

    result = _audit(repo, owner=REPOSITORY_OWNER)

    assert result.returncode == 1
    assert "outside.txt" in result.stderr


@pytest.mark.parametrize("with_maintainer_commit", [True, False])
def test_audit_enforces_blob_introduced_by_maintainer_and_outside_commit(
    tmp_path: Path, with_maintainer_commit: bool
) -> None:
    repo, initial = _repository(tmp_path)
    maintainer = None
    if with_maintainer_commit:
        (repo / "maintainer.txt").write_bytes(TERM_TEXT.encode())
        _git(repo, "add", ".")
        env = _identity_env() | {"GIT_AUTHOR_DATE": OLD_DATE, "GIT_COMMITTER_DATE": OLD_DATE}
        _git(repo, "commit", "-m", "maintainer adds blob", env=env)
        maintainer = _git(repo, "rev-parse", "HEAD")
    # The outside commit is newer, so rev-list visits it before the maintainer commit.
    _branch_commits(
        repo, "outsider-fix", [(OUTSIDER, {"copy.txt": TERM_TEXT})], start=initial, date=NEW_DATE
    )
    _merge_pull_request(repo, "outsider-fix", owner="outsider", number=1, title="fix")

    result = _audit(repo, owner=REPOSITORY_OWNER)

    if maintainer is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert maintainer[:12] in result.stderr
        assert "confidential term" in result.stderr


def _same_repository_topic(repo: Path, case: str) -> None:
    """Create `topic` (left checked out on main) holding the maintainer-owned violation."""
    if case == "evil-merge":
        _branch_commits(repo, "side", [(SAFE, {"side.txt": "clean side\n"})])
        _branch_commits(repo, "topic", [(SAFE, {"topic.txt": "clean topic\n"})])
        _git(repo, "checkout", "topic")
        _git(repo, "merge", "--no-ff", "--no-commit", "side", env=_identity_env())
        (repo / "merge-only.txt").write_bytes(TERM_TEXT.encode())
        _commit(repo, "evil merge")
        _git(repo, "checkout", "main")
        return
    identity, files = {
        "identity": (("Unexpected User", "unexpected@example.test"), {"a.txt": "clean\n"}),
        "term": (SAFE, {"a.txt": TERM_TEXT}),
    }[case]
    _branch_commits(repo, "topic", [(identity, files)])


_SAME_REPOSITORY_CASES = {
    "identity": "identity not allowed",
    "term": "confidential term",
    "evil-merge": "merge-only.txt",
}


@pytest.mark.parametrize("mode", ["pre-push", "audit"])
@pytest.mark.parametrize("case", sorted(_SAME_REPOSITORY_CASES))
def test_same_repository_range_stays_enforced(tmp_path: Path, mode: str, case: str) -> None:
    repo, base = _repository(tmp_path)
    _same_repository_topic(repo, case)
    if mode == "pre-push":
        head = _git(repo, "rev-parse", "topic")
        result = _push(repo, _update(head, base), "--trusted-base", base)
    else:
        # A same-repository PR merge: the owner differs from the branch's only by case.
        _merge_pull_request(repo, "topic", owner="BoldAxolotl", number=9, title="topic")
        result = _audit(repo, owner=REPOSITORY_OWNER)

    assert result.returncode == 1, result.stderr
    assert _SAME_REPOSITORY_CASES[case] in result.stderr


def test_pre_push_scans_fork_head_advertised_under_refs_pull(tmp_path: Path) -> None:
    repo, initial = _repository(tmp_path)
    origin = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", f"{initial}:refs/heads/main")
    fork_head = _branch_commits(repo, "fork", [(OUTSIDER, {"fork.txt": TERM_TEXT})])
    _git(repo, "push", "origin", f"{fork_head}:refs/pull/5/head")
    _git(repo, "checkout", "-b", "topic", fork_head)
    (repo / "topic.txt").write_bytes(b"clean topic\n")
    head = _commit(repo, "topic work")

    result = _push(repo, _update(head, _NEW_REF), "origin", str(origin))

    assert result.returncode == 1
    assert "confidential term" in result.stderr


def test_advisory_findings_do_not_displace_enforced_finding(tmp_path: Path) -> None:
    repo, initial = _repository(tmp_path)
    (repo / "maintainer.txt").write_bytes(TERM_TEXT.encode())
    _commit(repo, "maintainer adds a term blob")
    # 101 advisory path findings arise in the commit loop; the one enforced
    # finding is a blob finding, added only in the later blob pass.
    outside_files = {f"{SENTINEL}-{index}.txt": "clean\n" for index in range(101)}
    _branch_commits(repo, "outsider-fix", [(OUTSIDER, outside_files)], start=initial)
    _merge_pull_request(repo, "outsider-fix", owner="outsider", number=1, title="fix")

    result = _audit(repo, owner=REPOSITORY_OWNER)

    assert result.returncode == 1
    assert "maintainer.txt" in result.stderr
    assert "output limited to 100 advisory findings" in result.stderr
    assert SENTINEL not in result.stderr + result.stdout


def test_workflow_main_audit_passes_repository_owner() -> None:
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )
    main_scan = workflow.split("- name: Scan complete main history", 1)[1].split(
        "- name: Publish scan status", 1
    )[0]

    assert "REPOSITORY_OWNER: ${{ github.repository_owner }}" in main_scan
    assert '--repository-owner "${REPOSITORY_OWNER}"' in main_scan


def test_workflow_pr_scan_trusts_base_tip_only_for_main() -> None:
    workflow = (SCANNER.parent.parent / "workflows/confidential-content.yml").read_text(
        encoding="utf-8"
    )
    pr_scan = workflow.split("- name: Scan pull-request commits", 1)[1].split(
        "- name: Scan complete main history", 1
    )[0]

    assert "BASE_REF: ${{ github.event.pull_request.base.ref }}" in pr_scan
    assert '"${BASE_REF}" = main' in pr_scan
    assert '--trusted-base "${BASE_SHA}"' in pr_scan
    assert pr_scan.count('"${trusted_base[@]}"') == 2


# --- Advisory scans for outside PRs and Mergify candidates (issue #3331) ------

WORKFLOW = SCANNER.parent.parent / "workflows/confidential-content.yml"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt" or shutil.which("jq") is None,
    reason="the fake gh runs the real jq selector, and the scan step needs bash",
)
MERGIFY_USER = {"id": 37929162, "type": "Bot"}
_FAKE_GH_SCRIPT = """\
import json, os, subprocess, sys

state = os.environ["FAKE_GH_DIR"]
arguments = sys.argv[1:]


def record(**entry):
    with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\\n")


# The code under test must run gh non-interactively and without an ambient repository.
if "GH_REPO" in os.environ or os.environ.get("GH_PROMPT_DISABLED") != "1":
    sys.exit(2)
if arguments[:1] != ["api"]:
    sys.exit(2)
arguments = arguments[1:]
if "--method" in arguments:
    # A request body is only accepted verbatim through `--input -`, never as fields.
    if any(flag in arguments for flag in ("-f", "-F", "--field", "--raw-field")):
        sys.exit(2)
    if arguments[-2:] != ["--input", "-"]:
        sys.exit(2)
    endpoint = arguments[arguments.index("--method") + 2]
    body = json.loads(sys.stdin.read())
    failing = os.path.exists(os.path.join(state, "fail")) and endpoint.endswith(
        open(os.path.join(state, "fail"), encoding="utf-8").read().strip()
    )
    record(method="POST", endpoint=endpoint, body=body, failed=failing)
    sys.exit(1 if failing else 0)
if "--paginate" in arguments:
    selector = arguments[arguments.index("--jq") + 1]
    endpoint = arguments[arguments.index("--paginate") + 1]
    record(method="GET", endpoint=endpoint, jq=selector)
    if "/pulls?" in endpoint:
        if os.path.exists(os.path.join(state, "fail-pulls-lookup")):
            sys.exit(1)
        if "state=open" not in endpoint:
            sys.exit(2)  # only open pull requests share a head commit with live work
        name = "open-pulls.json"
    else:
        name = "comments.json"
    path = os.path.join(state, name)
    page = open(path, encoding="utf-8").read() if os.path.exists(path) else "[]"
    # gh applies the --jq selector to each page of the full API response: run the real jq.
    jq = subprocess.run(["jq", "-r", selector], input=page, capture_output=True, text=True)
    if jq.returncode:
        sys.exit(1)
    sys.stdout.write(jq.stdout)
    raw = os.path.join(state, "comments.raw")
    if "/pulls?" not in endpoint and os.path.exists(raw):
        sys.stdout.write(open(raw, encoding="utf-8").read())
    sys.exit(0)
endpoint = arguments[0]
record(method="GET", endpoint=endpoint)
path = os.path.join(state, "pulls", endpoint.rsplit("/", 1)[1] + ".json")
if not os.path.exists(path):
    sys.exit(1)
sys.stdout.write(open(path, encoding="utf-8").read())
"""


class _FakeGh:
    """A `gh` on PATH that serves PR fixtures and records every call it receives."""

    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "fake-gh"
        bin_dir = self.state / "bin"
        (self.state / "pulls").mkdir(parents=True)
        bin_dir.mkdir()
        script = self.state / "fake_gh.py"
        script.write_text(_FAKE_GH_SCRIPT, encoding="utf-8", newline="\n")
        for name, target in (("gh", script), ("python", None)):
            shim = bin_dir / name
            command = f'"{sys.executable}" "{target}"' if target else f'"{sys.executable}"'
            shim.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8", newline="\n")
            shim.chmod(0o755)
        self.env = os.environ | {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_DIR": str(self.state),
            "GH_REPO": "somewhere/else",
        }

    def serve_pull(
        self, number: int, *, head_sha: str, head_repo_id: int | None, association: str
    ) -> None:
        pull = {
            "author_association": association,
            "head": {"sha": head_sha, "repo": {"id": head_repo_id} if head_repo_id else None},
            "base": {"repo": {"id": 1}},
        }
        (self.state / "pulls" / f"{number}.json").write_text(json.dumps(pull), encoding="utf-8")

    def serve_open_pulls(self, *pulls: dict | None) -> None:
        """Serve the open pull requests the list returns; `None` makes the lookup fail."""
        failure = self.state / "fail-pulls-lookup"
        failure.unlink(missing_ok=True)
        if None in pulls:
            failure.write_text("", encoding="utf-8")
        page = json.dumps([pull for pull in pulls if pull is not None])
        (self.state / "open-pulls.json").write_text(page, encoding="utf-8", newline="\n")

    def serve_raw_comments(self, text: str) -> None:
        (self.state / "comments.raw").write_text(text, encoding="utf-8", newline="\n")

    def serve_comments(self, comments: list[dict]) -> None:
        (self.state / "comments.json").write_text(json.dumps(comments), encoding="utf-8")

    def fail(self, endpoint_suffix: str | None) -> None:
        marker = self.state / "fail"
        marker.unlink(missing_ok=True)
        if endpoint_suffix is not None:
            marker.write_text(endpoint_suffix, encoding="utf-8")

    def calls(self) -> list[dict]:
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def posts(self, endpoint_suffix: str) -> list[dict]:
        return [
            call
            for call in self.calls()
            if call["method"] == "POST" and call["endpoint"].endswith(endpoint_suffix)
        ]

    def reset(self) -> None:
        (self.state / "calls.jsonl").unlink(missing_ok=True)


def _pr_event(
    *,
    head: str,
    base: str,
    head_repo_id: int | None = 2,
    association: str | None = "NONE",
    user: dict | None = None,
    ref: str = "topic",
    title: str = "public",
    body: str = "",
) -> dict:
    """A trusted `pull_request_target` payload; repository 1 is the base repository."""
    return {
        "pull_request": {
            "number": 7,
            "title": title,
            "body": body,
            "author_association": association,
            "user": user or {"id": 99, "type": "User"},
            "head": {
                "ref": ref,
                "sha": head,
                "repo": {"id": head_repo_id} if head_repo_id is not None else None,
            },
            "base": {
                "ref": "main",
                "sha": base,
                "repo": {"id": 1, "full_name": "boldaxolotl/booley"},
            },
        }
    }


def _pr_scenario(
    tmp_path: Path, commits: list[tuple[tuple[str, str], dict[str, str]]]
) -> tuple[Path, str, str]:
    """A repository whose base commit holds the vocabulary; `commits` form the PR branch."""
    repo, _initial = _repository(tmp_path)
    _seal(repo)
    base = _commit(repo, "add vocabulary")
    return repo, base, _branch_commits(repo, "topic", commits, start=base)


def _scan_step() -> dict:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["confidential-content"][
        "steps"
    ]
    return next(step for step in steps if step.get("name") == "Scan pull-request commits")


def _run_pr_scan_step(
    repo: Path, event: dict, gh: _FakeGh, *, base: str
) -> subprocess.CompletedProcess[str]:
    """Run the shipped `Scan pull-request commits` script; its exit status is the required status."""
    step = _scan_step()
    script = step["run"]
    assert "--pull-request-event" in script and "post-advisory" in script, (
        "the scan step must pass the pull-request event to the scanner and post advisories"
    )
    scripts = repo / ".github/scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in ("confidential_content_guard.py", "confidential_vocabulary.py"):
        shutil.copy(SCANNER.parent / name, scripts / name)
    event_path = _write_event(repo, event)
    runner_temp = repo.parent / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    env = gh.env | {
        "BASE_SHA": base,
        "BASE_REF": "main",
        "HEAD_SHA": event["pull_request"]["head"]["sha"],
        "GITHUB_EVENT_PATH": str(event_path),
        "RUNNER_TEMP": str(runner_temp),
        "BOOLEY_LEAK_GUARD_KEY_DIR": str(repo / ".git/guard-keys"),
        "BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS": step["env"]["BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS"],
    }
    return run_bounded_child(["bash", "-e", "-c", script], cwd=repo, check=False, env=env)


_CLEAN_FILE = {"a.txt": "clean\n"}
_TERM_FILE = {"a.txt": TERM_TEXT}
_SENTINEL_AUTHOR = (f"Outsider {SENTINEL}", "outsider@example.test")

# name -> (commits, event overrides, expected status, text expected in stderr)
_STATUS_CASES: dict[str, tuple[list, dict, int, str]] = {
    "outside-unlisted-identity": ([(OUTSIDER, _CLEAN_FILE)], {}, 0, "identity not allowed"),
    "outside-term": ([(OUTSIDER, _TERM_FILE)], {}, 0, "confidential term"),
    "outside-term-in-identity": ([(_SENTINEL_AUTHOR, _CLEAN_FILE)], {}, 0, "confidential term"),
    "same-repository-term": (
        [(SAFE, _TERM_FILE)],
        {"head_repo_id": 1, "association": "CONTRIBUTOR"},
        1,
        "confidential term",
    ),
    "same-repository-unlisted-identity": (
        [(OUTSIDER, _CLEAN_FILE)],
        {"head_repo_id": 1, "association": "CONTRIBUTOR"},
        1,
        "identity not allowed",
    ),
    # Maintainer-side cases use an unlisted author, so exit 1 depends on classification alone.
    "owner-fork-term": (
        [(OUTSIDER, _TERM_FILE)],
        {"association": "OWNER"},
        1,
        "confidential term",
    ),
    "member-fork-term": (
        [(OUTSIDER, _TERM_FILE)],
        {"association": "MEMBER"},
        1,
        "confidential term",
    ),
    "lowercase-owner-fork-term": (
        [(OUTSIDER, _TERM_FILE)],
        {"association": "owner"},
        1,
        "confidential term",
    ),
    "null-association-fork-term": (
        [(OUTSIDER, _TERM_FILE)],
        {"association": None},
        1,
        "confidential term",
    ),
    "collaborator-fork-unlisted-identity": (
        [(OUTSIDER, _CLEAN_FILE)],
        {"association": "COLLABORATOR"},
        1,
        "identity not allowed",
    ),
    "deleted-fork-head": (
        [(OUTSIDER, _TERM_FILE)],
        {"head_repo_id": None},
        1,
        "confidential term",
    ),
}


@POSIX_ONLY
@pytest.mark.parametrize("case", sorted(_STATUS_CASES))
def test_pr_scan_step_status_follows_provenance(tmp_path: Path, case: str) -> None:
    commits, overrides, status, expected_text = _STATUS_CASES[case]
    repo, base, head = _pr_scenario(tmp_path, commits)
    gh = _FakeGh(tmp_path)

    result = _run_pr_scan_step(repo, _pr_event(head=head, base=base, **overrides), gh, base=base)

    _expect_exit(result, status)
    assert expected_text in result.stderr
    assert SENTINEL not in result.stdout + result.stderr
    labels, comments = gh.posts("/labels"), gh.posts("/comments")
    if case.startswith("outside-term"):
        assert [call["body"] for call in labels] == [{"labels": ["confidential-review"]}]
        body = comments[0]["body"]["body"]
        assert len(comments) == 1
        assert "<!-- confidential-content-advisory " in body
        assert "confidential term" in body
        # Identity-allowlist advisories are logged only (decision d3331-1).
        assert "identity not allowed" not in body
        assert SENTINEL not in body
        assert ("author identity" in body) is (case == "outside-term-in-identity")
        assert "WARNING" in result.stderr
    else:
        assert labels == comments == []
    if status == 1:
        assert "blocked this operation" in result.stderr


@POSIX_ONLY
@pytest.mark.parametrize("case", ["other-digest", "other-bot-login", "marker-in-cell", "u2028"])
def test_pr_scan_step_posts_unless_the_bot_posted_this_set(tmp_path: Path, case: str) -> None:
    repo, base, head = _pr_scenario(tmp_path, [(OUTSIDER, _TERM_FILE)])
    gh = _FakeGh(tmp_path)
    event = _pr_event(head=head, base=base)
    _expect_exit(_run_pr_scan_step(repo, event, gh, base=base), 0)
    posted = gh.posts("/comments")[0]["body"]["body"]
    marker = posted.splitlines()[0]
    bot = {"login": "github-actions[bot]", "type": "Bot"}
    comment = {
        "other-digest": {"user": bot, "body": marker[:-10] + "z" + marker[-9:] + "\nrows"},
        "other-bot-login": {"user": {"login": "other[bot]", "type": "Bot"}, "body": posted},
        # A contributor-controlled path can put the marker inside the bot's table.
        "marker-in-cell": {"user": bot, "body": f"<!-- other -->\n| `{marker}` |"},
        "u2028": {"user": {"login": "someone", "type": "User"}, "body": "a\u2028b"},
    }[case]
    gh.serve_comments([comment])
    gh.reset()

    _expect_exit(_run_pr_scan_step(repo, event, gh, base=base), 0)
    assert len(gh.posts("/labels")) == len(gh.posts("/comments")) == 1


def _open_pull(*, head: str, head_repo_id: int, association: str) -> dict:
    """An open pull request in the shape of GitHub's list response; the base repository is 1."""
    return {
        "number": 9,
        "state": "open",
        "title": "work",
        "user": {"login": "someone", "type": "User"},
        "author_association": association,
        "head": {"ref": "topic", "sha": head, "repo": {"id": head_repo_id, "name": "booley"}},
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 1, "name": "booley"}},
    }


_OTHER_HEAD = "f" * 40
# name -> (open pull requests, whether each shares the head commit, expected status, NOTE)
_SHARED_HEAD_CASES: dict[str, tuple[list[tuple[int, str] | None], bool, int, str]] = {
    "only-itself": ([(2, "NONE")], True, 0, ""),
    "another-outside-pr": ([(3, "NONE")], True, 0, ""),
    "maintainer-pr-with-another-head": ([(1, "OWNER"), (2, "MEMBER")], False, 0, ""),
    "same-repository-pr": ([(1, "CONTRIBUTOR")], True, 1, "another non-outside pull request"),
    "maintainer-fork-pr": ([(2, "MEMBER")], True, 1, "another non-outside pull request"),
    "lookup-fails": ([None], True, 1, "the head-commit lookup failed"),
}


@POSIX_ONLY
@pytest.mark.parametrize("case", sorted(_SHARED_HEAD_CASES))
def test_outside_pr_sharing_its_head_commit_with_other_work_stays_enforced(
    tmp_path: Path, case: str
) -> None:
    pulls, shares_head, status, note = _SHARED_HEAD_CASES[case]
    repo, base, head = _pr_scenario(tmp_path, [(OUTSIDER, _TERM_FILE)])
    gh = _FakeGh(tmp_path)
    gh.serve_open_pulls(
        *(
            None
            if pull is None
            else _open_pull(
                head=head if shares_head else _OTHER_HEAD,
                head_repo_id=pull[0],
                association=pull[1],
            )
            for pull in pulls
        )
    )

    result = _run_pr_scan_step(repo, _pr_event(head=head, base=base), gh, base=base)

    _expect_exit(result, status)
    if status:
        assert note in result.stderr
        assert "ERROR" in result.stderr
        assert gh.posts("/labels") == gh.posts("/comments") == []
    else:
        assert "NOTE" not in result.stderr
    lookups = [call for call in gh.calls() if "/pulls?" in call.get("endpoint", "")]
    assert lookups
    assert all(call["endpoint"].startswith("repos/boldaxolotl/booley/pulls?") for call in lookups)


@POSIX_ONLY
@pytest.mark.parametrize("raw", ["not json\n", "null\n", '["github-actions[bot]", "Bot", 5]\n'])
def test_pr_scan_step_fails_on_a_malformed_comment_lookup(tmp_path: Path, raw: str) -> None:
    repo, base, head = _pr_scenario(tmp_path, [(OUTSIDER, _TERM_FILE)])
    gh = _FakeGh(tmp_path)
    gh.serve_raw_comments(raw)

    result = _run_pr_scan_step(repo, _pr_event(head=head, base=base), gh, base=base)

    _expect_exit(result, 1)
    assert "the GitHub advisory lookup returned an invalid record" in result.stderr
    assert gh.posts("/labels") == gh.posts("/comments") == []


@POSIX_ONLY
@pytest.mark.parametrize("failing", ["label", "comment"])
def test_pr_scan_step_fails_when_advisory_cannot_be_posted(tmp_path: Path, failing: str) -> None:
    repo, base, head = _pr_scenario(tmp_path, [(OUTSIDER, _TERM_FILE)])
    gh = _FakeGh(tmp_path)
    event = _pr_event(head=head, base=base)
    gh.fail("/labels" if failing == "label" else "/comments")

    result = _run_pr_scan_step(repo, event, gh, base=base)

    _expect_exit(result, 1)
    assert f"the GitHub advisory {failing} failed" in result.stderr
    assert "blocked this operation" not in result.stderr
    attempted = [
        call["endpoint"].rsplit("/", 1)[1] for call in gh.calls() if call["method"] == "POST"
    ]
    # The label goes first; a failed label never reaches the comment.
    assert attempted == (["labels"] if failing == "label" else ["labels", "comments"])

    gh.fail(None)
    gh.reset()
    retried = _run_pr_scan_step(repo, event, gh, base=base)

    _expect_exit(retried, 0)
    assert len(gh.posts("/labels")) == len(gh.posts("/comments")) == 1


@POSIX_ONLY
def test_pr_scan_step_posts_one_comment_per_finding_set(tmp_path: Path) -> None:
    repo, base, head = _pr_scenario(tmp_path, [(OUTSIDER, _TERM_FILE)])
    gh = _FakeGh(tmp_path)
    _expect_exit(_run_pr_scan_step(repo, _pr_event(head=head, base=base), gh, base=base), 0)
    posted = gh.posts("/comments")[0]["body"]["body"]

    # An amend keeps the findings but changes the commit id and message.
    _git(repo, "checkout", "topic")
    (repo / "a.txt").write_bytes(b"\n" + TERM_TEXT.encode())  # same finding, new line number
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--amend", "-m", "amended", env=_identity_env(*OUTSIDER))
    amended = _git(repo, "rev-parse", "HEAD")
    event = _pr_event(head=amended, base=base)
    bot = {"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": posted}
    gh.serve_comments([bot])
    gh.reset()

    _expect_exit(_run_pr_scan_step(repo, event, gh, base=base), 0)
    assert not [call for call in gh.calls() if call["method"] == "POST"]

    # A comment stored with CRLF line ends still counts as already posted.
    gh.serve_comments([{**bot, "body": posted.replace("\n", "\r\n")}])
    gh.reset()

    _expect_exit(_run_pr_scan_step(repo, event, gh, base=base), 0)
    assert not gh.posts("/comments")

    # A contributor posting the marker must not suppress the advisory.
    gh.serve_comments([{"user": {"login": "someone", "type": "User"}, "body": posted}])
    gh.reset()

    _expect_exit(_run_pr_scan_step(repo, event, gh, base=base), 0)
    assert len(gh.posts("/labels")) == len(gh.posts("/comments")) == 1

    # A different finding set (the term now sits in another file) gets its own comment.
    _git(repo, "mv", "a.txt", "b.txt")
    _git(repo, "commit", "--amend", "--no-edit", env=_identity_env(*OUTSIDER))
    moved = _pr_event(head=_git(repo, "rev-parse", "HEAD"), base=base)
    gh.serve_comments([bot])
    gh.reset()

    _expect_exit(_run_pr_scan_step(repo, moved, gh, base=base), 0)
    assert len(gh.posts("/labels")) == len(gh.posts("/comments")) == 1


def _scan_with_event(
    repo: Path, command: str, event: dict, *, base: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run `pre-push` or `verify-candidate` against a PR event, reporting advisories."""
    report = ["--advisory-report", str(repo.parent / "report.jsonl")]
    head = event["pull_request"]["head"]["sha"]
    if command == "pre-push":
        event_path = str(_write_event(repo, event))
        args = ["pre-push", "--pull-request-event", event_path, *report]
        return _guard(repo, *args, stdin=_update(head, base), env=env)
    return _verify(repo, head, base, event, *report, env=env)


def _candidate_repository(
    tmp_path: Path,
    order: list[str],
    files: dict[str, dict],
    *,
    maintainer: tuple[str, str] = SAFE,
    stacked: bool = False,
) -> tuple:
    """A Mergify-style candidate merging queued PR branches in `order`, one `Merge of #N` each.

    `maintainer` authors PR 12; `stacked` builds PR 11 on top of PR 12's branch.
    Returns (repo, base, candidate head, branch tips by PR number).
    """
    repo, _initial = _repository(tmp_path)
    _seal(repo)
    base = _commit(repo, "add vocabulary")
    identities = {"11": OUTSIDER, "12": maintainer}
    tips: dict[str, str] = {}
    for number in sorted(files, key=lambda number: number != "12"):  # PR 12 first
        start = tips["12"] if stacked and number == "11" else base
        tips[number] = _branch_commits(
            repo, f"pr-{number}", [(identities[number], files[number])], start=start
        )
    _git(repo, "checkout", "-b", "mergify/merge-queue/abc123", base)
    for number in order:
        _git(
            repo,
            "merge",
            "--no-ff",
            "-m",
            f"Merge of #{number}",
            f"pr-{number}",
            env=_identity_env(),
        )
    return repo, base, _git(repo, "rev-parse", "HEAD"), tips


def _candidate_event(head: str, base: str, **overrides: object) -> dict:
    event = _pr_event(head=head, base=base, head_repo_id=1, association="CONTRIBUTOR")
    event["pull_request"]["user"] = dict(MERGIFY_USER)
    event["pull_request"]["head"]["ref"] = "mergify/merge-queue/abc123"
    event["pull_request"].update(overrides)
    return event


def _error_and_warning_blocks(stderr: str) -> tuple[str, str]:
    errors = stderr[stderr.index("ERROR:") :] if "ERROR:" in stderr else ""
    warnings = stderr[stderr.index("WARNING:") :] if "WARNING:" in stderr else ""
    return errors.split("WARNING:")[0], warnings


_OUTSIDE_FILES = {"outside.txt": TERM_TEXT + "outside\n"}
_MAINTAINER_FILES = {"maint.txt": TERM_TEXT}


_CANDIDATE_NOTES = {
    "outside-head-mismatch": "head mismatch",
    "lookup-fails": "lookup failed",
    "member-fork-pr": "not an outside PR",
}


def _candidate_case(tmp_path: Path, case: str) -> tuple[Path, str, dict, _FakeGh]:
    """Build one candidate scenario: the repository, base, PR event, and fake gh."""
    clean_maintainer = case == "outside-and-clean-maintainer"
    stacked = case == "outside-stacked-on-maintainer"
    order = ["12", "11"] if case == "maintainer-then-outside-term" or stacked else ["11", "12"]
    if case in {*_CANDIDATE_NOTES, "pushed-commit-on-candidate", "unlisted-merge-commit"}:
        order = ["11"]
    maintainer_files = {"maint.txt": "clean\n"} if clean_maintainer else _MAINTAINER_FILES
    # An unlisted maintainer identity makes enforcement visible even where a term would not.
    repo, base, head, tips = _candidate_repository(
        tmp_path,
        order,
        {"11": _OUTSIDE_FILES, "12": maintainer_files},
        maintainer=SAFE if clean_maintainer else OUTSIDER,
        stacked=stacked,
    )
    if case == "pushed-commit-on-candidate":
        (repo / "pushed.txt").write_bytes(TERM_TEXT.encode())
        head = _commit(repo, "pushed after the queue", name=OUTSIDER[0], email=OUTSIDER[1])
    if case == "unlisted-merge-commit":
        # A hand-made `Merge of #11` that also adds a term file under an unlisted identity.
        (repo / "evil.txt").write_bytes(TERM_TEXT.encode())
        _git(repo, "add", "evil.txt")
        _git(
            repo,
            "commit",
            "--amend",
            "--no-edit",
            "--reset-author",
            env=_identity_env(*OUTSIDER),
        )
        head = _git(repo, "rev-parse", "HEAD")
    gh = _FakeGh(tmp_path)
    served_head = "0" * 40 if case == "outside-head-mismatch" else tips["11"]
    if case != "lookup-fails":
        association = "MEMBER" if case == "member-fork-pr" else "NONE"
        gh.serve_pull(11, head_sha=served_head, head_repo_id=2, association=association)
    gh.serve_pull(12, head_sha=tips["12"], head_repo_id=1, association="OWNER")
    event = _candidate_event(head, base)
    if case == "not-mergify-user-type":
        event["pull_request"]["user"] = {"id": 37929162, "type": "User"}
    return repo, base, event, gh


def _assert_candidate_outcome(
    case: str, result: subprocess.CompletedProcess[str], repo: Path, gh: _FakeGh
) -> None:
    errors, warnings = _error_and_warning_blocks(result.stderr)
    assert SENTINEL not in result.stdout + result.stderr
    report = repo.parent / "report.jsonl"
    assert not report.exists() or not report.read_text(encoding="utf-8"), (
        "candidate advisories are logged only"
    )
    if case == "outside-and-clean-maintainer":
        _expect_exit(result, 0)
        assert "outside.txt" in warnings
        return
    _expect_exit(result, 1)
    if case in {
        "outside-and-maintainer-term",
        "maintainer-then-outside-term",
        "outside-stacked-on-maintainer",
    }:
        assert "maint.txt" in errors and "outside.txt" not in errors
        assert "outside.txt" in warnings and "maint.txt" not in warnings
        # The maintainer's unlisted identity is enforced; the outside author's is advisory.
        assert "identity not allowed" in errors and "identity not allowed" in warnings
    elif case == "unlisted-merge-commit":
        # The merge commit is never exempt, only the verified outside range behind it.
        assert "evil.txt" in errors and "identity not allowed" in errors
        assert "outside.txt" in warnings and "outside.txt" not in errors
    elif case == "pushed-commit-on-candidate":
        assert "pushed.txt" in errors and "outside.txt" not in errors
        assert "outside.txt" in warnings
        assert "NOTE" not in result.stderr
    elif case == "not-mergify-user-type":
        assert "outside.txt" in errors
        assert "NOTE" not in result.stderr
        assert not [call for call in gh.calls() if call["method"] == "GET"]
    else:
        assert "outside.txt" in errors
        assert f"stays enforced: {_CANDIDATE_NOTES[case]}" in result.stderr


@POSIX_ONLY
@pytest.mark.parametrize(
    "case",
    [
        "outside-and-clean-maintainer",
        "outside-and-maintainer-term",
        "maintainer-then-outside-term",
        "outside-stacked-on-maintainer",
        "outside-head-mismatch",
        "lookup-fails",
        "member-fork-pr",
        "pushed-commit-on-candidate",
        "unlisted-merge-commit",
        "not-mergify-user-type",
    ],
)
def test_candidate_enforces_only_maintainer_commits(tmp_path: Path, case: str) -> None:
    repo, base, event, gh = _candidate_case(tmp_path, case)

    for command in ("pre-push", "verify-candidate"):
        gh.reset()
        result = _scan_with_event(repo, command, event, base=base, env=gh.env)
        _assert_candidate_outcome(case, result, repo, gh)


def test_candidate_without_merge_commits_exempts_nothing(tmp_path: Path) -> None:
    repo, base, _head, _tips = _candidate_repository(tmp_path, [], {"11": _OUTSIDE_FILES})
    (repo / "plain.txt").write_bytes(TERM_TEXT.encode())
    head = _commit(repo, "plain commit", name=SAFE[0], email=SAFE[1])

    result = _scan_with_event(
        repo, "pre-push", _candidate_event(head, base), base=base, env=os.environ.copy()
    )

    _expect_exit(result, 1)
    assert "no 'Merge of #N' commit" in result.stderr
    assert "plain.txt" in result.stderr


def test_candidate_metadata_is_advisory(tmp_path: Path) -> None:
    repo, base, head = _pr_scenario(tmp_path, [(SAFE, _CLEAN_FILE)])
    event = _candidate_event(head, base, title=f"Queue {SENTINEL}")
    event_path = _write_event(repo, event)
    env = os.environ.copy()

    candidate = _guard(repo, "pull-request", "--event", str(event_path), env=env)

    _expect_exit(candidate, 0)
    assert "WARNING" in candidate.stderr and "pull request title" in candidate.stderr
    assert SENTINEL not in candidate.stdout + candidate.stderr


def _with_head_repo(event: dict) -> None:
    event["pull_request"]["head"]["repo"] = {"id": 2}
    event["pull_request"]["author_association"] = "OWNER"


# name -> edit that removes one marker of a Mergify candidate (a maintainer-scope PR remains)
_CANDIDATE_MARKER_EDITS: dict[str, Callable[[dict], None]] = {
    "other-bot-id": lambda event: event["pull_request"]["user"].update(id=5),
    "other-queue-branch": lambda event: event["pull_request"]["head"].update(ref="mergify/bp/x"),
    "fork-head": _with_head_repo,
    "float-bot-id": lambda event: event["pull_request"]["user"].update(id=37929162.0),
    "boolean-repository-id": lambda event: event["pull_request"]["head"].update(repo={"id": True}),
}


@pytest.mark.parametrize("case", sorted(_CANDIDATE_MARKER_EDITS))
def test_candidate_metadata_needs_every_mergify_marker(tmp_path: Path, case: str) -> None:
    repo, base, head = _pr_scenario(tmp_path, [(SAFE, _CLEAN_FILE)])
    event = _candidate_event(head, base, title=f"Queue {SENTINEL}")
    _CANDIDATE_MARKER_EDITS[case](event)
    event_path = _write_event(repo, event)

    result = _guard(repo, "pull-request", "--event", str(event_path))

    _expect_exit(result, 1)
    assert "ERROR" in result.stderr and "WARNING" not in result.stderr
    assert SENTINEL not in result.stdout + result.stderr


def test_outside_pr_invalid_proposed_vocabulary_still_fails(tmp_path: Path) -> None:
    repo, base, _head = _pr_scenario(tmp_path, [(OUTSIDER, _CLEAN_FILE)])
    (repo / ".github/confidential-vocabulary.enc").write_bytes(b"corrupt\n")
    _git(repo, "checkout", "-b", "tampered", base)
    head = _commit(repo, "tamper with the vocabulary", name=OUTSIDER[0], email=OUTSIDER[1])
    _git(repo, "checkout", base)  # the trusted vocabulary stays intact in the working tree

    result = _scan_with_event(
        repo, "verify-candidate", _pr_event(head=head, base=base), base=base, env=os.environ.copy()
    )

    _expect_exit(result, 1)
    assert "the proposed vocabulary is invalid" in result.stderr
    assert "WARNING" not in result.stderr


@POSIX_ONLY
def test_advisory_report_write_failure_fails_closed(tmp_path: Path) -> None:
    gh = _FakeGh(tmp_path)  # answers the head-commit lookup
    repo, base, head = _pr_scenario(tmp_path, [(OUTSIDER, _TERM_FILE)])
    unwritable = str(repo / "no/such/dir")

    for title, status in (("public", 0), (f"fix {SENTINEL}", 1)):
        event_path = _write_event(repo, _pr_event(head=head, base=base, title=title))
        args = ["pull-request", "--event", str(event_path), "--advisory-report", unwritable]

        result = _guard(repo, *args, env=gh.env)

        # A clean title and body write no report row, so the directory is touched only on 1.
        _expect_exit(result, status)
        assert SENTINEL not in result.stdout + result.stderr
    assert "the advisory report cannot be written" in result.stderr


@POSIX_ONLY
def test_no_plaintext_term_in_advisory_outputs(tmp_path: Path) -> None:
    repo, base, _head = _pr_scenario(tmp_path, [(OUTSIDER, _CLEAN_FILE)])
    _git(repo, "checkout", "-b", f"leaky-{SENTINEL}", base)
    (repo / f"{SENTINEL}.txt").write_bytes(TERM_TEXT.encode())
    head = _commit(
        repo, f"fix {SENTINEL}", name=f"Outsider {SENTINEL}", email="outsider@example.test"
    )
    gh = _FakeGh(tmp_path)
    event = _pr_event(
        head=head,
        base=base,
        title=f"fix {SENTINEL}",
        body=f"details {SENTINEL}",
        ref=f"leaky-{SENTINEL}",
    )

    result = _run_pr_scan_step(repo, event, gh, base=base)

    _expect_exit(result, 0)
    report = (repo.parent / "runner-temp/confidential-advisory.jsonl").read_text(encoding="utf-8")
    calls = (gh.state / "calls.jsonl").read_text(encoding="utf-8")
    assert gh.posts("/comments") and report
    for output in (result.stdout, result.stderr, report, calls):
        assert SENTINEL not in output
        assert SENTINEL.upper() not in output.upper()


def test_verify_candidate_redacts_with_trusted_vocabulary(tmp_path: Path) -> None:
    repo, _initial = _repository(tmp_path)
    _seal(repo)
    base = _commit(repo, "add vocabulary")
    # The proposal drops the trusted term, so only the trusted vocabulary can redact it.
    other = "other-term-xyz"
    _seal(repo, _words(other))
    (repo / f"{SENTINEL}-{other}.txt").write_bytes(b"clean\n")
    head = _commit(repo, "drop the trusted term")
    _git(repo, "checkout", base)

    result = _verify(repo, head, base, _pr_event(head=head, base=base, head_repo_id=1))

    _expect_exit(result, 1)
    assert "confidential term" in result.stderr
    assert SENTINEL not in result.stdout + result.stderr, "plaintext term printed"


def _post_advisory(
    repo: Path, rows: list[dict] | str, gh: _FakeGh, event: dict | None = None
) -> subprocess.CompletedProcess[str]:
    report = repo.parent / "post-report.jsonl"
    report.write_text(
        rows if isinstance(rows, str) else "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    event_path = _write_event(repo, event or _pr_event(head="a" * 40, base="b" * 40))
    return _guard(
        repo, "post-advisory", "--event", str(event_path), "--report", str(report), env=gh.env
    )


@POSIX_ONLY
def test_post_advisory_bounds_and_neutralises_rows(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    gh = _FakeGh(tmp_path)
    hostile = "commit abcdef012345 `x`|@maintainer\nline" + "p" * 300
    rows = [{"kind": "confidential term", "location": hostile, "term_id": "t0"}]
    rows += [
        {
            "kind": "confidential term",
            "location": f"commit abcdef012345 f{index}.txt:1",
            "term_id": "t",
        }
        for index in range(109)
    ]

    result = _post_advisory(repo, rows, gh)

    _expect_exit(result, 0)
    body = gh.posts("/comments")[0]["body"]["body"]
    assert len(body) < 65_536
    assert "10 more findings not shown" in body
    assert body.count("| confidential term |") == 100
    cell = next(line for line in body.splitlines() if "@maintainer" in line)
    assert "`x`" not in cell and "\\|" in cell and "?line" in cell
    assert "p" * 201 not in body


@POSIX_ONLY
@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("", "No advisory findings to post"),
        ("not json\n", "the advisory report cannot be read"),
        ('{"kind": "confidential term"}\n', "the advisory report is malformed"),
    ],
)
def test_post_advisory_reads_the_report_strictly(
    tmp_path: Path, report: str, expected: str
) -> None:
    repo, _base = _repository(tmp_path)
    gh = _FakeGh(tmp_path)

    result = _post_advisory(repo, report, gh)

    _expect_exit(result, 0 if not report else 1)
    assert expected in result.stdout + result.stderr
    assert gh.calls() == []


@POSIX_ONLY
def test_post_advisory_requires_a_valid_pull_request_target(tmp_path: Path) -> None:
    repo, _base = _repository(tmp_path)
    gh = _FakeGh(tmp_path)
    event = _pr_event(head="a" * 40, base="b" * 40)
    event["pull_request"]["number"] = True
    row = [{"kind": "confidential term", "location": "x", "term_id": "t"}]

    result = _post_advisory(repo, row, gh, event)

    _expect_exit(result, 1)
    assert "the pull-request repository or number is invalid" in result.stderr
    assert gh.calls() == []


def test_workflow_grants_pull_request_write_and_token_to_scan() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["confidential-content"]["steps"]
    scan = _scan_step()

    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "statuses": "write",
    }
    assert scan["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert scan["id"] == "scan-pr"
    # Posting runs in the step whose outcome publishes the status, and nowhere else.
    assert [step["name"] for step in steps if "post-advisory" in step.get("run", "")] == [
        scan["name"]
    ]
    publish = next(step for step in steps if step["name"] == "Publish scan status")
    assert publish["env"]["PR_OUTCOME"] == "${{ steps.scan-pr.outcome }}"
