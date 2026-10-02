#!/usr/bin/env python3
"""Git pre-push hook: block pushes whose outgoing commits leak or misattribute.

The commit-msg sanitizer covers ``git commit`` and ``git merge`` — but git
runs no message hook at all for ``git revert``/``git cherry-pick``, and
``--no-verify`` disables commit-msg entirely (F-17). This hook is the safety
net at the last interception point git offers: it scans every newly exposed commit
outside actual destination history and refuses the push on any of these offenses:

1. **Banned phrases** in the message, either identity, a changed tracked path,
   or a changed symlink target.
2. **An identity outside the allowlist**, when ``[stealth] allowed_authors``
   is configured. commit-msg cannot do this check: git hands that hook only
   the message file, so ``git commit --author='someone <else@local>'`` sails
   straight past it. Push time is the first place both identities are
   readable, and it is also the only place that catches a fabricated author
   arriving via rebase, cherry-pick, or ``--no-verify``.
3. **A repository-visible path into the project-state directory**, including
   a symlink whose committed target resolves there.

Packaged into the Project's ``.booley_project/.managed/project-git-hooks.pyz``
bundle by ``booley init``. Its flat imports resolve from the zip root, so Git
operations do not need an installed Booley package. Escape hatch:
``BOOLEY_SKIP_PUSH_GUARD=1`` explicitly skips all checks for one push.
History already advertised by the actual destination is excluded automatically.

Exit 0 = allow push; exit 1 = reject with diagnostic naming the commits.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Ensure the bundle root is on sys.path so flat imports resolve when called
# from the standalone zip application.
_HOOK_DIR = str(Path(__file__).resolve().parent)
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)

from commit_msg_utils import (
    StealthPolicy,
    allowed_authors,
    identity_allowed,
    source_checkout_policy_owner,
    stealth_enabled,
    stealth_policy,
)

try:
    from booley.commit_policy.policy import validate_push_configuration
except ImportError:
    from booley_commit_policy import validate_push_configuration

_MAX_SYMLINK_TARGET_BYTES = 64 * 1024
_SYMLINK_MODE = "120000"
# Kept local because this module is packaged into Projects and must run without
# an installed Booley package.
_PROJECT_DIR_NAME = ".booley_project"


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_id: str
    path: str


class InspectionError(ValueError):
    """A push cannot be completely inspected or its authority proved."""


@dataclass(frozen=True)
class _Update:
    local: str
    old: str


def _run_git(args, *, cwd=None, env=None, data=None, timeout=60):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InspectionError(
            "Git inspection timed out; repair slow Git/transport or reduce the selected "
            "range with complete inspected history and retry"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise InspectionError(
            "Git inspection unavailable; repair repository access and retry"
        ) from exc
    return result


def _required_git(args, **kwargs):
    result = _run_git(args, **kwargs)
    if result.returncode:
        raise InspectionError("Git inspection failed; fetch complete history and retry")
    return result.stdout


def _probe_environment(*, external=False):
    env = dict(os.environ)
    if external:
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_INDEX_FILE",
            "GIT_PREFIX",
        ):
            env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", LC_ALL="C")
    return env


def _config(cwd, env):
    raw = _required_git(["config", "--null", "--list"], cwd=cwd, env=env)
    values = {}
    for field in raw.split(b"\0"):
        if not field:
            continue
        key, separator, value = field.partition(b"\n")
        if not separator:
            value = b""
        values[key.decode("utf-8", "surrogateescape").lower()] = value.decode(
            "utf-8", "surrogateescape"
        )
    return values, raw


def _structural_node_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _resolve_repository_path(path: Path, *, strict: bool = False) -> Path:
    try:
        return path.resolve(strict=strict)
    except RuntimeError as exc:
        # Python 3.11/3.12 report this filesystem error as RuntimeError, not OSError.
        if not str(exc).startswith("Symlink loop from "):
            raise
        raise InspectionError("cannot resolve repository symlink loop; repair the path") from exc


def _repository_state(cwd, env, *, complete):
    config, raw = _config(cwd, env)
    common = Path(
        _required_git(["rev-parse", "--git-common-dir"], cwd=cwd, env=env).decode().strip()
    )
    objects = Path(
        _required_git(["rev-parse", "--git-path", "objects"], cwd=cwd, env=env).decode().strip()
    )
    common = _resolve_repository_path(
        cwd / common if not common.is_absolute() else common, strict=True
    )
    objects = _resolve_repository_path(
        cwd / objects if not objects.is_absolute() else objects, strict=True
    )
    if not common.is_dir() or not objects.is_dir():
        raise InspectionError("cannot resolve readable Git object storage")
    version = config.get("core.repositoryformatversion", "0")
    if version not in ("0", "1"):
        raise InspectionError("unsupported repository format")
    pack = objects / "pack"
    markers = (
        tuple(sorted(item for item in pack.iterdir() if item.name.endswith(".promisor")))
        if _structural_node_present(pack)
        else ()
    )
    promisor = any(
        key == "extensions.partialclone"
        or (key.startswith("remote.") and key.endswith((".promisor", ".partialclonefilter")))
        for key in config
    )
    if complete and (promisor or markers):
        raise InspectionError(
            "materialize a complete nonpromisor clone, or use Git supporting no-lazy-fetch inspection"
        )
    grafts = Path(env.get("GIT_GRAFT_FILE", str(common / "info" / "grafts")))
    graft_data = grafts.read_bytes() if _structural_node_present(grafts) else b""
    if graft_data.strip():
        raise InspectionError("remove active Git grafts before inspection")
    stat = tuple((str(item), item.stat().st_mtime_ns, item.stat().st_size) for item in markers)
    return common, objects, (raw, version, stat, graft_data)


def _file_authority_state(path, env):
    git_dir = _resolve_repository_path(
        Path(
            _required_git(["rev-parse", "--absolute-git-dir"], cwd=path, env=env).decode().strip()
        ),
        strict=True,
    )
    if path != git_dir:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=path, env=env)
        if result.returncode:
            raise InspectionError(
                "file authority must name its exact repository root or Git directory"
            )
        top = _resolve_repository_path(
            Path(result.stdout.decode().strip()),
            strict=True,
        )
        if path != top:
            raise InspectionError(
                "file authority must name its exact repository root or Git directory"
            )
    return _repository_state(path, env, complete=True)


class _Protocol:
    """Non-object-resolving validation of Git's complete hook input."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = _probe_environment()
        format_name = (
            _required_git(["rev-parse", "--show-object-format"], cwd=root, env=self.env)
            .decode()
            .strip()
        )
        if format_name not in ("sha1", "sha256"):
            raise InspectionError("unsupported Git object format")
        self.oid_length = 40 if format_name == "sha1" else 64

    def oid(self, value: str) -> bool:
        return re.fullmatch(rf"[0-9a-fA-F]{{{self.oid_length}}}", value) is not None


class _Inspection(_Protocol):
    """Bounded, graph-independent, nonfetching object reads for one push."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        probe = _run_git(["--no-lazy-fetch", "--version"], cwd=root, env=self.env, timeout=10)
        unsupported = (
            probe.returncode == 129 and b"unknown option: --no-lazy-fetch" in probe.stderr
        )
        if probe.returncode and not unsupported:
            raise InspectionError("cannot determine safe Git object inspection capability")
        self.modern = probe.returncode == 0
        self.state = _repository_state(root, self.env, complete=not self.modern)
        self.env.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
        self.prefix = (["--no-lazy-fetch"] if self.modern else []) + [
            "-c",
            "core.commitGraph=false",
        ]

    def stable(self) -> None:
        current = _repository_state(self.root, _probe_environment(), complete=not self.modern)
        if current != self.state:
            raise InspectionError("repository configuration changed during inspection; retry")

    def git(self, args: list[str], *, data: bytes | None = None) -> bytes:
        return _required_git([*self.prefix, *args], cwd=self.root, env=self.env, data=data)

    def inventory(self, tips: list[str], exclusions=()) -> list[str]:
        if not tips:
            return []
        revisions = [*tips, *("^" + oid for oid in exclusions)]
        raw = (
            self.git(["rev-list", "--stdin"], data=("\n".join(revisions) + "\n").encode("ascii"))
            .decode("ascii")
            .splitlines()
        )
        if any(not self.oid(value) for value in raw) or len(raw) != len(set(raw)):
            raise InspectionError("malformed outgoing commit inventory")
        return raw

    def objects(self, ids) -> dict[str, tuple[str, bytes] | None]:
        self.stable()
        requested = list(dict.fromkeys(ids))
        if not requested:
            return {}
        raw = self.git(["cat-file", "--batch"], data=("\n".join(requested) + "\n").encode())
        offset = 0
        result = {}
        for oid in requested:
            end = raw.find(b"\n", offset)
            if end < 0:
                raise InspectionError("truncated Git object batch")
            header = raw[offset:end].decode("ascii").split()
            offset = end + 1
            if header == [oid, "missing"]:
                result[oid] = None
                continue
            if len(header) != 3 or header[0] != oid or not header[2].isdigit():
                raise InspectionError("malformed Git object batch")
            size = int(header[2])
            if offset + size >= len(raw) or raw[offset + size] != 10:
                raise InspectionError("truncated Git object contents")
            result[oid] = (header[1], raw[offset : offset + size])
            offset += size + 1
        if offset != len(raw):
            raise InspectionError("unexpected Git object batch trailer")
        return result

    def commits(self, ids) -> list[str]:
        pending = {oid: oid for oid in ids}
        seen = {oid: set() for oid in pending}
        resolved = []
        while pending:
            objects = self.objects(pending.values())
            following = {}
            for requested, oid in pending.items():
                value = objects[oid]
                if value is None:
                    continue
                if value[0] == "commit":
                    resolved.append(oid)
                elif value[0] == "tag":
                    if oid in seen[requested]:
                        raise InspectionError("cyclic annotated tag")
                    seen[requested].add(oid)
                    first = value[1].split(b"\n", 1)[0]
                    if not first.startswith(b"object "):
                        raise InspectionError("malformed annotated tag")
                    target = first[7:].decode("ascii")
                    if not self.oid(target):
                        raise InspectionError("invalid annotated tag object")
                    following[requested] = target
            pending = following
        return list(dict.fromkeys(resolved))


def _valid_ref_name(ref: str) -> bool:
    if not ref.startswith("refs/") or ref.endswith(("/", ".")):
        return False
    if (
        ".." in ref
        or "@{" in ref
        or any(ord(char) < 33 or ord(char) == 127 or char in "~^:?*[\\" for char in ref)
    ):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in ref.split("/")
    )


def _updates(text, inspection):
    updates = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise InspectionError("malformed pre-push protocol")
        local_ref, local, remote_ref, old = fields
        if not inspection.oid(local) or not inspection.oid(old):
            raise InspectionError("invalid pre-push object ID")
        if not _valid_ref_name(remote_ref):
            raise InspectionError("invalid pre-push ref name")
        if local_ref == "(delete)":
            if set(local) != {"0"}:
                raise InspectionError("invalid pre-push deletion")
        elif local_ref.startswith("refs/") and not _valid_ref_name(local_ref):
            raise InspectionError("invalid pre-push ref name")
        # Git preserves typed revision labels; only validated protocol OIDs reach queries.
        if set(local) != {"0"}:
            updates.append(_Update(local.lower(), old.lower()))
    return updates


def _location(location, root):
    if any(char.isspace() or ord(char) < 32 for char in location):
        raise InspectionError("invalid repository location")
    if location.startswith("~") and ":" not in location:
        raise InspectionError("home-expanded authority requires an absolute repository path")
    parsed = urlsplit(location)
    if parsed.scheme == "file":
        if not location.startswith("file://"):
            raise InspectionError("file authority requires a literal file:// URL")
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise InspectionError("unsupported file authority")
        try:
            decoded = unquote(parsed.path, errors="strict")
        except UnicodeDecodeError as exc:
            raise InspectionError("file authority requires valid UTF-8 URL encoding") from exc
        if "\0" in decoded:
            raise InspectionError("invalid file authority path")
        return "file", _resolve_repository_path(Path(decoded))
    if Path(location).is_absolute() or (not parsed.scheme and ":" not in location):
        return "file", _resolve_repository_path(root / location)
    if parsed.scheme in ("https", "ssh"):
        if not parsed.hostname or parsed.fragment:
            raise InspectionError("invalid authenticated repository URL")
        return parsed.scheme, None
    if (
        "://" not in location
        and "::" not in location
        and re.fullmatch(r"(?:[^\s/@:]+@)?[^\s/:]+:[^\s]+", location)
    ):
        return "ssh", None
    raise InspectionError("repository transport cannot establish authority")


def _transport_environment(root, location):
    env = _probe_environment()
    config, raw_config = _config(root, env)
    protocol, path = _location(location, root)
    allowed = []
    for name in ("file", "ssh", "https"):
        policy = config.get(
            f"protocol.{name}.allow",
            config.get("protocol.allow", "user" if name == "file" else "always"),
        )
        if policy not in ("always", "never", "user"):
            raise InspectionError("invalid Git transport policy")
        if policy == "always" or (
            policy == "user" and env.get("GIT_PROTOCOL_FROM_USER", "1") == "1"
        ):
            allowed.append(name)
    if "GIT_ALLOW_PROTOCOL" in env:
        allowed = [name for name in allowed if name in env["GIT_ALLOW_PROTOCOL"].split(":")]
    if protocol not in allowed:
        raise InspectionError("repository transport denied by effective Git policy")
    for field in raw_config.split(b"\0"):
        key, _, replacement = field.partition(b"\n")
        key = key.decode("utf-8", "surrogateescape").lower()
        replacement = replacement.decode("utf-8", "surrogateescape")
        if (
            key.startswith("url.")
            and key.endswith(".insteadof")
            and location.startswith(replacement)
        ):
            raise InspectionError("configure the canonical unrewritten repository location")
    env["GIT_ALLOW_PROTOCOL"] = ":".join(allowed)
    if protocol == "ssh":
        _secure_ssh(config, env)
    if protocol == "https":
        _secure_https(root, location, env)
    return env, path


def _secure_ssh(config, env):
    command = env.get("GIT_SSH_COMMAND") or config.get("core.sshcommand") or env.get("GIT_SSH")
    if command:
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise InspectionError("invalid owner SSH command") from exc
        compact = " ".join(tokens).lower()
        compact = re.sub(
            r"(stricthostkeychecking|userknownhostsfile)\s*(?:=|\s)\s*", r"\1=", compact
        )
        compact = compact.replace(" ", "")
        insecure = (
            "stricthostkeychecking=no",
            "stricthostkeychecking=off",
            "stricthostkeychecking=accept-new",
            "userknownhostsfile=/dev/null",
            "userknownhostsfile=nul",
        )
        if any(option in compact for option in insecure):
            raise InspectionError("SSH authority requires host verification")
    elif env.get("GIT_SSH_VARIANT", config.get("ssh.variant", "ssh")) in ("ssh", "auto"):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"


def _secure_https(root, location, env):
    if "GIT_SSL_NO_VERIFY" in env:
        raise InspectionError("HTTPS authority requires certificate verification")
    for key in ("http.sslVerify", "http.followRedirects"):
        result = _run_git(
            [
                "-c",
                "http.followRedirects=false",
                "config",
                *(["--bool"] if key.endswith("sslVerify") else []),
                "--get-urlmatch",
                key,
                location,
            ],
            cwd=root,
            env=env,
        )
        if result.returncode not in (0, 1):
            raise InspectionError("cannot determine HTTPS authority policy")
        value = result.stdout.decode().strip().lower()
        if (key.endswith("sslVerify") and value in ("false", "0", "no", "off")) or (
            key.endswith("followRedirects") and value not in ("", "false", "0", "no", "off")
        ):
            raise InspectionError(
                "HTTPS authority requires certificate verification and disabled redirects"
            )


def _advertised(inspection, location):
    env, path = _transport_environment(inspection.root, location)
    if path is not None:
        external = _probe_environment(external=True)
        authority = _file_authority_state(path, external)
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            env.pop(key, None)
    result = _run_git(
        ["-c", "http.followRedirects=false", "ls-remote", "--refs", "--", location],
        cwd=inspection.root,
        env=env,
        timeout=10,
    )
    if result.returncode:
        raise InspectionError(
            "advertisement unavailable; repair credentials/network or fetch and retry"
        )
    ids = []
    for line in result.stdout.splitlines():
        fields = line.decode("utf-8", "strict").split("\t")
        if len(fields) != 2 or not inspection.oid(fields[0]) or not fields[1].startswith("refs/"):
            raise InspectionError("malformed repository advertisement")
        if not _valid_ref_name(fields[1]):
            raise InspectionError("invalid advertised ref")
        ids.append(fields[0].lower())
    if (
        path is not None
        and _file_authority_state(path, _probe_environment(external=True)) != authority
    ):
        raise InspectionError("file authority changed during advertisement")
    return ids


def _outgoing_range(inspection, updates, destination):
    tips = inspection.commits([update.local for update in updates])
    # Different tags may peel to the same commit; validate each local update.
    if len(tips) != len({update.local for update in updates}) and any(
        not inspection.commits([update.local]) for update in updates
    ):
        raise InspectionError(
            "missing or noncommit local update cannot be inspected; fetch missing pushed "
            "history or select a commit update and retry"
        )
    old = inspection.commits([update.old for update in updates if set(update.old) != {"0"}])
    excluded, unavailable = _destination_exclusions(inspection, destination, old)
    candidate = inspection.inventory(tips, excluded)
    shallow = Path(inspection.git(["rev-parse", "--git-path", "shallow"]).decode().strip())
    shallow = inspection.root / shallow if not shallow.is_absolute() else shallow
    if shallow.exists() and set(shallow.read_text().split()).intersection(candidate):
        raise InspectionError(
            "fetch complete pushed history from its source with --unshallow and retry"
        )
    return candidate, unavailable


def _parse_identity(raw):
    match = re.fullmatch(rb"(.*) <([^<>]*)> -?[0-9]+ [+-][0-9]{4}", raw)
    if match is None:
        raise InspectionError("malformed commit identity")
    return tuple(value.decode("utf-8", "replace") for value in match.groups())


def _parse_commit(value):
    if value is None or value[0] != "commit":
        raise InspectionError("missing inspected commit object")
    headers, separator, message = value[1].partition(b"\n\n")
    if not separator:
        raise InspectionError("malformed commit object")
    author = [line[7:] for line in headers.splitlines() if line.startswith(b"author ")]
    committer = [line[10:] for line in headers.splitlines() if line.startswith(b"committer ")]
    if len(author) != 1 or len(committer) != 1:
        raise InspectionError("missing commit identities")
    return (
        *_parse_identity(author[0]),
        *_parse_identity(committer[0]),
        message.decode("utf-8", "replace"),
    )


def _batch_trees(inspection, commits):
    if not commits:
        return {}
    raw = inspection.git(
        ["diff-tree", "--stdin", "--always", "--root", "-m", "-r", "--no-renames", "--raw", "-z"],
        data=("\n".join(commits) + "\n").encode(),
    )
    fields = raw.split(b"\0")
    if fields[-1] != b"":
        raise InspectionError("truncated changed-tree batch")
    result = {sha: [] for sha in commits}
    framed = set()
    current = None
    offset = 0
    while offset < len(fields) - 1:
        field = fields[offset]
        offset += 1
        if not field.startswith(b":"):
            current = field.decode("ascii")
            if current not in result:
                raise InspectionError("unexpected changed-tree commit frame")
            framed.add(current)
            continue
        if current is None or offset >= len(fields) - 1:
            raise InspectionError("missing changed-tree frame or path")
        metadata = field.decode("ascii").split()
        if (
            len(metadata) != 5
            or not inspection.oid(metadata[2])
            or not inspection.oid(metadata[3])
        ):
            raise InspectionError("malformed changed-tree entry")
        path = fields[offset].decode("utf-8", "replace")
        offset += 1
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise InspectionError("invalid committed path")
        if metadata[1] != "000000":
            result[current].append(_TreeEntry(metadata[1], metadata[3], path))
    if framed != set(commits):
        raise InspectionError("missing changed-tree commit frames")
    return result


def _check_symlink_sizes(inspection, ids):
    if not ids:
        return
    requested = sorted(ids)
    raw = inspection.git(
        ["cat-file", "--batch-check"], data=("\n".join(requested) + "\n").encode()
    )
    lines = raw.decode("ascii").splitlines()
    if len(lines) != len(requested):
        raise InspectionError("truncated symlink size batch")
    for oid, line in zip(requested, lines, strict=True):
        fields = line.split()
        if len(fields) != 3 or fields[:2] != [oid, "blob"] or not fields[2].isdigit():
            raise InspectionError("cannot inspect symlink object size")
        if int(fields[2]) > _MAX_SYMLINK_TARGET_BYTES:
            raise InspectionError("cannot safely inspect oversized symlink target")


def _inspect_commits(inspection, commits, allowlist, project_dir, policy):
    facts = inspection.objects(commits)
    trees = _batch_trees(inspection, commits)
    symlinks = {
        entry.object_id
        for entries in trees.values()
        for entry in entries
        if entry.mode == _SYMLINK_MODE
    }
    _check_symlink_sizes(inspection, symlinks)
    blobs = inspection.objects(symlinks)
    targets = {}
    for oid, value in blobs.items():
        if value is None or value[0] != "blob" or len(value[1]) > _MAX_SYMLINK_TARGET_BYTES:
            raise InspectionError("cannot safely inspect committed symlink target")
        targets[oid] = value[1].decode("utf-8", "replace")
    offenders = []
    for sha in commits:
        offenses = _fact_offenses(_parse_commit(facts[sha]), allowlist, policy)
        offenses.extend(_entry_offenses(trees[sha], inspection.root, project_dir, policy, targets))
        if offenses:
            offenders.append((sha, list(dict.fromkeys(offenses))))
    inspection.stable()
    return offenders


# Identity fields first, one per line, then the message last — the message is
# the only multi-line field, so putting it at the end makes the split
# unambiguous without needing a separator that could occur inside a name.
_COMMIT_FORMAT = "%an%n%ae%n%cn%n%ce%n%B"
_IDENTITY_FIELDS = 4


def _commit_facts(sha: str) -> tuple[str, str, str, str, str] | None:
    """``(author_name, author_email, committer_name, committer_email, message)``.

    None when Git cannot read the commit; callers refuse incomplete inspection.
    """
    result = subprocess.run(
        ["git", "log", "-1", f"--format={_COMMIT_FORMAT}", sha],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.split("\n")
    if len(lines) < _IDENTITY_FIELDS + 1:
        return None
    author_name, author_email, committer_name, committer_email = lines[:_IDENTITY_FIELDS]
    message = "\n".join(lines[_IDENTITY_FIELDS:])
    return author_name, author_email, committer_name, committer_email, message


def _repository_root() -> Path | None:
    """Return the current repository root, or ``None`` when Git cannot."""
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], env=_probe_environment(), timeout=10)
    except InspectionError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(os.fsdecode(result.stdout.strip())).resolve()


def _guard_project_dir(repository_root: Path) -> Path | None:
    """Resolve project state without requiring Booley in the managed bundle."""
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured:
        return Path(configured).resolve()
    hook_file = Path(__file__).resolve()
    if hook_file.parent.name != "hooks":
        # The zip-app launcher derives BOOLEY_PROJECT_DIR before importing this
        # module. Never probe an installed package as a fallback here: the
        # managed bundle is deliberately stdlib-only and isolated from it.
        return None
    try:
        from booley.runtime.project_dir import resolve_checkout_project_dir

        return resolve_checkout_project_dir(repository_root).resolve()
    except (ImportError, FileNotFoundError):
        hook_dir = hook_file.parent
        return hook_dir.parent if hook_dir.name == "hooks" else None


def _changed_tree_entries(sha: str) -> list[_TreeEntry] | None:
    """Changed non-deleted entries in *sha*, read from committed trees."""
    try:
        result = subprocess.run(
            [
                "git",
                "diff-tree",
                "--root",
                "-m",
                "-r",
                "--no-commit-id",
                "--no-renames",
                "--raw",
                "-z",
                sha,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        return None
    entries: list[_TreeEntry] = []
    for offset in range(0, len(fields), 2):
        metadata = fields[offset].decode("ascii", errors="replace").split()
        if len(metadata) != 5 or not metadata[0].startswith(":"):
            return None
        mode, object_id = metadata[1], metadata[3]
        if mode != "000000":
            path = fields[offset + 1].decode("utf-8", errors="replace")
            entries.append(_TreeEntry(mode, object_id, path))
    return entries


def _symlink_target(entry: _TreeEntry) -> tuple[str | None, str | None]:
    """Return a committed symlink target and an inspection error, if any."""
    try:
        size_result = subprocess.run(
            ["git", "cat-file", "-s", entry.object_id],
            capture_output=True,
            text=True,
            encoding="ascii",
            timeout=10,
            check=False,
        )
        size = int(size_result.stdout.strip()) if size_result.returncode == 0 else -1
        if size < 0 or size > _MAX_SYMLINK_TARGET_BYTES:
            return None, f"cannot safely inspect symlink target: {entry.path}"
        blob_result = subprocess.run(
            ["git", "cat-file", "blob", entry.object_id],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, f"cannot inspect symlink target: {entry.path}"
    if blob_result.returncode != 0:
        return None, f"cannot inspect symlink target: {entry.path}"
    return blob_result.stdout.decode("utf-8", errors="replace"), None


def _inside_project_state(path: Path, project_dir: Path | None) -> bool:
    if project_dir is None:
        return False
    # Normalize ``..`` without following the current worktree's symlinks; the
    # committed target above, not mutable checkout state, is authoritative.
    normalized = Path(os.path.normpath(path.absolute()))
    state = Path(os.path.normpath(project_dir.absolute()))
    return normalized == state or state in normalized.parents


def _inside_checkout_or_runtime_project_state(
    path: Path,
    repository_root: Path,
    project_dir: Path | None,
) -> bool:
    """Whether *path* uses either checkout or runtime spelling for state."""
    normalized = Path(os.path.normpath(path.absolute()))
    root = Path(os.path.normpath(repository_root.absolute()))
    try:
        relative = normalized.relative_to(root)
    except ValueError:
        relative = None
    inside_checkout_state = (
        relative is not None
        and bool(relative.parts)
        and relative.parts[0].casefold() == _PROJECT_DIR_NAME.casefold()
    )
    return inside_checkout_state or _inside_project_state(path, project_dir)


def _tree_offenses(
    sha: str,
    repository_root: Path,
    project_dir: Path | None,
    policy: StealthPolicy,
) -> list[str]:
    entries = _changed_tree_entries(sha)
    if entries is None:
        return ["cannot inspect changed tracked paths"]

    return _entry_offenses(entries, repository_root, project_dir, policy)


def _entry_offenses(entries, repository_root, project_dir, policy, targets=None):
    offenses: list[str] = []
    for entry in entries:
        path_leaks = policy.find_banned(entry.path)
        tracked_path = repository_root / entry.path
        if path_leaks:
            offenses.append(
                f"tracked path has banned terms ({', '.join(sorted(set(path_leaks)))}): "
                f"{entry.path}"
            )
        if _inside_checkout_or_runtime_project_state(tracked_path, repository_root, project_dir):
            offenses.append(f"tracked path exposes project state: {entry.path}")
        if entry.mode != _SYMLINK_MODE:
            continue
        target, error = (
            _symlink_target(entry) if targets is None else (targets[entry.object_id], None)
        )
        if error:
            offenses.append(error)
            continue
        assert target is not None
        target_leaks = policy.find_banned(target)
        target_path = tracked_path.parent / target
        if target_leaks:
            offenses.append(
                f"symlink target has banned terms ({', '.join(sorted(set(target_leaks)))}): "
                f"{entry.path} -> {target}"
            )
        if _inside_checkout_or_runtime_project_state(target_path, repository_root, project_dir):
            offenses.append(f"symlink target exposes project state: {entry.path} -> {target}")
    return list(dict.fromkeys(offenses))


def _commit_offenses(
    sha: str,
    allowlist: list[str],
    *,
    repository_root: Path | None = None,
    project_dir: Path | None = None,
    policy: StealthPolicy | None = None,
) -> list[str]:
    """Human-readable reasons this commit must not be pushed (empty = fine)."""
    root = repository_root or _repository_root()
    if source_checkout_policy_owner(root):
        return []

    active_policy = policy or stealth_policy(root)
    if not active_policy.enabled:
        return []

    facts = _commit_facts(sha)
    if facts is None:
        return ["cannot inspect commit facts"]
    offenses = _fact_offenses(facts, allowlist, active_policy)
    if root is None:
        offenses.append("cannot resolve repository root to inspect tracked metadata")
    else:
        state = project_dir if project_dir is not None else _guard_project_dir(root)
        offenses.extend(_tree_offenses(sha, root, state, active_policy))
    return offenses


def _fact_offenses(facts, allowlist, active_policy):
    author_name, author_email, committer_name, committer_email, message = facts

    offenses: list[str] = []
    scanned = f"{message}\n{author_name} <{author_email}>\n{committer_name} <{committer_email}>"
    leaks = active_policy.find_banned(scanned)
    if leaks:
        offenses.append(f"banned terms: {', '.join(sorted(set(leaks)))}")

    # Both identities are checked, not just the author: a fabricated author
    # with the real committer (what `git commit --author=...` produces) and a
    # real author with a fabricated committer are the same problem seen from
    # two ends, and the allowlist is cheap enough to apply to both.
    for role, name, email in (
        ("author", author_name, author_email),
        ("committer", committer_name, committer_email),
    ):
        if not identity_allowed(name, email, allowlist):
            offenses.append(f"{role} not in [stealth] allowed_authors: {name} <{email}>")
    return offenses


def main() -> int:
    if os.environ.get("BOOLEY_SKIP_PUSH_GUARD"):
        print("pre-push: leak guard skipped (BOOLEY_SKIP_PUSH_GUARD set)", file=sys.stderr)
        return 0
    repository_root = _repository_root()
    if repository_root is None:
        print(
            "ERROR: push blocked: leak guard could not resolve the repository root.",
            file=sys.stderr,
        )
        return 1
    if not stealth_enabled(repository_root):
        return 0

    try:
        offenders, unavailable = _inspect_push(repository_root)
    except (InspectionError, OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: push blocked: {exc}", file=sys.stderr)
        return 1

    if not offenders:
        return 0

    return _report_offenders(offenders, unavailable)


def _inspect_push(repository_root):
    # Consume the complete hook protocol before any child can acquire stdin.
    protocol = sys.stdin.read()
    updates = _updates(protocol, _Protocol(repository_root))
    if not updates:
        return [], False
    validate_push_configuration(repository_root)
    if len(sys.argv) != 3 or not sys.argv[1] or not sys.argv[2]:
        raise InspectionError("active push requires destination name and location")
    inspection = _Inspection(repository_root)
    commits, unavailable = _outgoing_range(inspection, updates, sys.argv[2])
    policy = stealth_policy(repository_root)
    offenders = _inspect_commits(
        inspection,
        commits,
        allowed_authors(repository_root),
        _guard_project_dir(repository_root),
        policy,
    )
    return offenders, unavailable


def _report_offenders(offenders, unavailable):
    print("ERROR: push blocked by leak-guard pre-push hook.", file=sys.stderr)
    print(
        "Outgoing commit(s) expose stealth metadata, carry banned terms, or have "
        "an author/committer identity that is not on the allowlist:",
        file=sys.stderr,
    )
    for sha, offenses in offenders:
        print(f"  - {sha[:12]}: {'; '.join(offenses)}", file=sys.stderr)
    print("", file=sys.stderr)
    if unavailable:
        print(
            "Some reported historical commits may already exist on the destination; "
            "repair credentials/network and retry verified discovery.",
            file=sys.stderr,
        )
    print(
        "Fix: rewrite offending commits, or repair destination discovery and retry.",
        file=sys.stderr,
    )
    return 1


def _destination_exclusions(inspection, destination, old):
    try:
        ids = _advertised(inspection, destination)
    except (InspectionError, OSError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, InspectionError) else "authority lookup failed"
        print(
            f"WARNING: destination advertisement unavailable ({reason}); scanning complete conservative history. "
            "Repair credentials/network/canonical transport and retry verified discovery.",
            file=sys.stderr,
        )
        return old, True
    # Object-reading failures are not optional advertisement failures.
    return [*old, *inspection.commits(ids)], False


if __name__ == "__main__":
    sys.exit(main())
