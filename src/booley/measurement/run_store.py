"""The Measurement Run store in Common Booley State (ADR 0092, #3045).

Layout: ``<Main Booley Directory>/measurements/<project_tree>/<run_id>.json``, and a
run's text reports under ``.../<run_id>/<n>-<flow>/<name>`` (``n`` is the table's
index, which keeps ``:`` and ``#`` of a VLNV out of Windows paths).

Every stored run is kept; the store never deletes one. Only full runs are stored:
a ``--target`` run never is. The JSON document is published last, so a run exists
only once complete. A crash between the report copies and the document leaves a
``<run_id>/`` directory without a document, which no lookup sees and which is
harmless (retention is out of scope).

Reads fail closed: an unreadable or unknown-schema document raises
``MeasurementStoreError``, so an unreadable latest run never lets an older passing
one count. Only ``clock_names``, a suggestion list, skips such a document.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.measurement.run_document import (
    SCHEMA,
    MeasurementRun,
    RunStatus,
    is_measured,
    run_document,
    tables_in_rules_order,
)
from booley.measurement.run_key import MeasurementKey
from booley.runtime.atomic_files import WriteOnceConflictError, atomic_write_once
from booley.runtime.timefmt import parse_timestamp

REPORT_CAP_BYTES = 64 * 1024 * 1024


class MeasurementStoreError(RuntimeError):
    """A stored document cannot be read or has an unknown schema."""


@dataclass(frozen=True)
class FlowReport:
    """A text report file a Flow wrote."""

    identity: str
    flow: str
    path: Path


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    path: Path
    key: MeasurementKey
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    measured: bool
    document: dict[str, Any]


def _load(path: Path) -> StoredRun:
    try:
        document = require_dict(json.loads(path.read_text(encoding="utf-8")))
        if document["schema"] != SCHEMA:
            raise ValueError(f"unknown schema {document['schema']!r}")
        key = document["key"]
        return StoredRun(
            run_id=str(document["run_id"]),
            path=path,
            key=MeasurementKey(str(key["project_tree"]), key["booley_tree"]),
            status=RunStatus(document["status"]),
            started_at=parse_timestamp(document["started_at"]),
            finished_at=parse_timestamp(document["finished_at"]),
            measured=is_measured(document),
            document=document,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise MeasurementStoreError(f"unreadable Measurement Run {path}: {error}") from error


def _clock_names_of(document: dict[str, Any], identity: str, flow: str) -> tuple[str, ...]:
    """Sorted clock names of the table's per-clock values in *document*, empty when it has none."""
    for table in document["tables"]:
        values = table["values"] or {}
        if (table["identity"], table["flow"]) == (identity, flow) and values.get("per_clock"):
            return tuple(sorted({clock for c in values["per_clock"].values() for clock in c}))
    return ()


def _order(run: StoredRun) -> tuple[datetime, datetime, str]:
    return run.finished_at, run.started_at, run.run_id


def _plan_reports(
    run: MeasurementRun, reports: Sequence[FlowReport]
) -> tuple[list[tuple[Path, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Choose the reports to copy: ``(source, relative target)``, kept and dropped entries."""
    indexes = {
        (table.identity, table.flow): index
        for index, (table, _) in enumerate(tables_in_rules_order(run))
    }
    copies: list[tuple[Path, str]] = []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    total = 0
    for report in reports:
        index, name = indexes.get((report.identity, report.flow)), report.path.name
        if index is None:
            raise ValueError(
                f"report {name} belongs to no rules table {report.identity} {report.flow}"
            )
        if (index, name) in seen:
            raise ValueError(f"duplicate report {name} of {report.identity} {report.flow}")
        seen.add((index, name))
        size = report.path.stat().st_size
        entry: dict[str, Any] = {"identity": report.identity, "flow": report.flow, "name": name}
        if total + size > REPORT_CAP_BYTES:
            dropped.append({**entry, "bytes": size})
            continue
        total += size
        relative = f"{run.run_id}/{index}-{report.flow}/{name}"
        copies.append((report.path, relative))
        kept.append({**entry, "path": relative, "bytes": size})
    return copies, kept, dropped


class MeasurementStore:
    def __init__(self, main_booley_dir: Path) -> None:
        """Open the store of the Project whose Main Booley Directory is given."""
        self._root = main_booley_dir / "measurements"

    def write(self, run: MeasurementRun, reports: Sequence[FlowReport]) -> StoredRun:
        """Store *run* with its text reports, dropping those beyond ``REPORT_CAP_BYTES``.

        Reports are kept in the given order while the running total fits; a dropped
        report is listed in the document with its size. Only full runs belong here.

        Raises:
            ValueError: the results do not match the rules, or a report name repeats
                or belongs to no rules table.
            WriteOnceConflictError: a run with this ``run_id`` is already stored.
        """
        tree = self._root / run.keyed.key.project_tree
        document_path = tree / f"{run.run_id}.json"
        if document_path.exists():
            raise WriteOnceConflictError(f"Measurement Run already stored: {document_path}")
        copies, kept, dropped = _plan_reports(run, reports)
        content = json.dumps(run_document(run, kept, dropped), indent=2, allow_nan=False)
        try:
            for source, relative in copies:
                (tree / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, tree / relative)
            tree.mkdir(parents=True, exist_ok=True)
            atomic_write_once(document_path, content.encode("utf-8"))
        except WriteOnceConflictError:
            raise  # a concurrent writer of this run_id owns the report directory
        except BaseException:
            shutil.rmtree(tree / run.run_id, ignore_errors=True)
            raise
        return _load(document_path)

    def _runs_in(self, project_tree: str) -> list[StoredRun]:
        directory = self._root / project_tree
        if not directory.is_dir():
            return []
        return sorted((_load(path) for path in directory.glob("*.json")), key=_order)

    def history(self, key: MeasurementKey) -> list[StoredRun]:
        """Every stored run of exactly this key, oldest first."""
        return [run for run in self._runs_in(key.project_tree) if run.key == key]

    def readable_history(self, key: MeasurementKey) -> tuple[list[StoredRun], tuple[str, ...]]:
        """Every readable run of exactly this key, oldest first, and the unreadable documents.

        Unlike :meth:`history` this never fails closed: it is for a view, which reports a
        document it cannot read instead of refusing. An unreadable document's key is
        unknown, so the message of every unreadable document of the key's project tree is
        returned, whichever key it might belong to.
        """
        directory = self._root / key.project_tree
        runs: list[StoredRun] = []
        problems: list[str] = []
        for path in sorted(directory.glob("*.json")):
            try:
                run = _load(path)
            except MeasurementStoreError as error:
                problems.append(str(error))
                continue
            if run.key == key:
                runs.append(run)
        return sorted(runs, key=_order), tuple(problems)

    def measured(self, key: MeasurementKey) -> StoredRun | None:
        """The latest run of the key when it is Measured; a later bad run un-measures the key."""
        latest = self.history(key)[-1:]
        return latest[0] if latest and latest[0].measured else None

    def base_run(self, project_tree: str) -> StoredRun | None:
        """The latest run of the project tree, whatever its rules tree or status."""
        runs = self._runs_in(project_tree)
        return runs[-1] if runs else None

    def clock_names(self, identity: str, flow: str) -> tuple[str, ...]:
        """Clock names of the newest stored run that has per-clock values for the table."""
        for run in sorted(self._readable_runs(), key=_order, reverse=True):
            try:
                names = _clock_names_of(run.document, identity, flow)
            except (KeyError, TypeError, AttributeError):
                continue  # a document with a damaged table body has no usable clock names
            if names:
                return names
        return ()

    def _readable_runs(self) -> Iterator[StoredRun]:
        for path in sorted(self._root.glob("*/*.json")):
            try:
                yield _load(path)
            except MeasurementStoreError:
                continue
