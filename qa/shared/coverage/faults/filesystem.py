"""Run one public command with a recorded, one-shot filesystem failure."""

import argparse
import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path


def should_fail(source: Path, gate: str, baseline: set[str]) -> bool:
    if gate == "any":
        return True
    document = json.loads(source.read_text())
    if gate == "complete":
        return document.get("complete") is True
    return bool(set(document.get("acceptance_transactions", [])) - baseline)


def service(control: Path, gate: str, baseline: set[str], seen: set[Path], producer: int) -> None:
    for event in sorted(control.glob("event-*")):
        if event in seen or "." in event.name:
            continue
        # The hook fsyncs before waiting. Ignore a partially observed line.
        text = event.read_text()
        if not text.endswith("\n"):
            continue
        _, source, _ = text.rstrip("\n").split("\t")
        if gate == "interrupt":
            if source and Path(source).is_file():
                event.with_suffix(".source.json").write_bytes(Path(source).read_bytes())
            (control / "interrupted").write_text(
                json.dumps({"producer_group": producer, "event": event.name})
            )
            seen.add(event)
            os.killpg(producer, signal.SIGTERM)
            return
        answer = "F" if should_fail(Path(source), gate, baseline) else "P"
        if source and Path(source).is_file():
            # Preserve the publication predicate before product cleanup removes temp JSON.
            event.with_suffix(".source.json").write_bytes(Path(source).read_bytes())
        event.with_suffix(".reply").write_text(answer)
        seen.add(event)


def group_exists(group: int) -> bool:
    """Linux-only runtime: zombies have no executing code or live authority."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == group and fields[0] != "Z":
            return True
    return False


def shutdown(child: subprocess.Popen, grace: float = 5) -> None:
    """Escalate for surviving descendants even when the group leader has exited."""
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        child.poll()
        if not group_exists(child.pid):
            break
        time.sleep(0.01)
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=5)
    deadline = time.monotonic() + 5
    while group_exists(child.pid):
        if time.monotonic() >= deadline:
            raise TimeoutError("owned process group survived SIGKILL")
        time.sleep(0.01)


def run(
    command: list[str],
    owned: Path,
    control: Path,
    library: Path,
    operation: str,
    suffix: str,
    gate: str,
    baseline: set[str],
    timeout: int,
) -> int:
    """Bound the child process group and preserve handshake evidence on every exit."""
    owned = owned.resolve(strict=True)
    if not command or not suffix or any(c in str(owned) + suffix for c in "\n\t"):
        raise ValueError("command, safe owned root and nonempty exact suffix required")
    control.mkdir(mode=0o700, parents=False, exist_ok=False)
    env = {
        **os.environ,
        "LD_PRELOAD": str(library.resolve(strict=True)),
        "QA_FAULT_OWNED": str(owned),
        "QA_FAULT_CONTROL": str(control.resolve()),
        "QA_FAULT_OPERATION": operation,
        "QA_FAULT_SUFFIX": suffix,
    }
    seen: set[Path] = set()
    child = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + timeout
        while child.poll() is None and time.monotonic() < deadline:
            service(control, gate, baseline, seen, child.pid)
            time.sleep(0.01)
        if child.poll() is None:
            raise TimeoutError("owned fault command exceeded operational ceiling")
        service(control, gate, baseline, seen, child.pid)
        if not (control / ("interrupted" if gate == "interrupt" else "consumed")).exists():
            raise ValueError("fault not injected: intended publication boundary was not reached")
        return child.returncode
    finally:
        shutdown(child)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owned", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--operation", choices=["link", "rename", "unlink"], required=True)
    parser.add_argument("--suffix", required=True)
    parser.add_argument(
        "--gate", choices=["any", "complete", "acceptance", "interrupt"], default="any"
    )
    parser.add_argument("--baseline-state", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    baseline = set()
    if args.baseline_state:
        baseline = set(json.loads(args.baseline_state.read_text())["acceptance_transactions"])
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    raise SystemExit(
        run(
            command,
            args.owned,
            args.control,
            args.library,
            args.operation,
            args.suffix,
            args.gate,
            baseline,
            args.timeout,
        )
    )


if __name__ == "__main__":
    main()
