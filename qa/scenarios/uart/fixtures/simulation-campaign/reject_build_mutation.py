"""Fail after proving immutable Pre-Sim Commands disclose no build path."""

import os
import sys

if os.environ.get("BOOLEY_BUILD_ROOT"):
    raise RuntimeError("immutable Pre-Sim Commands disclosed BOOLEY_BUILD_ROOT")
print("QA_MUTATION_REFUSED: BOOLEY_BUILD_ROOT is not disclosed", file=sys.stderr)
raise SystemExit(73)
