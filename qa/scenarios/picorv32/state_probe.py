"""Read-only pre/post-state gate for run-owned Vivado Grant and Session recovery.

Input is a retained JSON snapshot assembled from the host's structured projects,
EDA, session, and mount evidence. This probe never mutates a Grant or Session.
"""

import argparse
import json
from pathlib import Path


class TransitionError(ValueError):
    """A required run-owned Grant or Session transition is missing."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise TransitionError(message)


def _state(states: dict, name: str) -> dict:
    value = states.get(name)
    _need(isinstance(value, dict), f"missing {name} state snapshot")
    return value


def _identity(snapshot: dict, owner: dict, name: str) -> None:
    _need(snapshot.get("project_root") == owner["project_root"],
          f"{name}: Project root differs from run-owned identity")
    _need(snapshot.get("eda_kind") == "vivado", f"{name}: EDA kind is not Vivado")


def grant_replacement(record: dict) -> dict:
    """Validate revoke, regrant, issuance, start, and mount in that order."""
    owner = record["owner"]
    states = record["states"]
    before = _state(states, "before")
    revoked = _state(states, "revoked")
    regranted = _state(states, "regranted")
    issued = _state(states, "issued")
    started = _state(states, "started")
    for name, snapshot in (("before", before), ("revoked", revoked),
                           ("regranted", regranted), ("issued", issued),
                           ("started", started)):
        _identity(snapshot, owner, name)
    old = before.get("grant_registration")
    _need(old is None or old in owner["run_owned_registrations"],
          "existing Grant is borrowed; do not revoke it")
    _need(revoked.get("grant_registration") is None,
          "old run-owned Grant was not revoked")
    _need(regranted.get("grant_registration") == owner["registration"],
          "new run-owned Grant is missing")
    _need(issued.get("grant_registration") == owner["registration"],
          "Grant changed during host Session issuance")
    _need(started.get("grant_registration") == owner["registration"],
          "Grant changed before Session start")
    _need(issued.get("session_grant_epoch") == issued.get("grant_epoch")
          and issued.get("session_valid") is True,
          "stale Session spec; run booley init --seed after regrant")
    _need(started.get("session_grant_epoch") == started.get("grant_epoch")
          and started.get("session_valid") is True and started.get("session_running") is True,
          "Session not validated and running after issuance")
    _need(started.get("mount_probe") is True,
          "intended Vivado mount/command probe did not succeed")
    return {"ready": True, "project_root": owner["project_root"],
            "registration": owner["registration"]}


def ready(record: dict, name: str = "current") -> dict:
    """Gate Doctor or FPGA execution on the current run-owned state."""
    owner = record["owner"]
    current = _state(record["states"], name)
    _identity(current, owner, name)
    _need(current.get("grant_registration") == owner["registration"],
          "run-owned Vivado Grant is missing")
    _need(current.get("session_grant_epoch") == current.get("grant_epoch")
          and current.get("session_valid") is True,
          "stale Session spec; run booley init --seed")
    _need(current.get("session_running") is True, "Session is not running")
    _need(current.get("mount_probe") is True, "Vivado mount/command probe missing")
    return {"ready": True, "project_root": owner["project_root"]}


def denied(record: dict) -> dict:
    """Grade the negative Check from its own denial, independent of recovery."""
    owner = record["owner"]
    snapshot = _state(record["states"], "revoked")
    _identity(snapshot, owner, "revoked")
    _need(snapshot.get("grant_registration") is None, "Grant remains attached")
    result = record.get("denial_evidence", {})
    _need(result.get("declared_expected") == "denied", "denial expectation missing")
    _need(result.get("observed") == "denied" and result.get("flow_executed") is False,
          "revoked-Grant denial not independently observed")
    return {"denial_pass": True, "project_root": owner["project_root"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("stage", choices=("replace", "ready", "denied"))
    args = parser.parse_args()
    try:
        record = json.loads(args.snapshot.read_text())
        result = {"replace": grant_replacement, "ready": ready,
                  "denied": denied}[args.stage](record)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"state transition missing: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
