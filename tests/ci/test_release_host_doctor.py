from __future__ import annotations

import json
import os
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


def _fake_booley(root: Path) -> tuple[Path, Path]:
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
        "    print('[OK] initialized')\n"
        "elif sys.argv[1] == 'doctor':\n"
        "    print('MCP server exposes 17 MCP tool(s)')\n"
        "    print('0 failed.')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


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
        ["init", "--skip-credentials"],
        ["doctor", "--deep", "--skip-agent-checks"],
    ]
    assert evidence["schema"] == 1
    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:candidate",
    }
    assert evidence["identity"] == {"uid": os.getuid(), "gid": os.getgid()}
    assert evidence["checks"][-1] == {
        "id": "host-doctor.deep-issued-image",
        "status": "pass",
    }
    assert evidence["cleanup"] == {
        "editor_probe_removed": True,
        "editor_marker_removed": True,
    }
    assert not (home / "bin" / "code").exists()
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
