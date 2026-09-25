"""Create bounded wall-clock evidence while a literal run directory is held."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

test = os.environ["BOOLEY_TEST_NAME"]
run_cwd = Path(os.environ["BOOLEY_RUN_CWD"])
start_ns = time.monotonic_ns()
(run_cwd / "qa-literal-owner.txt").write_text(test + "\n", encoding="utf-8")
time.sleep(0.25)
end_ns = time.monotonic_ns()
print(
    "QA_LITERAL_CWD="
    + json.dumps(
        {"test": test, "run_directory": str(run_cwd), "start_ns": start_ns, "end_ns": end_ns},
        sort_keys=True,
    )
)
