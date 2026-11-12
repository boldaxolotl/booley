"""The view of a stored Measurement Run for one asked commit (ADR 0092, #3045, #3747).

Contract for the caller (``booley measure``, #3507; rendering and ``--json`` are #3505):
call :func:`run_view` with a stored run and the full sha of the commit the user asked
about. The result is the stored document, unchanged, plus a ``view`` block that holds
everything derived at read time: the commit, its Base and changed files, the rules diff
and what turns the header amber, Δ and base-run warnings, the run's place among the runs
of its key, verdict counts and excerpts, the Targets that no rules table names, and where
the evidence is. Everything in it is JSON, and the result equals its own JSON round trip.

The view is derived on every call and never stored. It never changes the stored
document, a verdict or whether the run is Measured, and it never starts a run.

A commit other than the one measured is accepted when its tree is the run's: the run is
then reported as "measured as" the stored commit, while the Base, changed files and
rules diff are those of the asked commit. A commit whose tree differs is refused.
``view.history`` counts readable runs only; the place of an unreadable document is
unknown, so it is carried in ``problems`` instead.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from booley.measurement.base import (
    Base,
    ChangedFiles,
    changed_files,
    is_protected,
    protected_edits,
    resolve_base,
)
from booley.measurement.base_run import base_run_view
from booley.measurement.rules_diff import (
    RulesDiff,
    header_amber,
    paired_protected_edits,
    rules_diff,
)
from booley.measurement.run_document import RunStatus, Verdict
from booley.measurement.run_key import KeyedCommit, MeasurementKey
from booley.measurement.run_store import MeasurementStore, StoredRun
from booley.runtime.commit_export import versioned_booley_dir
from booley.runtime.pinned_history import raw_git, validate_pin

_REPORTED_VERDICTS = (Verdict.ERROR, Verdict.UNMET)


class TreeMismatchError(ValueError):
    """The asked commit's tree is not the run's measured tree."""


def _git_text(repository: Path, *args: str) -> str:
    return raw_git(repository, *args).decode("utf-8", "replace").strip()


def _commit_identity(
    repository: Path, commit: str, document: Mapping[str, Any]
) -> tuple[KeyedCommit, str]:
    """The asked commit as the identity the rules diff reads, and its subject.

    Message and parents come from the commit object itself, so no ``log.*`` user
    configuration changes the text.
    """
    raw = raw_git(repository, "cat-file", "commit", commit).decode("utf-8", "replace")
    headers, _, message = raw.partition("\n\n")
    parents = tuple(line.split()[1] for line in headers.splitlines() if line.startswith("parent "))
    stored = document["key"]
    key = MeasurementKey(stored["project_tree"], stored["booley_tree"])
    return KeyedCommit(key, commit, message, parents, document["booley_commit"]), message


def _check_tree(repository: Path, commit: str, run: StoredRun) -> str:
    """The tree of *commit*; it must be the run's measured tree."""
    validate_pin(repository, commit)
    tree = _git_text(repository, "rev-parse", "--verify", f"{commit}^{{tree}}")
    if tree != run.key.project_tree:
        raise TreeMismatchError(
            f"commit {commit[:12]} has tree {tree}, but run {run.run_id} measured "
            f"tree {run.key.project_tree}"
        )
    return tree


def _commit_block(
    keyed: KeyedCommit, tree: str, message: str, document: Mapping[str, Any]
) -> dict[str, Any]:
    stored_sha = document["commit"]["sha"]
    subject, _, body = message.partition("\n")
    return {
        "sha": keyed.commit,
        "tree": tree,
        "subject": subject.strip(),
        "body": body.strip(),
        "measured_as": stored_sha if stored_sha != keyed.commit else None,
        "booley_commit": document["booley_commit"],
        "booley_tree": document["key"]["booley_tree"],
    }


def _changed_block(
    changed: ChangedFiles | None, protected: tuple[str, ...], edits: tuple[str, ...]
) -> dict[str, Any] | None:
    """What the commit changed since the Base; None without a Base commit."""
    if changed is None:
        return None
    return {
        "files": [
            {
                "path": file.path,
                "status": file.status,
                "protected": is_protected(file.path, protected),
                "constraint": file.constraint,
            }
            for file in changed.files
        ],
        "submodules": [
            {**asdict(change), "protected": change.path in edits} for change in changed.submodules
        ],
        "file_count": len(changed.files),
        "submodule_count": len(changed.submodules),
    }


def _rules_diff_block(diff: RulesDiff, base: Base) -> dict[str, Any]:
    """The rules diff, with why none was made when the Base gave nothing to compare."""
    source = base if diff.paired_base is None else diff.paired_base
    return {
        "changes": [
            {
                "target": change.target,
                "flow": change.flow,
                "key": change.key,
                "kind": change.kind,
                "direction": change.direction,
                "old": change.old,
                "new": change.new,
                "note": change.note_text,
            }
            for change in diff.changes
        ],
        "tests_diff": diff.tests_diff,
        "unreadable": diff.unreadable,
        "not_compared": source.no_base,
    }


def _outcome(document: Mapping[str, Any]) -> str:
    """The first that applies of cancelled, stopped, error, unmet, met."""
    if document["status"] in (RunStatus.CANCELLED, RunStatus.STOPPED):
        return str(document["status"])
    verdicts = {result["verdict"] for result in _results(document)}
    for verdict in _REPORTED_VERDICTS:
        if verdict in verdicts:
            return str(verdict)
    return Verdict.MET.value


def _results(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [result for table in document["tables"] for result in table["results"]]


def _history_block(store: MeasurementStore, run: StoredRun) -> dict[str, Any]:
    runs, problems = store.readable_history(run.key)
    position = [stored.run_id for stored in runs].index(run.run_id)
    return {
        "position": position + 1,
        "total": len(runs),
        "earlier": [
            {"run_id": earlier.run_id, "outcome": _outcome(earlier.document)}
            for earlier in runs[:position]
        ],
        "problems": list(problems),
    }


def _counts_block(document: Mapping[str, Any]) -> dict[str, int]:
    results = _results(document)
    tally = Counter(result["verdict"] for result in results)
    counts = {verdict.value: tally[verdict] for verdict in Verdict}
    counts["enforced"] = sum(result["bound"] is not None for result in results)
    return counts


def _excerpts(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stored detail of every error or unmet result, in document order."""
    return [
        {
            "identity": table["identity"],
            "flow": table["flow"],
            "key": result["key"],
            "verdict": result["verdict"],
            "excerpt": result["detail"],
        }
        for table in document["tables"]
        for result in table["results"]
        if result["verdict"] in _REPORTED_VERDICTS
    ]


def _evidence(run: StoredRun) -> list[dict[str, Any]]:
    """Per table: the full path of each kept report and each dropped one with its size."""
    document = run.document
    return [
        {
            "identity": table["identity"],
            "flow": table["flow"],
            "reports": [
                str(run.path.parent / report["path"])
                for report in document["reports"]
                if (report["identity"], report["flow"]) == (table["identity"], table["flow"])
            ],
            "dropped": [
                {"name": report["name"], "bytes": report["bytes"]}
                for report in document["dropped_reports"]
                if (report["identity"], report["flow"]) == (table["identity"], table["flow"])
            ],
        }
        for table in document["tables"]
    ]


def _base_run_block(
    repository: Path, store: MeasurementStore, document: Mapping[str, Any], base: Base
) -> dict[str, Any]:
    block = asdict(base_run_view(repository, store, document, base))
    del block["base"]  # the Base is the view's ``base`` block
    return block


def _view(
    repository: Path,
    store: MeasurementStore,
    run: StoredRun,
    commit: str,
    base: str | None,
    targets: Sequence[str],
) -> dict[str, Any]:
    document = run.document
    tree = _check_tree(repository, commit, run)
    keyed, message = _commit_identity(repository, commit, document)
    paired = versioned_booley_dir(repository)
    if keyed.booley_commit is not None and paired is not None:
        # A vanished paired commit must not read as "shares no history" below.
        validate_pin(paired, keyed.booley_commit)
    resolved = resolve_base(repository, commit, base)
    diff = rules_diff(repository, keyed, resolved.commit)
    protected = tuple(document["protected_paths"])
    changed = (
        None if resolved.commit is None else changed_files(repository, resolved.commit, commit)
    )
    edits = () if changed is None else protected_edits(changed, protected)
    tabled = {rules["identity"] for rules in document["rules"]}
    return {
        "commit": _commit_block(keyed, tree, message, document),
        "base": asdict(resolved),
        "rules_base": None if diff.paired_base is None else asdict(diff.paired_base),
        "changed": _changed_block(changed, protected, edits),
        "protected_edits": {
            "repository": list(edits),
            "booley_directory": list(paired_protected_edits(repository, keyed, diff, protected)),
        },
        "rules_diff": _rules_diff_block(diff, resolved),
        "amber": list(header_amber(diff)),
        "base_run": _base_run_block(repository, store, document, resolved),
        "excerpts": _excerpts(document),
        "history": _history_block(store, run),
        "counts": _counts_block(document),
        "targets_without_table": [target for target in targets if target not in tabled],
        "evidence": _evidence(run),
        "paths": {"json": str(run.path), "markdown": str(run.path.with_suffix(".md"))},
    }


def run_view(
    repository: Path,
    store: MeasurementStore,
    run: StoredRun,
    commit: str,
    *,
    base: str | None,
    targets: Sequence[str],
) -> dict[str, Any]:
    """The stored document of *run* plus a ``view`` block for the asked *commit*.

    *commit* is a full sha the caller resolved from user input (#3507); *base* is the
    user's ``--base`` or None; *targets* are the Target identities (``vlnv#name``) of
    the Project's catalog, in the order to list them. Everything is a deep copy in JSON
    types: ``run.document`` is never changed.

    Raises:
        TreeMismatchError: the tree of *commit* is not the run's.
        BaseError: *base* is not a commit or shares no history with *commit*.
        FileCommitError: *commit*, or the paired commit the run was measured with,
            does not exist.
        ValueError: the run's rules came from a Versioned Booley Directory that this
            checkout no longer has.
    """
    view = _view(repository, store, run, commit, base, targets)
    result: dict[str, Any] = json.loads(
        json.dumps({**run.document, "view": view}, allow_nan=False)
    )
    return result
