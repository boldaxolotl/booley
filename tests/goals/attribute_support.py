import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def system_attributes(tmp_path, monkeypatch, isolated_git_attributes):
    """Each test owns its relocated Git's actual system attributes file."""
    executable = os.environ.get("BOOLEY_TEST_RELOCATABLE_GIT") or shutil.which("git")
    assert executable is not None
    binary = tmp_path / ("git-prefix/bin/git.exe" if os.name == "nt" else "git-prefix/bin/git")
    binary.parent.mkdir(parents=True)
    shutil.copy2(executable, binary)
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    result = subprocess.run(
        [str(binary), "var", "GIT_ATTR_SYSTEM"], capture_output=True, check=False, timeout=60
    )
    missing = pytest.fail if os.environ.get("BOOLEY_TEST_REQUIRE_RELOCATABLE_GIT") else pytest.skip
    if result.returncode:
        missing("a runnable relocatable Git is required for the real system-file oracle")
    path = Path(os.fsdecode(result.stdout).strip())
    if not path.is_absolute() or not path.resolve().is_relative_to(binary.parent.parent.resolve()):
        missing("Git has a fixed system prefix; use BOOLEY_TEST_RELOCATABLE_GIT")
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "git-template/info").mkdir(parents=True)
    (tmp_path / "git-template/info/exclude").write_bytes(b"")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "git-template"))
    return path
