"""Flow-neutral baseline pinning and recipe freezing, driven by a plain context.

These tests prove the seam works for any caller that only knows a checkout, a
baseline revision, and a scratch dir. Ticket-specific translation into
``FatalError`` is covered by ``tests/harness/test_intake_baseline_pin_errors.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.core.boundary import BoundaryError
from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.evidence.fields import (
    BASELINE_REF_PARAM,
    RECIPE_FINGERPRINT_PARAM,
    RECIPE_SNAPSHOT_PARAM,
)
from booley.evidence.recipe import recipe_snapshot_fingerprint
from booley.flows import baseline_worktree as baseline_module
from booley.flows.baseline_pins import (
    BaselinePinError,
    freeze_recipe_family,
    pin_cycle_count_baselines,
)
from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    AmbiguousTargetError,
    TargetResolutionError,
    UnknownTargetError,
)

_SHA = "b" * 40
_PREFIX = "synthesis_ok_"


@dataclass(frozen=True)
class _PlainContext:
    """A non-Ticket caller: no slug, no Ticket logs, just the three members."""

    work_dir: Path
    base_sha: str
    recipe_freeze_root: Path


def _context(tmp_path: Path, base_sha: str = _SHA) -> _PlainContext:
    return _PlainContext(tmp_path / "checkout", base_sha, tmp_path / "freeze")


def _freeze(
    ctx: _PlainContext,
    params: dict[str, dict[str, Any]],
    expanded: dict[str, bool] | None = None,
) -> None:
    """Freeze the synthesis-like family with a trivial injected snapshot builder."""
    freeze_recipe_family(
        ctx,
        dict.fromkeys(params, True) if expanded is None else expanded,
        params,
        prefix=_PREFIX,
        flow_label="Synthesis",
        snapshot_builder=lambda resolved, selector: {
            "target": selector,
            "root": str(resolved.project_root),
        },
    )


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Fake Target catalog; returns the map of known Targets to their selecting root."""
    known: dict[str, Path] = {}

    def select(root: Path, target: str) -> SimpleNamespace:
        if target == "ambiguous":
            raise AmbiguousTargetError("two Targets match 'ambiguous'")
        if target not in {"present", "before"}:
            raise UnknownTargetError(target)
        known[target] = Path(root)
        return SimpleNamespace(selector=f"sel_{target}", project_root=Path(root))

    monkeypatch.setattr(
        TargetCatalog,
        "build",
        classmethod(lambda _cls, root: SimpleNamespace(select=lambda t: select(root, t))),
    )
    monkeypatch.setattr(
        fusesoc_registry,
        "resolve_target_handle",
        lambda handle, *, build_root: SimpleNamespace(project_root=handle.project_root),
    )
    return known


@pytest.fixture
def baseline_checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """Fake baseline worktree; records each ``(project_root, ref)`` materialized."""
    calls: list[tuple[Path, str]] = []

    @contextmanager
    def fake(project_root: Path, ref: str) -> Iterator[Path]:
        calls.append((project_root, ref))
        yield tmp_path / "baseline"

    monkeypatch.setattr(baseline_module, "baseline_worktree", fake)
    return calls


# --- Cycle Count pinning ----------------------------------------------------


def test_cycle_count_relative_criterion_pins_base_sha(tmp_path: Path) -> None:
    params = {
        "cycle_count_core": {"cycle_count_reduce_at_least": 5},
        "cycle_count_cap": {"cycle_count_max": 100},
        "lint_clean": {"cycle_count_reduce_at_least": 5},
    }

    pin_cycle_count_baselines(_context(tmp_path), params)

    assert params["cycle_count_core"][BASELINE_REF_PARAM] == _SHA
    assert BASELINE_REF_PARAM not in params["cycle_count_cap"]
    assert BASELINE_REF_PARAM not in params["lint_clean"]


def test_cycle_count_relative_criterion_without_base_sha_raises(tmp_path: Path) -> None:
    params = {"cycle_count_core": {"cycle_count_reduce_at_least": 5}}

    with pytest.raises(BaselinePinError) as caught:
        pin_cycle_count_baselines(_context(tmp_path, base_sha=""), params)

    assert str(caught.value) == (
        "Simulation criterion 'cycle_count_core' requires a baseline-relative "
        "threshold, but the ticket has no base_sha"
    )
    assert BASELINE_REF_PARAM not in params["cycle_count_core"]


def test_cycle_count_absolute_criterion_needs_no_base_sha(tmp_path: Path) -> None:
    params = {"cycle_count_core": {"cycle_count_max": 100}}

    pin_cycle_count_baselines(_context(tmp_path, base_sha=""), params)

    assert params == {"cycle_count_core": {"cycle_count_max": 100}}


# --- Recipe freezing --------------------------------------------------------


def test_relative_recipe_freezes_baseline_target_in_baseline_checkout(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
) -> None:
    ctx = _context(tmp_path)
    key = f"{_PREFIX}after"
    stale = ctx.recipe_freeze_root / "synthesis_ok" / key / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    params = {
        key: {"target": "after", "area_increase_at_most": 1, BASELINE_TARGET_PARAM: "before"}
    }

    _freeze(ctx, params)

    frozen = params[key]
    assert baseline_checkouts == [(ctx.work_dir, _SHA)]
    assert catalog == {"before": tmp_path / "baseline"}
    assert frozen[BASELINE_REF_PARAM] == _SHA
    assert frozen[RECIPE_SNAPSHOT_PARAM] == {
        "target": "sel_before",
        "root": str(tmp_path / "baseline"),
    }
    assert frozen[RECIPE_FINGERPRINT_PARAM] == recipe_snapshot_fingerprint(
        frozen[RECIPE_SNAPSHOT_PARAM]
    )
    assert not stale.parent.exists()


def test_absolute_recipe_uses_work_dir_without_baseline(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
) -> None:
    ctx = _context(tmp_path, base_sha="")
    key = f"{_PREFIX}present"
    params = {key: {"target": "present"}, "lint_clean": {}}

    _freeze(ctx, params)

    assert baseline_checkouts == []
    assert catalog == {"present": ctx.work_dir}
    assert BASELINE_REF_PARAM not in params[key]
    assert params[key][RECIPE_SNAPSHOT_PARAM]["target"] == "sel_present"
    assert params["lint_clean"] == {}


def test_absent_candidate_target_defers_without_pinning(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = f"{_PREFIX}future"
    params = {key: {"target": "future"}}

    with caplog.at_level(logging.INFO, logger="booley.flows.baseline_pins"):
        _freeze(_context(tmp_path), params)

    assert params == {key: {"target": "future"}}
    assert baseline_checkouts == []
    assert (
        "Synthesis Target 'future' is not authored at ticket intake; deferring validation"
        in caplog.messages
    )


def test_relative_recipe_with_missing_baseline_target_raises(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
) -> None:
    key = f"{_PREFIX}future"
    params = {key: {"target": "future", "area_increase_at_most": 1}}

    with pytest.raises(BaselinePinError) as caught:
        _freeze(_context(tmp_path), params)

    assert str(caught.value) == (
        f"Synthesis criterion {key!r} requires baseline metrics, but "
        "Target 'future' does not exist at ticket intake"
    )
    # ``from None``: the catalog miss is hidden behind the actionable message.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert params[key][BASELINE_REF_PARAM] == _SHA
    assert RECIPE_SNAPSHOT_PARAM not in params[key]


@pytest.mark.parametrize(
    ("base_sha", "params", "message"),
    [
        (_SHA, {}, "Synthesis criterion 'synthesis_ok_x' has no Target"),
        (
            "",
            {"target": "present", "area_increase_at_most": 1},
            "Synthesis criterion 'synthesis_ok_x' requires a baseline-relative "
            "threshold, but the ticket has no base_sha",
        ),
        (
            _SHA,
            {"target": "present", "area_increase_at_most": 1, BASELINE_TARGET_PARAM: ""},
            "Synthesis criterion 'synthesis_ok_x' has invalid baseline Target metadata",
        ),
    ],
)
def test_invalid_recipe_metadata_raises_before_any_checkout(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
    base_sha: str,
    params: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(BaselinePinError, match=f"^{message}$"):
        _freeze(_context(tmp_path, base_sha=base_sha), {"synthesis_ok_x": params})

    assert baseline_checkouts == []
    assert catalog == {}


def test_baseline_materialization_failure_chains_cause(
    tmp_path: Path,
    catalog: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = baseline_module.BaselineWorktreeError("ref is gone")

    @contextmanager
    def broken(_project_root: Path, _ref: str) -> Iterator[Path]:
        raise failure
        yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr(baseline_module, "baseline_worktree", broken)
    params = {f"{_PREFIX}after": {"target": "present", "area_increase_at_most": 1}}

    with pytest.raises(BaselinePinError) as caught:
        _freeze(_context(tmp_path), params)

    assert str(caught.value) == f"Cannot materialize synthesis baseline {_SHA}: ref is gone"
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    "failure",
    [
        TargetResolutionError("fusesoc setup failed"),
        BoundaryError("fusesoc setup failed"),
        OSError("fusesoc setup failed"),
    ],
    ids=["resolution", "boundary", "os"],
)
def test_unresolvable_target_chains_cause(
    tmp_path: Path,
    catalog: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def fail(_handle: object, *, build_root: Path) -> None:
        raise failure

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", fail)
    params = {f"{_PREFIX}present": {"target": "present"}}

    with pytest.raises(BaselinePinError) as caught:
        _freeze(_context(tmp_path, base_sha=""), params)

    assert str(caught.value) == (
        "Cannot freeze synthesis recipe for Target 'present': fusesoc setup failed"
    )
    assert caught.value.__cause__ is failure


def test_target_selection_failure_chains_cause(
    tmp_path: Path,
    catalog: dict[str, Path],
) -> None:
    params = {f"{_PREFIX}x": {"target": "ambiguous"}}

    with pytest.raises(BaselinePinError) as caught:
        _freeze(_context(tmp_path, base_sha=""), params)

    assert str(caught.value) == (
        "Cannot freeze synthesis recipe for Target 'ambiguous': two Targets match 'ambiguous'"
    )
    assert isinstance(caught.value.__cause__, AmbiguousTargetError)
    assert caught.value.__suppress_context__ is True


# --- Mixed families, precedence, partial mutation ---------------------------


def _expected_snapshot(selector: str, root: Path) -> dict[str, str]:
    return {"target": selector, "root": str(root)}


def test_mixed_family_freezes_relative_and_absolute_criteria(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
) -> None:
    ctx = _context(tmp_path)
    relative, absolute = f"{_PREFIX}rel", f"{_PREFIX}abs"
    params = {
        relative: {
            "target": "present",
            "area_increase_at_most": 1,
            BASELINE_TARGET_PARAM: "before",
        },
        absolute: {"target": "present"},
    }
    expanded = {relative: True, "lint_clean": True, absolute: False}

    _freeze(ctx, params, expanded)

    relative_snapshot = _expected_snapshot("sel_before", tmp_path / "baseline")
    absolute_snapshot = _expected_snapshot("sel_present", ctx.work_dir)
    assert params == {
        relative: {
            "target": "present",
            "area_increase_at_most": 1,
            BASELINE_TARGET_PARAM: "before",
            BASELINE_REF_PARAM: _SHA,
            RECIPE_FINGERPRINT_PARAM: recipe_snapshot_fingerprint(relative_snapshot),
            RECIPE_SNAPSHOT_PARAM: relative_snapshot,
        },
        absolute: {
            "target": "present",
            RECIPE_FINGERPRINT_PARAM: recipe_snapshot_fingerprint(absolute_snapshot),
            RECIPE_SNAPSHOT_PARAM: absolute_snapshot,
        },
    }
    assert list(params[relative])[-3:] == [
        BASELINE_REF_PARAM,
        RECIPE_FINGERPRINT_PARAM,
        RECIPE_SNAPSHOT_PARAM,
    ]
    # One baseline checkout serves the whole family.
    assert baseline_checkouts == [(ctx.work_dir, _SHA)]


def test_expanded_key_missing_from_params_fails_after_partial_pinning(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
) -> None:
    relative, missing = f"{_PREFIX}rel", f"{_PREFIX}missing"
    params: dict[str, dict[str, Any]] = {
        relative: {"target": "present", "area_increase_at_most": 1}
    }

    with pytest.raises(BaselinePinError, match=f"^Synthesis criterion {missing!r} has no Target$"):
        _freeze(_context(tmp_path), params, {relative: True, missing: True})

    # Validation runs before any checkout or snapshot; earlier keys are pinned.
    assert params == {
        relative: {"target": "present", "area_increase_at_most": 1, BASELINE_REF_PARAM: _SHA},
        missing: {},
    }
    assert baseline_checkouts == []
    assert catalog == {}


def test_missing_base_sha_is_reported_before_invalid_baseline_metadata(
    tmp_path: Path,
    catalog: dict[str, Path],
) -> None:
    params = {
        f"{_PREFIX}x": {"target": "present", "area_increase_at_most": 1, BASELINE_TARGET_PARAM: 7}
    }

    with pytest.raises(BaselinePinError, match=r"no base_sha$"):
        _freeze(_context(tmp_path, base_sha=""), params)


def test_expanded_order_decides_which_criterion_error_wins(
    tmp_path: Path,
    catalog: dict[str, Path],
) -> None:
    no_target, bad_meta = f"{_PREFIX}a", f"{_PREFIX}b"
    params = {
        no_target: {},
        bad_meta: {"target": "present", "area_increase_at_most": 1, BASELINE_TARGET_PARAM: ""},
    }

    with pytest.raises(BaselinePinError, match=r"invalid baseline Target metadata$"):
        _freeze(_context(tmp_path), params, {bad_meta: True, no_target: True})


def test_validation_error_precedes_missing_baseline_target(
    tmp_path: Path,
    catalog: dict[str, Path],
    baseline_checkouts: list[tuple[Path, str]],
) -> None:
    unknown, no_target = f"{_PREFIX}a", f"{_PREFIX}b"
    params: dict[str, dict[str, Any]] = {
        unknown: {"target": "future", "area_increase_at_most": 1},
        no_target: {},
    }

    with pytest.raises(BaselinePinError, match=r"has no Target$"):
        _freeze(_context(tmp_path), params)

    assert baseline_checkouts == []
    assert catalog == {}
