"""Contracts for the read-only Agent Readiness Check."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import tomllib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from booley.core import file_lock
from booley.core.host_probes import GithubProbe, HostProbe, ProbeState, probe_docker, probe_github
from booley.dev_support import agent_readiness as readiness


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
    assert readiness.aggregate_status(checks) is readiness.Status.ESCALATION
    assert readiness.exit_code(readiness.Status.ESCALATION) == 0
    assert readiness.exit_code(readiness.Status.BLOCKED) == 1


def test_verification_commands_are_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setattr(readiness, "agent_tools_root", lambda: tmp_path / "cache" / "agent-tools")
    python = str(tmp_path / "bin/python")
    commands = readiness._verification_commands(tmp_path / "bin/python", tmp_path)
    assert [command.id for command in commands] == [
        "ruff.agent-gate",
        "ruff.ci-check",
        "ruff.ci-format",
        "pytest.broad",
    ]
    assert commands[0].argv == (python, "-m", "ruff", "check", "src/", "tests/")
    assert commands[0].required_at == "before-commit"
    assert commands[-1].argv == (
        python,
        "-m",
        "pytest",
        "tests/",
        "--test-profile",
        "python",
        "-n",
        "auto",
        "--maxprocesses=8",
        "--dist=loadscope",
        f"--basetemp={readiness.pytest_basetemp(tmp_path)}",
    )
    assert commands[-1].required_at == "optional-broad-verification"


def test_broad_pytest_keeps_default_basetemp_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "win32")
    commands = readiness._verification_commands(tmp_path / "python.exe", tmp_path)
    assert commands[-1].argv[-4:] == ("-n", "auto", "--maxprocesses=8", "--dist=loadscope")


def test_pytest_basetemp_is_per_checkout_and_outside_it(tmp_path, monkeypatch):
    cache = tmp_path / "cache" / "agent-tools"
    monkeypatch.setattr(readiness, "agent_tools_root", lambda: cache)
    first = readiness.pytest_basetemp(tmp_path / "first")
    second = readiness.pytest_basetemp(tmp_path / "second")
    assert first != second
    assert first.parent == cache
    assert first.name.startswith("pytest-basetemp-")
    assert first == readiness.pytest_basetemp(tmp_path / "first")
    assert not first.is_relative_to(tmp_path / "first")


def test_project_runner_pins_match_the_readiness_contract():
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    for extra in ("test", "dev"):
        declared = {item.split("==", 1)[0]: item for item in extras[extra]}
        runners = {
            name: version for name, version in readiness.PINNED_RUNNERS.items() if name != "ruff"
        }
        for name, version in runners.items():
            assert declared.get(name) == f"{name}=={version}"
    quality = {item.split("==", 1)[0]: item for item in extras["quality"]}
    assert quality["ruff"] == f"ruff=={readiness.PINNED_RUNNERS['ruff']}"


def test_bootstrap_dependencies_never_install_booley():
    arguments = readiness.dependency_argv(Path(__file__).parents[2])
    assert not any(
        item.startswith("-e") or item in {".", "booley", "booley-rtl"} for item in arguments
    )


def test_default_docker_check_is_passive(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(readiness, "_run_process", lambda *args, **kwargs: calls.append(args))
    check = readiness._docker_check(False)
    assert check.status is readiness.Status.READY
    assert not calls


def test_escalation_is_visible_even_when_exit_is_zero():
    check = readiness.Check(
        "git.write-policy", readiness.Status.ESCALATION, False, "approval required"
    )
    result = readiness.Result(
        "prepare",
        readiness.Status.ESCALATION,
        readiness.Repository("/tmp/x", "codex/x", "primary"),
        (check,),
        (),
    )
    human = readiness.render_human(result)
    assert "ESCALATION-REQUIRED" in human
    assert "exit zero is not permission" in human
    assert result.as_dict()["status"] == "escalation-required"


def test_real_launcher_emits_one_versioned_json_document(tmp_path):
    launcher = Path(__file__).parents[2] / ".github/scripts/agent_readiness.py"
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "--phase",
            "prepare",
            "--branch",
            "codex/contract-test",
            "--json",
        ],
        cwd=launcher.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode in {0, 1}
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["phase"] == "prepare"
    assert not result.stderr


def test_neutral_docker_probe_does_not_disclose_output():
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="secret", stderr="permission denied")

    observation = probe_docker(which=lambda _: "/docker", run=run)
    assert observation.state is ProbeState.PERMISSION
    assert not hasattr(observation, "stdout")


def test_neutral_github_probes_auth_and_connectivity_independently():
    seen = []

    def run(args, **kwargs):
        seen.append(tuple(args[1:]))
        return subprocess.CompletedProcess(
            args, 0 if args[1] == "auth" else 1, stdout="", stderr=""
        )

    observation = probe_github(which=lambda _: "/gh", run=run)
    assert observation.authentication is ProbeState.HEALTHY
    assert observation.connectivity is ProbeState.FAILED
    assert seen == [("auth", "status"), ("repo", "view", "--json", "nameWithOwner")]


@pytest.mark.parametrize("value", ["bad", "allowed-now"])
def test_git_write_policy_is_closed(monkeypatch, value):
    monkeypatch.setenv("BOOLEY_AGENT_GIT_WRITE_POLICY", value)
    with pytest.raises(readiness.ReadinessUsageError):
        readiness._prepare(Path.cwd(), "codex/x", None)


RECEIPT = {"schema_version": 2, "fingerprint": "fp", "installed": readiness.PINNED_RUNNERS}
STALE = "shared tools environment receipt or interpreter is stale"
PINS = "shared tools environment has mismatched Ruff/Pytest runner pins"


def _publish(environment: Path, receipt: object = RECEIPT) -> None:
    """Publish *receipt* (raw JSON text when a string) beside a venv interpreter."""
    python = readiness.venv_python(environment)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    text = receipt if isinstance(receipt, str) else json.dumps(receipt)
    (environment / "receipt.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("receipt", "summary"),
    [
        (RECEIPT, "shared tools environment matches the repository fingerprint"),
        ("[", "shared tools environment is missing or has no valid receipt"),
        ("null", STALE),
        ({**RECEIPT, "schema_version": 1}, STALE),
        ({**RECEIPT, "fingerprint": "other"}, STALE),
        ({**RECEIPT, "installed": None}, PINS),
        ({**RECEIPT, "installed": {**readiness.PINNED_RUNNERS, "ruff": "0.0.0"}}, PINS),
    ],
    ids=["valid", "unparsable", "null", "schema-1", "fingerprint", "no-pins", "wrong-pin"],
)
def test_develop_trusts_the_receipt_without_probing(monkeypatch, tmp_path, receipt, summary):
    monkeypatch.setattr(readiness, "dependency_fingerprint", lambda _root: "fp")
    monkeypatch.setattr(readiness, "agent_tools_root", lambda: tmp_path)
    monkeypatch.setattr(readiness.subprocess, "run", lambda *_a, **_k: pytest.fail("probed"))
    _publish(tmp_path / "fp", receipt)
    check, commands, _python = readiness._environment_check(tmp_path)
    blocked = receipt is not RECEIPT
    assert (check.id, check.summary) == ("python.shared-environment", summary)
    assert check.status is (readiness.Status.BLOCKED if blocked else readiness.Status.READY)
    assert [command.id for command in commands] == (
        ["python.bootstrap-agent-tools", "readiness.rerun-develop"] if blocked else []
    )


def test_invalid_environment_is_rebuilt_in_place(monkeypatch, tmp_path):
    module = _bootstrap_module()
    destination = tmp_path / "environment"
    destination.mkdir()
    (destination / "stale").write_text("stale", encoding="utf-8")
    built_at = []

    def build(environment, *_args):
        built_at.append(environment)
        environment.mkdir()
        (environment / "ready").write_text("ready", encoding="utf-8")

    monkeypatch.setattr(module, "_build", build)
    module._rebuild(destination, tmp_path, "fingerprint", {})

    assert built_at == [destination]
    assert (destination / "ready").read_text(encoding="utf-8") == "ready"
    assert not (destination / "stale").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["environment"]


def test_failed_build_leaves_neither_tree_nor_receipt(monkeypatch, tmp_path):
    module = _bootstrap_module()
    environment = tmp_path / "fp"
    _publish(environment, {**RECEIPT, "schema_version": 1})
    error = RuntimeError("pip failed")

    def build(environment, *_args):
        readiness.venv_python(environment).parent.mkdir(parents=True)
        raise error

    def rmtree_in_use(path):
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(module, "_build", build)
    with pytest.raises(RuntimeError) as raised:
        module.main(["--environment", str(environment), "--fingerprint", "fp"])
    assert raised.value is error
    assert sorted(path.name for path in tmp_path.iterdir()) == [".fp.lock"]

    monkeypatch.setattr(module.shutil, "rmtree", rmtree_in_use)
    with pytest.raises(RuntimeError) as raised:
        module._rebuild(environment, tmp_path, "fp", {})
    assert raised.value is error
    assert "partial agent-tools tree" in raised.value.__notes__[0]


def test_waiting_bootstrap_reuses_the_published_environment(monkeypatch, tmp_path):
    module = _bootstrap_module()
    acquire, contended = file_lock.acquire_file_lock, threading.Event()

    def observed_acquire(handle, **kwargs):
        try:
            acquire(handle, **kwargs)
        except file_lock.LockContentionError:
            contended.set()
            raise

    monkeypatch.setattr(file_lock, "acquire_file_lock", observed_acquire)
    monkeypatch.setattr(module, "_build", lambda *_args: pytest.fail("rebuilt"))
    argv = ["--environment", str(tmp_path / "fp"), "--fingerprint", "fp"]
    lock = tmp_path / ".fp.lock"
    with ThreadPoolExecutor() as pool, lock.open("a+", encoding="utf-8") as first:
        acquire(first)
        second = pool.submit(module.main, argv)
        assert contended.wait(30)
        _publish(tmp_path / "fp")
        file_lock.release_file_lock(first)
        assert second.result(60) == 0


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
        return subprocess.run((launcher,), capture_output=True, text=True, check=False, timeout=60)
    except OSError as error:
        return subprocess.CompletedProcess((launcher,), 127, "", str(error))


@pytest.fixture
def offline_bootstrap(monkeypatch, tmp_path):
    """Make the bootstrap install one offline console-script wheel and no pins."""
    pytest.importorskip("ensurepip")
    wheel = _console_script_wheel(tmp_path)
    monkeypatch.setattr(readiness, "PINNED_RUNNERS", {})
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
        environment, "fingerprint", readiness.venv_python(environment)
    )[0]


def test_models_and_renderers_serialize_nested_commands():
    command = readiness.Command("check", ("python", "-V"), "/repo", "inspect", "now")
    check = readiness.Check(
        "example", readiness.Status.READY, True, "x" * 300, write_probed=True, commands=(command,)
    )
    result = readiness.Result(
        "develop",
        readiness.Status.READY,
        readiness.Repository("/repo", "codex/example", "linked"),
        (check,),
        (command,),
    )

    assert command.as_dict()["argv"] == ["python", "-V"]
    assert len(check.as_dict()["summary"]) == 240
    assert result.as_dict()["repository"]["branch"] == "codex/example"
    assert "Run [now] check" in readiness.render_human(result)
    assert readiness._argv(("hello world", "--flag")) == "'hello world' --flag"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--phase", "prepare"], "--branch is required"),
        (["--phase", "publish", "--branch", "codex/x"], "only valid"),
        (["--phase", "publish", "--require", "docker"], "only valid"),
        (["--phase", "prepare", "--branch", "bad"], "new local"),
    ],
)
def test_parse_args_rejects_invalid_phase_combinations(argv, message, capsys):
    with pytest.raises(SystemExit):
        readiness._parse_args(argv)
    assert message in capsys.readouterr().err


def test_main_renders_usage_error_as_json(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert readiness.main(["--phase", "publish", "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "blocked"
    assert document["checks"][0]["id"] == "repository.source-marker"


def test_find_source_checkout_handles_markers_and_bad_toml(tmp_path):
    root = tmp_path / "checkout"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n", encoding="utf-8"
    )
    assert readiness.find_source_checkout(nested) == root

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "pyproject.toml").write_text("[", encoding="utf-8")
    with pytest.raises(readiness.ReadinessUsageError, match="cannot read"):
        readiness.find_source_checkout(broken)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(readiness.ReadinessUsageError, match="not inside"):
        readiness.find_source_checkout(empty)


def test_run_phase_dispatches_each_phase(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(readiness, "find_source_checkout", lambda _path: root)
    expected = readiness.Result(
        "x", readiness.Status.READY, readiness.Repository("", None, ""), (), ()
    )
    calls = []
    monkeypatch.setattr(
        readiness, "_prepare", lambda *args: calls.append(("prepare", args)) or expected
    )
    monkeypatch.setattr(
        readiness, "_publish", lambda *args: calls.append(("publish", args)) or expected
    )
    monkeypatch.setattr(
        readiness,
        "_develop",
        lambda *args, **kwargs: calls.append(("develop", args, kwargs)) or expected,
    )

    readiness.run_phase(readiness._parse_args(["--phase", "prepare", "--branch", "codex/x"]))
    readiness.run_phase(readiness._parse_args(["--phase", "publish"]))
    readiness.run_phase(readiness._parse_args(["--phase", "develop", "--require", "docker"]))
    assert [call[0] for call in calls] == ["prepare", "publish", "develop"]


def test_prepare_reports_safe_and_unsafe_paths(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    def process(args):
        return subprocess.CompletedProcess(args, 0, str(root), "")

    monkeypatch.setattr(readiness.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(readiness, "_git", lambda _root, args: process(args))
    monkeypatch.setattr(
        readiness, "_git_text", lambda _root, args: "main" if "symbolic" in args else None
    )
    monkeypatch.setenv("BOOLEY_AGENT_GIT_WRITE_POLICY", "escalation-required")

    result = readiness._prepare(root, "codex/x", root / "outside")
    assert result.status is readiness.Status.BLOCKED
    assert any(check.id == "git.write-policy" for check in result.checks)
    assert not result.commands


def test_develop_and_publish_assemble_all_checks(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    python = tmp_path / "python"
    python.touch()
    ready_check = readiness.Check("environment", readiness.Status.READY, True, "ok")
    monkeypatch.setattr(readiness, "_is_linked_worktree", lambda _root: True)
    monkeypatch.setattr(
        readiness,
        "_git_text",
        lambda _root, args: {
            "symbolic-ref": "codex/x",
            "rev-parse": "head",
            "merge-base": "base",
            "status": "",
        }.get(args[0]),
    )
    monkeypatch.setattr(readiness, "_git_count", lambda *_args: 2)
    monkeypatch.setattr(readiness, "_environment_check", lambda _root: (ready_check, (), python))
    monkeypatch.setattr(readiness, "_cache_path_check", lambda _root: ready_check)
    monkeypatch.setattr(readiness, "_docker_check", lambda required: ready_check)
    monkeypatch.setattr(readiness, "_verification_commands", lambda *_args: ())
    monkeypatch.setattr(readiness.shutil, "which", lambda _name: "/usr/bin/git")
    developed = readiness._develop(root, require_docker=True)
    assert developed.phase == "develop"

    monkeypatch.setattr(
        readiness, "_git", lambda _root, args: subprocess.CompletedProcess(args, 0, str(root), "")
    )
    monkeypatch.setattr(
        readiness,
        "probe_github",
        lambda **_kwargs: GithubProbe("gh", ProbeState.HEALTHY, ProbeState.HEALTHY),
    )
    published = readiness._publish(root)
    assert published.phase == "publish"
    assert published.status is readiness.Status.READY


@pytest.mark.parametrize("state", list(ProbeState))
def test_docker_check_maps_all_probe_states(monkeypatch, state):
    monkeypatch.setattr(readiness, "probe_docker", lambda **_kwargs: HostProbe("docker", state))
    check = readiness._docker_check(True)
    if state is ProbeState.HEALTHY:
        assert check.status is readiness.Status.READY
    elif state is ProbeState.PERMISSION:
        assert check.status is readiness.Status.ESCALATION
    else:
        assert check.status is readiness.Status.BLOCKED


def test_environment_check_emits_bootstrap_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(readiness, "dependency_fingerprint", lambda _root: "fingerprint")
    monkeypatch.setattr(readiness, "agent_tools_root", lambda: tmp_path)
    python = tmp_path / "bin/python"
    monkeypatch.setattr(readiness, "venv_python", lambda _root: python)
    monkeypatch.setattr(readiness, "validate_environment", lambda *args: (False, "stale"))
    check, commands, shared = readiness._environment_check(tmp_path / "root")
    assert check.status is readiness.Status.BLOCKED
    assert [command.id for command in commands] == [
        "python.bootstrap-agent-tools",
        "readiness.rerun-develop",
    ]
    assert shared == python


def test_dependency_helpers_and_platform_paths(monkeypatch, tmp_path):
    root = Path(__file__).parents[2]
    monkeypatch.setattr(readiness, "_git_text", lambda *_args: None)
    assert len(readiness.dependency_fingerprint(root)) == 32
    assert readiness.dependency_argv(root)[0] == "install"
    monkeypatch.setattr(readiness.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert readiness.agent_tools_root() == tmp_path / "local" / "Booley" / "agent-tools-v2"
    monkeypatch.setattr(readiness, "RECEIPT_SCHEMA_VERSION", 1)
    assert readiness.agent_tools_root().name == "agent-tools-v1"
    assert readiness.venv_python(tmp_path) == tmp_path / "Scripts/python.exe"


def test_low_level_helpers_handle_failures(monkeypatch, tmp_path):
    failed = subprocess.CompletedProcess([], 1, "", "")
    monkeypatch.setattr(readiness, "_git", lambda *_args: failed)
    assert readiness._git_text(tmp_path, ()) is None
    assert not readiness._git_success(tmp_path, ())
    monkeypatch.setattr(readiness, "_git_text", lambda *_args: "not-an-int")
    assert readiness._git_count(tmp_path, ()) is None
    monkeypatch.setattr(
        readiness.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    assert readiness._run_process(("missing",), tmp_path) is None
    assert not readiness._successful(None)


def test_worktree_and_linked_checkout_guards(tmp_path):
    root = tmp_path / "root"
    worktrees = root / ".worktrees"
    worktrees.mkdir(parents=True)
    assert readiness._worktree_path(root, "codex/x", None) == worktrees / "x"
    assert readiness._worktree_path(root, "codex/x", root / "unsafe") is None
    assert readiness._worktree_path(root, "codex/x", worktrees / "safe") == worktrees / "safe"
    gitfile = root / ".git"
    gitfile.write_text("gitdir: /tmp/git", encoding="utf-8")
    assert readiness._is_linked_worktree(root)
    gitfile.unlink()
    gitfile.mkdir()
    assert not readiness._is_linked_worktree(root)


def test_remaining_readiness_boundaries(monkeypatch, tmp_path):
    assert readiness.aggregate_status(()) is readiness.Status.READY
    assert readiness._parse_args(["--phase", "develop"]).phase == "develop"

    marker = tmp_path / "pyproject.toml"
    marker.write_text("[tool.booley]\nsource_checkout = false\n", encoding="utf-8")
    with pytest.raises(readiness.ReadinessUsageError):
        readiness.find_source_checkout(tmp_path)

    monkeypatch.setattr(readiness, "_git_text", lambda *_args: None)
    monkeypatch.setattr(readiness, "_is_linked_worktree", lambda _root: False)
    result = readiness._result("x", tmp_path, (), (), git=None)
    assert result.repository.branch is None

    def text_factory(items):
        values_iter = iter(items)

        def git_text(*_args):
            return next(values_iter)

        return git_text

    for values in (
        (None, None, None),
        ("origin/main", "same", "same"),
        ("origin/main", "local", "remote"),
    ):
        monkeypatch.setattr(readiness, "_git_text", text_factory(values))
        check = readiness._tracking_advisory(tmp_path)
        assert check.id == "git.main-tracking"

    monkeypatch.setattr(readiness, "_within", lambda *_args: False)
    assert readiness._worktree_path(tmp_path, "codex/x", None) is None
    source_root = Path(__file__).parents[2]

    monkeypatch.setattr(readiness, "_git_text", lambda *_args: "relative/common")
    assert len(readiness.dependency_fingerprint(source_root)) == 32

    monkeypatch.setattr(readiness.sys, "platform", "win32")
    monkeypatch.setattr(
        readiness, "_project_dependencies", lambda _root: {"dependencies": ("x" * 30000,)}
    )
    with pytest.raises(RuntimeError, match="command limit"):
        readiness.dependency_argv(tmp_path)
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    assert readiness.agent_tools_root()

    monkeypatch.setattr(readiness, "agent_tools_root", lambda: tmp_path / "inside")
    monkeypatch.setattr(readiness, "_within", lambda *_args: True)
    assert readiness._cache_path_check(tmp_path).status is readiness.Status.BLOCKED
    monkeypatch.setattr(
        readiness.Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    assert not readiness._is_linked_worktree(tmp_path)


def test_probe_boundaries_and_human_main(monkeypatch, capsys, tmp_path):
    assert probe_docker(which=lambda _name: None).state is ProbeState.MISSING

    def docker_error(*_args, **_kwargs):
        raise OSError

    assert probe_docker(which=lambda _name: "docker", run=docker_error).state is ProbeState.FAILED
    assert probe_github(which=lambda _name: None).authentication is ProbeState.MISSING

    def github_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("gh", 5)

    assert (
        probe_github(which=lambda _name: "gh", run=github_timeout).authentication
        is ProbeState.TIMEOUT
    )
    monkeypatch.setattr(
        readiness,
        "run_phase",
        lambda _args: readiness.Result(
            "develop",
            readiness.Status.READY,
            readiness.Repository(str(tmp_path), None, "primary"),
            (),
            (),
        ),
    )
    assert readiness.main(["--phase", "develop"]) == 0
    assert "Agent Readiness: ready" in capsys.readouterr().out


def test_remaining_probe_and_environment_branches(monkeypatch, tmp_path):
    def github_error(*_args, **_kwargs):
        raise OSError

    observed = probe_github(which=lambda _name: "gh", run=github_error)
    assert observed.authentication is ProbeState.FAILED
    monkeypatch.setattr(
        readiness.Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    gitfile = tmp_path / ".git"
    gitfile.write_text("gitdir", encoding="utf-8")
    assert not readiness._is_linked_worktree(tmp_path)
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    assert readiness.agent_tools_root().name == "agent-tools-v2"
