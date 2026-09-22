"""Record the private build generation exposed by legacy-per-test mode."""

import os
from pathlib import Path

test = os.environ["BOOLEY_TEST_NAME"]
build = Path(os.environ["BOOLEY_BUILD_ROOT"])
build.mkdir(parents=True, exist_ok=True)
(build / "qa-legacy-generation.txt").write_text(test + "\n", encoding="utf-8")
print(f"QA_LEGACY_BUILD={build}:{test}")
