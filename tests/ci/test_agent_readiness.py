"""Contracts for the read-only Agent Readiness Check."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.bwave.native_binary import require_native_binary
from tests.git_command_fake import GitCommandFake
from tests.git_support import git, repository
from tests.path_tool_fake import PathToolFake
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child
from tests.time_budget import hang_guard_s

from booley.core import file_lock
from booley.core.host_probes import ProbeState, probe_docker
from booley.dev_support import agent_readiness as readiness
from booley.dev_support.test_profiles import BWAVE_BUILD, native_test_binary

# The serialized shape of one readiness check, with no per-check probe flag.
CHECK_KEYS = {"id", "status", "required", "summary", "commands"}
LAUNCHER = Path(__file__).parents[2] / ".github/scripts/agent_readiness.py"
ENVIRONMENT = "python.shared-environment"
PREPARE_IDS = [
    "git.executable",
    "git.repository-identity",
    "repository.source-marker",
    "git.main",
    "git.branch-absent",
    "git.main-tracking",
    "worktree.path",
    "git.write-policy",
    "git.current-checkout",
]
_RUNNER_NAMES = (
    "pytest",
    "pytest-asyncio",
    "pytest-xdist",
    "pytest-timeout",
    "pytest-repeat",
    "pytest-cov",
    "coverage",
)
PINS = dict.fromkeys((*_RUNNER_NAMES, "ruff", "pyright"), "1")


def _bootstrap_module():
    path = Path(__file__).parents[2] / ".github/scripts/bootstrap_agent_tools.py"
    spec = importlib.util.spec_from_file_location("bootstrap_agent_tools", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("facts", "status"),
    [
        (
            readiness.TopologyFacts(False, "codex/x", "h", "m", "b", 1, False),
            readiness.Status.BLOCKED,
        ),
        (readiness.TopologyFacts(True, "main", "h", "m", "b", 1, False), readiness.Status.BLOCKED),
        (
            readiness.TopologyFacts(True, "codex/x", "h", None, None, None, False),
            readiness.Status.BLOCKED,
        ),
        (
            readiness.TopologyFacts(True, "codex/x", "h", "h", "h", 0, False),
            readiness.Status.READY,
        ),
        (
            readiness.TopologyFacts(True, "codex/x", "h", "h", "h", 0, True),
            readiness.Status.DEGRADED,
        ),
        (readiness.TopologyFacts(True, "codex/x", "h", "m", "b", 2, True), readiness.Status.READY),
        (
            readiness.TopologyFacts(True, "codex/x", "h", "m", "b", 0, False),
            readiness.Status.DEGRADED,
        ),
    ],
)
def test_topology_classifier_is_ordered_and_total(facts, status):
    assert readiness.classify_topology(facts)[0] is status


def test_status_precedence_and_exit_contract():
    checks = [
        readiness.Check("one", readiness.Status.READY, True, "ok"),
        readiness.Check("two", readiness.Status.ESCALATION, True, "approval"),
        readiness.Check("three", readiness.Status.DEGRADED, False, "optional"),
    ]
    assert readiness.aggregate_status(()) is readiness.Status.READY
    assert readiness.aggregate_status(checks) is readiness.Status.ESCALATION
    assert readiness.exit_code(readiness.Status.ESCALATION) == 0
    assert readiness.exit_code(readiness.Status.BLOCKED) == 1


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A pinned source checkout (a Git repository with one commit) and a private tools cache."""
    root = repository(tmp_path / "repo")
    _write_pinned(root)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    monkeypatch.delenv("BOOLEY_AGENT_GIT_WRITE_POLICY", raising=False)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def bare_path(tmp_path, monkeypatch):
    """An empty ``PATH``: Git, Docker and Cargo are absent unless a test installs a fake."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


def _report(capsys, *argv):
    """Run ``main`` with ``--json``; the document plus its exit code, raw text and checks by id."""
    code = readiness.main([*argv, "--json"])
    raw = capsys.readouterr().out
    document = json.loads(raw)
    by_id = {check["id"]: check for check in document["checks"]}
    return document | {"exit": code, "raw": raw, "by_id": by_id}


def _shared_environment(capsys) -> tuple[Path, str]:
    """The environment folder and fingerprint the reported bootstrap command names."""
    argv = _report(capsys, "--phase", "develop")["by_id"][ENVIRONMENT]["commands"][0]["argv"]
    return Path(argv[argv.index("--environment") + 1]), argv[argv.index("--fingerprint") + 1]


def test_develop_reports_verification_commands(checkout, capsys):
    root = checkout.resolve()
    document = _report(capsys, "--phase", "develop")
    python = str(readiness.venv_python(Path(document["commands"][0]["argv"][-3])))
    scripts, report = root / ".github/scripts", readiness.coverage_xml_path(root)
    commands = {command["id"]: command for command in document["commands"]}
    assert " ".join(commands) == (
        "python.bootstrap-agent-tools readiness.rerun-develop ruff.agent-gate ruff.ci-check "
        "ruff.ci-format pyright.ci-check packaged-copies.check markdown.line-length "
        "function-length.check pytest.broad coverage.changed-lines"
    )
    options = ["-n", "auto", "--maxprocesses=8", "--dist=loadscope"]
    if sys.platform != "win32":
        options.append(f"--basetemp={readiness.pytest_basetemp(root)}")
    coverage = ["--cov=booley", f"--cov-report=xml:{report}", "--cov-fail-under=0"]
    tool, launcher = [python, "-m"], [python, "-B"]
    expected = {
        "ruff.agent-gate": [*tool, "ruff", "check", "src/", "tests/"],
        "ruff.ci-check": [*tool, "ruff", "check", "."],
        "ruff.ci-format": [*tool, "ruff", "format", "--check", "."],
        # CI's bare `pyright` step, with imports resolved in the shared environment.
        "pyright.ci-check": [*tool, "pyright", "--pythonpath", python],
        "packaged-copies.check": [*launcher, str(scripts / "sync_packaged_copies.py"), "--check"],
        "markdown.line-length": [*launcher, str(scripts / "markdown_line_length.py")],
        "function-length.check": [*launcher, str(scripts / "check_function_length.py")],
        "pytest.broad": [
            *tool,
            "pytest",
            "tests/",
            "--test-profile",
            "python",
            *options,
            *coverage,
        ],
        # CI's changed-line gate reads the report the broad run wrote.
        "coverage.changed-lines": [
            *launcher,
            str(scripts / "changed_line_coverage.py"),
            "--coverage-xml",
            str(report),
        ],
    }
    handoff = {"ruff.ci-check", "ruff.ci-format", "pyright.ci-check", "coverage.changed-lines"}
    for identifier, argv in expected.items():
        required_at = "handoff" if identifier in handoff else "before-commit"
        if identifier == "pytest.broad":
            required_at = "optional-broad-verification"
        assert commands[identifier]["argv"] == argv, identifier
        assert commands[identifier]["required_at"] == required_at, identifier
        assert commands[identifier]["cwd"] == str(root), identifier


def test_packaged_copies_check_runs_as_reported():
    """The reported argv works as written: right launcher path, src insert, flag."""
    root = Path(__file__).parents[2]
    # private: the reported command names the shared environment's interpreter, which a test run need not have; this builds it for sys.executable.
    commands = readiness._verification_commands(Path(sys.executable), root)
    command = next((item for item in commands if item.id == "packaged-copies.check"), None)
    assert command is not None
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = run_bounded_child(
        list(command.argv),
        cwd=command.cwd,
        env=environment,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert (result.returncode, result.stdout) == (0, "")


def test_broad_pytest_keeps_default_basetemp_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "win32")
    # private: develop cannot run under a patched win32 platform (shutil.which then calls Windows-only APIs), so the Windows options are built directly.
    commands = readiness._verification_commands(tmp_path / "python.exe", tmp_path)
    assert commands[-2].argv[6:10] == ("-n", "auto", "--maxprocesses=8", "--dist=loadscope")
    assert not any(arg.startswith("--basetemp") for arg in commands[-2].argv)


def test_pytest_basetemp_is_per_checkout_and_outside_it(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = tmp_path / "cache" / "booley" / "agent-tools-v2"
    assert readiness.agent_tools_root() == cache
    assert readiness.venv_python(tmp_path) == tmp_path / "bin/python"
    first = readiness.pytest_basetemp(tmp_path / "first")
    second = readiness.pytest_basetemp(tmp_path / "second")
    assert first != second
    assert first.parent == cache
    assert first.name.startswith("pytest-basetemp-")
    assert first == readiness.pytest_basetemp(tmp_path / "first")
    assert not first.is_relative_to(tmp_path / "first")
    monkeypatch.setattr(readiness.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert readiness.agent_tools_root() == tmp_path / "local" / "Booley" / "agent-tools-v2"
    assert readiness.venv_python(tmp_path) == tmp_path / "Scripts/python.exe"


def test_coverage_report_mirrors_the_checkout_outside_it(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = tmp_path / "cache" / "booley" / "agent-tools-v2"
    checkout = (tmp_path / "work" / "checkout").resolve()
    report = readiness.coverage_xml_path(checkout)
    assert report.is_relative_to(cache / "changed-line-coverage")
    assert not report.is_relative_to(checkout)
    # The pipeline's worktree-path normalization rewrites the mirrored part.
    assert str(report.parent).endswith(str(checkout.relative_to(checkout.anchor)))


def test_project_runner_pins_match_the_readiness_contract():
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    pins = readiness.runner_pins(root)
    # External gate probes read the module attribute; it must stay derived.
    assert pins == readiness.PINNED_RUNNERS
    assert set(pins) == {
        "pytest",
        "pytest-asyncio",
        "pytest-xdist",
        "pytest-timeout",
        "pytest-repeat",
        "pytest-cov",
        "coverage",
        "ruff",
        "pyright",
    }
    declared = {item.split("==", 1)[0]: item for item in extras["test"]}
    for name, version in pins.items():
        if name not in readiness.RUNNER_EXTRAS["quality"]:
            assert declared.get(name) == f"{name}=={version}"
    # dev composes the narrow extras by self-reference instead of repeating their pins.
    assert extras["dev"] == [f"{project['name']}[test,quality]"]
    quality = {item.split("==", 1)[0]: item for item in extras["quality"]}
    for name in readiness.RUNNER_EXTRAS["quality"]:
        assert quality[name] == f"{name}=={pins[name]}"


def _write_project(
    root: Path, test: list[str], quality: list[str], extras: dict[str, list[str]] | None = None
) -> None:
    """Write a minimal source-checkout pyproject.toml declaring the given extras."""
    lines = ["[project]", 'name = "demo"', 'version = "0"', "dependencies = []"]
    lines += ["[project.optional-dependencies]"]
    lines += ["test = [" + ", ".join(f'"{item}"' for item in test) + "]"]
    lines += ["quality = [" + ", ".join(f'"{item}"' for item in quality) + "]"]
    for name, items in (extras or {}).items():
        lines += [f"{name} = [" + ", ".join(f'"{item}"' for item in items) + "]"]
    lines += ["[tool.booley]", "source_checkout = true"]
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_pinned(root: Path) -> None:
    """Write a source-checkout pyproject.toml that pins every runner to ``PINS``."""
    test = [
        f"{name}=={version}" for name, version in PINS.items() if name not in {"ruff", "pyright"}
    ]
    _write_project(root, test, ["ruff==1", "pyright==1"])


def test_runner_pins_derive_from_project_metadata(tmp_path):
    """A version bump in pyproject.toml alone changes the pins; no second edit exists."""
    test = [f"{name}=={index}.0.1" for index, name in enumerate(_RUNNER_NAMES, start=1)]
    _write_project(tmp_path, [*test, "hypothesis>=1.0"], ["Ruff==9.9.9", "pyright==8.8.8"])
    pins = readiness.runner_pins(tmp_path)
    assert pins == {
        **{name: f"{index}.0.1" for index, name in enumerate(_RUNNER_NAMES, start=1)},
        "ruff": "9.9.9",
        "pyright": "8.8.8",
    }


def test_runner_pins_and_install_argv_expand_self_referencing_extras(tmp_path):
    """``demo[parser]`` in an extra means the parser extra's pins, never the published package."""
    test = [f"{name}==1" for name in _RUNNER_NAMES]
    _write_project(
        tmp_path,
        [*test, "hypothesis>=1", "demo[parser]"],
        ["ruff==2", "pyright==3"],
        extras={"parser": ["pyslang==2"]},
    )
    assert readiness.runner_pins(tmp_path)["coverage"] == "1"
    argv = readiness.dependency_argv(tmp_path)
    assert "pyslang==2" in argv
    assert not any(item.startswith("demo") for item in argv)
    assert argv.index("pyslang==2") == len(argv) - 3  # test extra order is kept, quality follows


def test_self_reference_cycles_terminate_and_unknown_extras_fail(tmp_path):
    """A loop between extras adds each pin once; a reference to a missing extra is refused."""
    test = [f"{name}==1" for name in _RUNNER_NAMES]
    _write_project(
        tmp_path,
        [*test, "demo[Parser]"],
        ["ruff==2", "pyright==3"],
        extras={"parser": ["pyslang==2", "Demo[test]"]},
    )
    assert readiness.dependency_argv(tmp_path).count("pyslang==2") == 1
    _write_project(tmp_path, [*test, "demo[missing]"], ["ruff==2", "pyright==3"])
    with pytest.raises(readiness.ReadinessUsageError, match="unknown extra 'missing'"):
        readiness.dependency_argv(tmp_path)


@pytest.mark.parametrize("reference", ["demo[parser]>=1", "demo[parser]; python_version>'3'"])
def test_self_reference_with_a_specifier_is_a_usage_error(tmp_path, reference):
    """A versioned or marked self-reference would install the published package: refuse it."""
    test = [f"{name}==1" for name in _RUNNER_NAMES]
    _write_project(
        tmp_path, [*test, reference], ["ruff==2", "pyright==3"], extras={"parser": ["pyslang==2"]}
    )
    with pytest.raises(readiness.ReadinessUsageError, match=r"test.*self-reference"):
        readiness.dependency_argv(tmp_path)


_OTHERS = [f"{name}==1" for name in _RUNNER_NAMES[1:]]


@pytest.mark.parametrize(
    ("test", "quality", "culprit"),
    [
        (["pytest>=1", *_OTHERS], ["ruff==1", "pyright==1"], "pytest"),
        (["pytest==1.*", *_OTHERS], ["ruff==1", "pyright==1"], "pytest"),
        (["pytest===1", *_OTHERS], ["ruff==1", "pyright==1"], "pytest"),
        (["pytest==1", *_OTHERS[:-1]], ["ruff==1", "pyright==1"], "coverage"),
        (["pytest==1", *_OTHERS], ["ruff>=0.1", "pyright==1"], "ruff"),
        (["pytest==1", *_OTHERS], ["ruff==1", "pyright>=1.1"], "pyright"),
    ],
)
def test_runner_without_exact_pin_is_a_usage_error(tmp_path, test, quality, culprit):
    """Floors, wildcards, `===` and missing runners are rejected, naming the runner."""
    _write_project(tmp_path, test, quality)
    with pytest.raises(readiness.ReadinessUsageError, match=f"{culprit}.*exact"):
        readiness.runner_pins(tmp_path)


def test_bootstrap_dependencies_never_install_booley():
    arguments = readiness.dependency_argv(Path(__file__).parents[2])
    assert not any(
        item.startswith("-e") or item in {".", "booley", "booley-rtl"} for item in arguments
    )


def _run_launcher(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the real launcher in this checkout with a neutral readiness environment."""
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("BOOLEY_AGENT_GIT_WRITE_POLICY", None)
    return run_bounded_child(
        [sys.executable, str(LAUNCHER), *args],
        cwd=LAUNCHER.parents[2],
        env=environment,
        timeout=SUBPROCESS_TIMEOUT_S,
    )


def test_real_launcher_reports_current_check_ids():
    result = _run_launcher("--phase", "prepare", "--branch", "codex/contract-test", "--json")
    assert result.returncode in {0, 1}
    assert not result.stderr
    document = json.loads(result.stdout)
    assert (document["schema_version"], document["phase"]) == (1, "prepare")
    assert [check["id"] for check in document["checks"]] == PREPARE_IDS
    assert all(set(check) == CHECK_KEYS for check in document["checks"])


@pytest.mark.parametrize("case", ["missing", "oserror", "timeout"])
def test_probe_docker_maps_runner_failures(case):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 10) if case == "timeout" else OSError

    which = (lambda _name: None) if case == "missing" else (lambda _name: "docker")
    state = {"missing": ProbeState.MISSING, "oserror": ProbeState.FAILED}.get(case)
    assert probe_docker(which=which, run=run).state is (state or ProbeState.TIMEOUT)
    assert bool(calls) is (case != "missing")


_OK, _DENIED = (0, "", ""), (1, "secret", "permission denied")
# case: (require, installed, `docker info` answer, check id, status, summary fragment)
_DOCKER = {
    "optional-present": (False, True, _OK, "docker.executable", "ready", "daemon not contacted"),
    "optional-absent": (False, False, _OK, "docker.executable", "degraded", "optional"),
    "required-absent": (True, False, _OK, "docker.executable", "blocked", "unavailable"),
    "healthy": (True, True, _OK, "docker.daemon", "ready", "responds"),
    "permission": (True, True, _DENIED, "docker.daemon", "escalation-required", "approval"),
    "failed": (True, True, (1, "", "boom"), "docker.daemon", "blocked", "did not respond"),
}


@pytest.mark.parametrize("case", _DOCKER)
def test_docker_check_reports_the_probe(checkout, bare_path, monkeypatch, capsys, case):
    require, installed, info, check_id, status, summary = _DOCKER[case]
    docker = PathToolFake.install(bare_path, "docker", monkeypatch) if installed else None
    if docker:  # `info` is answered, so that a call to it would be logged
        docker.answer("info", returncode=info[0], stdout=info[1], stderr=info[2])
    document = _report(capsys, "--phase", "develop", *(["--require", "docker"] if require else []))
    check = document["by_id"][check_id]
    assert check["status"] == status
    assert summary in check["summary"]
    assert "secret" not in document["raw"]
    if docker and not require:
        assert docker.calls() == []  # passive: the daemon is never contacted


@pytest.mark.parametrize(
    "case", ["build", "no-cargo", pytest.param("binary", marks=pytest.mark.native_bwave)]
)
def test_full_profile_reports_native_prerequisites(checkout, bare_path, monkeypatch, capsys, case):
    if case == "build":
        PathToolFake.install(bare_path, "cargo", monkeypatch)
    if case == "binary":
        binary = native_test_binary(checkout)
        binary.parent.mkdir(parents=True)
        shutil.copy2(require_native_binary(), binary)
    document = _report(capsys, "--phase", "develop", "--test-profile", "full")
    check = document["by_id"]["native.bwave"]
    commands = {command["id"]: command for command in document["commands"]}
    if case == "binary":
        assert check["status"] == "ready"
        assert commands["pytest.broad"]["argv"][4:6] == ["--test-profile", "full"]
        return
    assert check["status"] == "blocked"
    assert "pytest.broad" not in commands
    assert all(command["purpose"] != "test" for command in document["commands"])
    if case == "build":
        assert [command["argv"] for command in check["commands"]] == [list(BWAVE_BUILD)]
    else:
        assert "install Rust/Cargo" in check["summary"]
        assert check["commands"] == []


_PREPARE = ["--phase", "prepare", "--branch"]
# case: (argv, argparse message, what Git answers to `check-ref-format`; None: not asked)
_PARSE_ERRORS = {
    "no-branch": (["--phase", "prepare"], "--branch is required", None),
    "develop-branch": (["--phase", "develop", "--branch", "codex/x"], "only valid", None),
    "prepare-require": ([*_PREPARE, "codex/x", "--require", "docker"], "only valid", None),
    "prepare-test-profile": ([*_PREPARE, "codex/x", "--test-profile", "full"], "only valid", None),
    "publish-removed": (["--phase", "publish"], "invalid choice", None),
    "no-prefix": ([*_PREPARE, "feature/x"], "new local", None),
    "git-refuses": ([*_PREPARE, "codex/x..y"], "new local", "real"),
    "git-refused": ([*_PREPARE, "codex/ok"], "new local", "refused"),
    "git-missing": ([*_PREPARE, "codex/ok"], "new local", "missing"),
}


@pytest.mark.parametrize("case", _PARSE_ERRORS)
def test_parse_errors_name_the_rule(case, monkeypatch, capsys):
    """The codex/ prefix is gated locally; every other ref rule is Git's."""
    argv, message, git_answer = _PARSE_ERRORS[case]
    fake = GitCommandFake.install(monkeypatch)
    if git_answer == "refused":
        fake.answer("check-ref-format", returncode=128)
    if git_answer == "missing":
        fake.crash("check-ref-format", error=OSError())
    with pytest.raises(SystemExit):
        readiness.main(argv)
    assert message in capsys.readouterr().err
    checked = [["git", "check-ref-format", "--branch", argv[-1]]] if git_answer else []
    assert fake.commands == checked


@pytest.mark.parametrize(
    ("pyproject", "fragment"),
    [
        (None, "not inside"),
        ("[", "cannot read"),
        ("[tool.booley]\nsource_checkout = false\n", "not inside"),
    ],
    ids=["no-pyproject", "unreadable", "not-source"],
)
def test_usage_errors_render_as_blocked_json(tmp_path, monkeypatch, capsys, pyproject, fragment):
    folder = tmp_path / "folder"
    folder.mkdir()
    if pyproject is not None:
        (folder / "pyproject.toml").write_bytes(pyproject.encode())
    monkeypatch.chdir(folder)
    document = _report(capsys, "--phase", "develop")
    assert (document["exit"], document["status"]) == (1, "blocked")
    assert document["by_id"].keys() == {"repository.source-marker"}
    assert fragment in document["by_id"]["repository.source-marker"]["summary"]


def _branch_exists(root, fake, monkeypatch):
    git(root, "branch", "codex/x")


def _dirty(root, fake, monkeypatch):
    (root / "scratch.txt").write_bytes(b"untracked")


def _symlink_escape(root, fake, monkeypatch):
    if os.name == "nt":
        pytest.skip("symlinks need privileges")
    (root / ".worktrees").mkdir()
    (root / ".worktrees" / "x").symlink_to(root.parent / "missing", target_is_directory=True)


def _tracking(root, fake, monkeypatch, *, drifted=False):
    if drifted:
        git(root, "commit", "--allow-empty", "-q", "-m", "second")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD~1" if drifted else "HEAD")
    fake.answer("--abbrev-ref", returncode=0, stdout=b"origin/main\n")


def _tracking_drift(root, fake, monkeypatch):
    _tracking(root, fake, monkeypatch, drifted=True)


def _identity_crash(root, fake, monkeypatch):
    fake.crash("--show-toplevel", error=OSError())


def _bad_policy(root, fake, monkeypatch):
    monkeypatch.setenv("BOOLEY_AGENT_GIT_WRITE_POLICY", "bad")


_FRESH = dict.fromkeys(PREPARE_IDS, "ready")
_WORKTREE = ".worktrees/x"
# case: (setup, --worktree-path under the checkout, expected statuses, planned worktree path)
_PREPARE_CASES = {
    "fresh": (None, None, _FRESH, _WORKTREE),
    "branch-exists": (_branch_exists, None, {"git.branch-absent": "blocked"}, None),
    "requested-inside": (None, "inside", {"worktree.path": "blocked"}, None),
    "requested-outside": (None, "../elsewhere", {"worktree.path": "ready"}, "../elsewhere"),
    "symlink-escape": (_symlink_escape, None, {"worktree.path": "blocked"}, None),
    "dirty": (_dirty, None, {"git.current-checkout": "degraded"}, _WORKTREE),
    "tracking-match": (_tracking, None, {"git.main-tracking": "ready"}, _WORKTREE),
    "tracking-drift": (_tracking_drift, None, {"git.main-tracking": "degraded"}, _WORKTREE),
    "identity-crash": (_identity_crash, None, {"git.repository-identity": "blocked"}, _WORKTREE),
    "bad-policy": (_bad_policy, None, {"repository.source-marker": "blocked"}, None),
}


@pytest.mark.parametrize("case", _PREPARE_CASES)
def test_prepare_reports_each_check(checkout, monkeypatch, capsys, case):
    setup, requested, statuses, planned = _PREPARE_CASES[case]
    root = checkout.resolve()
    fake = GitCommandFake.install(monkeypatch)
    if setup:
        setup(checkout, fake, monkeypatch)
    (root / "nested").mkdir()
    monkeypatch.chdir(root / "nested")  # the checkout is found from a subfolder
    path = ["--worktree-path", str(root / requested)] if requested else []
    document = _report(capsys, *_PREPARE, "codex/x", *path)
    assert {key: document["by_id"][key]["status"] for key in statuses} == statuses
    assert document["exit"] == int("blocked" in statuses.values())
    argv = [command["argv"] for command in document["commands"]]
    worktree = [str((root / planned).resolve())] if planned else None
    assert argv == (
        [["git", "worktree", "add", "-b", "codex/x", *worktree, "main"]] if planned else []
    )


def test_prepare_escalation_is_visible_in_human_output(checkout, monkeypatch, capsys):
    git(checkout, "checkout", "-q", "--detach")
    monkeypatch.setenv("BOOLEY_AGENT_GIT_WRITE_POLICY", "escalation-required")
    assert readiness.main(["--phase", "prepare", "--branch", "codex/x"]) == 0
    output = capsys.readouterr().out
    assert "ESCALATION-REQUIRED" in output
    assert "exit zero is not permission" in output
    assert "current checkout is detached" in output
    assert "Run [before-edit] git.worktree-create:" in output
    assert "Branch:" not in output


def test_develop_human_output_names_the_branch(checkout, capsys):
    assert readiness.main(["--phase", "develop"]) == 1  # a primary checkout is not a worktree
    output = capsys.readouterr().out
    assert "Branch: main (primary)" in output
    assert "OK: repository.source-marker" in output
    assert "exit zero is not permission" not in output


def _linked(root, fake=None, monkeypatch=None):
    path = root.parent / "linked"
    git(root, "worktree", "add", "-q", "-b", "codex/x", str(path), "main")
    return path


def _count_unreadable(root, fake, monkeypatch):
    path = _linked(root)
    git(path, "commit", "--allow-empty", "-q", "-m", "work")
    unreadable = subprocess.CompletedProcess([], 0, "not-an-int", "")
    fake.rewrite("--count", edit=lambda result, _options: unreadable)
    return path


def _unreadable_gitfile(root, fake, monkeypatch):
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs a mode-000 file that its owner cannot read")
    plain = root.parent / "plain"
    plain.mkdir()
    _write_pinned(plain)
    (plain / ".git").write_bytes(b"gitdir: elsewhere\n")
    (plain / ".git").chmod(0)
    return plain


# case: (setup returning the folder to run in, status, summary fragment, kind, branch)
_TOPOLOGY = {
    "primary": (None, "blocked", "linked worktree", "primary", "main"),
    "fresh-linked": (_linked, "ready", "fresh linked", "linked", "codex/x"),
    "count-unreadable": (_count_unreadable, "degraded", "no unique commits", "linked", "codex/x"),
    "unreadable-gitfile": (_unreadable_gitfile, "blocked", "linked worktree", "primary", None),
}


@pytest.mark.parametrize("case", _TOPOLOGY)
def test_develop_reads_topology_from_git(checkout, monkeypatch, capsys, case):
    setup, status, summary, kind, branch = _TOPOLOGY[case]
    fake = GitCommandFake.install(monkeypatch)
    monkeypatch.chdir(setup(checkout, fake, monkeypatch) if setup else checkout)
    document = _report(capsys, "--phase", "develop")
    topology = document["by_id"]["git.topology"]
    assert (topology["status"], summary in topology["summary"]) == (status, True)
    assert (document["repository"]["worktree_kind"], document["repository"]["branch"]) == (
        kind,
        branch,
    )
    ids = ["git.topology", "repository.source-marker", ENVIRONMENT, "docker.executable"]
    assert [check["id"] for check in document["checks"]] == ids
    assert all(set(check) == CHECK_KEYS for check in document["checks"])


def test_worktrees_of_one_repository_share_the_tools_environment(
    checkout, tmp_path, monkeypatch, capsys
):
    nested = checkout / "nested"
    nested.mkdir()
    other = repository(tmp_path / "other")
    _write_pinned(other)
    plain = tmp_path / "plain"
    plain.mkdir()
    _write_pinned(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    def fingerprint(folder: Path) -> str:
        monkeypatch.chdir(folder)
        return _shared_environment(capsys)[1]

    # `--git-common-dir` prints `.git` in the primary checkout: it names the root, not the folder.
    primary = fingerprint(nested)
    assert fingerprint(_linked(checkout)) == primary
    assert fingerprint(other) != primary
    assert len(fingerprint(plain)) == 32  # no Git: the root is the identity


STALE = "shared tools environment receipt or interpreter is stale"
MISMATCH = "shared tools environment has mismatched Ruff/Pyright/Pytest runner pins"
VALID = "shared tools environment matches the repository fingerprint"


def _receipt(**overrides: object) -> dict[str, object]:
    """A valid schema-2 receipt for fingerprint ``fp``, with *overrides* applied."""
    return {"schema_version": 2, "fingerprint": "fp", "installed": PINS} | overrides


def _write_receipt(environment: Path, receipt: object = None) -> None:
    """Publish *receipt* (raw JSON text when a string) beside a venv interpreter."""
    if receipt is None:
        receipt = _receipt()
    python = readiness.venv_python(environment)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    text = receipt if isinstance(receipt, str) else json.dumps(receipt)
    (environment / "receipt.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("receipt", "summary"),
    [
        (lambda fp: _receipt(fingerprint=fp), VALID),
        (lambda fp: "[", "shared tools environment is missing or has no valid receipt"),
        (lambda fp: "null", STALE),
        (lambda fp: _receipt(fingerprint=fp, schema_version=1), STALE),
        (lambda fp: _receipt(), STALE),
        (lambda fp: _receipt(fingerprint=fp, installed=None), MISMATCH),
        (lambda fp: _receipt(fingerprint=fp, installed=PINS | {"ruff": "0.0.0"}), MISMATCH),
    ],
    ids=["valid", "unparsable", "null", "schema-1", "fingerprint", "no-pins", "wrong-pin"],
)
def test_develop_trusts_the_receipt_without_probing(
    checkout, monkeypatch, capsys, receipt, summary
):
    fake = GitCommandFake.install(monkeypatch)
    environment, fingerprint = _shared_environment(capsys)
    _write_receipt(environment, receipt(fingerprint))
    check = _report(capsys, "--phase", "develop")["by_id"][ENVIRONMENT]
    blocked = summary != VALID
    assert (check["status"], check["summary"]) == ("blocked" if blocked else "ready", summary)
    assert [command["id"] for command in check["commands"]] == (
        ["python.bootstrap-agent-tools", "readiness.rerun-develop"] if blocked else []
    )
    assert all(command[0] == "git" for command in fake.commands)  # no probe ran the environment


def test_unpinned_runner_blocks_the_shared_environment_check(checkout, capsys):
    """The failure is reported on the environment check, not as a missing source marker."""
    _write_project(checkout, ["pytest>=1"], ["ruff==1"])
    check = _report(capsys, "--phase", "develop")["by_id"][ENVIRONMENT]
    assert check["status"] == "blocked"
    assert "pytest" in check["summary"]
    assert check["commands"] == []


@pytest.mark.timeout(240)  # One real venv build.
@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleaned", "in-use"])
def test_failed_build_leaves_neither_tree_nor_receipt(
    offline_bootstrap, tmp_path, monkeypatch, cleanup_fails
):
    next(tmp_path.glob("*.whl")).unlink()  # the real `pip install` now fails
    environment = tmp_path / "fp"
    if not cleanup_fails:
        _write_receipt(environment, _receipt(schema_version=1))  # a stale tree to replace
    if cleanup_fails:

        def rmtree_in_use(path):
            raise PermissionError(13, "in use", str(path))

        monkeypatch.setattr(offline_bootstrap.shutil, "rmtree", rmtree_in_use)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        offline_bootstrap.main(["--environment", str(environment), "--fingerprint", "fp"])
    assert not (environment / "receipt.json").exists()
    if cleanup_fails:
        assert "partial agent-tools tree" in raised.value.__notes__[0]
    else:
        assert sorted(path.name for path in tmp_path.iterdir()) == [".fp.lock"]


def test_waiting_bootstrap_reuses_the_published_environment(
    offline_bootstrap, monkeypatch, tmp_path
):
    acquire, contended = file_lock.acquire_file_lock, threading.Event()

    def observed_acquire(handle, **kwargs):
        try:
            acquire(handle, **kwargs)
        except file_lock.LockContentionError:
            contended.set()
            raise

    # Patched only to observe contention: no public event says the waiter is blocked on the lock.
    monkeypatch.setattr(file_lock, "acquire_file_lock", observed_acquire)
    argv = ["--environment", str(tmp_path / "fp"), "--fingerprint", "fp"]
    lock = tmp_path / ".fp.lock"
    with ThreadPoolExecutor() as pool, lock.open("a+", encoding="utf-8") as first:
        acquire(first)
        second = pool.submit(offline_bootstrap.main, argv)
        assert contended.wait(hang_guard_s())
        _write_receipt(tmp_path / "fp")
        published = (tmp_path / "fp" / "receipt.json").read_text(encoding="utf-8")
        file_lock.release_file_lock(first)
        assert second.result(2 * hang_guard_s()) == 0
    # A rebuild would have rewritten the receipt.
    assert (tmp_path / "fp" / "receipt.json").read_text(encoding="utf-8") == published


def _console_script_wheel(directory: Path) -> Path:
    """Write an offline wheel whose only content is a ``demo-tool`` console script."""
    wheel = directory / "demo_tool-1.0-py3-none-any.whl"
    files = {
        "demo_tool.py": "def main():\n    print('demo-tool ran')\n",
        "demo_tool-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: demo-tool\nVersion: 1.0\n",
        "demo_tool-1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "demo_tool-1.0.dist-info/entry_points.txt": "[console_scripts]\ndemo-tool = demo_tool:main\n",
    }
    record = "".join(f"{name},,\n" for name in files) + "demo_tool-1.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        archive.writestr("demo_tool-1.0.dist-info/RECORD", record)
    return wheel


def _run_console_script(environment: Path) -> subprocess.CompletedProcess[str]:
    """Start ``demo-tool`` through its installed launcher, as PATH lookups do.

    A dangling direct shebang makes ``execve`` itself fail with ENOENT, while a
    ``/bin/sh`` trampoline (used for long paths) exits 127; both map to 127.
    """
    name = "demo-tool.exe" if sys.platform == "win32" else "demo-tool"
    launcher = str(readiness.venv_python(environment).with_name(name))
    try:
        return run_bounded_child([launcher], timeout=2 * hang_guard_s())
    except OSError as error:
        return subprocess.CompletedProcess((launcher,), 127, "", str(error))


@pytest.fixture
def offline_bootstrap(monkeypatch, tmp_path):
    """Make the bootstrap install one offline console-script wheel and no pins."""
    pytest.importorskip("ensurepip")
    wheel = _console_script_wheel(tmp_path)
    monkeypatch.setattr(readiness, "runner_pins", lambda _root: {})
    monkeypatch.setattr(
        readiness,
        "dependency_argv",
        lambda _root: ("install", "--no-index", "--disable-pip-version-check", str(wheel)),
    )
    return _bootstrap_module()


# Real venv + offline pip install. No Windows CI duration observed yet: these
# take 2-5 s on Linux; the budget assumes a Windows venv build is ~10x slower
# (tests/timeout_headroom.py sizing rule).
@pytest.mark.timeout(240)  # Two real venv builds.
def test_schema_one_receipt_is_rebuilt_with_working_console_scripts(offline_bootstrap, tmp_path):
    """Launchers once named a deleted staging interpreter (exit 127); schema 2 retires them."""
    environment = tmp_path / "agent-tools" / "fingerprint"
    receipt = environment / "receipt.json"
    argv = ["--environment", str(environment), "--fingerprint", "fingerprint"]
    assert offline_bootstrap.main(argv) == 0
    old = {**json.loads(receipt.read_text(encoding="utf-8")), "schema_version": 1}
    receipt.write_text(json.dumps(old), encoding="utf-8")
    (environment / "stale").touch()
    assert offline_bootstrap.main(argv) == 0

    result = _run_console_script(environment)
    assert (result.returncode, result.stdout.strip()) == (0, "demo-tool ran"), result.stderr
    assert not (environment / "stale").exists()
    assert readiness.validate_environment(
        environment, "fingerprint", readiness.venv_python(environment), {}
    )[0]
