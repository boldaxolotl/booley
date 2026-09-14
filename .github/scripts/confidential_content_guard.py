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
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

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
        findings.append(Finding("confidential term", f"{safe_location}:{line}", pattern.term_id))
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
    metadata = metadata_raw.decode("ascii", errors="replace").split()
    if len(metadata) != 5 or not metadata[0].startswith(":"):
        raise GuardError("a changed tree record could not be parsed")
    old_mode = metadata[0][1:]
    new_mode, old_oid, new_oid, status = metadata[1:]
    if (
        re.fullmatch(r"[0-7]{6}", old_mode) is None
        or re.fullmatch(r"[0-7]{6}", new_mode) is None
        or _OID_RE.fullmatch(old_oid) is None
        or _OID_RE.fullmatch(new_oid) is None
        or re.fullmatch(r"[ACDMRTUXB]+", status) is None
    ):
        raise GuardError("a changed tree record could not be parsed")
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
        "-m",
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
    remaining = _MAX_FINDINGS - len(target)
    if remaining > 0:
        target.extend(list(additions)[:remaining])


def inspect_commits(repo: Path, commits: Iterable[str], config: GuardConfig) -> list[Finding]:
    """Inspect every commit fact and changed tree entry, de-duplicating blobs."""
    findings: list[Finding] = []
    blob_context: dict[str, tuple[str, str]] = {}
    for sha in dict.fromkeys(commits):
        author_name, author_email, committer_name, committer_email, message = _commit_facts(
            repo, sha
        )
        _add_limited(findings, _scan_text(message, f"commit {sha[:12]} message", config))
        for role, name, email in (
            ("author", author_name, author_email),
            ("committer", committer_name, committer_email),
        ):
            _add_limited(
                findings, _identity_findings(name, email, role, f"commit {sha[:12]}", config)
            )
        for entry in _changed_tree_entries(repo, sha):
            path = entry.path
            if not _path_ignored(path, config):
                _add_limited(
                    findings,
                    _scan_text(path, f"commit {sha[:12]} {path} path", config),
                )
                if entry.object_id is not None:
                    blob_context.setdefault(entry.object_id, (sha, path))
    for object_id, data in _iter_blob_data(repo, list(blob_context)):
        sha, path = blob_context[object_id]
        _add_limited(findings, _scan_blob(data, path, f"commit {sha[:12]} {path}", config))
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
    print("ERROR: confidential-content leak guard blocked this operation.", file=stream)
    for finding in findings:
        location = _redact_location(finding.location, config)
        suffix = f" (term id {finding.term_id})" if finding.term_id else ""
        print(f"  - {finding.kind}: {location}{suffix}", file=stream)
    if len(findings) >= _MAX_FINDINGS:
        print(f"  - output limited to {_MAX_FINDINGS} findings", file=stream)


def _guard_failure(exc: GuardError, stream: TextIO) -> int:
    print("ERROR: confidential-content leak guard could not complete.", file=stream)
    print(f"  {exc}", file=stream)
    print("  The operation is refused because an incomplete scan cannot be safe.", file=stream)
    return 1


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
) -> int:
    root = Path(repo or Path.cwd()).resolve()
    try:
        if (remote_name is None) != (remote_location is None):
            raise GuardError("the pre-push destination is incomplete")
        config = load_config(root)
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
        findings = inspect_commits(root, commits, config)
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
        return 1
    return 0


def pull_request_main(repo: Path | str, event_path: Path) -> int:
    """Inspect PR title, body, and head ref without executing pull-request code."""
    root = Path(repo).resolve()
    try:
        config = load_config(root)
        event = json.loads(event_path.read_text(encoding="utf-8"))
        findings = inspect_pull_request_event(event, config)
    except (GuardError, OSError, json.JSONDecodeError) as exc:
        error = exc if isinstance(exc, GuardError) else GuardError("the GitHub event is invalid")
        return _guard_failure(error, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
        return 1
    print("Confidential-content pull-request metadata guard: clean.")
    return 0


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
        env = os.environ.copy()
        env["GH_PROMPT_DISABLED"] = "1"
        env.pop("GH_REPO", None)
        result = subprocess.run(
            _publish_pr_command(args, title, body),
            input=body.encode("utf-8") if body is not None else b"",
            cwd=root,
            env=env,
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
    return 0


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


def verify_candidate_main(repo: Path | str, revision: str, base: str, event_path: Path) -> int:
    """Validate and scan a proposed vocabulary using the trusted current key."""
    root = Path(repo).resolve()
    try:
        if _OID_RE.fullmatch(revision) is None or _OID_RE.fullmatch(base) is None:
            raise GuardError("the candidate or base revision is invalid")
        trusted = _sealed_config_bytes(root)
        identifier = sealed_key_id(trusted)
        key = _vocabulary_key(identifier)
        _parse_config(unseal_vocabulary(trusted, key), root)
        spec = f"{revision}:{_SEALED_CONFIG_NAME}"
        size = int(_run_git(root, ["cat-file", "-s", spec]).strip())
        if size > 2 * 1024 * 1024:
            raise GuardError("the proposed encrypted vocabulary exceeds the size limit")
        proposed = _run_git(root, ["cat-file", "blob", spec])
        if sealed_key_id(proposed) != identifier:
            raise GuardError("the proposed encrypted vocabulary uses a different key")
        candidate_config = _parse_config(unseal_vocabulary(proposed, key), root)
        commits = outgoing_commits(root, revision, base)
        findings = inspect_commits(root, commits, candidate_config)
        event = json.loads(event_path.read_text(encoding="utf-8"))
        _add_limited(findings, inspect_pull_request_event(event, candidate_config))
    except (ValueError, OSError, json.JSONDecodeError, SealedVocabularyError, GuardError) as exc:
        error = (
            exc
            if isinstance(exc, GuardError)
            else GuardError("the proposed vocabulary is invalid")
        )
        return _guard_failure(error, sys.stderr)
    if findings:
        _print_findings(findings, candidate_config, sys.stderr)
        return 1
    print("Proposed encrypted vocabulary: valid.")
    return 0


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


def audit_main(repo: Path | str, revisions: list[str], *, include_worktree: bool = False) -> int:
    root = Path(repo).resolve()
    try:
        config = load_config(root)
        commits: list[str] = []
        for revision in revisions:
            commits.extend(_rev_list(root, [revision]))
        findings = inspect_commits(root, commits, config)
        if include_worktree:
            _add_limited(findings, inspect_worktree(root, config))
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
        return 1
    print("Confidential-content leak guard: clean.")
    return 0


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
    pull_request_parser = subparsers.add_parser(
        "pull-request", help="inspect GitHub pull-request metadata"
    )
    pull_request_parser.add_argument("--event", required=True, type=Path)
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
    verify_parser = subparsers.add_parser(
        "verify-candidate", help="validate proposed encrypted vocabulary"
    )
    verify_parser.add_argument("--rev", required=True)
    verify_parser.add_argument("--base", required=True)
    verify_parser.add_argument("--event", required=True, type=Path)
    subparsers.add_parser("sync-ci-key", help="publish the decryption key to Actions")
    audit_parser = subparsers.add_parser("audit", help="inspect full ancestry of revisions")
    audit_parser.add_argument("--rev", action="append", default=[], help="revision to inspect")
    audit_parser.add_argument(
        "--worktree", action="store_true", help="also inspect tracked and untracked checkout files"
    )
    return parser


def _pr_command_main(parsed: argparse.Namespace) -> int:
    if parsed.command == "pull-request":
        return pull_request_main(parsed.repo, parsed.event)
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
        return verify_candidate_main(parsed.repo, parsed.rev, parsed.base, parsed.event)
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
        )
    if parsed.command in {"pull-request", "pr-text", "publish-pr"}:
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
    return audit_main(parsed.repo, revisions, include_worktree=parsed.worktree)


if __name__ == "__main__":
    raise SystemExit(main())
