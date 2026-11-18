#!/usr/bin/env python3
"""Forward an exact EDA executable, changing only the declared negative boundary."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    real = Path(os.environ["QA_REAL_EDA"]).resolve(strict=True)
    owned = Path(os.environ["QA_EDA_OWNED"]).resolve(strict=True)
    mode = os.environ["QA_EDA_FAULT"]
    if real == Path(__file__).resolve() or mode not in {"version", "merge", "merge-unsafe"}:
        raise ValueError("exact original EDA executable and known fault required")
    if mode == "version" and sys.argv[1:] == ["--version"]:
        print("Verilator 5.050 QA controlled version rejection")
        return
    result = subprocess.run([str(real), *sys.argv[1:]], check=False, timeout=120)
    if mode.startswith("merge") and result.returncode == 0 and "--write" in sys.argv:
        output = Path(sys.argv[sys.argv.index("--write") + 1])
        if output.is_symlink() or not output.resolve(strict=True).is_relative_to(owned):
            raise ValueError("merge destination is outside owned fixture root")
        if mode == "merge-unsafe":
            # Keep the real bytes beside it and leave a directory where the merged file was,
            # so the merge output is no longer a regular file.
            shutil.move(output, output.with_name(output.name + ".qa-merged"))
            output.mkdir()
            raise SystemExit(result.returncode)
        lines = output.read_text().splitlines()
        for index, line in enumerate(lines):
            if line.startswith("C '"):
                key, count = line.rsplit("' ", 1)
                lines[index] = key + "' " + str(int(count) + 1)
                output.write_text("\n".join(lines) + "\n")
                break
        else:
            raise ValueError("no native count reached for merge fault")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
