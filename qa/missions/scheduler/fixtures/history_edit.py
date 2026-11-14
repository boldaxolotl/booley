"""QA helper: show or rewrite Job Resource History records by hand.

The scheduler mission forces queueing, jump-ahead and expiry by editing
`runtime/resources/history.json` in the Booley Directory. This script does the
edit the same way every run, keeps a backup, and restores it. Run it only
while no Job is running: it does not take Booley's history lock.

    python3 -I history_edit.py show    <history.json>
    python3 -I history_edit.py set     <history.json> --phase P [--target T]
                                       [--memory-mb N] [--wall-s S] [--finished-at RFC3339]
    python3 -I history_edit.py restore <history.json>

`set` changes every record whose key matches the phase (and the Target, when
given). The first `set` saves `<history.json>.qa-bak`; `restore` copies it back.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


def _backup_path(history: Path) -> Path:
    return history.with_name(history.name + ".qa-bak")


def _records(document: dict) -> list[dict]:
    """Return the records of a `booley.job-resource-history/v1` document."""
    return document["records"]


def _show(history: Path) -> int:
    document = json.loads(history.read_text(encoding="utf-8"))
    for record in _records(document):
        key = record["key"]
        print(
            f"{key.get('target')}\t{key.get('phase')}\ttests={key.get('test_run')}\t"
            f"mem={record.get('peak_memory_mb')} MiB\twall={record.get('wall_s')} s\t"
            f"{record.get('outcome')}\t{record.get('finished_at')}"
        )
    return 0


def _set(history: Path, args: argparse.Namespace) -> int:
    backup = _backup_path(history)
    if not backup.exists():
        shutil.copy2(history, backup)
    document = json.loads(history.read_text(encoding="utf-8"))
    changed = 0
    for record in _records(document):
        key = record["key"]
        if key.get("phase") != args.phase:
            continue
        if args.target is not None and key.get("target") != args.target:
            continue
        if args.memory_mb is not None:
            record["peak_memory_mb"] = args.memory_mb
        if args.wall_s is not None:
            record["wall_s"] = args.wall_s
        if args.finished_at is not None:
            record["finished_at"] = args.finished_at
        changed += 1
    if not changed:
        print(f"no record matches phase={args.phase} target={args.target}", file=sys.stderr)
        return 1
    history.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"changed {changed} record(s); backup at {backup}")
    return 0


def _restore(history: Path) -> int:
    backup = _backup_path(history)
    if not backup.exists():
        print(f"no backup at {backup}", file=sys.stderr)
        return 1
    shutil.copy2(backup, history)
    backup.unlink()
    print(f"restored {history}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "restore"):
        sub.add_parser(name).add_argument("history", type=Path)
    edit = sub.add_parser("set")
    edit.add_argument("history", type=Path)
    edit.add_argument("--phase", required=True)
    edit.add_argument("--target")
    edit.add_argument("--memory-mb", type=float)
    edit.add_argument("--wall-s", type=float)
    edit.add_argument("--finished-at")
    args = parser.parse_args()
    if args.command == "show":
        return _show(args.history)
    if args.command == "restore":
        return _restore(args.history)
    return _set(args.history, args)


if __name__ == "__main__":
    sys.exit(main())
