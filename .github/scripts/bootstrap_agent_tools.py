#!/usr/bin/env python3
"""Create one fingerprinted, tools-only environment atomically."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True


def main(argv: list[str] | None = None) -> int:
    """Build and publish an environment only when explicitly requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    if args.environment.exists() and _valid_receipt(args.environment, args.fingerprint):
        return 0
    lock = args.environment.with_name(f".{args.environment.name}.lock")
    staging = args.environment.with_name(
        f".{args.environment.name}.staging-{os.getpid()}-{uuid.uuid4().hex}"
    )
    acquired = False
    try:
        _acquire(lock)
        acquired = True
        if args.environment.exists() and _valid_receipt(args.environment, args.fingerprint):
            return 0
        _build(staging, root, args.fingerprint)
        if args.environment.exists():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            staging.replace(args.environment)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if acquired:
            with contextlib.suppress(FileNotFoundError):
                lock.unlink()


def _acquire(lock: Path) -> None:
    """Acquire a bounded lock and recover only dead owners."""
    deadline = time.monotonic() + 60
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "created": time.time()}, handle)
            return
        except FileExistsError:
            if _stale(lock):
                try:
                    lock.unlink()
                except FileNotFoundError:
                    continue
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for the agent-tools lock") from None
            time.sleep(0.1)


def _stale(lock: Path) -> bool:
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return True
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _build(staging: Path, root: Path, fingerprint: str) -> None:
    sys.path.insert(0, str(root / "src"))
    from booley.dev_support.agent_readiness import PINNED_RUNNERS, dependency_argv, venv_python

    subprocess.run((sys.executable, "-B", "-m", "venv", str(staging)), cwd=root, check=True)
    python = venv_python(staging)
    subprocess.run((str(python), "-B", "-m", "pip", *dependency_argv(root)), cwd=root, check=True)
    subprocess.run((str(python), "-B", "-m", "pip", "check"), cwd=root, check=True)
    installed = _installed(python, root)
    if any(installed.get(name) != version for name, version in PINNED_RUNNERS.items()):
        raise RuntimeError("bootstrap did not install the exact runner pins")
    receipt = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "python": sys.version_info[:3],
        "platform": sys.platform,
        "installed": installed,
    }
    (staging / "receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _installed(python: Path, root: Path) -> dict[str, str]:
    code = "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name'].lower(): d.version for d in m.distributions()}))"
    result = subprocess.run(
        (str(python), "-B", "-c", code), cwd=root, capture_output=True, text=True, check=True
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("shared environment returned an invalid distribution set")
    return value


def _valid_receipt(root: Path, fingerprint: str) -> bool:
    try:
        payload = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return (
        payload.get("schema_version") == 1
        and payload.get("fingerprint") == fingerprint
        and python.is_file()
        and isinstance(payload.get("installed"), dict)
    )


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
