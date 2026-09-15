"""Read-only pre/post-state gate for run-owned Vivado Grant and Session recovery.

Input is a retained JSON snapshot assembled from the host's structured projects,
EDA, session, and mount evidence. This probe never mutates a Grant or Session.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


class TransitionError(ValueError):
    """A required run-owned Grant or Session transition is missing."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise TransitionError(message)


def _state(states: dict, name: str) -> dict:
    _need(isinstance(states, dict), "states must be a JSON object")
    value = states.get(name)
    _need(isinstance(value, dict), f"missing {name} state snapshot")
    return value


def _owner(record: dict) -> dict:
    _need(isinstance(record, dict), "state snapshot must be a JSON object")
    owner = record.get("owner")
    _need(isinstance(owner, dict), "run-owned identity is missing")
    for field in ("project_root", "registration"):
        _need(isinstance(owner.get(field), str) and bool(owner[field]),
              f"run-owned {field} is missing")
    registrations = owner.get("run_owned_registrations")
    _need(isinstance(registrations, list)
          and all(isinstance(item, str) and bool(item) for item in registrations),
          "run-owned registration ledger is invalid")
    _need(owner["registration"] in registrations,
          "intended registration is not in the run-owned ledger")
    return owner


def _identity(snapshot: dict, owner: dict, name: str) -> None:
    _need(snapshot.get("project_root") == owner["project_root"],
          f"{name}: Project root differs from run-owned identity")
    _need(snapshot.get("eda_kind") == "vivado", f"{name}: EDA kind is not Vivado")


def _epoch(snapshot: dict, field: str, name: str) -> int:
    value = snapshot.get(field)
    _need(type(value) is int and value >= 0, f"{name}: {field} is not a valid integer epoch")
    return value


def _protected_unchanged(record: dict) -> None:
    protected = record.get("protected")
    _need(isinstance(protected, dict), "borrowed inventory snapshots are missing")
    before = protected.get("before")
    after = protected.get("after")
    _need(isinstance(before, dict) and isinstance(after, dict),
          "borrowed before/after inventory snapshots are missing")
    _need(set(before) == {"grants", "installations"}
          and set(after) == {"grants", "installations"},
          "borrowed inventory must contain grants and installations")
    for kind in ("grants", "installations"):
        _need(isinstance(before[kind], list) and isinstance(after[kind], list),
              f"borrowed {kind} inventory must be a list")
        _need(all(isinstance(item, dict) for item in before[kind] + after[kind]),
              f"borrowed {kind} inventory rows must be mappings")
        old = Counter(json.dumps(item, sort_keys=True, separators=(",", ":"))
                      for item in before[kind])
        new = Counter(json.dumps(item, sort_keys=True, separators=(",", ":"))
                      for item in after[kind])
        _need(old == new, f"borrowed {kind} changed during run-owned transition")


def grant_replacement(record: dict) -> dict:
    """Validate revoke, regrant, issuance, start, and mount in that order."""
    owner = _owner(record)
    states = record.get("states")
    before = _state(states, "before")
    revoked = _state(states, "revoked")
    regranted = _state(states, "regranted")
    issued = _state(states, "issued")
    started = _state(states, "started")
    for name, snapshot in (("before", before), ("revoked", revoked),
                           ("regranted", regranted), ("issued", issued),
                           ("started", started)):
        _identity(snapshot, owner, name)
        _epoch(snapshot, "grant_epoch", name)
    old = before.get("grant_registration")
    _need(isinstance(old, str) and old in owner["run_owned_registrations"],
          "existing Grant is borrowed; do not revoke it")
    _need(revoked.get("grant_registration") is None,
          "old run-owned Grant was not revoked")
    _need(regranted.get("grant_registration") == owner["registration"],
          "new run-owned Grant is missing")
    _need(issued.get("grant_registration") == owner["registration"],
          "Grant changed during host Session issuance")
    _need(started.get("grant_registration") == owner["registration"],
          "Grant changed before Session start")
    before_grant_epoch = _epoch(before, "grant_epoch", "before")
    revoked_grant_epoch = _epoch(revoked, "grant_epoch", "revoked")
    regranted_grant_epoch = _epoch(regranted, "grant_epoch", "regranted")
    before_session_epoch = _epoch(before, "session_grant_epoch", "before")
    _need(before_grant_epoch < revoked_grant_epoch < regranted_grant_epoch,
          "Grant epochs do not prove revoke then regrant")
    _need(_epoch(revoked, "session_grant_epoch", "revoked") == before_session_epoch
          and _epoch(regranted, "session_grant_epoch", "regranted") == before_session_epoch,
          "Session epoch changed before host reissue")
    for name, snapshot in (("revoked", revoked), ("regranted", regranted)):
        _need(snapshot.get("session_valid") is False
              and snapshot.get("session_running") is False
              and snapshot.get("mount_probe") is False,
              f"{name}: stale Session was not invalidated")
    _need(_epoch(issued, "grant_epoch", "issued") == regranted_grant_epoch
          and issued.get("session_running") is False and issued.get("mount_probe") is False,
          "issued Session does not belong to the regranted epoch")
    _need(_epoch(issued, "session_grant_epoch", "issued")
          == _epoch(issued, "grant_epoch", "issued")
          and issued.get("session_valid") is True,
          "stale Session spec; run booley init --seed after regrant")
    _need(_epoch(started, "session_grant_epoch", "started")
          == _epoch(started, "grant_epoch", "started")
          == _epoch(issued, "grant_epoch", "issued")
          and started.get("session_valid") is True and started.get("session_running") is True,
          "Session not validated and running after issuance")
    _need(started.get("mount_probe") is True,
          "intended Vivado mount/command probe did not succeed")
    _protected_unchanged(record)
    return {"ready": True, "project_root": owner["project_root"],
            "registration": owner["registration"]}


def ready(record: dict, name: str = "current") -> dict:
    """Gate Doctor or FPGA execution on the current run-owned state."""
    owner = _owner(record)
    current = _state(record.get("states"), name)
    _identity(current, owner, name)
    _need(current.get("grant_registration") == owner["registration"],
          "run-owned Vivado Grant is missing")
    _need(_epoch(current, "session_grant_epoch", name)
          == _epoch(current, "grant_epoch", name)
          and current.get("session_valid") is True,
          "stale Session spec; run booley init --seed")
    _need(current.get("session_running") is True, "Session is not running")
    _need(current.get("mount_probe") is True, "Vivado mount/command probe missing")
    _protected_unchanged(record)
    return {"ready": True, "project_root": owner["project_root"]}


def denied(record: dict) -> dict:
    """Grade the negative Check from its own denial, independent of recovery."""
    owner = _owner(record)
    snapshot = _state(record.get("states"), "revoked")
    _identity(snapshot, owner, "revoked")
    _need(snapshot.get("grant_registration") is None, "Grant remains attached")
    result = record.get("denial_evidence")
    _need(isinstance(result, dict), "denial evidence must be a JSON object")
    _need(result.get("declared_expected") == "denied", "denial expectation missing")
    _need(result.get("flow_executed") is False,
          "revoked-Grant denial did not prevent FPGA execution")
    _need(isinstance(owner.get("evidence_root"), str) and bool(owner["evidence_root"]),
          "retained evidence root is missing")
    evidence_root = Path(owner["evidence_root"]).resolve(strict=True)
    log_path = Path(result["log_path"]).resolve(strict=True)
    _need(evidence_root.is_dir() and log_path.is_file()
          and log_path.is_relative_to(evidence_root),
          "denial log is outside the retained evidence root")
    content = log_path.read_bytes()
    _need(hashlib.sha256(content).hexdigest() == result.get("log_sha256"),
          "denial log digest does not match retained evidence")
    text = content.decode("utf-8")
    _need(re.search(r"(?m)^exit:\s*2\s*$", text) is not None,
          "denial log does not record exit 2")
    _need('"flow", "fpga"' in text or re.search(r"\bbooley\s+flow\s+fpga\b", text),
          "denial log does not identify the FPGA command")
    authority_denials = ("host-issued spec stamp", "no exact vivado grant",
                         "has no exact vivado grant")
    _need(any(fragment in text.casefold() for fragment in authority_denials),
          "denial log does not contain a recognized authority denial")
    _protected_unchanged(record)
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
