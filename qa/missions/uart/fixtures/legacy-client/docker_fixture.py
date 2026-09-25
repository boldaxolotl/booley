"""Inject one missing owned bind after a real, explicitly selected Docker stop."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def inject(root: Path, relative: str) -> None:
    source = (root / relative).resolve(strict=True)
    if not source.is_relative_to(root) or source == root or source.is_symlink():
        raise ValueError("Fault source must be a contained owned bind")
    backup = source.with_name(source.name + ".qa-restoration")
    if backup.exists():
        raise ValueError("Restoration bytes already exist; do not overwrite evidence")
    source.rename(backup)


def main() -> int:
    root = Path(os.environ["QA_LEGACY_FIXTURE_ROOT"]).resolve(strict=True)
    config = json.loads((root / "fixture.json").read_text())
    if not re.fullmatch("[0-9a-f]{64}", config["container_id"]):
        raise ValueError("Require the exact immutable run-owned container ID")
    executable = Path(config["docker"]).resolve(strict=True)
    if executable == Path(sys.argv[0]).resolve():
        raise ValueError("Docker fixture must forward to the original executable")
    command = sys.argv[1:]
    result = subprocess.run([str(executable), *command], check=False, timeout=120)
    if result.returncode == 0 and command == ["stop", config["container_id"]]:
        inject(root, config["owned_bind"])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
