"""Guard the single-source-of-truth acceptance-criteria reference.

The canonical criteria list is rendered by :mod:`booley.dev_support.criteria_reference`
from ``criteria.toml`` (+ the tool registry for the "Set by" column). These tests
fail the moment a committed doc block drifts from the source of truth, or a
criterion defined in the TOML is silently dropped from the reference — so the list
can only be updated from one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.dev_support.criteria import load_base_criteria
from booley.dev_support.criteria_reference import (
    extract_generated,
    render_criteria_params_reference,
    render_criteria_reference,
)
from booley.paths import cheatsheet_path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Docs that embed a committed copy of the generated block (base render, no
# project criteria). cheatsheet.md is also spliced live by `booley cheat`, but its
# committed copy must still match so the raw file is correct.
_EMBEDDED_DOCS = [
    REPO_ROOT / "docs" / "USAGE.md",
    cheatsheet_path(),
]

REGEN_HINT = (
    "Regenerate with: python -m booley.dev_support.criteria_reference "
    "docs/USAGE.md src/booley/data/cheatsheet.md"
)


@pytest.mark.parametrize("doc", _EMBEDDED_DOCS, ids=lambda p: p.name)
def test_committed_block_matches_source(doc: Path) -> None:
    """Each committed criteria block is byte-identical to the current render."""
    assert doc.exists(), f"missing doc: {doc}"
    committed = extract_generated(doc.read_text(encoding="utf-8"))
    assert committed == render_criteria_reference(), (
        f"{doc.name} criteria block is stale. {REGEN_HINT}"
    )


@pytest.mark.parametrize("doc", _EMBEDDED_DOCS, ids=lambda p: p.name)
def test_committed_params_block_matches_source(doc: Path) -> None:
    """Each committed criteria-params (flavours) block matches the current render."""
    assert doc.exists(), f"missing doc: {doc}"
    committed = extract_generated(doc.read_text(encoding="utf-8"), name="criteria-params")
    assert committed == render_criteria_params_reference(), (
        f"{doc.name} criteria-params block is stale. {REGEN_HINT}"
    )


def test_every_visible_criterion_is_rendered() -> None:
    """No visible criterion defined in criteria.toml may vanish from the reference."""
    rendered = render_criteria_reference()
    for cdef in load_base_criteria():
        if cdef.hidden:
            continue
        assert f"`{cdef.name}" in rendered, (
            f"criterion {cdef.name!r} is defined in criteria.toml but absent "
            f"from the rendered reference"
        )


def test_per_target_criteria_use_target_placeholder() -> None:
    rendered = render_criteria_reference()
    assert "sim_pass_{target}" in rendered
    assert "{cfg}" not in rendered


def test_hidden_criteria_are_omitted() -> None:
    """``hidden = true`` keeps a criterion out of the reference (still usable in tickets)."""
    rendered = render_criteria_reference()
    hidden = [c.name for c in load_base_criteria() if c.hidden]
    assert hidden, "expected at least one hidden criterion to exercise this path"
    for name in hidden:
        assert f"`{name}" not in rendered, (
            f"criterion {name!r} is marked hidden but still renders in the reference"
        )


def test_grouped_render_has_group_headings() -> None:
    """The criteria render is split into functional group sub-tables."""
    rendered = render_criteria_reference()
    for heading in ("#### Build & Elaborate", "#### Implementation & PPA"):
        assert heading in rendered, f"missing group heading: {heading!r}"


def test_every_threshold_param_is_documented() -> None:
    """Every synthesis_ok / fpga_impl_ok param appears in the flavours block.

    Guards against a param being added to the registry but missed by the
    suffix-splitting renderer (which would silently drop it from the docs).
    """
    from booley.dev_support.criteria import FPGA_IMPL_OK_PARAMS, SYNTHESIS_OK_PARAMS

    rendered = render_criteria_params_reference()
    for param in SYNTHESIS_OK_PARAMS | FPGA_IMPL_OK_PARAMS:
        # Renderer shows base metric + flavour columns; assert the metric stem is present.
        for suffix in ("_increase_at_most", "_reduce_at_least", "_max", "_min"):
            if param.endswith(suffix):
                metric = param[: -len(suffix)]
                break
        else:
            metric = param
        assert f"`{metric}`" in rendered, (
            f"threshold param {param!r} (metric {metric!r}) is missing from the "
            f"rendered flavours block"
        )
