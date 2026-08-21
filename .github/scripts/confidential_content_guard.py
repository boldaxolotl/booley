#!/usr/bin/env python3
"""Fail-closed confidential-content guard for Git hooks and CI.

The vocabulary is deliberately not stored in this repository.  Local hooks
load TOML from ``.git/booley-leak-guard.toml`` (or the path named by the
repo-local ``booley.leakGuardConfig`` setting).  CI receives the same TOML as
base64 in ``BOOLEY_LEAK_GUARD_CONFIG_B64``.  Diagnostics identify matched
terms by a one-way digest and never echo the confidential text.

This file is stdlib-only because CI executes the trusted default-branch copy
against an untrusted pull-request checkout without importing candidate code.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import threading
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

_CONFIG_B64_ENV = "BOOLEY_LEAK_GUARD_CONFIG_B64"
_CONFIG_PATH_ENV = "BOOLEY_LEAK_GUARD_CONFIG"
_ALLOWED_AUTHORS_ENV = "BOOLEY_LEAK_GUARD_ALLOWED_AUTHORS"
_LOCAL_CONFIG_NAME = "booley-leak-guard.toml"
_MAX_BLOB_BYTES = 256 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_FINDINGS = 100
_BINARY_RUN_RE = re.compile(rb"[\t\x20-\x7e]{6,}")
_IDENT_RE = re.compile(r"^(?P<name>.*) <(?P<email>[^<>]*)> \d+ [+-]\d{4}$")


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
    term_lookup: dict[str, TermPattern]
    allowed_authors: tuple[str, ...]
    ignored_paths: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    term_id: str | None = None


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
    try:
        raw = _run_git(repo, ["rev-parse", "--git-common-dir"]).decode().strip()
    except (GuardError, UnicodeDecodeError):
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = repo / common
    candidate = common.resolve() / _LOCAL_CONFIG_NAME
    return candidate if candidate.is_file() else None


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


def _config_bytes(repo: Path) -> bytes:
    encoded = os.environ.get(_CONFIG_B64_ENV, "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GuardError("the CI confidential configuration is not valid base64") from exc
    path = _configured_path(repo)
    if path is None:
        raise GuardError("the confidential vocabulary is required but not configured")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GuardError("the configured confidential vocabulary cannot be read") from exc


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


def _combined_matcher(patterns: tuple[TermPattern, ...]) -> re.Pattern[str]:
    alternatives = (re.escape(pattern.literal) for pattern in patterns)
    return re.compile("|".join(alternatives), re.IGNORECASE)


def load_config(repo: Path | str | None = None) -> GuardConfig:
    """Load and validate the explicit confidential guard configuration."""
    root = Path(repo or Path.cwd()).resolve()
    try:
        raw_config = _config_bytes(root)
        document = tomllib.loads(raw_config.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GuardError("the confidential configuration is not valid UTF-8 TOML") from exc
    terms, authors, ignored = _config_lists(document)
    authors.extend(_extra_authors(root))
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
        {pattern.literal.casefold(): pattern for pattern in patterns},
        tuple(authors),
        tuple(dict.fromkeys(ignored)),
    )


def _path_ignored(path: str, config: GuardConfig) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in config.ignored_paths)


def _redact_location(location: str, config: GuardConfig) -> str:
    redacted = location
    for pattern in config.patterns:
        redacted = pattern.regex.sub("[redacted]", redacted)
    return redacted


def _scan_text(text: str, location: str, config: GuardConfig) -> list[Finding]:
    findings: list[Finding] = []
    safe_location = _redact_location(location, config)
    found_ids: set[str] = set()
    for match in config.matcher.finditer(text):
        pattern = config.term_lookup.get(match.group().casefold())
        if pattern is None or not _valid_boundaries(text, match, pattern):
            continue
        if pattern.term_id in found_ids:
            continue
        found_ids.add(pattern.term_id)
        line = text.count("\n", 0, match.start()) + 1
        findings.append(Finding("confidential term", f"{safe_location}:{line}", pattern.term_id))
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings


def _valid_boundaries(text: str, match: re.Match[str], pattern: TermPattern) -> bool:
    left_ok = match.start() == 0 or not _ascii_alphanumeric(text[match.start() - 1])
    if not left_ok:
        return False
    if pattern.literal.endswith("__"):
        return match.end() < len(text) and (
            text[match.end()].isalnum() or text[match.end()] == "_"
        )
    return match.end() == len(text) or not _ascii_alphanumeric(text[match.end()])


def _ascii_alphanumeric(character: str) -> bool:
    return character.isascii() and character.isalnum()


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


def _commit_facts(repo: Path, sha: str) -> tuple[str, str, str, str, str]:
    fmt = "%an%x00%ae%x00%cn%x00%ce%x00%B"
    raw = _run_git(repo, ["show", "-s", f"--format={fmt}", sha])
    fields = raw.decode("utf-8", errors="replace").split("\x00", 4)
    if len(fields) != 5:
        raise GuardError("a commit identity record could not be parsed")
    return fields[0], fields[1], fields[2], fields[3], fields[4]


def _tree_blobs(repo: Path, sha: str) -> list[tuple[str, str]]:
    raw = _run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", sha])
    blobs: list[tuple[str, str]] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            _mode, kind, raw_oid = metadata.split(b" ", 2)
        except ValueError as exc:
            raise GuardError("a commit tree record could not be parsed") from exc
        if kind == b"blob":
            blobs.append((raw_oid.decode("ascii"), raw_path.decode(errors="surrogateescape")))
    return blobs


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
    """Inspect every commit fact and every complete tree, de-duplicating blobs."""
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
        for object_id, path in _tree_blobs(repo, sha):
            if not _path_ignored(path, config):
                _add_limited(
                    findings,
                    _scan_text(path, f"commit {sha[:12]} {path} path", config),
                )
                blob_context.setdefault(object_id, (sha, path))
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
    if any(not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) for sha in commits):
        raise GuardError("git rev-list returned an invalid object name")
    return commits


def outgoing_commits(repo: Path, local_sha: str, remote_sha: str) -> list[str]:
    """Return all commits exposed by one update, including full new-ref history."""
    if _is_zero_sha(remote_sha):
        return _rev_list(repo, [local_sha])
    return _rev_list(repo, [local_sha, "--not", remote_sha])


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


def pre_push_hook_main(repo: Path | str | None = None, stdin: TextIO = sys.stdin) -> int:
    root = Path(repo or Path.cwd()).resolve()
    try:
        config = load_config(root)
        commits: list[str] = []
        for line in stdin:
            parts = line.split()
            if len(parts) != 4:
                raise GuardError("the pre-push input record is malformed")
            _local_ref, local_sha, _remote_ref, remote_sha = parts
            if _is_zero_sha(local_sha):
                continue
            commits.extend(outgoing_commits(root, local_sha, remote_sha))
        findings = inspect_commits(root, commits, config)
    except GuardError as exc:
        return _guard_failure(exc, sys.stderr)
    if findings:
        _print_findings(findings, config, sys.stderr)
        return 1
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository to inspect")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commit_parser = subparsers.add_parser("commit-msg", help="inspect a pending commit")
    commit_parser.add_argument("message_file", type=Path)
    subparsers.add_parser("pre-push", help="consume Git pre-push records on stdin")
    audit_parser = subparsers.add_parser("audit", help="inspect full ancestry of revisions")
    audit_parser.add_argument("--rev", action="append", default=[], help="revision to inspect")
    audit_parser.add_argument(
        "--worktree", action="store_true", help="also inspect tracked and untracked checkout files"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parsed = _parser().parse_args(argv)
    if parsed.command == "commit-msg":
        return commit_message_hook_main(parsed.message_file, parsed.repo)
    if parsed.command == "pre-push":
        return pre_push_hook_main(parsed.repo)
    revisions = parsed.rev or ["HEAD"]
    return audit_main(parsed.repo, revisions, include_worktree=parsed.worktree)


if __name__ == "__main__":
    raise SystemExit(main())
