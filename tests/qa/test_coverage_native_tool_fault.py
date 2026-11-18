"""The EDA launcher fault leaves a merge output Booley must refuse as unsafe."""

import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults/native_tool.py"
#: Stands in for `verilator_coverage`: Python itself is the real executable, and this
#: code, forwarded first, writes one native count to the `--write` path.
_FAKE_MERGE = (
    "import sys; open(sys.argv[sys.argv.index('--write') + 1], 'w').write(\"C 'p' 3\\n\")"
)


def test_merge_unsafe_leaves_a_directory_where_the_merged_file_was(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    merged = owned / "merged.dat"
    environment = {
        **os.environ,
        "QA_REAL_EDA": sys.executable,
        "QA_EDA_OWNED": str(owned),
        "QA_EDA_FAULT": "merge-unsafe",
    }

    subprocess.run(
        [sys.executable, str(TOOL), "-c", _FAKE_MERGE, "--write", str(merged)],
        env=environment,
        check=True,
    )

    assert merged.is_dir()
    assert (owned / "merged.dat.qa-merged").read_text() == "C 'p' 3\n"
