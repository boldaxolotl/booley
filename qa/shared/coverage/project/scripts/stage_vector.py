"""Stage the literal per-test vector in the public Simulation working directory."""

import json
import os
from pathlib import Path

VECTORS = {"half": [0, 3, 0], "upper": [0, 12, 0]}


def main() -> None:
    if "cocotb" in os.environ["BOOLEY_TARGET"]:
        names = os.environ["BOOLEY_TEST_NAMES"].split()
        vectors = {"gap": [0, 1] * 9, "full": [0, 1, 2] * 6}
        if not names or not set(names) <= set(vectors):
            raise ValueError("unexpected staged Cocotb test selection")
        root = Path(os.environ["BOOLEY_RUN_CWD"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "qa-cocotb-vectors.json").write_text(json.dumps({n: vectors[n] for n in names}))
        print("QA_STAGED_BATCH " + " ".join(names))
        return
    test = os.environ["BOOLEY_TEST_NAME"]
    if test not in VECTORS:
        raise ValueError("unexpected staged fixture test")
    root = Path(os.environ["BOOLEY_RUN_CWD"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / "qa-vector.txt"
    path.write_text(test + "\n" + " ".join(map(str, VECTORS[test])) + "\n")
    print(f"QA_STAGED {test}: {VECTORS[test]}")


if __name__ == "__main__":
    main()
