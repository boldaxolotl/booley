"""The EDA launcher fault leaves a merge output Booley must refuse as unsafe."""

import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults/native_tool.py"


def _fake_merge_tool(directory: Path) -> Path:
    """A stand-in `verilator_coverage` that writes one native count to its `--write` path."""
    tool = directory / "verilator_coverage"
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "out = sys.argv[sys.argv.index('--write') + 1]\n"
        "open(out, 'w').write(\"C 'point' 3\\n\")\n"
    )
    tool.chmod(0o755)
    return tool


def test_merge_unsafe_leaves_a_directory_where_the_merged_file_was(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    merged = owned / "merged.dat"
    environment = {
        **os.environ,
        "QA_REAL_EDA": str(_fake_merge_tool(tmp_path)),
        "QA_EDA_OWNED": str(owned),
        "QA_EDA_FAULT": "merge-unsafe",
    }

    subprocess.run(
        [sys.executable, str(TOOL), "--write", str(merged)], env=environment, check=True
    )

    assert merged.is_dir()
    assert (owned / "merged.dat.qa-merged").read_text() == "C 'point' 3\n"
