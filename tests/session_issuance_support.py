"""Test-only helpers that issue Runtime specs through ``preview``/``issue_prepared``."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.runtime import devcontainer as dc
from booley.runtime import session_issuance as runtime_spec


def stub_keeper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Docker keeper-tag calls that `issue_prepared` makes around persistence.

    `_persist_prepared` snapshots the keeper before writing and restores it when a
    guard refuses, so without these stubs a refusal is replaced by a Docker error.
    Patching the test's own ``monkeypatch`` lets a test override a stub later.
    """
    monkeypatch.setattr("booley.runtime.interactive_docker.image_id_strict", lambda _image: None)
    monkeypatch.setattr("booley.runtime.interactive_docker.tag_image", lambda *_args: None)
    monkeypatch.setattr("booley.runtime.interactive_docker.remove_image_tag", lambda _image: None)


def fixed(spec: dict[str, Any]) -> runtime_spec.SpecBuilder:
    """Return a builder that hands out a deep copy of an already built spec."""
    return lambda _inputs: copy.deepcopy(spec)


def issue_built(
    project: Path,
    build: runtime_spec.SpecBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[runtime_spec.PreparedSessionSpec, Path, runtime_spec.Issuance]:
    """Preview *build* and issue it, as production does; return prepared, path and stamp."""
    stub_keeper(monkeypatch)
    prepared = runtime_spec.preview(project, build)
    stamp = runtime_spec.issue_prepared(project, prepared, reconcile_dashboard=False)
    return prepared, dc.devcontainer_path(project), stamp


def damaged(
    prepared: runtime_spec.PreparedSessionSpec,
    mutate: Callable[[dict[str, Any]], None],
    **prospective_overrides: Any,
) -> runtime_spec.PreparedSessionSpec:
    """Return *prepared* with its spec damaged after pinning, as a stale preview would be.

    The digest and the prospective issuance follow the damaged spec, so revalidation
    accepts it and the guard under test is the one that fires at issue time.
    """
    spec = copy.deepcopy(prepared.spec)
    mutate(spec)
    digest = runtime_spec._spec_digest(spec)
    prospective = replace(
        prepared.prospective_issuance, spec_sha256=digest, **prospective_overrides
    )
    run_args: list[str] = []
    for item in spec["runArgs"]:
        if run_args and run_args[-1] == "--label" and runtime_spec._is_issuance_label(item):
            run_args.pop()
        elif not runtime_spec._is_issuance_label(item):
            run_args.append(item)
    for label in runtime_spec.labels(prospective):
        run_args += ["--label", label]
    spec["runArgs"] = run_args
    return replace(prepared, spec=spec, digest=digest, prospective_issuance=prospective)
