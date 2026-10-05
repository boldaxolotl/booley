"""Pin baseline refs and freeze recorded Target recipes for relative Criteria.

A relative Criterion (for example "cycle count reduces by at least 5%" or "LUT
count grows by at most 10") compares the candidate checkout against a baseline
revision. Before any work starts, the caller pins that baseline revision into
the Criterion's params and freezes the normalized recipe of each implementation
Target, so later evidence can prove it measured the same recipe.

This module owns that policy independently of who calls it. Ticket intake uses
it today; any other entry point that knows a checkout, a baseline revision, and
a scratch directory can reuse it by satisfying :class:`PinContext`.

Flow-specific recipe normalization is injected as a :data:`SnapshotBuilder`:
this Flow-neutral module may not select a concrete Flow implementation
(source-dependency contract rule D9), so the caller supplies the synthesis or
FPGA snapshot function.

Heavy collaborators (Target catalog, FuseSoC registry, baseline worktrees) are
imported lazily, at call time, to keep import of this module cheap.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.targets.domain import TARGET_IDENTITY_PARAM

logger = logging.getLogger(__name__)

SnapshotBuilder = Callable[[Any, str], dict[str, Any]]
"""Build a normalized recipe snapshot from a resolved Target and its selector."""


class BaselinePinError(Exception):
    """Pinning or freezing failed; ``str(error)`` is the complete user message.

    Callers translate this neutral error into their own failure type. The
    exception chain (``__cause__`` / ``__suppress_context__``) mirrors the
    underlying failure so translators can preserve it.
    """


class PinContext(Protocol):
    """The minimal view of a work item that baseline pinning needs."""

    @property
    def work_dir(self) -> Path:
        """Candidate checkout whose Targets are resolved for absolute Criteria."""
        ...

    @property
    def base_sha(self) -> str:
        """Baseline git revision for relative Criteria; empty when there is none."""
        ...

    @property
    def recipe_freeze_root(self) -> Path:
        """Scratch directory under which per-Criterion recipe build roots are created.

        Read once per recipe family, before any Criterion is inspected.
        """
        ...


def pin_cycle_count_baselines(
    ctx: PinContext,
    criterion_params: dict[str, dict[str, Any]],
) -> None:
    """Pin every relative Cycle Count Criterion to ``ctx.base_sha``.

    Raises:
        BaselinePinError: a relative Cycle Count Criterion exists but there is
            no baseline revision.
    """
    from booley.criteria.thresholds import has_relative_threshold
    from booley.evidence.fields import BASELINE_REF_PARAM

    for key, params in criterion_params.items():
        if not key.startswith("cycle_count_") or not has_relative_threshold(params):
            continue
        if not ctx.base_sha:
            raise BaselinePinError(
                f"Simulation criterion {key!r} requires a baseline-relative "
                "threshold, but the ticket has no base_sha"
            )
        params[BASELINE_REF_PARAM] = ctx.base_sha


def freeze_recipe_family(
    ctx: PinContext,
    expanded: dict[str, bool],
    criterion_params: dict[str, dict[str, Any]],
    *,
    prefix: str,
    flow_label: str,
    snapshot_builder: SnapshotBuilder,
) -> None:
    """Freeze the recorded Target recipe of every Criterion starting with ``prefix``.

    Relative Criteria are pinned to ``ctx.base_sha`` and their recipe is taken
    from a temporary baseline checkout; absolute Criteria use ``ctx.work_dir``.
    A candidate Target that does not exist yet is skipped (validated later).

    Raises:
        BaselinePinError: missing or invalid Target metadata, a relative
            Criterion without a baseline, an unmaterializable baseline, or a
            Target whose recipe cannot be resolved.
    """
    from booley.evidence.fields import (
        RECIPE_FINGERPRINT_PARAM,
        RECIPE_SNAPSHOT_PARAM,
    )
    from booley.evidence.recipe import recipe_snapshot_fingerprint

    recipe_root = ctx.recipe_freeze_root / prefix.rstrip("_")
    prepared = _prepare_recipe_targets(ctx, expanded, criterion_params, prefix, flow_label)

    with _baseline_recipe_root(ctx, any(item[3] for item in prepared), flow_label) as base_root:
        for key, recipe_target, params, needs_baseline in prepared:
            build_root = recipe_root / key
            shutil.rmtree(build_root, ignore_errors=True)
            snapshot = snapshot_recipe(
                base_root if needs_baseline else ctx.work_dir,
                key,
                recipe_target,
                build_root,
                needs_baseline,
                flow_label,
                snapshot_builder,
            )
            if snapshot is None:
                continue
            params[RECIPE_FINGERPRINT_PARAM] = recipe_snapshot_fingerprint(snapshot)
            params[RECIPE_SNAPSHOT_PARAM] = snapshot


def snapshot_recipe(
    project_root: Path,
    key: str,
    target: str,
    build_root: Path,
    needs_baseline: bool,
    flow_label: str,
    snapshot_builder: SnapshotBuilder,
) -> dict[str, Any] | None:
    """Resolve one Target under ``project_root`` and return its normalized recipe.

    Returns ``None`` (and logs a deferral) when the Target is not authored yet
    and no baseline metrics are needed.

    Raises:
        BaselinePinError: the Target is missing but baseline metrics are
            needed, or the Target cannot be resolved.
    """
    from booley.core.boundary import BoundaryError
    from booley.fusesoc import fusesoc_registry
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import FuseSocError, TargetResolutionError, UnknownTargetError

    try:
        handle = TargetCatalog.build(project_root).select(target)
    except UnknownTargetError:
        if needs_baseline:
            raise BaselinePinError(
                f"{flow_label} criterion {key!r} requires baseline metrics, but "
                f"Target {target!r} does not exist at ticket intake"
            ) from None
        logger.info(
            "%s Target %r is not authored at ticket intake; deferring validation",
            flow_label,
            target,
        )
        return None
    except FuseSocError as exc:
        raise BaselinePinError(
            f"Cannot freeze {flow_label.lower()} recipe for Target {target!r}: {exc}"
        ) from exc
    try:
        resolved = fusesoc_registry.resolve_target_handle(
            handle,
            build_root=build_root,
        )
        return snapshot_builder(resolved, handle.selector)
    except (TargetResolutionError, BoundaryError, OSError) as exc:
        raise BaselinePinError(
            f"Cannot freeze {flow_label.lower()} recipe for Target {target!r}: {exc}"
        ) from exc


def _prepare_recipe_targets(
    ctx: PinContext,
    expanded: dict[str, bool],
    criterion_params: dict[str, dict[str, Any]],
    prefix: str,
    flow_label: str,
) -> list[tuple[str, str, dict[str, Any], bool]]:
    """Validate each family Criterion and pick the Target whose recipe is frozen.

    Returns ``(key, recipe Target, params, needs_baseline)`` per Criterion.
    """
    prepared = []
    for key in (item for item in expanded if item.startswith(prefix)):
        params = criterion_params.setdefault(key, {})
        candidate = params.get(TARGET_IDENTITY_PARAM)
        if not isinstance(candidate, str) or not candidate:
            raise BaselinePinError(f"{flow_label} criterion {key!r} has no Target")
        needs_baseline = _pin_recipe_baseline(ctx, key, params, flow_label)
        baseline = params.get(BASELINE_TARGET_PARAM, candidate)
        if not isinstance(baseline, str) or not baseline:
            raise BaselinePinError(
                f"{flow_label} criterion {key!r} has invalid baseline Target metadata"
            )
        prepared.append((key, baseline if needs_baseline else candidate, params, needs_baseline))
    return prepared


@contextlib.contextmanager
def _baseline_recipe_root(
    ctx: PinContext,
    needed: bool,
    flow_label: str,
) -> Iterator[Path]:
    """Yield the exact baseline checkout used to freeze relative recipe evidence."""
    if not needed:
        yield ctx.work_dir
        return
    from booley.flows.baseline_worktree import BaselineWorktreeError, baseline_worktree

    try:
        with baseline_worktree(Path(ctx.work_dir), ctx.base_sha) as root:
            yield root
    except BaselineWorktreeError as exc:
        raise BaselinePinError(
            f"Cannot materialize {flow_label.lower()} baseline {ctx.base_sha}: {exc}"
        ) from exc


def _pin_recipe_baseline(
    ctx: PinContext,
    key: str,
    params: dict[str, Any],
    flow_label: str,
) -> bool:
    """Pin relative recipe evidence to ``ctx.base_sha``, returning whether needed."""
    from booley.criteria.thresholds import has_relative_threshold
    from booley.evidence.fields import BASELINE_REF_PARAM

    needs_baseline = has_relative_threshold(params)
    if needs_baseline and not ctx.base_sha:
        raise BaselinePinError(
            f"{flow_label} criterion {key!r} requires a baseline-relative "
            "threshold, but the ticket has no base_sha"
        )
    if needs_baseline:
        params[BASELINE_REF_PARAM] = ctx.base_sha
    return needs_baseline
