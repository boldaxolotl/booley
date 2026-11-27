"""Shared record file, JSON and digest helpers."""

from __future__ import annotations

import importlib
from types import ModuleType

from booley.flows.sim.coverage_json import content_digest


def _simulation_record_files() -> ModuleType:
    return importlib.import_module("booley.flows.sim.record_files")


def test_document_and_evidence_digests_keep_their_newline_rules() -> None:
    record_files = _simulation_record_files()
    # A document digest drops the canonical trailing newline; an evidence digest keeps
    # every byte it is given.
    assert record_files.document_digest({"a": 1}) == (
        "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )
    assert content_digest(b'{"a":1}\n') == (
        "sha256:e346432021b04179518d9614f3560ccd71354a4ee101ddcb893d6959a9d6301c"
    )
