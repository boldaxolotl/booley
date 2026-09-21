"""Read-only, phase-aware readiness checks for Booley source development.

This module intentionally depends only on the Python standard library.  The
launcher can therefore run before Booley or any contributor environment is
installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.runtime.host_probes import ProbeState, probe_docker, probe_github

SCHEMA_VERSION = 1
MIN_PYTHON = (3, 11)
PINNED_RUNNERS = {
    "pytest": "9.1.1",
    "pytest-asyncio": "1.4.0",
    "pytest-xdist": "3.8.0",
    "pytest-timeout": "2.4.0",
    "pytest-cov": "7.1.0",
    "ruff": "0.16.7",
}
_BRANCH_RE = re.compile(r"^codex/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_STATUS_ORDER = {"ready": 0, "degraded": 1, "escalation-required": 2, "blocked": 3}


class Status(StrEnum):
    """Stable readiness statuses."""

    READY = "ready"
    ESCALATION = "escalation-required"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Command:
    """An exact command, separated from human prose."""

    id: str
    argv: tuple[str, ...]
    cwd: str
    purpose: str
    required_at: str

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "argv": list(self.argv)}


@dataclass(frozen=True)
class Check:
    """One bounded observation and its phase relevance."""

    id: str
    status: Status
    required: bool
    summary: str
    write_probed: bool = False
    commands: tuple[Command, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "required": self.required,
            "summary": self.summary[:240],
            "write_probed": self.write_probed,
            "commands": [command.as_dict() for command in self.commands],
        }


@dataclass(frozen=True)
class Repository:
    """Safe repository identity exposed in readiness output."""

    root: str
    branch: str | None
    worktree_kind: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Result:
    """Versioned readiness result shared by human and JSON renderers."""

    phase: str
    status: Status
    repository: Repository
    checks: tuple[Check, ...]
    commands: tuple[Command, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": self.phase,
            "status": self.status.value,
            "repository": self.repository.as_dict(),
            "checks": [check.as_dict() for check in self.checks],
            "commands": [command.as_dict() for command in self.commands],
        }


@dataclass(frozen=True)
class TopologyFacts:
    """Pure inputs for the ordered develop topology classifier."""

    linked: bool
    branch: str | None
    head: str | None
    main: str | None
    merge_base: str | None
    unique_commits: int | None
    dirty: bool


def classify_topology(facts: TopologyFacts) -> tuple[Status, str]:
    """Classify a checkout using the plan's total, ordered rules."""
    if not facts.linked or facts.branch in {None, "main"}:
        return Status.BLOCKED, "develop requires a linked worktree on a named branch"
    if facts.head is None or facts.main is None or facts.merge_base is None:
        return Status.BLOCKED, "Git topology is incomplete: main or its merge base is missing"
    if facts.head == facts.main:
        if facts.dirty:
            return Status.DEGRADED, "linked branch equals main but has dirty files"
        return Status.READY, "fresh linked branch candidate"
    if facts.unique_commits is not None and facts.unique_commits > 0:
        return Status.READY, "branch contains commits unique from the merge base"
    return Status.DEGRADED, "branch has no unique commits while local main is ahead"


def aggregate_status(checks: Sequence[Check]) -> Status:
    """Return the worst status among required checks and explicit warnings."""
    if not checks:
        return Status.READY
    return max((check.status for check in checks), key=lambda status: _STATUS_ORDER[status.value])


def exit_code(status: Status) -> int:
    """Map an aggregate status to the stable process contract."""
    return 1 if status is Status.BLOCKED else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one phase and render exactly one result."""
    args = _parse_args(argv)
    try:
        result = run_phase(args)
    except ReadinessUsageError as error:
        check = Check(
            "repository.source-marker",
            Status.BLOCKED,
            True,
            str(error)[:240],
        )
        result = Result(
            args.phase,
            Status.BLOCKED,
            Repository(str(Path.cwd().resolve()), None, "unknown"),
            (check,),
            (),
        )
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
    else:
        print(render_human(result))
    return exit_code(result.status)


class ReadinessUsageError(ValueError):
    """A valid CLI invocation that cannot be evaluated safely."""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "develop", "publish"), default="develop")
    parser.add_argument("--branch")
    parser.add_argument("--worktree-path", type=Path)
    parser.add_argument("--require", choices=("docker",))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.phase == "prepare" and not args.branch:
        parser.error("--branch is required for --phase prepare")
    if args.phase != "prepare" and (args.branch or args.worktree_path):
        parser.error("--branch and --worktree-path are only valid for --phase prepare")
    if args.require and args.phase != "develop":
        parser.error("--require docker is only valid for --phase develop")
    if args.branch and not _valid_branch(args.branch):
        parser.error("--branch must be a new local codex/<task-slug> branch")
    return args


def run_phase(args: argparse.Namespace) -> Result:
    """Dispatch after CLI combinations have been validated."""
    root = find_source_checkout(Path.cwd())
    if args.phase == "prepare":
        return _prepare(root, args.branch, args.worktree_path)
    if args.phase == "publish":
        return _publish(root)
    return _develop(root, require_docker=args.require == "docker")


def find_source_checkout(start: Path) -> Path:
    """Find the nearest checkout whose marker explicitly identifies Booley."""
    for candidate in (start.resolve(), *start.resolve().parents):
        config = candidate / "pyproject.toml"
        if not config.is_file():
            continue
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ReadinessUsageError(f"cannot read pyproject.toml: {error}") from error
        if data.get("tool", {}).get("booley", {}).get("source_checkout") is True:
            return candidate
    raise ReadinessUsageError("current directory is not inside a Booley source checkout")


def _prepare(root: Path, branch: str, requested_path: Path | None) -> Result:
    checks: list[Check] = []
    checks.append(_python_check())
    git = shutil.which("git")
    checks.append(_presence_check("git.executable", git is not None, "Git executable found"))
    identity = _git(root, ("rev-parse", "--show-toplevel")) if git else None
    checks.append(_git_identity_check(root, identity))
    checks.append(
        Check("repository.source-marker", Status.READY, True, "source checkout marker is enabled")
    )
    main_ref = _git(root, ("show-ref", "--verify", "refs/heads/main")) if git else None
    checks.append(_presence_check("git.main", _successful(main_ref), "local main branch exists"))
    branch_ref = _git(root, ("show-ref", "--verify", f"refs/heads/{branch}")) if git else None
    checks.append(
        _presence_check(
            "git.branch-absent", not _successful(branch_ref), "requested branch is absent"
        )
    )
    checks.append(_tracking_advisory(root))
    worktree = _worktree_path(root, branch, requested_path)
    path_ok = worktree is not None and not worktree.exists()
    checks.append(_presence_check("worktree.path", path_ok, "worktree path is safe and absent"))
    command = _worktree_command(
        root,
        branch,
        worktree if _successful(main_ref) and not _successful(branch_ref) and path_ok else None,
    )
    policy = os.environ.get("BOOLEY_AGENT_GIT_WRITE_POLICY", "unknown")
    if policy not in {"unknown", "allowed", "escalation-required"}:
        raise ReadinessUsageError("BOOLEY_AGENT_GIT_WRITE_POLICY has an invalid value")
    policy_status = Status.ESCALATION if policy == "escalation-required" else Status.READY
    policy_summary = (
        "Git write approval is required before running the worktree command"
        if policy == "escalation-required"
        else "Git write not probed; approval may be required"
    )
    checks.append(
        Check(
            "git.write-policy",
            policy_status,
            False,
            policy_summary,
            commands=(command,) if command else (),
        )
    )
    checks.append(_current_checkout_advisory(root))
    commands = (command,) if command else ()
    return _result("prepare", root, checks, commands, git=git)


def _develop(root: Path, *, require_docker: bool) -> Result:
    checks: list[Check] = []
    linked = _is_linked_worktree(root)
    branch = _git_text(root, ("symbolic-ref", "--short", "-q", "HEAD"))
    head = _git_text(root, ("rev-parse", "HEAD"))
    main = _git_text(root, ("rev-parse", "refs/heads/main"))
    merge_base = _git_text(root, ("merge-base", "HEAD", "refs/heads/main"))
    unique_commits = _git_count(root, ("rev-list", "--count", "refs/heads/main..HEAD"))
    dirty = _git_text(root, ("status", "--porcelain")) is not None
    topology_status, topology_summary = classify_topology(
        TopologyFacts(linked, branch, head, main, merge_base, unique_commits, dirty)
    )
    checks.append(Check("git.topology", topology_status, True, topology_summary))
    checks.append(
        _presence_check("repository.source-marker", True, "source checkout marker is enabled")
    )
    environment, env_commands, shared_python = _environment_check(root)
    checks.append(environment)
    checks.extend(_tool_checks(shared_python, root))
    checks.append(_cache_path_check(root))
    docker_check = _docker_check(require_docker)
    checks.append(docker_check)
    commands = list(env_commands)
    commands.extend(_verification_commands(shared_python, root))
    return _result(
        "develop", root, checks, tuple(commands), git=shutil.which("git"), branch=branch
    )


def _publish(root: Path) -> Result:
    checks = [_git_identity_check(root, _git(root, ("rev-parse", "--show-toplevel")))]
    gh = probe_github(which=shutil.which, run=subprocess.run, cwd=root)
    checks.append(
        _presence_check("github.executable", gh.executable is not None, "GitHub CLI found")
    )
    checks.append(
        _probe_check(
            "github.authentication", gh.authentication, "GitHub authentication is available"
        )
    )
    checks.append(
        _probe_check(
            "github.connectivity", gh.connectivity, "GitHub read-only connectivity is available"
        )
    )
    return _result("publish", root, checks, (), git=shutil.which("git"))


def _result(
    phase: str,
    root: Path,
    checks: Sequence[Check],
    commands: Sequence[Command],
    *,
    git: str | None,
    branch: str | None = None,
) -> Result:
    kind = "linked" if _is_linked_worktree(root) else "primary"
    actual_branch = (
        branch
        if branch is not None
        else _git_text(root, ("symbolic-ref", "--short", "-q", "HEAD"))
    )
    if git is None:
        actual_branch = actual_branch or None
    return Result(
        phase,
        aggregate_status(checks),
        Repository(str(root), actual_branch, kind),
        tuple(checks),
        tuple(commands),
    )


def render_human(result: Result) -> str:
    """Render the same result model without leaking subprocess diagnostics."""
    lines = [
        f"Agent Readiness: {result.status.value} ({result.phase})",
        f"Repository: {result.repository.root}",
    ]
    if result.repository.branch:
        lines.append(f"Branch: {result.repository.branch} ({result.repository.worktree_kind})")
    for check in result.checks:
        marker = "OK" if check.status is Status.READY else check.status.value.upper()
        lines.append(f"{marker}: {check.id} — {check.summary}")
    for command in result.commands:
        lines.append(
            f"Run [{command.required_at}] {command.id}: {_argv(command.argv)} (cwd {command.cwd})"
        )
    if result.status is Status.ESCALATION:
        lines.append(
            "Resolve the reported approval requirement before acting; exit zero is not permission to proceed."
        )
    return "\n".join(lines)


def _argv(argv: Sequence[str]) -> str:
    import shlex

    return shlex.join(list(argv))


def _python_check() -> Check:
    current = sys.version_info[:2]
    good = current >= MIN_PYTHON
    summary = (
        f"Python {current[0]}.{current[1]} is supported"
        if good
        else "Python 3.11 or newer is required"
    )
    return Check("python.bootstrap", Status.READY if good else Status.BLOCKED, True, summary)


def _presence_check(identifier: str, present: bool, success: str) -> Check:
    return Check(
        identifier,
        Status.READY if present else Status.BLOCKED,
        True,
        success if present else f"{identifier} is unavailable",
    )


def _valid_branch(branch: str) -> bool:
    """Apply the portable subset of Git's branch-name rules at the CLI edge."""
    forbidden = ("..", "//", "@{", "~", "^", ":", "?", "*", "[", "\\")
    return (
        bool(_BRANCH_RE.fullmatch(branch))
        and not any(token in branch for token in forbidden)
        and not any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        and not branch.endswith((".", "/"))
    )


def _git_identity_check(root: Path, result: subprocess.CompletedProcess[str] | None) -> Check:
    good = _successful(result) and Path((result.stdout or "").strip()).resolve() == root.resolve()
    return Check(
        "git.repository-identity",
        Status.READY if good else Status.BLOCKED,
        True,
        "repository identity is readable" if good else "Git repository identity is unavailable",
    )


def _current_checkout_advisory(root: Path) -> Check:
    branch = _git_text(root, ("symbolic-ref", "--short", "-q", "HEAD"))
    dirty = _git_text(root, ("status", "--porcelain")) is not None
    detail = (
        "current checkout is clean"
        if not dirty
        else "current checkout has dirty files; this does not block prepare"
    )
    if branch is None:
        detail = "current checkout is detached; this does not block prepare"
    return Check(
        "git.current-checkout",
        Status.DEGRADED if dirty or branch is None else Status.READY,
        False,
        detail,
    )


def _tracking_advisory(root: Path) -> Check:
    """Report existing local-main tracking drift without fetching."""
    upstream = _git_text(root, ("rev-parse", "--abbrev-ref", "refs/heads/main@{upstream}"))
    if upstream is None:
        return Check(
            "git.main-tracking", Status.READY, False, "no local main tracking ref observed"
        )
    local = _git_text(root, ("rev-parse", "refs/heads/main"))
    tracked = _git_text(root, ("rev-parse", upstream))
    if local is not None and local == tracked:
        return Check(
            "git.main-tracking",
            Status.READY,
            False,
            "local main matches its existing tracking ref",
        )
    return Check(
        "git.main-tracking",
        Status.DEGRADED,
        False,
        "local main differs from its existing tracking ref; no fetch was performed",
    )


def _worktree_path(root: Path, branch: str, requested: Path | None) -> Path | None:
    slug = branch.removeprefix("codex/").replace("/", "-")
    candidate = (requested or root / ".worktrees" / slug).expanduser().resolve()
    worktrees = (root / ".worktrees").resolve()
    if requested is None and not _within(candidate, worktrees):
        return None
    if requested is not None and _within(candidate, root) and not _within(candidate, worktrees):
        return None
    if _within(candidate, root) and not _within(candidate, worktrees):
        return None
    return candidate


def _worktree_command(root: Path, branch: str, path: Path | None) -> Command | None:
    if path is None:
        return None
    return Command(
        "git.worktree-create",
        ("git", "worktree", "add", "-b", branch, str(path), "main"),
        str(root),
        "create-worktree",
        "before-edit",
    )


def _is_linked_worktree(root: Path) -> bool:
    entry = root / ".git"
    if not entry.is_file():
        return False
    try:
        return entry.read_text(encoding="utf-8", errors="replace").startswith("gitdir:")
    except OSError:
        return False


def _environment_check(root: Path) -> tuple[Check, tuple[Command, ...], Path]:
    fingerprint = dependency_fingerprint(root)
    env_root = agent_tools_root() / fingerprint
    shared_python = venv_python(env_root)
    valid, reason = validate_environment(env_root, fingerprint, shared_python)
    if valid:
        return Check("python.shared-environment", Status.READY, True, reason), (), shared_python
    bootstrap = Command(
        "python.bootstrap-agent-tools",
        (
            sys.executable,
            "-B",
            str(root / ".github/scripts/bootstrap_agent_tools.py"),
            "--environment",
            str(env_root),
            "--fingerprint",
            fingerprint,
        ),
        str(root),
        "bootstrap-tools",
        "remediation",
    )
    rerun = Command(
        "readiness.rerun-develop",
        (
            sys.executable,
            "-B",
            str(root / ".github/scripts/agent_readiness.py"),
            "--phase",
            "develop",
        ),
        str(root),
        "readiness",
        "after-remediation",
    )
    return (
        Check(
            "python.shared-environment", Status.BLOCKED, True, reason, commands=(bootstrap, rerun)
        ),
        (bootstrap, rerun),
        shared_python,
    )


def dependency_fingerprint(root: Path) -> str:
    """Hash repository identity, interpreter/platform facts, and declarations."""
    data = _project_dependencies(root)
    identity = _git_text(root, ("rev-parse", "--git-common-dir")) or str(root)
    identity_path = Path(identity)
    if not identity_path.is_absolute():
        identity_path = root / identity_path
    payload = {
        "identity": str(identity_path.resolve()),
        "python": [platform.python_implementation(), *map(str, sys.version_info[:2])],
        "platform": [sys.platform, platform.machine()],
        "dependencies": data,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:32]


def _project_dependencies(root: Path) -> dict[str, tuple[str, ...]]:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    extras = project.get("optional-dependencies", {})
    selected = {
        "dependencies": tuple(project.get("dependencies", ())),
        "test": tuple(extras.get("test", ())),
        "quality": tuple(extras.get("quality", ())),
    }
    return selected


def dependency_argv(root: Path) -> tuple[str, ...]:
    """Return a bounded, platform-neutral pip install argument list."""
    values = _project_dependencies(root)
    flat = tuple(item for group in values.values() for item in group)
    argv = ("install", "--disable-pip-version-check", "--no-input", *flat)
    if sys.platform == "win32" and sum(len(item) + 1 for item in argv) >= 30_000:
        raise RuntimeError("agent-tools dependency command exceeds the Windows command limit")
    return argv


def agent_tools_root() -> Path:
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "Booley"
            / "agent-tools"
        )
    return (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "booley" / "agent-tools"
    )


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def validate_environment(root: Path, fingerprint: str, python: Path) -> tuple[bool, str]:
    receipt_path = root / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "shared tools environment is missing or has no valid receipt"
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("fingerprint") != fingerprint
        or not python.is_file()
    ):
        return False, "shared tools environment receipt or interpreter is stale"
    if not _runner_versions(receipt.get("installed", {})):
        return False, "shared tools environment has mismatched Ruff/Pytest runner pins"
    actual = _installed_versions(python)
    if actual is None or actual != receipt.get("installed", {}):
        return False, "shared tools environment distribution set differs from its receipt"
    pip_check = _run_child(python, ("-m", "pip", "check"), root)
    if not _successful(pip_check):
        return False, "shared tools environment failed pip check"
    return True, "shared tools environment matches the repository fingerprint"


def _runner_versions(installed: Any) -> bool:
    if not isinstance(installed, dict):
        return False
    return all(installed.get(name) == version for name, version in PINNED_RUNNERS.items())


def _installed_versions(python: Path) -> dict[str, str] | None:
    code = "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name'].lower(): d.version for d in m.distributions()}))"
    result = _run_child(python, ("-c", code), Path.cwd())
    if not _successful(result):
        return None
    try:
        value = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_checks(python: Path, root: Path) -> tuple[Check, ...]:
    if not python.is_file():
        return (Check("tools.runners", Status.BLOCKED, True, "shared Python is unavailable"),)
    return (_runner_check(python, root),)


def _runner_check(python: Path, root: Path) -> Check:
    versions = _installed_versions(python)
    good = versions is not None and _runner_versions(versions)
    return Check(
        "tools.runners",
        Status.READY if good else Status.BLOCKED,
        True,
        "exact Ruff/Pytest runner stack is installed"
        if good
        else "exact Ruff/Pytest runner stack is unavailable",
    )


def _cache_path_check(root: Path) -> Check:
    cache = agent_tools_root().resolve()
    safe = not _within(cache, root.resolve())
    return Check(
        "python.cache-path",
        Status.READY if safe else Status.BLOCKED,
        True,
        "shared tools cache is outside the checkout"
        if safe
        else "shared tools cache would be inside the checkout",
    )


def _docker_check(required: bool) -> Check:
    observation = probe_docker(
        which=shutil.which,
        run=subprocess.run,
        probe_daemon=required,
        cwd=Path.cwd(),
    )
    if observation.state is ProbeState.MISSING:
        return Check(
            "docker.executable",
            Status.BLOCKED if required else Status.DEGRADED,
            required,
            "Docker is unavailable"
            if required
            else "Docker not found; optional for ordinary development",
        )
    if not required:
        return Check(
            "docker.executable",
            Status.READY,
            False,
            "Docker executable discovered; daemon not contacted",
        )
    if observation.state is ProbeState.HEALTHY:
        return Check(
            "docker.daemon", Status.READY, True, "Docker daemon responds to the bounded probe"
        )
    status = Status.ESCALATION if observation.state is ProbeState.PERMISSION else Status.BLOCKED
    summary = (
        "Docker daemon access requires approval"
        if status is Status.ESCALATION
        else "Docker daemon did not respond successfully"
    )
    return Check("docker.daemon", status, True, summary)


def _probe_check(identifier: str, state: ProbeState, success: str) -> Check:
    return Check(
        identifier,
        Status.READY if state is ProbeState.HEALTHY else Status.BLOCKED,
        True,
        success if state is ProbeState.HEALTHY else "GitHub read-only probe failed",
    )


def _verification_commands(python: Path, root: Path) -> tuple[Command, ...]:
    common = (str(python), "-m")
    return (
        Command(
            "ruff.agent-gate",
            (*common, "ruff", "check", "src/", "tests/"),
            str(root),
            "lint",
            "before-commit",
        ),
        Command("ruff.ci-check", (*common, "ruff", "check", "."), str(root), "lint", "handoff"),
        Command(
            "ruff.ci-format",
            (*common, "ruff", "format", "--check", "."),
            str(root),
            "format",
            "handoff",
        ),
        Command(
            "pytest.broad",
            (*common, "pytest", "tests/"),
            str(root),
            "test",
            "optional-broad-verification",
        ),
    )


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    return _run_process(("git", *args), root, timeout=5, git=True)


def _git_text(root: Path, args: Sequence[str]) -> str | None:
    result = _git(root, args)
    if not _successful(result):
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_success(root: Path, args: Sequence[str]) -> bool:
    return _successful(_git(root, args))


def _git_count(root: Path, args: Sequence[str]) -> int | None:
    value = _git_text(root, args)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _run_child(
    python: Path, args: Sequence[str], cwd: Path
) -> subprocess.CompletedProcess[str] | None:
    return _run_process((str(python), "-B", *args), cwd, timeout=10)


def _run_process(
    args: Sequence[str], cwd: Path, *, timeout: float = 5, git: bool = False
) -> subprocess.CompletedProcess[str] | None:
    env = os.environ.copy()
    if git:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            tuple(args),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _successful(result: subprocess.CompletedProcess[str] | None) -> bool:
    return result is not None and result.returncode == 0
