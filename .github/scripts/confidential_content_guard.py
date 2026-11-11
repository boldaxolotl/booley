#!/usr/bin/env python3
"""Fail-closed confidential-content guard for Git hooks, PR drafts, and CI.

The tracked encrypted vocabulary is the single source of truth. Local checks
decrypt it with a key outside the repository; CI uses the same encrypted file
and a GitHub Actions key secret. Plaintext TOML is a local edit draft only.
Diagnostics identify matched terms by a one-way digest and never echo them.

CI executes the trusted default-branch scanner and encryption module against
an untrusted pull-request checkout without importing candidate code.

Local pre-push wrappers should pass Git's remote name and location arguments
after the ``pre-push`` command. Without them, the guard can exclude only the
updated ref's previous tip because it cannot prove what else the destination
already contains.

Pull-request scans in CI classify the pull request from the trusted event
payload. Work by an outside contributor (a fork whose author is neither owner,
member, nor collaborator) is advisory, as are the commits of such a pull
request inside a Mergify candidate; everything else stays enforced.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Literal, TextIO, cast

from confidential_vocabulary import (
    SealedVocabularyError,
    key_id,
    seal_vocabulary,
    sealed_key_id,
    unseal_vocabulary,
)

_CONFIG_PATH_ENV = "BOOLEY_LEAK_GUARD_CONFIG"
_KEY_B64_ENV = "BOOLEY_LEAK_GUARD_KEY_B64"
_KEY_DIR_ENV = "BOOLEY_LEAK_GUARD_KEY_DIR"
_ALLOWED_AUTHORS_ENV = "BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS"
_LOCAL_CONFIG_NAME = "booley-leak-guard.toml"
_SEALED_CONFIG_NAME = ".github/confidential-vocabulary.enc"
_MAX_BLOB_BYTES = 256 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_PR_TEXT_BYTES = 1024 * 1024
_MAX_FINDINGS = 100
_BINARY_RUN_RE = re.compile(rb"[\t\x20-\x7e]{6,}")
_ASCII_REGEX_FOLD = str.maketrans({"\u0130": "i", "\u0131": "i"})
_PREFILTER_CHUNK_CHARS = 1024 * 1024
_IDENT_RE = re.compile(r"^(?P<name>.*) <(?P<email>[^<>]*)> \d+ [+-]\d{4}$")
_OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
# GitHub login form: alphanumerics and inner hyphens, never surrounding whitespace.
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
# GitHub-shaped merge subject: text only, not verified against GitHub.
_PR_MERGE_SUBJECT_RE = re.compile(
    r"Merge pull request #[1-9][0-9]* from (?P<owner>[A-Za-z0-9-]+)/\S+"
)
# The only `gh` output a PR write may echo: the URL of what it created.
_PR_WRITE_URL_RE = re.compile(
    r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+"
    r"(?:#(?:issuecomment|pullrequestreview)-\d+)?"
)
# A fork PR is outside work unless its author can write to this repository.
_MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# A Mergify candidate is a same-repository PR opened by this bot on its queue branch.
_MERGIFY_BOT_ID = 37929162
_CANDIDATE_REF_PREFIX = "mergify/merge-queue/"
# Mergify's first-parent merge commits carry exactly this message; the second
# parent is the head of queued PR N.
_CANDIDATE_MERGE_RE = re.compile(r"Merge of #(?P<number>[1-9][0-9]*)")
_REPOSITORY_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REVIEW_LABEL = "confidential-review"
_ADVISORY_MARKER = "confidential-content-advisory"
_TERM_KIND = "confidential term"
_BOT_LOGIN = "github-actions[bot]"
# Per associated pull request, only the fields `_is_outside` and the head check read.
_COMMIT_PULLS_JQ = (
    ".[] | {number, author_association, head: {repo: {id: .head.repo.id}}, "
    "base: {repo: {id: .base.repo.id}}} | @json"
)
_MAX_ADVISORY_ROWS = 100
_MAX_LOCATION_CHARS = 200
_LOOKUP_TIMEOUT_SECONDS = 10
_GH_TIMEOUT_SECONDS = 120
# What `_scan_text` and `_commit_*` locations look like: drop the volatile parts
# (commit prefix, trailing line number) so an amended or rebased commit keeps its digest.
_LOCATION_COMMIT_RE = re.compile(r"^commit [0-9a-f]{12} ")
_LOCATION_LINE_RE = re.compile(r":[0-9]+$")

PullRequestScope = Literal["outside", "candidate", "maintainer"]


class GuardError(RuntimeError):
    """The guard could not establish that content is safe."""


@dataclass(frozen=True)
class TermPattern:
    literal: str
    term_id: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class GuardConfig:
    patterns: tuple[TermPattern, ...]
    matcher: re.Pattern[str]
    allowed_authors: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    ascii_prefilter_terms: tuple[str, ...] | None


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    term_id: str | None = None
    # Advisory findings come from attributed outside work: reported, never blocking.
    advisory: bool = False


@dataclass(frozen=True)
class Provenance:
    """Where commits and tree entries came from, so scans can attribute them.

    ``outside`` holds commits attributed to outside work (merged from another
    owner's pull request, an outside PR's own commits, or a verified candidate
    range); ``attributing_merges`` the merges that attribute them (their messages are
    outside-authored); ``trusted_entries`` the exact ``(path, blob oid)`` pairs
    already accepted on the base tip.
    """

    outside: frozenset[str] = frozenset()
    attributing_merges: frozenset[str] = frozenset()
    trusted_entries: frozenset[tuple[str, str]] = frozenset()


NO_PROVENANCE = Provenance()


@dataclass(frozen=True)
class _TreeEntry:
    object_id: str | None
    path: str


@dataclass(frozen=True)
class RefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @classmethod
    def parse(cls, line: str) -> RefUpdate:
        parts = line.split()
        if len(parts) != 4:
            raise GuardError("the pre-push input record is malformed")
        local_ref, local_sha, remote_ref, remote_sha = parts
        if (
            _OID_RE.fullmatch(local_sha) is None
            or _OID_RE.fullmatch(remote_sha) is None
            or len(local_sha) != len(remote_sha)
            or (_is_zero_sha(local_sha) and _is_zero_sha(remote_sha))
        ):
            raise GuardError("the pre-push input record is malformed")
        return cls(local_ref, local_sha, remote_ref, remote_sha)


def _run_git(repo: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    """Run Git or raise a deliberately non-sensitive failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"git {args[0]} could not run") from exc
    if result.returncode != 0:
        raise GuardError(f"git {args[0]} failed")
    return result.stdout


def _optional_git_values(repo: Path, key: str) -> list[str]:
    """Read a local configuration key; absence is not an operational error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--get-all", key],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode not in (0, 1):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _common_git_config(repo: Path) -> Path | None:
    candidate = _default_private_config_path(repo)
    return candidate if candidate is not None and candidate.is_file() else None


def _default_private_config_path(repo: Path) -> Path | None:
    try:
        raw = _run_git(repo, ["rev-parse", "--git-common-dir"]).decode().strip()
    except (GuardError, UnicodeDecodeError):
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = repo / common
    return common.resolve() / _LOCAL_CONFIG_NAME


def _configured_path(repo: Path) -> Path | None:
    env_path = os.environ.get(_CONFIG_PATH_ENV, "").strip()
    if env_path:
        configured = Path(env_path).expanduser()
        return configured if configured.is_absolute() else repo / configured
    values = _optional_git_values(repo, "booley.leakGuardConfig")
    if values:
        configured = Path(values[-1]).expanduser()
        return configured if configured.is_absolute() else repo / configured
    return _common_git_config(repo)


def _read_local_config(path: Path | None) -> bytes:
    if path is None:
        raise GuardError("the confidential vocabulary is required but not configured")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GuardError("the configured confidential vocabulary cannot be read") from exc


def _local_config_bytes(repo: Path) -> bytes:
    return _read_local_config(_configured_path(repo))


def _sealed_config_bytes(repo: Path) -> bytes:
    try:
        with (repo / _SEALED_CONFIG_NAME).open("rb") as stream:
            sealed = stream.read(2 * 1024 * 1024 + 1)
    except OSError as exc:
        raise GuardError("the encrypted confidential vocabulary cannot be read") from exc
    if len(sealed) > 2 * 1024 * 1024:
        raise GuardError("the encrypted confidential vocabulary exceeds the size limit")
    return sealed


def _key_path(identifier: str) -> Path:
    explicit = os.environ.get(_KEY_DIR_ENV, "").strip()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    key_dir = Path(explicit).expanduser() if explicit else config_home / "booley/leak-guard"
    return key_dir / f"{identifier}.key"


def _decode_key(encoded: bytes, identifier: str) -> bytes:
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
        if key_id(key) != identifier:
            raise GuardError("the confidential vocabulary key does not match the encrypted file")
    except (ValueError, binascii.Error, SealedVocabularyError) as exc:
        raise GuardError("the confidential vocabulary key is invalid") from exc
    return key


def _vocabulary_key(identifier: str, *, local_only: bool = False) -> bytes:
    path = _key_path(identifier)
    if path.is_file():
        try:
            if os.name == "posix" and path.stat().st_mode & 0o077:
                raise GuardError("the local confidential vocabulary key must be owner-only")
            return _decode_key(path.read_bytes(), identifier)
        except OSError as exc:
            raise GuardError("the local confidential vocabulary key cannot be read") from exc
    if local_only:
        raise GuardError("the local confidential vocabulary key is missing")
    encoded = os.environ.get(_KEY_B64_ENV, "").encode("ascii", errors="ignore")
    if not encoded:
        raise GuardError("the confidential vocabulary key is required but not configured")
    return _decode_key(encoded, identifier)


def _config_bytes(repo: Path) -> bytes:
    sealed = _sealed_config_bytes(repo)
    try:
        identifier = sealed_key_id(sealed)
        return unseal_vocabulary(sealed, _vocabulary_key(identifier))
    except SealedVocabularyError as exc:
        raise GuardError(str(exc)) from exc


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GuardError("a confidential configuration list must contain only strings")
    return [item.strip() for item in value if item.strip()]


def _config_lists(document: dict) -> tuple[list[str], list[str], list[str]]:
    terms: list[str] = []
    authors: list[str] = []
    ignored: list[str] = []
    for name, section in document.items():
        if not isinstance(section, dict):
            continue
        terms.extend(_string_list(section.get("words"), f"{name}.words"))
        terms.extend(_string_list(section.get("banned_words"), f"{name}.banned_words"))
        if name in {"guard", "stealth"}:
            authors.extend(_string_list(section.get("allowed_authors"), f"{name}.allowed_authors"))
            ignored.extend(_string_list(section.get("ignored_paths"), f"{name}.ignored_paths"))
    terms.extend(_string_list(document.get("banned_words"), "banned_words"))
    return terms, authors, ignored


def _extra_authors(repo: Path) -> list[str]:
    env_value = os.environ.get(_ALLOWED_AUTHORS_ENV, "")
    from_env = [line.strip() for line in env_value.splitlines() if line.strip()]
    return from_env + _optional_git_values(repo, "booley.leakGuardAllowedAuthor")


def _compile_term(term: str, key: bytes) -> TermPattern:
    if "\x00" in term:
        raise GuardError("confidential vocabulary entries cannot contain NUL")
    body = re.escape(term)
    if term.endswith("__"):
        body = rf"(?<![A-Za-z0-9]){body}\w+"
    else:
        body = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    term_id = hashlib.blake2s(term.casefold().encode("utf-8"), key=key, digest_size=6).hexdigest()
    return TermPattern(term, term_id, re.compile(body, re.IGNORECASE))


def _combined_matcher(
    patterns: tuple[TermPattern, ...], selected: tuple[int, ...] | None = None
) -> re.Pattern[str]:
    indexed = (
        enumerate(patterns)
        if selected is None
        else ((index, patterns[index]) for index in selected)
    )
    ordered = sorted(indexed, key=lambda item: len(item[1].literal), reverse=True)
    alternatives = (f"(?P<term_{index}>{pattern.regex.pattern})" for index, pattern in ordered)
    return re.compile("|".join(alternatives), re.IGNORECASE)


def load_config(repo: Path | str | None = None) -> GuardConfig:
    """Load and validate the explicit confidential guard configuration."""
    root = Path(repo or Path.cwd()).resolve()
    return _parse_config(_config_bytes(root), root)


def _parse_config(raw_config: bytes, repo: Path) -> GuardConfig:
    try:
        document = tomllib.loads(raw_config.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GuardError("the confidential configuration is not valid UTF-8 TOML") from exc
    terms, authors, ignored = _config_lists(document)
    authors.extend(_extra_authors(repo))
    terms = list(dict.fromkeys(terms))
    authors = list(dict.fromkeys(authors))
    if not terms:
        raise GuardError("the confidential vocabulary is empty")
    if not authors:
        raise GuardError("the confidential identity allowlist is empty")
    term_key = hashlib.sha256(raw_config).digest()
    patterns = tuple(_compile_term(term, term_key) for term in terms)
    return GuardConfig(
        patterns,
        _combined_matcher(patterns),
        tuple(authors),
        tuple(dict.fromkeys(ignored)),
        tuple(term.casefold() for term in terms)
        if all(term.isascii() for term in terms)
        else None,
    )


def _path_ignored(path: str, config: GuardConfig) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in config.ignored_paths)


def _redact_location(location: str, config: GuardConfig) -> str:
    redacted = location
    for pattern in config.patterns:
        redacted = pattern.regex.sub("[redacted]", redacted)
    return redacted


def _ascii_candidate_indices(text: str, terms: tuple[str, ...]) -> tuple[int, ...]:
    """Find a safe superset of ASCII regex matches using bounded text chunks."""
    overlap = max(map(len, terms)) - 1
    candidates: set[int] = set()
    for start in range(0, len(text), _PREFILTER_CHUNK_CHARS):
        chunk = text[start : start + _PREFILTER_CHUNK_CHARS + overlap]
        if "\u0130" in chunk or "\u0131" in chunk:
            chunk = chunk.translate(_ASCII_REGEX_FOLD)
        folded = chunk.casefold()
        candidates.update(index for index, term in enumerate(terms) if term in folded)
    return tuple(sorted(candidates))


def _scan_text(text: str, location: str, config: GuardConfig) -> list[Finding]:
    matcher = config.matcher
    if config.ascii_prefilter_terms is not None:
        candidates = _ascii_candidate_indices(text, config.ascii_prefilter_terms)
        if not candidates:
            return []
        if len(candidates) < len(config.patterns):
            matcher = _combined_matcher(config.patterns, candidates)
    findings: list[Finding] = []
    safe_location = _redact_location(location, config)
    found_ids: set[str] = set()
    for match in matcher.finditer(text):
        assert match.lastgroup is not None
        pattern = config.patterns[int(match.lastgroup.removeprefix("term_"))]
        if pattern.term_id in found_ids:
            continue
        found_ids.add(pattern.term_id)
        line = text.count("\n", 0, match.start()) + 1
        findings.append(Finding(_TERM_KIND, f"{safe_location}:{line}", pattern.term_id))
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings


def _gunzip_bounded(data: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            decompressed = stream.read(_MAX_DECOMPRESSED_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise GuardError("a gzip blob could not be inspected") from exc
    if len(decompressed) > _MAX_DECOMPRESSED_BYTES:
        raise GuardError("a gzip blob exceeds the inspection size limit")
    return decompressed


def _visible_text(data: bytes) -> str:
    if len(data) > _MAX_BLOB_BYTES:
        raise GuardError("a blob exceeds the inspection size limit")
    if data.startswith(b"\x1f\x8b"):
        data = _gunzip_bounded(data)
    if b"\x00" not in data[:8192]:
        return data.decode("utf-8", errors="replace")
    runs = _BINARY_RUN_RE.findall(data)
    return "\n".join(run.decode("ascii", errors="ignore") for run in runs)


def _scan_blob(data: bytes, path: str, location: str, config: GuardConfig) -> list[Finding]:
    if _path_ignored(path, config):
        return []
    return _scan_text(_visible_text(data), location, config)


def _identity_allowed(name: str, email: str, patterns: tuple[str, ...]) -> bool:
    candidates = (email.casefold(), name.casefold(), f"{name} <{email}>".casefold())
    return any(
        fnmatch.fnmatchcase(candidate, pattern.casefold())
        for pattern in patterns
        for candidate in candidates
    )


def _identity_findings(
    name: str, email: str, role: str, location: str, config: GuardConfig
) -> list[Finding]:
    rendered = f"{name} <{email}>"
    findings = _scan_text(rendered, f"{location} {role} identity", config)
    if not _identity_allowed(name, email, config.allowed_authors):
        findings.append(Finding("identity not allowed", f"{location} {role} identity"))
    return findings


def _pending_identity(repo: Path, variable: str) -> tuple[str, str]:
    raw = _run_git(repo, ["var", variable]).decode("utf-8", errors="replace").strip()
    match = _IDENT_RE.match(raw)
    if match is None:
        raise GuardError(f"git {variable} identity could not be parsed")
    return match.group("name"), match.group("email")


def _staged_paths(repo: Path) -> list[str]:
    raw = _run_git(repo, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"])
    return [part.decode("utf-8", errors="surrogateescape") for part in raw.split(b"\x00") if part]


def inspect_pending_commit(repo: Path, message: str, config: GuardConfig) -> list[Finding]:
    """Inspect the message, pending identities, paths, and complete staged blobs."""
    findings = _scan_text(message, "pending commit message", config)
    for role, variable in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        name, email = _pending_identity(repo, variable)
        findings.extend(_identity_findings(name, email, role, "pending commit", config))
    for path in _staged_paths(repo):
        if _path_ignored(path, config):
            continue
        data = _run_git(repo, ["show", f":{path}"])
        findings.extend(_scan_text(path, f"staged {path} path", config))
        findings.extend(_scan_blob(data, path, f"staged {path}", config))
    return findings[:_MAX_FINDINGS]


def inspect_worktree(repo: Path, config: GuardConfig) -> list[Finding]:
    """Inspect tracked and untracked non-ignored files in the current checkout."""
    raw = _run_git(repo, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    paths = [part.decode(errors="surrogateescape") for part in raw.split(b"\x00") if part]
    findings: list[Finding] = []
    for path in paths:
        if _path_ignored(path, config):
            continue
        candidate = repo / path
        try:
            if candidate.is_symlink():
                data = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
            elif candidate.is_file():
                if candidate.stat().st_size > _MAX_BLOB_BYTES:
                    raise GuardError("a worktree file exceeds the inspection size limit")
                data = candidate.read_bytes()
            else:
                continue
        except OSError as exc:
            raise GuardError("a worktree file could not be inspected") from exc
        _add_limited(findings, _scan_text(path, f"worktree {path} path", config))
        _add_limited(findings, _scan_blob(data, path, f"worktree {path}", config))
    return findings


def inspect_pull_request_event(event: object, config: GuardConfig) -> list[Finding]:
    """Inspect public PR metadata from GitHub's trusted event payload."""
    if not isinstance(event, dict) or not isinstance(event.get("pull_request"), dict):
        raise GuardError("the GitHub event has no pull-request object")
    pull_request = event["pull_request"]
    title = pull_request.get("title")
    body = pull_request.get("body")
    head = pull_request.get("head")
    if not isinstance(title, str) or (body is not None and not isinstance(body, str)):
        raise GuardError("the pull-request title or body has an invalid type")
    if not isinstance(head, dict) or not isinstance(head.get("ref"), str):
        raise GuardError("the pull-request head ref has an invalid type")
    findings = _scan_text(title, "pull request title", config)
    _add_limited(findings, _scan_text(body or "", "pull request body", config))
    _add_limited(findings, _scan_text(head["ref"], "pull request head ref", config))
    return findings


def _commit_facts(repo: Path, sha: str) -> tuple[str, str, str, str, str]:
    fmt = "%an%x00%ae%x00%cn%x00%ce%x00%B"
    raw = _run_git(repo, ["show", "-s", f"--format={fmt}", sha])
    fields = raw.decode("utf-8", errors="replace").split("\x00", 4)
    if len(fields) != 5:
        raise GuardError("a commit identity record could not be parsed")
    return fields[0], fields[1], fields[2], fields[3], fields[4]


def _parse_changed_tree_entry(metadata_raw: bytes, path_raw: bytes) -> _TreeEntry | None:
    """Parse one raw record; a merge (combined diff) carries one extra mode and oid per parent.

    The record is ``:`` repeated once per parent, one mode and one oid for each
    parent plus the result, and one status token. The result's mode and oid win.
    """
    metadata = metadata_raw.decode("ascii", errors="replace").split()
    if not metadata or not metadata[0].startswith(":"):
        raise GuardError("a changed tree record could not be parsed")
    parents = len(metadata[0]) - len(metadata[0].lstrip(":"))
    if len(metadata) != 2 * (parents + 1) + 1:
        raise GuardError("a changed tree record could not be parsed")
    modes = [metadata[0][parents:], *metadata[1 : parents + 1]]
    oids = metadata[parents + 1 : 2 * parents + 2]
    status = metadata[-1]
    if (
        any(re.fullmatch(r"[0-7]{6}", mode) is None for mode in modes)
        or any(_OID_RE.fullmatch(oid) is None for oid in oids)
        or re.fullmatch(r"[ACDMRTUXB]+", status) is None
    ):
        raise GuardError("a changed tree record could not be parsed")
    new_mode, new_oid = modes[-1], oids[-1]
    if new_mode == "000000":
        return None
    path = path_raw.decode("utf-8", errors="surrogateescape")
    if new_mode in {"100644", "100755", "120000"}:
        return _TreeEntry(new_oid, path)
    if new_mode == "160000":
        return _TreeEntry(None, path)
    raise GuardError("a changed tree record has an unsupported mode")


def _changed_tree_entries(repo: Path, sha: str) -> list[_TreeEntry]:
    args = [
        "diff-tree",
        "--root",
        # Combined diff: a merge lists only paths whose result differs from every
        # parent, so content inherited from one parent is not attributed to the merge.
        "-c",
        "-r",
        "--no-commit-id",
        "--no-abbrev",
        "--no-renames",
        "--raw",
        "-z",
        sha,
    ]
    fields = _run_git(repo, args).split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise GuardError("a changed tree record could not be parsed")
    entries = [
        _parse_changed_tree_entry(fields[offset], fields[offset + 1])
        for offset in range(0, len(fields), 2)
    ]
    return [entry for entry in entries if entry is not None]


def _feed_batch(stream: BinaryIO, object_ids: list[str], errors: list[Exception]) -> None:
    try:
        for object_id in object_ids:
            stream.write(f"{object_id}\n".encode("ascii"))
        stream.close()
    except (BrokenPipeError, OSError) as exc:
        errors.append(exc)


def _batch_process(repo: Path) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise GuardError("git cat-file could not run") from exc


def _iter_blob_data(repo: Path, object_ids: list[str]) -> Iterable[tuple[str, bytes]]:
    process = _batch_process(repo)
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise GuardError("git cat-file pipes could not be created")
    feed_errors: list[Exception] = []
    feeder = threading.Thread(target=_feed_batch, args=(process.stdin, object_ids, feed_errors))
    feeder.start()
    try:
        for expected in object_ids:
            header = process.stdout.readline().decode("ascii", errors="replace").split()
            if len(header) != 3 or header[0] != expected or header[1] != "blob":
                raise GuardError("git cat-file returned an invalid blob record")
            try:
                size = int(header[2])
            except ValueError as exc:
                raise GuardError("git cat-file returned an invalid blob size") from exc
            if size > _MAX_BLOB_BYTES:
                raise GuardError("a blob exceeds the inspection size limit")
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b"\n":
                raise GuardError("git cat-file returned a truncated blob")
            yield expected, data
    finally:
        feeder.join(timeout=5)
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
    if feed_errors or process.returncode != 0:
        raise GuardError("git cat-file failed while inspecting content")


def _add_limited(target: list[Finding], additions: Iterable[Finding]) -> None:
    """Append findings, capping enforced and advisory findings separately."""
    pending = list(additions)
    for advisory in (False, True):
        remaining = _MAX_FINDINGS - sum(finding.advisory is advisory for finding in target)
        if remaining > 0:
            target.extend([f for f in pending if f.advisory is advisory][:remaining])


def _as_advisory(findings: Iterable[Finding]) -> list[Finding]:
    return [replace(finding, advisory=True) for finding in findings]


def _commit_fact_findings(
    repo: Path, sha: str, config: GuardConfig, provenance: Provenance
) -> tuple[list[Finding], bool]:
    """Return a commit's message and identity findings, and whether it is exempt.

    A commit is exempt when it is attributed outside work and its author is not
    allowlisted: all of its findings are advisory. An attributing merge keeps its
    identities enforced but its message (GitHub copies the branch name and PR
    title into it) is advisory.
    """
    author_name, author_email, committer_name, committer_email, message = _commit_facts(repo, sha)
    exempt = sha in provenance.outside and not _identity_allowed(
        author_name, author_email, config.allowed_authors
    )
    message_findings = _scan_text(message, f"commit {sha[:12]} message", config)
    identity_findings: list[Finding] = []
    for role, name, email in (
        ("author", author_name, author_email),
        ("committer", committer_name, committer_email),
    ):
        identity_findings.extend(
            _identity_findings(name, email, role, f"commit {sha[:12]}", config)
        )
    if exempt:
        return _as_advisory(message_findings + identity_findings), True
    if sha in provenance.attributing_merges:
        message_findings = _as_advisory(message_findings)
    return message_findings + identity_findings, False


def _commit_entries(
    repo: Path, sha: str, config: GuardConfig, trusted: frozenset[tuple[str, str]]
) -> tuple[list[Finding], list[tuple[str, str]]]:
    """Return path findings and (blob oid, path) pairs for entries not trusted on the base."""
    path_findings: list[Finding] = []
    blobs: list[tuple[str, str]] = []
    for entry in _changed_tree_entries(repo, sha):
        path = entry.path
        if _path_ignored(path, config):
            continue
        if entry.object_id is not None and (path, entry.object_id) in trusted:
            continue
        path_findings.extend(_scan_text(path, f"commit {sha[:12]} {path} path", config))
        if entry.object_id is not None:
            blobs.append((entry.object_id, path))
    return path_findings, blobs


def inspect_commits(
    repo: Path,
    commits: Iterable[str],
    config: GuardConfig,
    provenance: Provenance = NO_PROVENANCE,
) -> list[Finding]:
    """Inspect every commit fact and changed tree entry, de-duplicating blobs."""
    findings: list[Finding] = []
    # blob oid -> (commit, path, advisory); an enforced context replaces an advisory one.
    blob_context: dict[str, tuple[str, str, bool]] = {}
    for sha in dict.fromkeys(commits):
        fact_findings, exempt = _commit_fact_findings(repo, sha, config, provenance)
        _add_limited(findings, fact_findings)
        path_findings, blobs = _commit_entries(repo, sha, config, provenance.trusted_entries)
        _add_limited(findings, _as_advisory(path_findings) if exempt else path_findings)
        for object_id, path in blobs:
            known = blob_context.get(object_id)
            if known is None or (known[2] and not exempt):
                blob_context[object_id] = (sha, path, exempt)
    for object_id, data in _iter_blob_data(repo, list(blob_context)):
        sha, path, advisory = blob_context[object_id]
        blob_findings = _scan_blob(data, path, f"commit {sha[:12]} {path}", config)
        _add_limited(findings, _as_advisory(blob_findings) if advisory else blob_findings)
    return findings


def _rev_list(repo: Path, args: list[str]) -> list[str]:
    try:
        raw = _run_git(repo, ["rev-list", *args]).decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise GuardError("git rev-list returned non-ASCII output") from exc
    commits = [line for line in raw.splitlines() if line]
    if any(_OID_RE.fullmatch(sha) is None for sha in commits):
        raise GuardError("git rev-list returned an invalid object name")
    return commits


def _trusted_provenance(repo: Path, trusted_base: str | None) -> Provenance:
    """Trust the exact (path, blob oid) entries of ``trusted_base``; absent trusts nothing."""
    if trusted_base is None:
        return NO_PROVENANCE
    if _OID_RE.fullmatch(trusted_base) is None:
        raise GuardError("the trusted base revision is invalid")
    resolved = _run_git(
        repo, ["rev-parse", "--verify", "--end-of-options", f"{trusted_base}^{{commit}}"]
    )
    if resolved.decode("ascii", errors="replace").strip().casefold() != trusted_base.casefold():
        raise GuardError("the trusted base revision is not a commit")
    entries: set[tuple[str, str]] = set()
    for record in _run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", trusted_base]).split(
        b"\0"
    ):
        if not record:
            continue
        # "<mode> <type> <oid>\t<path>"
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.decode("ascii", errors="replace").split()
        if not separator or len(fields) != 3 or _OID_RE.fullmatch(fields[2]) is None:
            raise GuardError("a trusted base tree record could not be parsed")
        if fields[1] == "blob":
            entries.add((path.decode("utf-8", errors="surrogateescape"), fields[2]))
    return Provenance(trusted_entries=frozenset(entries))


def _outside_attribution(repo: Path, revision: str, owner: str) -> Provenance:
    """Attribute commits merged from another owner's pull request on first-parent history.

    A first-parent two-parent commit whose message first line is GitHub's merge
    title for a branch of another owner attributes the commits its second parent
    brought in. Nothing else attributes a commit, so unattributable commits stay
    enforced.
    """
    raw = _run_git(
        repo,
        ["log", "--first-parent", "-z", "--format=%H %P%n%B", "--end-of-options", revision],
    )
    outside: set[str] = set()
    attributing: set[str] = set()
    for record in raw.decode("utf-8", errors="replace").split("\0"):
        header, _, body = record.strip("\n").partition("\n")
        oids = header.split()
        if any(_OID_RE.fullmatch(oid) is None for oid in oids):
            raise GuardError("git log returned an invalid object name")
        match = _PR_MERGE_SUBJECT_RE.fullmatch(body.split("\n", 1)[0])
        if len(oids) != 3 or match is None or match["owner"].casefold() == owner.casefold():
            continue
        attributing.add(oids[0])
        outside.update(_rev_list(repo, [oids[2], "--not", oids[1]]))
    return Provenance(frozenset(outside), frozenset(attributing))


def _nested(value: object, *keys: str) -> object:
    """Read ``value[keys[0]][keys[1]]...``; anything missing or not a dict is ``None``."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = cast("dict[str, object]", value).get(key)
    return value


def _repository_ids(pull_request: object) -> tuple[int, int] | None:
    """Return the (head, base) repository IDs, or ``None`` when either is unreadable."""
    head_id = _nested(pull_request, "head", "repo", "id")
    base_id = _nested(pull_request, "base", "repo", "id")
    if (
        isinstance(head_id, int)
        and isinstance(base_id, int)
        and not isinstance(head_id, bool)
        and not isinstance(base_id, bool)
    ):
        return head_id, base_id
    return None


def _is_outside(pull_request: object) -> bool:
    """True for a fork PR whose author cannot write here; unreadable fields fail closed."""
    ids = _repository_ids(pull_request)
    association = _nested(pull_request, "author_association")
    return (
        ids is not None
        and ids[0] != ids[1]
        and isinstance(association, str)
        and association.strip().upper() not in _MAINTAINER_ASSOCIATIONS
    )


def _is_mergify_candidate(pull_request: object) -> bool:
    """True for a same-repository PR opened by the Mergify bot on its queue branch."""
    ids = _repository_ids(pull_request)
    head_ref = _nested(pull_request, "head", "ref")
    return (
        ids is not None
        and ids[0] == ids[1]
        and type(_nested(pull_request, "user", "id")) is int
        and _nested(pull_request, "user", "id") == _MERGIFY_BOT_ID
        and _nested(pull_request, "user", "type") == "Bot"
        and isinstance(head_ref, str)
        and head_ref.startswith(_CANDIDATE_REF_PREFIX)
    )


def _pull_request_object(event: object) -> dict[str, object]:
    pull_request = _nested(event, "pull_request")
    if not isinstance(pull_request, dict):
        raise GuardError("the GitHub event has no pull-request object")
    return cast("dict[str, object]", pull_request)


def _pull_request_scope(pull_request: object) -> PullRequestScope:
    """Classify a pull request; the classifiers are disjoint (a candidate is same-repository)."""
    if _is_outside(pull_request):
        return "maintainer" if _shares_head_with_other_work(pull_request) else "outside"
    if _is_mergify_candidate(pull_request):
        return "candidate"
    return "maintainer"


def _shares_head_with_other_work(pull_request: object) -> bool:
    """True when another PR with this head commit is not outside work, or the lookup fails.

    The required status is published on the head commit, which several PRs can
    share. An outside PR whose head is a maintainer PR's (or a Mergify
    candidate's) head commit would otherwise overwrite that PR's status with its
    own advisory success, so such a PR is enforced like maintainer work.
    """
    try:
        if not isinstance(pull_request, dict):
            raise GuardError("the pull request is invalid")
        pull_request = cast("dict[str, object]", pull_request)
        full_name, _number = _pull_request_target(pull_request)
        head_sha = _nested(pull_request, "head", "sha")
        if not isinstance(head_sha, str) or _OID_RE.fullmatch(head_sha) is None:
            raise GuardError("the pull-request head revision is invalid")
        output = _gh_api(
            [
                "--paginate",
                f"repos/{full_name}/commits/{head_sha}/pulls",
                "--jq",
                _COMMIT_PULLS_JQ,
            ],
            "head-commit lookup",
        )
        associated = _json_lines(output, "head-commit lookup")
    except GuardError:
        print(
            "NOTE: the head-commit lookup failed; this pull request stays enforced",
            file=sys.stderr,
        )
        return True
    # This PR is outside work itself, so it never counts as another one.
    shared = any(not _is_outside(row) for row in associated)
    if shared:
        print(
            "NOTE: another non-outside pull request has this head commit; enforced",
            file=sys.stderr,
        )
    return shared


def _read_event(event_path: Path) -> object:
    try:
        return json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError("the GitHub event is invalid") from exc


def _gh_env() -> dict[str, str]:
    """Environment for non-interactive ``gh``: no prompts, no ambient repository override."""
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env.pop("GH_REPO", None)
    env.pop(_KEY_B64_ENV, None)
    return env


def _github_pull_request(full_name: str, number: int) -> dict[str, object] | None:
    """Read pull request ``number`` through the workflow token; ``None`` on any failure.

    Output is never printed, so a failure cannot leak content.
    """
    if _REPOSITORY_NAME_RE.fullmatch(full_name) is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{full_name}/pulls/{number}"],
            capture_output=True,
            check=False,
            env=_gh_env(),
            timeout=_LOOKUP_TIMEOUT_SECONDS,
        )
        parsed: object = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


def _first_parent_chain(repo: Path, head: str, base: str) -> list[list[str]]:
    """Return ``[commit, *parents]`` for the first-parent chain from ``head`` down to ``base``."""
    raw = _run_git(repo, ["rev-list", "--first-parent", "--parents", head, "--not", base])
    chain = [line.split() for line in raw.decode("ascii", errors="replace").splitlines() if line]
    if any(_OID_RE.fullmatch(oid) is None for oids in chain for oid in oids):
        raise GuardError("git rev-list returned an invalid object name")
    return chain


def _candidate_merge_exemption(
    repo: Path, full_name: str, oids: list[str]
) -> tuple[list[str], str | None] | None:
    """Return a ``Merge of #N`` commit's exempt commits and, if none, why; else ``None``.

    Commits reachable from the second parent and not the first belong to queued
    PR N, and are exempt only when GitHub reports PR N as outside work whose head
    is exactly that second parent.
    """
    if len(oids) != 3:
        return None
    match = _CANDIDATE_MERGE_RE.fullmatch(_commit_facts(repo, oids[0])[4].strip())
    if match is None:
        return None
    pull_request = _github_pull_request(full_name, int(match["number"]))
    if pull_request is None:
        reason = "lookup failed"
    elif not _is_outside(pull_request):
        reason = "not an outside PR"
    elif str(_nested(pull_request, "head", "sha")).casefold() != oids[2].casefold():
        reason = "head mismatch"
    else:
        return _rev_list(repo, [oids[2], "--not", oids[1]]), None
    return (
        [],
        f"NOTE: candidate merge {oids[0][:12]} of #{match['number']} stays enforced: {reason}",
    )


def _candidate_exempt_commits(repo: Path, pull_request: dict[str, object]) -> frozenset[str]:
    """Commits of a Mergify candidate that belong to verified outside pull requests."""
    head = _nested(pull_request, "head", "sha")
    base = _nested(pull_request, "base", "sha")
    full_name = _nested(pull_request, "base", "repo", "full_name")
    if (
        not isinstance(head, str)
        or not isinstance(base, str)
        or _OID_RE.fullmatch(head) is None
        or _OID_RE.fullmatch(base) is None
    ):
        raise GuardError("the candidate pull request has no valid head or base revision")
    exempt: set[str] = set()
    merges = 0
    for oids in _first_parent_chain(repo, head, base):
        exemption = _candidate_merge_exemption(
            repo, full_name if isinstance(full_name, str) else "", oids
        )
        if exemption is None:
            continue
        merges += 1
        exempt.update(exemption[0])
        if exemption[1] is not None:
            print(exemption[1], file=sys.stderr)
    if merges == 0:
        print("NOTE: candidate has no 'Merge of #N' commit; nothing is exempt", file=sys.stderr)
    return frozenset(exempt)


def _scope_provenance(
    repo: Path,
    scope: PullRequestScope,
    pull_request: dict[str, object],
    commits: Iterable[str],
    base_provenance: Provenance,
) -> Provenance:
    """Mark the commits that are advisory for this pull request's scope."""
    if scope == "outside":
        return replace(base_provenance, outside=frozenset(commits))
    if scope == "candidate":
        return replace(base_provenance, outside=_candidate_exempt_commits(repo, pull_request))
    return base_provenance


def _advertised_destination_oids(repo: Path, remote_location: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--refs", "--", remote_location],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        lines = result.stdout.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return None
    advertised: list[str] = []
    for line in lines:
        fields = line.split("\t", 1)
        if len(fields) != 2:
            return None
        object_id = fields[0]
        if _OID_RE.fullmatch(object_id) is None:
            return None
        # GitHub advertises unmerged fork heads under refs/pull/*; they are
        # outside content, not accepted destination history.
        if fields[1].startswith("refs/pull/"):
            continue
        advertised.append(object_id)
    return list(dict.fromkeys(advertised))


def _available_commit_oids(repo: Path, object_ids: list[str]) -> list[str]:
    if not object_ids:
        return []
    expressions = [f"{object_id}^{{commit}}" for object_id in object_ids]
    try:
        raw = _run_git(
            repo,
            ["cat-file", "--batch-check"],
            input_bytes=("\n".join(expressions) + "\n").encode("ascii"),
        )
    except GuardError:
        return []
    tips: list[str] = []
    for line in raw.decode("ascii", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2 and _OID_RE.fullmatch(fields[0]) and fields[1] == "commit":
            tips.append(fields[0])
    return list(dict.fromkeys(tips))


def _local_destination_tips(repo: Path, remote_location: str) -> list[str]:
    """Return advertised destination tips that are available in the local object store."""
    advertised = _advertised_destination_oids(repo, remote_location)
    return _available_commit_oids(repo, advertised or [])


def outgoing_commits(
    repo: Path,
    local_sha: str,
    remote_sha: str,
    *,
    destination_tips: Iterable[str] = (),
) -> list[str]:
    """Return commits newly exposed by one ref update."""
    exclusions = [] if _is_zero_sha(remote_sha) else [remote_sha]
    exclusions.extend(destination_tips)
    exclusions = list(dict.fromkeys(exclusions))
    if exclusions:
        return _rev_list(repo, [local_sha, "--not", *exclusions])
    return _rev_list(repo, [local_sha])


def _is_zero_sha(value: str) -> bool:
    return len(value) in (40, 64) and not value.strip("0")


def _print_findings(findings: list[Finding], config: GuardConfig, stream: TextIO) -> None:
    """Print enforced findings under ERROR and advisory ones under WARNING, redacted."""
    for advisory, header in (
        (False, "ERROR: confidential-content leak guard blocked this operation."),
        (True, "WARNING: advisory findings in attributed outside work (not blocking):"),
    ):
        selected = [finding for finding in findings if finding.advisory is advisory]
        if not selected:
            continue
        print(header, file=stream)
        for finding in selected:
            location = _redact_location(finding.location, config)
            suffix = f" (term id {finding.term_id})" if finding.term_id else ""
            print(f"  - {finding.kind}: {location}{suffix}", file=stream)
        if len(selected) >= _MAX_FINDINGS:
            label = "advisory" if advisory else "enforced"
            print(f"  - output limited to {_MAX_FINDINGS} {label} findings", file=stream)


def _has_enforced(findings: list[Finding]) -> bool:
    return any(not finding.advisory for finding in findings)


def _guard_failure(exc: GuardError, stream: TextIO) -> int:
    print("ERROR: confidential-content leak guard could not complete.", file=stream)
    print(f"  {exc}", file=stream)
    print("  The operation is refused because an incomplete scan cannot be safe.", file=stream)
    return 1


def _write_advisory_report(
    report_path: Path, findings: list[Finding], config: GuardConfig
) -> None:
    """Append one JSON line per advisory term finding for ``post-advisory``.

    Selected by kind, never by location text. Identity advisories are logged only:
    every outside contributor's identity is unlisted, so reporting them would
    label every outside pull request.
    """
    rows = [
        json.dumps(
            {
                "kind": finding.kind,
                "location": _redact_location(finding.location, config),
                "term_id": finding.term_id,
            },
            sort_keys=True,
        )
        for finding in findings
        if finding.advisory and finding.kind == _TERM_KIND
    ]
    try:
        with report_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.writelines(f"{row}\n" for row in rows)
    except OSError as exc:
        raise GuardError("the advisory report cannot be written") from exc


def _finish_scan(
    findings: list[Finding],
    config: GuardConfig,
    *,
    report_path: Path | None,
    clean_message: str | None = None,
) -> int:
    """Print findings, write the advisory report when given, and return the exit status.

    Only enforced findings fail the scan; advisory findings are printed as warnings.
    """
    if findings:
        _print_findings(findings, config, sys.stderr)
        if report_path is not None:
            _write_advisory_report(report_path, findings, config)
    if _has_enforced(findings):
        return 1
    if not findings and clean_message is not None:
        print(clean_message)
    return 0


AdvisoryRow = tuple[str, str, str]


def _read_advisory_report(report_path: Path) -> list[AdvisoryRow]:
    """Read the report as distinct ``(kind, location, term id)`` rows, in file order."""
    try:
        records = [
            json.loads(line)
            for line in report_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        raise GuardError("the advisory report cannot be read") from exc
    rows: list[AdvisoryRow] = []
    for record in records:
        fields = [_nested(record, name) for name in ("kind", "location", "term_id")]
        if not all(isinstance(field, str) for field in fields):
            raise GuardError("the advisory report is malformed")
        rows.append(cast("AdvisoryRow", tuple(fields)))
    return list(dict.fromkeys(rows))


def _finding_set_digest(rows: list[AdvisoryRow]) -> str:
    """Digest the finding set, ignoring commit ids and line numbers so a rebase keeps it."""
    normalized = sorted(
        {
            (kind, _LOCATION_LINE_RE.sub("", _LOCATION_COMMIT_RE.sub("", location)), term_id)
            for kind, location, term_id in rows
        }
    )
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def _table_cell(text: str) -> str:
    """Render untrusted text as one inert code span in a Markdown table cell."""
    cleaned = "".join("?" if ord(char) < 32 or ord(char) == 127 else char for char in text)
    cleaned = cleaned[:_MAX_LOCATION_CHARS].replace("`", "'").replace("|", "\\|")
    return f"`{cleaned}`"


def _advisory_comment_body(marker: str, rows: list[AdvisoryRow]) -> str:
    """Build the comment: the marker, a plain note, and a bounded table of redacted rows."""
    lines = [
        marker,
        "",
        "Confidential-content scan: advisory findings on this outside pull request.",
        "They do not block it and nothing is required from you; a maintainer will review them.",
        "",
        "| Kind | Location | Term id |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {kind} | {_table_cell(location)} | {_table_cell(term_id)} |"
        for kind, location, term_id in rows[:_MAX_ADVISORY_ROWS]
    )
    if len(rows) > _MAX_ADVISORY_ROWS:
        lines.extend(["", f"{len(rows) - _MAX_ADVISORY_ROWS} more findings not shown."])
    return "\n".join(lines) + "\n"


def _gh_api(arguments: list[str], what: str, stdin: bytes | None = None) -> bytes:
    """Run ``gh api``; any failure is a ``GuardError`` that never echoes ``gh`` output."""
    try:
        result = subprocess.run(
            ["gh", "api", *arguments],
            input=stdin,
            capture_output=True,
            check=False,
            env=_gh_env(),
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"the GitHub {what} could not run") from exc
    if result.returncode != 0:
        raise GuardError(f"the GitHub {what} failed")
    return result.stdout


def _json_lines(output: bytes, what: str) -> list[object]:
    """Parse ``gh --jq`` output, one JSON value per line (``@json`` never emits a raw newline)."""
    try:
        text = output.decode("utf-8", errors="replace")
        return [json.loads(line) for line in text.split("\n") if line]
    except ValueError as exc:
        raise GuardError(f"the GitHub {what} returned an invalid record") from exc


def _advisory_posted(full_name: str, number: int, marker: str) -> bool:
    """True when the workflow bot already commented this finding set.

    Only the bot's own comments count, and only when the marker is the comment's
    first line, so neither a contributor's comment nor a path planted in the
    table can suppress the advisory. ``--jq`` runs once per page, so each comment
    is emitted as one JSON line (``@json`` never emits a raw newline) and filtered here.
    """
    output = _gh_api(
        [
            "--paginate",
            f"repos/{full_name}/issues/{number}/comments",
            "--jq",
            '.[] | [.user.login, .user.type, (.body // "")] | @json',
        ],
        "advisory lookup",
    )
    for row in _json_lines(output, "advisory lookup"):
        try:
            login, account_type, body = cast("list[str]", row)
        except (ValueError, TypeError) as exc:
            raise GuardError("the GitHub advisory lookup returned an invalid record") from exc
        if (
            login == _BOT_LOGIN
            and account_type == "Bot"
            and body.split("\n", 1)[0].rstrip("\r") == marker
        ):
            return True
    return False


def _pull_request_target(pull_request: dict[str, object]) -> tuple[str, int]:
    """Return the base repository's full name and the pull request number."""
    full_name = _nested(pull_request, "base", "repo", "full_name")
    number = pull_request.get("number")
    if (
        not isinstance(full_name, str)
        or _REPOSITORY_NAME_RE.fullmatch(full_name) is None
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
    ):
        raise GuardError("the pull-request repository or number is invalid")
    return full_name, number


def post_advisory_main(event_path: Path, report_path: Path) -> int:
    """Comment and label an outside PR once per finding set, inside the status-bearing step.

    The label goes first so the bot marker exists only once the label is on; a
    failed label or comment leaves no marker and the next run retries both.
    """
    try:
        rows = _read_advisory_report(report_path)
        if not rows:
            print("No advisory findings to post.")
            return 0
        full_name, number = _pull_request_target(_pull_request_object(_read_event(event_path)))
        marker = f"<!-- {_ADVISORY_MARKER} {_finding_set_digest(rows)} -->"
        if _advisory_posted(full_name, number, marker):
            print("Advisory for this finding set was already posted.")
            return 0
        label = json.dumps({"labels": [_REVIEW_LABEL]}).encode("utf-8")
        _gh_api(
            ["--method", "POST", f"repos/{full_name}/issues/{number}/labels", "--input", "-"],
            "advisory label",
            label,
        )
        comment = json.dumps({"body": _advisory_comment_body(marker, rows)}).encode("utf-8")
        _gh_api(
            ["--method", "POST", f"repos/{full_name}/issues/{number}/comments", "--input", "-"],
            "advisory comment",
            comment,
        )
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)
    print("Advisory comment posted and review label applied.")
    return 0


def commit_message_hook_main(message_path: Path, repo: Path | str | None = None) -> int:
    root = Path(repo or Path.cwd()).resolve()
    try:
        config = load_config(root)
        message = message_path.read_text(encoding="utf-8", errors="replace")
        findings = inspect_pending_commit(root, message, config)
    except (GuardError, OSError) as exc:
        error = (
            exc if isinstance(exc, GuardError) else GuardError("the commit message cannot be read")
        )
        return _guard_failure(error, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
        return 1
    return 0


def pre_push_hook_main(
    repo: Path | str | None = None,
    stdin: TextIO = sys.stdin,
    remote_name: str | None = None,
    remote_location: str | None = None,
    trusted_base: str | None = None,
    pull_request_event: Path | None = None,
    advisory_report: Path | None = None,
) -> int:
    """Scan outgoing commits; with a PR event, outside work is advisory (see module docs)."""
    root = Path(repo or Path.cwd()).resolve()
    scope: PullRequestScope = "maintainer"
    try:
        if (remote_name is None) != (remote_location is None):
            raise GuardError("the pre-push destination is incomplete")
        config = load_config(root)
        provenance = _trusted_provenance(root, trusted_base)
        updates = [RefUpdate.parse(line) for line in stdin]
        has_non_deletion = any(not _is_zero_sha(update.local_sha) for update in updates)
        destination_tips = (
            _local_destination_tips(root, remote_location)
            if remote_location and has_non_deletion
            else []
        )
        commits: list[str] = []
        for update in updates:
            if _is_zero_sha(update.local_sha):
                continue
            commits.extend(
                outgoing_commits(
                    root,
                    update.local_sha,
                    update.remote_sha,
                    destination_tips=destination_tips,
                )
            )
        if pull_request_event is not None:
            pull_request = _pull_request_object(_read_event(pull_request_event))
            scope = _pull_request_scope(pull_request)
            provenance = _scope_provenance(root, scope, pull_request, commits, provenance)
        findings = inspect_commits(root, commits, config, provenance)
        return _finish_scan(
            findings, config, report_path=advisory_report if scope == "outside" else None
        )
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)


def _event_metadata_findings(
    event: object, config: GuardConfig, scope: PullRequestScope
) -> list[Finding]:
    """Scan PR metadata; outside work and Mergify-generated candidate text is advisory."""
    findings = inspect_pull_request_event(event, config)
    return _as_advisory(findings) if scope != "maintainer" else findings


def pull_request_main(
    repo: Path | str, event_path: Path, advisory_report: Path | None = None
) -> int:
    """Inspect PR title, body, and head ref without executing pull-request code."""
    root = Path(repo).resolve()
    try:
        config = load_config(root)
        event = _read_event(event_path)
        scope = _pull_request_scope(_pull_request_object(event))
        findings = _event_metadata_findings(event, config, scope)
        return _finish_scan(
            findings,
            config,
            report_path=advisory_report if scope == "outside" else None,
            clean_message="Confidential-content pull-request metadata guard: clean.",
        )
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)


def _read_pr_text(stream: BinaryIO) -> str:
    data = stream.read(_MAX_PR_TEXT_BYTES + 1)
    if len(data) > _MAX_PR_TEXT_BYTES:
        raise GuardError("proposed PR text exceeds the inspection size limit")
    return data.decode("utf-8")


def pr_text_main(repo: Path | str, files: list[Path], *, read_stdin: bool = False) -> int:
    """Inspect proposed PR text before a GitHub write publishes it."""
    root = Path(repo).resolve()
    try:
        if not files and not read_stdin:
            raise GuardError("at least one proposed PR text input is required")
        config = load_config(root)
        findings: list[Finding] = []
        for index, path in enumerate(files, start=1):
            with path.open("rb") as stream:
                text = _read_pr_text(stream)
            _add_limited(findings, _scan_text(text, f"proposed PR text {index}", config))
        if read_stdin:
            text = _read_pr_text(sys.stdin.buffer)
            _add_limited(findings, _scan_text(text, "proposed PR stdin text", config))
    except (GuardError, OSError, UnicodeError) as exc:
        error = (
            exc if isinstance(exc, GuardError) else GuardError("proposed PR text cannot be read")
        )
        return _guard_failure(error, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
        return 1
    print("Confidential-content proposed PR text guard: clean.")
    return 0


def _publish_pr_command(
    args: argparse.Namespace, title: str | None, body: str | None
) -> list[str]:
    command = ["gh", "pr", args.pr_action]
    if args.pr_action == "create":
        command.extend(("--base", args.base, "--head", args.head, "--title", title))
        if args.draft:
            command.append("--draft")
    else:
        command.append(args.pr)
        if title is not None:
            command.extend(("--title", title))
        if args.pr_action == "review":
            command.append(f"--{args.verdict}")
        if args.pr_action == "comment" and args.edit_last:
            command.append("--edit-last")
    if body is not None:
        command.extend(("--body-file", "-"))
    if args.repo_name:
        command.extend(("--repo", args.repo_name))
    return command


def publish_pr_main(repo: Path | str, args: argparse.Namespace) -> int:
    """Scan private TOML, then send the same text in one PR write operation."""
    root = Path(repo).resolve()
    try:
        title_file = getattr(args, "title_file", None)
        if args.pr_action == "edit" and title_file is None and args.body_file is None:
            raise GuardError("a PR edit requires a title or body draft")
        config = load_config(root)
        title = None
        if title_file is not None:
            with title_file.open("rb") as stream:
                title = _read_pr_text(stream).rstrip("\r\n")
            if not title or "\n" in title or "\r" in title or "\x00" in title:
                raise GuardError("the proposed PR title is invalid")
        body = None
        if args.body_file is not None:
            with args.body_file.open("rb") as stream:
                body = _read_pr_text(stream)
        findings: list[Finding] = []
        if title is not None:
            _add_limited(findings, _scan_text(title, "proposed PR title", config))
        if body is not None:
            _add_limited(findings, _scan_text(body, "proposed PR body", config))
        if args.pr_action == "create":
            _add_limited(findings, _scan_text(args.head, "proposed PR head", config))
        if findings:
            _print_findings(findings, config, sys.stderr)
            return 1
        result = subprocess.run(
            _publish_pr_command(args, title, body),
            input=body.encode("utf-8") if body is not None else b"",
            cwd=root,
            env=_gh_env(),
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            raise GuardError("the GitHub pull-request write failed")
    except (OSError, UnicodeError, subprocess.SubprocessError, GuardError) as exc:
        error = exc if isinstance(exc, GuardError) else GuardError("the PR write could not run")
        return _guard_failure(error, sys.stderr)
    print("GitHub pull-request write completed after confidential-content scan.")
    reference = _pr_write_reference(result.stdout)
    if reference is not None:
        print(f"GitHub reference: {reference}")
    return 0


def _pr_write_reference(gh_stdout: bytes) -> str | None:
    """Return the created PR, comment, or review URL that ``gh`` reported.

    Callers need the returned identifier to tell their own write from a later
    identical one. Any other output stays suppressed.
    """
    words = gh_stdout.decode("utf-8", errors="replace").split()
    matches = [word for word in words if _PR_WRITE_URL_RE.fullmatch(word)]
    return matches[-1] if matches else None


def _create_local_key() -> bytes:
    key = os.urandom(32)
    path = _key_path(key_id(key))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(base64.b64encode(key) + b"\n")
    return key


def _write_sealed_config(repo: Path, sealed: bytes) -> None:
    path = repo / _SEALED_CONFIG_NAME
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(sealed)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def seal_config_main(repo: Path | str) -> int:
    """Encrypt a local edit draft into the tracked source of truth."""
    root = Path(repo).resolve()
    try:
        raw_config = _local_config_bytes(root)
        _parse_config(raw_config, root)
        path = root / _SEALED_CONFIG_NAME
        if path.exists():
            previous = _sealed_config_bytes(root)
            identifier = sealed_key_id(previous)
            key = _vocabulary_key(identifier, local_only=True)
            if unseal_vocabulary(previous, key) == raw_config:
                print("Encrypted confidential vocabulary is already current.")
                return 0
        else:
            key = _create_local_key()
        sealed = seal_vocabulary(raw_config, key)
        if unseal_vocabulary(sealed, key) != raw_config:
            raise GuardError("the encrypted vocabulary could not be verified")
        _write_sealed_config(root, sealed)
    except (OSError, SealedVocabularyError, GuardError) as exc:
        error = (
            exc
            if isinstance(exc, GuardError)
            else GuardError("the vocabulary could not be sealed")
        )
        return _guard_failure(error, sys.stderr)
    print("Encrypted confidential vocabulary updated; back up the separate key file.")
    return 0


def key_path_main(repo: Path | str) -> int:
    root = Path(repo).resolve()
    try:
        identifier = sealed_key_id(_sealed_config_bytes(root))
    except (SealedVocabularyError, GuardError) as exc:
        error = (
            exc if isinstance(exc, GuardError) else GuardError("the key path cannot be resolved")
        )
        return _guard_failure(error, sys.stderr)
    print(_key_path(identifier))
    return 0


def restore_draft_main(repo: Path | str) -> int:
    """Recreate a private edit draft after cloning and restoring the key."""
    root = Path(repo).resolve()
    try:
        default_path = _default_private_config_path(root)
        if default_path is None:
            raise GuardError("the private draft location cannot be resolved")
        path = _configured_path(root) or default_path
        resolved_path = path.resolve()
        if resolved_path.is_relative_to(root) and not resolved_path.is_relative_to(
            default_path.parent
        ):
            raise GuardError("the private draft destination must be outside the worktree")
        raw_config = _config_bytes(root)
        _parse_config(raw_config, root)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(raw_config)
    except FileExistsError:
        return _guard_failure(GuardError("the private edit draft already exists"), sys.stderr)
    except (OSError, GuardError) as exc:
        error = (
            exc
            if isinstance(exc, GuardError)
            else GuardError("the private draft could not be restored")
        )
        return _guard_failure(error, sys.stderr)
    print("Private edit draft restored.")
    return 0


def _load_vocabularies(root: Path, revision: str) -> tuple[GuardConfig, GuardConfig]:
    """Return the trusted and the proposed vocabulary; the proposal must use the trusted key."""
    trusted = _sealed_config_bytes(root)
    identifier = sealed_key_id(trusted)
    key = _vocabulary_key(identifier)
    trusted_config = _parse_config(unseal_vocabulary(trusted, key), root)
    spec = f"{revision}:{_SEALED_CONFIG_NAME}"
    size = int(_run_git(root, ["cat-file", "-s", spec]).strip())
    if size > 2 * 1024 * 1024:
        raise GuardError("the proposed encrypted vocabulary exceeds the size limit")
    proposed = _run_git(root, ["cat-file", "blob", spec])
    if sealed_key_id(proposed) != identifier:
        raise GuardError("the proposed encrypted vocabulary uses a different key")
    return trusted_config, _parse_config(unseal_vocabulary(proposed, key), root)


def _candidate_findings(
    root: Path,
    commits: list[str],
    event: object,
    scope: PullRequestScope,
    config: GuardConfig,
    trusted: Provenance,
) -> list[Finding]:
    """Commit and metadata findings of a proposed candidate, per the PR's provenance scope."""
    provenance = _scope_provenance(root, scope, _pull_request_object(event), commits, trusted)
    findings = inspect_commits(root, commits, config, provenance)
    _add_limited(findings, _event_metadata_findings(event, config, scope))
    return findings


def verify_candidate_main(
    repo: Path | str,
    revision: str,
    base: str,
    event_path: Path,
    trusted_base: str | None = None,
    advisory_report: Path | None = None,
) -> int:
    """Validate and scan a proposed vocabulary using the trusted current key.

    Outside work is advisory (see module docs), but an invalid or foreign
    proposed vocabulary always fails. Locations are redacted with both the trusted
    and the proposed vocabulary, so a term the proposal removes never prints.
    """
    root = Path(repo).resolve()
    try:
        if _OID_RE.fullmatch(revision) is None or _OID_RE.fullmatch(base) is None:
            raise GuardError("the candidate or base revision is invalid")
        trusted_config, candidate_config = _load_vocabularies(root, revision)
        event = _read_event(event_path)
        scope = _pull_request_scope(_pull_request_object(event))
        findings = _candidate_findings(
            root,
            outgoing_commits(root, revision, base),
            event,
            scope,
            candidate_config,
            _trusted_provenance(root, trusted_base),
        )
        findings = [
            replace(finding, location=_redact_location(finding.location, trusted_config))
            for finding in findings
        ]
        return _finish_scan(
            findings,
            candidate_config,
            report_path=advisory_report if scope == "outside" else None,
            clean_message="Proposed encrypted vocabulary: valid.",
        )
    except (ValueError, OSError, SealedVocabularyError, GuardError) as exc:
        error = (
            exc
            if isinstance(exc, GuardError)
            else GuardError("the proposed vocabulary is invalid")
        )
        return _guard_failure(error, sys.stderr)


def sync_ci_key_main(repo: Path | str) -> int:
    """Publish only the decryption key; the vocabulary stays in Git."""
    root = Path(repo).resolve()
    try:
        sealed = _sealed_config_bytes(root)
        identifier = sealed_key_id(sealed)
        key = _vocabulary_key(identifier, local_only=True)
        _parse_config(unseal_vocabulary(sealed, key), root)
        env = os.environ.copy()
        env["GH_PROMPT_DISABLED"] = "1"
        env.pop(_KEY_B64_ENV, None)
        env.pop("GH_REPO", None)
        result = subprocess.run(
            ["gh", "secret", "set", _KEY_B64_ENV, "--app", "actions"],
            input=base64.b64encode(key),
            cwd=root,
            env=env,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            raise GuardError("the GitHub Actions decryption key could not be updated")
    except (OSError, subprocess.SubprocessError, SealedVocabularyError, GuardError) as exc:
        error = exc if isinstance(exc, GuardError) else GuardError("gh secret set could not run")
        return _guard_failure(error, sys.stderr)
    print("GitHub Actions confidential vocabulary key synchronized.")
    return 0


def audit_main(
    repo: Path | str,
    revisions: list[str],
    *,
    include_worktree: bool = False,
    repository_owner: str | None = None,
) -> int:
    """Audit full ancestry; with an owner, attributed outside PR work is advisory."""
    root = Path(repo).resolve()
    try:
        config = load_config(root)
        provenance = NO_PROVENANCE
        if repository_owner is not None:
            if _OWNER_RE.fullmatch(repository_owner) is None:
                raise GuardError("the repository owner is not a valid login")
            if len(revisions) != 1:
                raise GuardError("attribution needs exactly one revision")
            provenance = _outside_attribution(root, revisions[0], repository_owner)
        commits: list[str] = []
        for revision in revisions:
            commits.extend(_rev_list(root, [revision]))
        findings = inspect_commits(root, commits, config, provenance)
        if include_worktree:
            _add_limited(findings, inspect_worktree(root, config))
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
    if _has_enforced(findings):
        return 1
    print("Confidential-content leak guard: clean.")
    return 0


_ADVISORY_REPORT_HELP = "append outside-PR advisory term findings (JSON lines) to this file"


def _add_pr_publish_parser(subparsers: argparse._SubParsersAction) -> None:
    publish_parser = subparsers.add_parser(
        "publish-pr", help="scan and submit PR text in one operation"
    )
    publish_actions = publish_parser.add_subparsers(dest="pr_action", required=True)
    for action in ("create", "edit", "comment", "review"):
        action_parser = publish_actions.add_parser(action)
        action_parser.add_argument("--repo", dest="repo_name")
        if action == "create":
            action_parser.add_argument("--title-file", required=True, type=Path)
            action_parser.add_argument("--body-file", required=True, type=Path)
            action_parser.add_argument("--base", required=True)
            action_parser.add_argument("--head", required=True)
            action_parser.add_argument("--draft", action="store_true")
        else:
            action_parser.add_argument("--pr", required=True)
            if action == "edit":
                action_parser.add_argument("--title-file", type=Path)
                action_parser.add_argument("--body-file", type=Path)
            else:
                action_parser.add_argument("--body-file", required=True, type=Path)
                if action == "review":
                    action_parser.add_argument(
                        "--verdict",
                        required=True,
                        choices=("approve", "comment", "request-changes"),
                    )
                else:
                    action_parser.add_argument("--edit-last", action="store_true")


def self_test_main() -> int:
    """Prove the scanner's encryption dependency seals, unseals, and rejects tampering.

    Needs no key, repository, or network: it uses a throwaway key and plaintext.
    Exit 1 when the dependency is missing, cannot round-trip, or accepts a
    tampered ciphertext (a base64 or format error is not tamper detection).
    """
    plaintext = b"self-test vocabulary"
    try:
        key = os.urandom(32)
        sealed = seal_vocabulary(plaintext, key)
        if unseal_vocabulary(sealed, key) != plaintext:
            raise GuardError("the dependency did not return the sealed plaintext")
        header, identifier, payload, _tail = sealed.split(b"\n")
        raw = bytearray(base64.b64decode(payload, validate=True))
        raw[-1] ^= 0x01  # last byte of the authentication tag
        tampered = b"\n".join((header, identifier, base64.b64encode(bytes(raw)), b""))
        try:
            unseal_vocabulary(tampered, key)
        except SealedVocabularyError as exc:
            if "failed authentication" not in str(exc):
                raise GuardError(f"tampering was rejected for the wrong reason: {exc}") from exc
        else:
            raise GuardError("the dependency accepted a tampered ciphertext")
    except (ImportError, SealedVocabularyError, GuardError) as exc:
        print(f"Confidential-content scanner self-test failed: {exc}", file=sys.stderr)
        return 1
    print("Confidential-content scanner self-test: passed.")
    return 0


def _add_history_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register the candidate-vocabulary and full-history subcommands."""
    verify_parser = subparsers.add_parser(
        "verify-candidate", help="validate proposed encrypted vocabulary"
    )
    verify_parser.add_argument("--rev", required=True)
    verify_parser.add_argument("--base", required=True)
    verify_parser.add_argument("--event", required=True, type=Path)
    verify_parser.add_argument(
        "--trusted-base", help="commit whose exact path and blob entries are already accepted"
    )
    verify_parser.add_argument("--advisory-report", type=Path, help=_ADVISORY_REPORT_HELP)
    subparsers.add_parser("sync-ci-key", help="publish the decryption key to Actions")
    subparsers.add_parser(
        "self-test", help="check the encryption dependency without a key or repository"
    )
    audit_parser = subparsers.add_parser("audit", help="inspect full ancestry of revisions")
    audit_parser.add_argument("--rev", action="append", default=[], help="revision to inspect")
    audit_parser.add_argument(
        "--worktree", action="store_true", help="also inspect tracked and untracked checkout files"
    )
    audit_parser.add_argument(
        "--repository-owner",
        help="exempt commits merged from pull requests of any other owner (one --rev)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository to inspect")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commit_parser = subparsers.add_parser("commit-msg", help="inspect a pending commit")
    commit_parser.add_argument("message_file", type=Path)
    pre_push_parser = subparsers.add_parser(
        "pre-push", help="consume Git pre-push records on stdin"
    )
    pre_push_parser.add_argument("remote_name", nargs="?", help="Git's destination name")
    pre_push_parser.add_argument(
        "remote_location", nargs="?", help="Git's destination URL or path"
    )
    pre_push_parser.add_argument(
        "--trusted-base", help="commit whose exact path and blob entries are already accepted"
    )
    pre_push_parser.add_argument(
        "--pull-request-event",
        type=Path,
        help="GitHub pull_request event: scan this pull request by its provenance",
    )
    pre_push_parser.add_argument("--advisory-report", type=Path, help=_ADVISORY_REPORT_HELP)
    pull_request_parser = subparsers.add_parser(
        "pull-request", help="inspect GitHub pull-request metadata"
    )
    pull_request_parser.add_argument("--event", required=True, type=Path)
    pull_request_parser.add_argument("--advisory-report", type=Path, help=_ADVISORY_REPORT_HELP)
    post_parser = subparsers.add_parser(
        "post-advisory", help="comment and label an outside PR from an advisory report"
    )
    post_parser.add_argument("--event", required=True, type=Path)
    post_parser.add_argument("--report", required=True, type=Path)
    pr_text_parser = subparsers.add_parser(
        "pr-text", help="inspect proposed PR text before publishing it"
    )
    pr_text_parser.add_argument("--file", action="append", default=[], type=Path)
    pr_text_parser.add_argument("--stdin", action="store_true")
    _add_pr_publish_parser(subparsers)
    subparsers.add_parser("seal-config", help="encrypt the private TOML into the tracked file")
    subparsers.add_parser("key-path", help="show the local key path for secure backup")
    subparsers.add_parser(
        "restore-draft", help="restore a private edit draft from the encrypted file"
    )
    _add_history_parsers(subparsers)
    return parser


def _pr_command_main(parsed: argparse.Namespace) -> int:
    if parsed.command == "pull-request":
        return pull_request_main(parsed.repo, parsed.event, parsed.advisory_report)
    if parsed.command == "post-advisory":
        return post_advisory_main(parsed.event, parsed.report)
    if parsed.command == "pr-text":
        return pr_text_main(parsed.repo, parsed.file, read_stdin=parsed.stdin)
    return publish_pr_main(parsed.repo, parsed)


def _vocabulary_command_main(parsed: argparse.Namespace) -> int:
    if parsed.command == "seal-config":
        return seal_config_main(parsed.repo)
    if parsed.command == "key-path":
        return key_path_main(parsed.repo)
    if parsed.command == "restore-draft":
        return restore_draft_main(parsed.repo)
    if parsed.command == "verify-candidate":
        return verify_candidate_main(
            parsed.repo,
            parsed.rev,
            parsed.base,
            parsed.event,
            parsed.trusted_base,
            parsed.advisory_report,
        )
    return sync_ci_key_main(parsed.repo)


def main(argv: list[str] | None = None) -> int:
    parsed = _parser().parse_args(argv)
    if parsed.command == "commit-msg":
        return commit_message_hook_main(parsed.message_file, parsed.repo)
    if parsed.command == "pre-push":
        return pre_push_hook_main(
            parsed.repo,
            remote_name=parsed.remote_name,
            remote_location=parsed.remote_location,
            trusted_base=parsed.trusted_base,
            pull_request_event=parsed.pull_request_event,
            advisory_report=parsed.advisory_report,
        )
    if parsed.command == "self-test":
        return self_test_main()
    if parsed.command in {"pull-request", "post-advisory", "pr-text", "publish-pr"}:
        return _pr_command_main(parsed)
    if parsed.command in {
        "seal-config",
        "key-path",
        "restore-draft",
        "verify-candidate",
        "sync-ci-key",
    }:
        return _vocabulary_command_main(parsed)
    revisions = parsed.rev or ["HEAD"]
    return audit_main(
        parsed.repo,
        revisions,
        include_worktree=parsed.worktree,
        repository_owner=parsed.repository_owner,
    )


if __name__ == "__main__":
    raise SystemExit(main())
