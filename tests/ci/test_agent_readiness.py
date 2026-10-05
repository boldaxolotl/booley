"""Contracts for the read-only Agent Readiness Check."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert quality["ruff"] == "ruff==0.16.10"


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


def test_invalid_receipts_are_rejected_without_attribute_errors(tmp_path):
    module = _bootstrap_module()
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").touch()
    for value in ("null", "[]"):
        (environment / "receipt.json").write_text(value, encoding="utf-8")
        assert not module._valid_receipt(environment, "fingerprint", readiness.PINNED_RUNNERS)


def test_receipt_runner_pins_are_checked_before_reusing_environment(tmp_path):
    module = _bootstrap_module()
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").touch()
    installed = dict(readiness.PINNED_RUNNERS)
    installed["ruff"] = "0.0.0"
    (environment / "receipt.json").write_text(
        json.dumps({"schema_version": 1, "fingerprint": "fingerprint", "installed": installed}),
        encoding="utf-8",
    )
    assert not module._valid_receipt(environment, "fingerprint", readiness.PINNED_RUNNERS)


def test_readiness_rejects_nonobject_receipts(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "receipt.json").write_text("null", encoding="utf-8")

    valid, reason = readiness.validate_environment(
        environment, "fingerprint", environment / "bin/python"
    )

    assert not valid
    assert "receipt" in reason


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


def test_failed_rebuild_restores_the_previous_environment(monkeypatch, tmp_path):
    module = _bootstrap_module()
    destination = tmp_path / "environment"
    destination.mkdir()
    (destination / "previous").write_text("previous", encoding="utf-8")

    def build(environment, *_args):
        environment.mkdir()
        (environment / "partial").write_text("partial", encoding="utf-8")
        raise RuntimeError("pip failed")

    monkeypatch.setattr(module, "_build", build)
    with pytest.raises(RuntimeError, match="pip failed"):
        module._rebuild(destination, tmp_path, "fingerprint", {})

    assert (destination / "previous").read_text(encoding="utf-8") == "previous"
    assert not (destination / "partial").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["environment"]


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
@pytest.mark.timeout(240)
def test_published_environment_console_scripts_run(offline_bootstrap, tmp_path):
    """Regression: launchers once named a deleted staging interpreter (exit 127)."""
    environment = tmp_path / "agent-tools" / "fingerprint"

    argv = ["--environment", str(environment), "--fingerprint", "fingerprint"]
    assert offline_bootstrap.main(argv) == 0

    result = _run_console_script(environment)
    assert (result.returncode, result.stdout.strip()) == (0, "demo-tool ran"), result.stderr
    assert offline_bootstrap._valid_receipt(environment, "fingerprint", {})
    assert sorted(path.name for path in environment.parent.iterdir()) == ["fingerprint"]


@pytest.mark.timeout(240)  # Two real venv builds; see the budget note above.
def test_relocated_environment_is_rejected_and_rebuilt(offline_bootstrap, tmp_path):
    """An environment renamed into place by the old bootstrap self-heals."""
    tools = tmp_path / "agent-tools"
    staging = tools / ".fingerprint.staging-old"
    environment = tools / "fingerprint"
    offline_bootstrap._build(staging, Path(__file__).parents[2], "fingerprint", {})
    staging.replace(environment)
    # POSIX exits 127; a Windows .exe launcher reports its own non-zero code.
    assert _run_console_script(environment).returncode != 0

    assert not offline_bootstrap._valid_receipt(environment, "fingerprint", {})
    valid, reason = readiness.validate_environment(
        environment, "fingerprint", readiness.venv_python(environment)
    )
    assert (valid, reason) == (False, "shared tools environment console scripts cannot start")

    argv = ["--environment", str(environment), "--fingerprint", "fingerprint"]
    assert offline_bootstrap.main(argv) == 0
    assert _run_console_script(environment).returncode == 0
    assert readiness.validate_environment(
        environment, "fingerprint", readiness.venv_python(environment)
    )[0]


def test_failed_rebuild_keeps_backup_when_partial_tree_survives(monkeypatch, tmp_path):
    module = _bootstrap_module()
    destination = tmp_path / "environment"
    destination.mkdir()
    (destination / "previous").write_text("previous", encoding="utf-8")
    build_error = RuntimeError("pip failed")

    def build(environment, *_args):
        environment.mkdir()
        (environment / "removable").write_text("x", encoding="utf-8")
        (environment / "locked").write_text("x", encoding="utf-8")
        raise build_error

    def rmtree_leaving_residue(path, ignore_errors=False):
        (Path(path) / "removable").unlink()
        if not ignore_errors:
            raise PermissionError(13, "in use", str(Path(path) / "locked"))

    monkeypatch.setattr(module, "_build", build)
    monkeypatch.setattr(module.shutil, "rmtree", rmtree_leaving_residue)
    with pytest.raises(RuntimeError) as raised:
        module._rebuild(destination, tmp_path, "fingerprint", {})

    (backup,) = tmp_path.glob(".environment.backup-*")
    assert raised.value.__cause__ is build_error
    assert str(destination) in str(raised.value)
    assert str(backup) in str(raised.value)
    assert (backup / "previous").read_text(encoding="utf-8") == "previous"
    assert sorted(path.name for path in destination.iterdir()) == ["locked"]


def test_orphan_sweep_removes_only_this_environments_set_aside_trees(tmp_path):
    module = _bootstrap_module()
    token = "0123456789abcdef" * 2
    orphans = [f".fp.backup-{token}", f".fp.staging-4242-{token}"]
    survivors = [
        "fp",
        ".fp.lock",
        ".fp.backup-short",
        f".fp.backup-{token}x",
        f".fp2.backup-{token}",
        f".fpx.staging-1-{token}",
        f"fp.backup-{token}",
    ]
    for name in orphans + survivors:
        (tmp_path / name).mkdir()
        (tmp_path / name / "content").write_text("x", encoding="utf-8")

    module._sweep_orphans(tmp_path / "fp")

    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(survivors)


def test_lock_waiter_outlasts_a_live_owners_normal_build(monkeypatch, tmp_path):
    """A second bootstrapper waits for a full build instead of failing at 60 s."""
    module = _bootstrap_module()
    worst_build = (
        module.VENV_TIMEOUT_SECONDS + module.PIP_TIMEOUT_SECONDS + 3 * module.PROBE_TIMEOUT_SECONDS
    )
    assert worst_build < module.LOCK_WAIT_SECONDS
    lock = tmp_path / ".fp.lock"
    # The current PID is always treated as a live owner.
    lock.write_text(json.dumps({"pid": os.getpid(), "created": 0}), encoding="utf-8")
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += max(seconds, 10.0)
        if clock["now"] >= worst_build - 10:
            lock.unlink(missing_ok=True)

    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep, time=lambda: 0.0),
    )
    module._acquire(lock)

    assert clock["now"] >= worst_build - 10
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_lock_waiter_reclaims_a_dead_owners_lock(tmp_path):
    module = _bootstrap_module()
    lock = tmp_path / ".fp.lock"
    dead = subprocess.run(
        (sys.executable, "-c", "import os; print(os.getpid())"),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    lock.write_text(json.dumps({"pid": int(dead.stdout), "created": 0}), encoding="utf-8")

    module._acquire(lock)

    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def _fake_environment(root: Path, launcher_body: str) -> Path:
    """Create a venv-shaped tree whose ``pip`` launcher is a POSIX shell script."""
    launcher = readiness.venv_python(root).with_name("pip")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n" + launcher_body, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


@pytest.mark.skipif(sys.platform == "win32", reason="shell-script launchers are POSIX-only")
def test_console_script_problem_names_launcher_and_cause(tmp_path):
    healthy = tmp_path / "healthy"
    _fake_environment(healthy, "echo pip 1.0\n")
    assert readiness.console_script_problem(healthy) is None

    failing = tmp_path / "failing"
    launcher = _fake_environment(
        failing, "echo noise >&2\necho 'interpreter gone' >&2\nexit 127\n"
    )
    problem = readiness.console_script_problem(failing)
    assert problem == f"launcher {launcher} exited 127: noise | interpreter gone"

    slow = tmp_path / "slow"
    launcher = _fake_environment(slow, "exec sleep 30\n")
    assert readiness.console_script_problem(slow, timeout=0.2) == (
        f"launcher {launcher} did not finish within 0.2 s"
    )


def test_console_script_problem_reports_a_missing_launcher(tmp_path):
    problem = readiness.console_script_problem(tmp_path)
    assert problem is not None
    assert problem.startswith(f"launcher {readiness.venv_python(tmp_path).parent}")
    assert "could not start" in problem


def test_partial_lock_metadata_is_not_deleted_while_fresh(tmp_path):
    module = _bootstrap_module()
    lock = tmp_path / "lock"
    lock.write_text("", encoding="utf-8")

    assert not module._stale(lock)


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
    monkeypatch.setattr(readiness, "_tool_checks", lambda *_args: (ready_check,))
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
    assert readiness.agent_tools_root() == tmp_path / "local" / "Booley" / "agent-tools"
    assert readiness.venv_python(tmp_path) == tmp_path / "Scripts/python.exe"


def test_validate_environment_rejects_each_stale_condition(monkeypatch, tmp_path):
    python = tmp_path / "bin/python"
    python.parent.mkdir()
    python.touch()
    receipt = {
        "schema_version": 1,
        "fingerprint": "fp",
        "installed": dict(readiness.PINNED_RUNNERS),
    }
    (tmp_path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(readiness, "_installed_versions", lambda _python: receipt["installed"])
    monkeypatch.setattr(
        readiness, "_run_child", lambda *_args: subprocess.CompletedProcess([], 0, "", "")
    )
    monkeypatch.setattr(readiness, "console_script_problem", lambda _venv: None)
    assert readiness.validate_environment(tmp_path, "fp", python)[0]
    for installed in (None, {}, {"ruff": "bad"}):
        monkeypatch.setattr(
            readiness, "_installed_versions", lambda _python, value=installed: value
        )
        assert not readiness.validate_environment(tmp_path, "fp", python)[0]
    monkeypatch.setattr(readiness, "_installed_versions", lambda _python: receipt["installed"])
    monkeypatch.setattr(
        readiness, "_run_child", lambda *_args: subprocess.CompletedProcess([], 1, "", "")
    )
    assert "pip check" in readiness.validate_environment(tmp_path, "fp", python)[1]


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


def test_remaining_readiness_boundaries(monkeypatch, tmp_path):  # noqa: PLR0915
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
    monkeypatch.setattr(readiness, "_git_text", lambda *_args: None)
    monkeypatch.setattr(readiness, "validate_environment", lambda *_args: (True, "valid"))
    monkeypatch.setattr(readiness, "agent_tools_root", lambda: tmp_path)
    monkeypatch.setattr(readiness, "venv_python", lambda _root: tmp_path / "python")
    source_root = Path(__file__).parents[2]
    check, commands, _python = readiness._environment_check(source_root)
    assert check.status is readiness.Status.READY and not commands

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

    assert not readiness._runner_versions([])
    assert readiness._installed_versions(tmp_path / "missing") is None
    assert (
        readiness._tool_checks(tmp_path / "missing", tmp_path)[0].status
        is readiness.Status.BLOCKED
    )
    monkeypatch.setattr(readiness, "_installed_versions", lambda _python: None)
    assert (
        readiness._runner_check(tmp_path / "python", tmp_path).status is readiness.Status.BLOCKED
    )
    monkeypatch.setattr(readiness, "agent_tools_root", lambda: tmp_path / "inside")
    monkeypatch.setattr(readiness, "_within", lambda *_args: True)
    assert readiness._cache_path_check(tmp_path).status is readiness.Status.BLOCKED
    monkeypatch.setattr(readiness, "_run_process", lambda *_args, **_kwargs: None)
    assert readiness._run_child(tmp_path / "python", (), tmp_path) is None
    monkeypatch.setattr(
        readiness.Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    assert not readiness._is_linked_worktree(tmp_path)


def test_validate_environment_rejects_invalid_json(tmp_path):
    (tmp_path / "receipt.json").write_text("[", encoding="utf-8")
    valid, reason = readiness.validate_environment(tmp_path, "fp", tmp_path / "python")
    assert not valid
    assert "valid receipt" in reason


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
        readiness, "_run_child", lambda *_args: subprocess.CompletedProcess([], 0, "[]", "")
    )
    assert readiness._installed_versions(tmp_path / "python") is None
    monkeypatch.setattr(
        readiness, "_run_child", lambda *_args: subprocess.CompletedProcess([], 0, "not-json", "")
    )
    assert readiness._installed_versions(tmp_path / "python") is None
    python = tmp_path / "python"
    python.touch()
    assert readiness._tool_checks(python, tmp_path)[0].status is readiness.Status.BLOCKED
    monkeypatch.setattr(
        readiness.Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    gitfile = tmp_path / ".git"
    gitfile.write_text("gitdir", encoding="utf-8")
    assert not readiness._is_linked_worktree(tmp_path)
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    assert readiness.agent_tools_root().name == "agent-tools"


def test_validate_environment_reports_runner_pin_mismatch(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.touch()
    installed = dict(readiness.PINNED_RUNNERS)
    installed["ruff"] = "old"
    (tmp_path / "receipt.json").write_text(
        json.dumps({"schema_version": 1, "fingerprint": "fp", "installed": installed}),
        encoding="utf-8",
    )
    valid, reason = readiness.validate_environment(tmp_path, "fp", python)
    assert not valid
    assert "runner pins" in reason
