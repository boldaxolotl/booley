"""Import isolation of the Job slot store for spawned workers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_job_slot_import_does_not_require_fusesoc_api(tmp_path: Path) -> None:
    """Spawned slot workers must not load acceptance repository adapters."""
    fake_fusesoc = tmp_path / "fusesoc"
    fake_fusesoc.mkdir()
    (fake_fusesoc / "__init__.py").write_text("", encoding="utf-8")
    source_root = Path(__file__).parents[2] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(source_root)))

    result = subprocess.run(
        [sys.executable, "-c", "import booley.runtime.job_slots"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
