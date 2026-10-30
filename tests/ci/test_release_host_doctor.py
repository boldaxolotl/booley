from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import host_doctor

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="host release validation requires POSIX identity and executables",
)


def _fake_booley(root: Path, *, init_output: str = "[OK] initialized") -> tuple[Path, Path]:
    log = root / "commands.jsonl"
    executable = root / "bin" / "booley"
    executable.parent.mkdir()
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'bootstrap':\n"
        "    before = subprocess.run(['code', '--list-extensions'], check=True, "
        "capture_output=True, text=True)\n"
        "    assert 'ms-vscode-remote.remote-containers' not in before.stdout\n"
        "    subprocess.run(['code', '--install-extension', "
        "'ms-vscode-remote.remote-containers', '--force'], check=True)\n"
        "    after = subprocess.run(['code', '--list-extensions'], check=True, "
        "capture_output=True, text=True)\n"
        "    assert 'ms-vscode-remote.remote-containers' in after.stdout\n"
        "elif sys.argv[1] == 'init':\n"
        "    import shutil\n"
        "    agent = shutil.which('codex')\n"
        "    assert agent == os.path.join(os.environ['HOME'], 'bin', 'codex'), agent\n"
        "    subprocess.run([agent, '--version'], check=True)\n"
        f"    print({init_output!r})\n"
        "elif sys.argv[1] == 'doctor':\n"
        "    print('MCP server exposes 17 MCP tool(s)')\n"
        "    print('  ✗ failed     0')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


@pytest.mark.parametrize("scenario", ["clean", "failing", "colored"])
def test_no_failure_check_reads_real_doctor_counts(
    scenario: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import colors, doctor

    monkeypatch.setattr(colors, "COLORS_ENABLED", scenario == "colored")
    reporter = doctor._Reporter.create()
    reporter.pass_("fine")
    if scenario == "failing":
        reporter.fail_("broken")
    reporter.finish()

    output = capsys.readouterr().out
    assert host_doctor.doctor_reports_no_failures(output) is (scenario != "failing")


def test_host_doctor_isolates_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = tmp_path / "ambient-config"
    home = tmp_path / "home"
    executable = home / ".local" / "bin" / "booley"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(ambient))

    env = host_doctor._environment(home, executable)

    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")


def test_host_doctor_uses_isolated_paths_and_records_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    booley, log = _fake_booley(tmp_path)
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.setenv("COMMAND_LOG", str(log))

    evidence = host_doctor.validate(
        allowed_root=tmp_path,
        project=project,
        home=home,
        booley=booley,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        candidate_sha="candidate-sha",
        image_digest="sha256:candidate",
    )

    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert commands == [
        ["bootstrap"],
        [
            "init",
            "--scaffold",
            "release_host_doctor",
            "--sim-eda-tool",
            "verilator",
            "--tb-style",
            "sv",
            "--lint-eda-tool",
            "verilator",
            "--no-asic",
            "--provider",
            "codex",
            "--auth",
            "auto",
            "--skip-credentials",
        ],
        ["doctor"],
    ]
    assert (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "true"
    )
    assert evidence["schema"] == 1
    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:candidate",
    }
    assert evidence["identity"] == {"uid": os.getuid(), "gid": os.getgid()}
    assert evidence["checks"][-1] == {
        "id": "host-doctor.plain-issued-image",
        "status": "pass",
    }
    assert evidence["cleanup"] == {
        "editor_probe_removed": True,
        "agent_cli_probe_removed": True,
        "editor_marker_removed": True,
    }
    assert not (home / "bin" / "code").exists()
    assert not (home / "bin" / "codex").exists()
    assert not (home / ".booley-ci-dev-containers").exists()


def test_host_doctor_rejects_project_outside_isolated_root(tmp_path: Path) -> None:
    booley, _log = _fake_booley(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(ValueError, match="project must be inside allowed root"):
        host_doctor.validate(
            allowed_root=tmp_path,
            project=tmp_path.parent,
            home=home,
            booley=booley,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            candidate_sha="candidate-sha",
            image_digest="sha256:candidate",
        )


def test_host_doctor_names_the_initialization_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning fails the gate, and the error carries the offending line itself."""
    booley, log = _fake_booley(tmp_path, init_output="[!!] host-agent-cli: something drifted")
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.setenv("COMMAND_LOG", str(log))

    with pytest.raises(RuntimeError, match=r"\[!!\] host-agent-cli: something drifted"):
        host_doctor.validate(
            allowed_root=tmp_path,
            project=project,
            home=home,
            booley=booley,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            candidate_sha="candidate-sha",
            image_digest="sha256:candidate",
        )
    # Both stubs are removed even when the gate fails.
    assert not (home / "bin" / "code").exists()
    assert not (home / "bin" / "codex").exists()


def test_host_doctor_keeps_a_preexisting_agent_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``codex`` this run did not write is refused and left in place."""
    booley, log = _fake_booley(tmp_path)
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    (home / "bin").mkdir(parents=True)
    existing = home / "bin" / "codex"
    existing.write_text("not ours", encoding="utf-8")
    monkeypatch.setenv("COMMAND_LOG", str(log))

    with pytest.raises(ValueError, match="codex probe path already exists"):
        host_doctor.validate(
            allowed_root=tmp_path,
            project=project,
            home=home,
            booley=booley,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            candidate_sha="candidate-sha",
            image_digest="sha256:candidate",
        )
    assert existing.read_text(encoding="utf-8") == "not ours"
    assert not (home / "bin" / "code").exists()


def test_no_failure_check_uses_the_closing_counts_row() -> None:
    echoed = "  ✗ failed     3\ntool log\n  ✗ failed     0\n"

    assert host_doctor.doctor_reports_no_failures(echoed) is True
    assert host_doctor.doctor_reports_no_failures("  ✗ failed     0\n  ✗ failed     2\n") is False
