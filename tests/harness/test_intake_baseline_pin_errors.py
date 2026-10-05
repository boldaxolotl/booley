"""Ticket intake translates neutral baseline-pin errors into ``FatalError``.

Each intake wrapper must surface exactly what intake raised before the logic
moved to :mod:`booley.flows.baseline_pins`: a ``FatalError`` with the same
message, slug, ``__cause__``, and ``__suppress_context__``, plus the frames
of the real raise site, and never the neutral ``BaselinePinError`` itself.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import baseline_worktree as baseline_module
from booley.flows.baseline_pins import BaselinePinError
from booley.harness.blocking import FatalError
from booley.harness.models import TicketContext
from booley.harness.setup.intake import (
    _freeze_recipe_family,
    _pin_cycle_count_baselines,
    _snapshot_intake_recipe,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import AmbiguousTargetError, UnknownTargetError

_SLUG = "pin-errors"
_SHA = "c" * 40
_RELATIVE = {"target": "present", "area_increase_at_most": 1}


def _ticket(tmp_path: Path, base_sha: str = _SHA) -> TicketContext:
    return TicketContext(
        slug=_SLUG,
        ticket_path=tmp_path / "ticket.md",
        ticket_type="feature",
        branch="main",
        summary="pin errors",
        project_root=tmp_path,
        base_sha=base_sha,
    )


def _freeze(ctx: TicketContext, params: dict[str, dict[str, object]]) -> None:
    _freeze_recipe_family(
        ctx,
        dict.fromkeys(params, True),
        params,
        prefix="synthesis_ok_",
        flow_label="Synthesis",
        snapshot_builder=lambda _resolved, selector: {"target": selector},
    )


@pytest.fixture
def selection_failure(monkeypatch: pytest.MonkeyPatch) -> Callable[[Exception], None]:
    """Make every Target selection raise the given error."""

    def install(error: Exception) -> None:
        def select(_target: str) -> None:
            raise error

        monkeypatch.setattr(
            TargetCatalog,
            "build",
            classmethod(lambda _cls, _root: SimpleNamespace(select=select)),
        )

    return install


def _fatal(call: Callable[[], object]) -> FatalError:
    with pytest.raises(FatalError) as caught:
        call()
    return caught.value


def _assert_translated(
    error: FatalError,
    message: str,
    cause: type[BaseException] | None,
    suppress: bool,
) -> None:
    assert type(error) is FatalError
    assert not isinstance(error, BaselinePinError)
    assert error.error == message
    assert str(error) == message
    assert error.slug == _SLUG
    if cause is None:
        assert error.__cause__ is None
    else:
        assert type(error.__cause__) is cause
    assert error.__suppress_context__ is suppress
    assert not isinstance(error.__context__, BaselinePinError)
    formatted = traceback.format_exception(error)
    assert any("baseline_pins.py" in chunk for chunk in formatted)
    # Source lines may quote ``raise BaselinePinError(``; no chained header may.
    headers = [line for line in "".join(formatted).splitlines() if not line.startswith(" ")]
    assert not any("BaselinePinError" in line for line in headers)
    assert headers[-1] == f"booley.harness.blocking.FatalError: {message}"


def test_cycle_count_plain_raise(tmp_path: Path) -> None:
    params = {"cycle_count_core": {"cycle_count_reduce_at_least": 5}}

    error = _fatal(lambda: _pin_cycle_count_baselines(_ticket(tmp_path, base_sha=""), params))

    _assert_translated(
        error,
        "Simulation criterion 'cycle_count_core' requires a baseline-relative "
        "threshold, but the ticket has no base_sha",
        cause=None,
        suppress=False,
    )
    assert error.__context__ is None


def test_recipe_family_plain_raise(tmp_path: Path) -> None:
    error = _fatal(lambda: _freeze(_ticket(tmp_path), {"synthesis_ok_x": {}}))

    _assert_translated(
        error,
        "Synthesis criterion 'synthesis_ok_x' has no Target",
        cause=None,
        suppress=False,
    )
    assert error.__context__ is None


def test_recipe_family_raise_from_exc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def broken(_project_root: Path, _ref: str) -> Iterator[Path]:
        raise baseline_module.BaselineWorktreeError("ref is gone")
        yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr(baseline_module, "baseline_worktree", broken)

    error = _fatal(lambda: _freeze(_ticket(tmp_path), {"synthesis_ok_x": dict(_RELATIVE)}))

    _assert_translated(
        error,
        f"Cannot materialize synthesis baseline {_SHA}: ref is gone",
        cause=baseline_module.BaselineWorktreeError,
        suppress=True,
    )
    assert error.__context__ is error.__cause__


def test_snapshot_raise_from_none(
    tmp_path: Path,
    selection_failure: Callable[[Exception], None],
) -> None:
    selection_failure(UnknownTargetError("future"))

    error = _fatal(
        lambda: _snapshot_intake_recipe(
            _ticket(tmp_path),
            tmp_path,
            "synthesis_ok_x",
            "future",
            tmp_path / "build",
            True,
            "Synthesis",
            lambda _resolved, selector: {"target": selector},
        )
    )

    _assert_translated(
        error,
        "Synthesis criterion 'synthesis_ok_x' requires baseline metrics, but "
        "Target 'future' does not exist at ticket intake",
        cause=None,
        suppress=True,
    )
    assert type(error.__context__) is UnknownTargetError


def test_snapshot_raise_from_exc(
    tmp_path: Path,
    selection_failure: Callable[[Exception], None],
) -> None:
    selection_failure(AmbiguousTargetError("two Targets match"))

    error = _fatal(
        lambda: _snapshot_intake_recipe(
            _ticket(tmp_path),
            tmp_path,
            "synthesis_ok_x",
            "ambiguous",
            tmp_path / "build",
            False,
            "Synthesis",
            lambda _resolved, selector: {"target": selector},
        )
    )

    _assert_translated(
        error,
        "Cannot freeze synthesis recipe for Target 'ambiguous': two Targets match",
        cause=AmbiguousTargetError,
        suppress=True,
    )


def test_outer_handler_keeps_cause_and_suppression(tmp_path: Path) -> None:
    """Inside an outer ``except``, ``raise`` sets ``__context__`` to that exception.

    A direct ``FatalError`` raise inside the outer handler had the same
    context for the plain form; for ``from`` forms only the hidden (suppressed)
    context differs, so the formatted traceback is unchanged.
    """
    outer = ValueError("outer failure")
    params = {"cycle_count_core": {"cycle_count_reduce_at_least": 5}}

    with pytest.raises(FatalError) as caught:
        try:
            raise outer
        except ValueError:
            _pin_cycle_count_baselines(_ticket(tmp_path, base_sha=""), params)

    error = caught.value
    assert error.__cause__ is None
    assert error.__suppress_context__ is False
    assert error.__context__ is outer
    assert "baseline_pins.py" in "".join(traceback.format_exception(error))
