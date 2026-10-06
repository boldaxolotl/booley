"""OpenROAD diagnostics survive early failure without changing its status."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime probe")
@pytest.mark.parametrize("blocked_export", [False, True])
def test_openroad_failure_exports_partial_logs_and_removes_scratch(
    tmp_path: Path, blocked_export: bool
) -> None:
    pdk = tmp_path / "pdk"
    for filename in (
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
        "nangate45/Nangate45_tech.lef",
        "nangate45/Nangate45_stdcell.lef",
        "nangate45/Nangate45.rc",
    ):
        path = pdk / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    probe = binaries / "openroad"
    probe.write_text("#!/usr/bin/env bash\necho 'probe failed with diagnostics'\nexit 17\n")
    probe.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    evidence = tmp_path / "evidence"
    if blocked_export:
        evidence.touch()
    result = subprocess.run(
        [
            "bash",
            str(_ROOT / ".github/scripts/verify_openroad_runtime.sh"),
            str(pdk),
            str(evidence),
        ],
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "TMPDIR": str(scratch)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 17, result.stdout + result.stderr
    if blocked_export:
        assert "Could not retain all OpenROAD runtime evidence" in result.stderr
    else:
        assert (evidence / "openroad-gui.log").read_text() == "probe failed with diagnostics\n"
    assert not list(scratch.iterdir())
