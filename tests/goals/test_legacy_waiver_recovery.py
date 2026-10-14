"""Replay an interrupted apply saved by the dictionary-based v1 implementation."""

import base64
import hashlib
import json
from pathlib import Path, PureWindowsPath

from booley.goals import apply_effects
from booley.goals.apply import ApplyTransaction, ChangeEnvironment, recover
from booley.goals.entry import EntryEnvironment
from booley.goals.paths import record_paths
from booley.goals.proposals import load_proposal
from booley.mcp.goal_waivers import GoalWaiverService
from tests.goals.conftest import git
from tests.goals.test_waiver_binding import FIXTURE


def legacy_bytes(value):
    # Independent old on-disk encoding; never call the new binding codec.
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def legacy_digest(value):
    return hashlib.sha256(legacy_bytes(value)).hexdigest()


def relocate(value, replacements):
    if isinstance(value, dict):
        return {key: relocate(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
    return value


def relocated_snapshot(tmp_path, path_type=Path):
    payload = json.loads((FIXTURE / "payload.json").read_bytes())
    state = json.loads((FIXTURE / "state.json").read_bytes())
    transaction = json.loads((FIXTURE / "apply.json").read_bytes())
    old_root = payload["waiver"]["anchor_root"].split("/main/")[0]
    from booley.core.booley_layout import BOOLEY_DIR_NAME, RETIRED_DIR_NAME

    replacements = [(old_root, str(tmp_path)), (RETIRED_DIR_NAME, BOOLEY_DIR_NAME)]
    payload = relocate(payload, replacements)
    replacements.append((state["payload_digest"], legacy_digest(payload)))
    old_ledger = legacy_digest({"proposal": payload["id"], "decision": state["decision"]})
    state = relocate(state, replacements)
    new_ledger = legacy_digest({"proposal": payload["id"], "decision": state["decision"]})
    replacements.append((old_ledger, new_ledger))
    transaction = relocate(transaction, replacements)
    for effect in transaction["effects"]:
        # Match the native path spelling used by the captured-effect loader.
        effect["path"] = str(path_type(effect["path"]))
        for image in ("before", "after"):
            if effect[image] is None:
                continue
            content = base64.b64decode(effect[image])
            if effect["label"] in {"record", "state", "ledger"}:
                # Relocate decoded JSON so Windows backslashes remain escaped correctly.
                content = legacy_bytes(relocate(json.loads(content), replacements))
            effect[image] = base64.b64encode(content).decode()
    state["transaction_digest"] = legacy_digest(transaction)
    return payload, state, transaction


def test_relocated_windows_transaction_preserves_digest_when_loaded(monkeypatch):
    monkeypatch.setattr(apply_effects, "Path", PureWindowsPath)
    _, state, transaction = relocated_snapshot(
        PureWindowsPath("D:/fixture-replay"), path_type=PureWindowsPath
    )
    loaded = ApplyTransaction.from_json(transaction)
    assert state["transaction_digest"] == legacy_digest(loaded.to_json())


def install_snapshot(tmp_path):
    payload, state, transaction = relocated_snapshot(tmp_path)
    for effect in transaction["effects"]:
        if effect["before"] is not None:
            path = Path(effect["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(effect["before"]))
    booley_dir = Path(payload["waiver"]["candidate_root"]).parent.parent
    root = record_paths(booley_dir, payload["record_id"]).root
    proposal_root = root / "proposals" / payload["id"]
    proposal_root.mkdir(parents=True)
    (proposal_root / "payload.json").write_bytes(legacy_bytes(payload))
    (proposal_root / "state.json").write_bytes(legacy_bytes(state))
    apply_root = root / "applies"
    apply_root.mkdir()
    (apply_root / f"{transaction['id']}.json").write_bytes(legacy_bytes(transaction))
    worktree = Path(payload["waiver"]["anchor_root"])
    worktree.mkdir(parents=True, exist_ok=True)
    git(worktree, "init", "-q")
    entry = EntryEnvironment(booley_dir)
    store = ChangeEnvironment(entry).store
    record = store.load(payload["record_id"])
    return ChangeEnvironment(entry, GoalWaiverService(booley_dir, record)), payload, transaction


def test_saved_dictionary_based_interrupted_apply_recovers_without_producer_inputs(tmp_path):
    env, payload, transaction = install_snapshot(tmp_path)
    root = record_paths(env.entry.booley_dir, payload["record_id"]).root
    before = load_proposal(root, payload["id"])
    assert before.state == "approved"
    assert before.decision.payload_digest == legacy_digest(payload)
    assert not (root / "runtime" / "flow-reports").exists()
    assert not (root / "waiver-candidates.json").exists()
    recover(env.store, payload["record_id"], env)
    assert load_proposal(root, payload["id"]).state == "applied"
    for effect in transaction["effects"]:
        assert Path(effect["path"]).read_bytes() == base64.b64decode(effect["after"])
    stable = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    recover(env.store, payload["record_id"], env)
    assert stable == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
