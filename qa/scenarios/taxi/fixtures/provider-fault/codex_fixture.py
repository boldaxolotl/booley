"""Process-boundary Codex failure fixture; never sends a provider request."""

import os
import subprocess
import sys
import time
from pathlib import Path

ERRORS = {
    "subscription": "You've hit your usage limit. QA controlled subscription fixture.",
    "transient": "response stalled mid-stream: QA controlled fixture",
    "crash": "QA controlled ordinary provider failure",
}


def main() -> int:
    root = Path(os.environ["QA_PROVIDER_FIXTURE_ROOT"]).resolve(strict=True)
    mode = (root / "mode").read_text().strip()
    if mode not in {*ERRORS, "timeout", "restored"}:
        raise ValueError("Unknown operator fixture mode")
    if mode == "restored" or sys.argv[1:2] != ["exec"]:
        real = Path(os.environ["QA_REAL_CODEX"]).resolve(strict=True)
        if not real.is_absolute() or real == Path(sys.argv[0]).resolve():
            raise ValueError("Expected the recorded original provider executable")
        return subprocess.run([str(real), *sys.argv[1:]], check=False, timeout=120).returncode
    # Drain, but never retain or echo, the prompt from the ordinary public CLI caller.
    if len(sys.stdin.buffer.read(16 * 1024 * 1024)) >= 16 * 1024 * 1024:
        raise ValueError("Fixture input ceiling exceeded")
    for attempt in range(1000):
        path = root / f"attempt-{attempt:04d}.txt"
        try:
            with path.open("x") as stream:
                stream.write(mode + "\n")
            break
        except FileExistsError:
            continue
    else:
        raise ValueError("Fixture attempt ceiling exhausted")
    if mode == "timeout":
        time.sleep(60)
        print("QA fixture operational ceiling exhausted", file=sys.stderr)
    else:
        print(ERRORS[mode], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
