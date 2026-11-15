"""Guard the single-source-of-truth acceptance-criteria reference.

The canonical criteria list is rendered by :mod:`booley.criteria.reference`
from ``criteria.toml`` (+ the tool registry for the "Set by" column). These tests
fail the moment a criterion defined in the TOML is silently dropped from the
reference.
"""

from __future__ import annotations

from pathlib import Path

from booley.criteria.endpoint_catalog import (
    CriterionEndpointCatalog,
    EndpointCriterionRelationship,
)
from booley.criteria.reference import (
    render_criteria_params_reference,
    render_criteria_reference,
)
from booley.criteria.templates import load_base_criteria, load_project_criteria
from tests.criterion_endpoint_support import builtin_endpoint_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
_ENDPOINTS = builtin_endpoint_catalog()


def test_every_visible_criterion_is_rendered() -> None:
    """No visible criterion defined in criteria.toml may vanish from the reference."""
    rendered = render_criteria_reference(_ENDPOINTS)
    for cdef in load_base_criteria():
        if cdef.hidden:
            continue
        assert f"`{cdef.name}" in rendered, (
            f"criterion {cdef.name!r} is defined in criteria.toml but absent "
            f"from the rendered reference"
        )


def test_per_target_criteria_use_target_placeholder() -> None:
    rendered = render_criteria_reference(_ENDPOINTS)
    assert "sim_pass_{target}" in rendered
    assert "cycle_count_{target,test}" in rendered
    assert "{cfg}" not in rendered


def test_hidden_criteria_are_omitted(tmp_path: Path) -> None:
    """A hidden project Criterion is omitted while public coverage is rendered."""
    config = tmp_path / "criteria.toml"
    config.write_text('[private_check]\ndescription = "Internal check"\nhidden = true\n')
    rendered = render_criteria_reference(_ENDPOINTS, project_criteria_path=config)
    assert "`private_check" not in rendered
    assert "`coverage_{target}`" in rendered


def test_custom_criterion_uses_injected_endpoint_binding(tmp_path: Path) -> None:
    config = tmp_path / "criteria.toml"
    config.write_text(
        """[drc_clean]
description = "No design-rule violations"
workflow_region = "post_sim"
group = "verification"
""",
        encoding="utf-8",
    )
    catalog = CriterionEndpointCatalog.build(
        load_project_criteria(config),
        [EndpointCriterionRelationship("drc", ("drc_clean",))],
    )

    rendered = render_criteria_reference(catalog, project_criteria_path=config)

    row = next(line for line in rendered.splitlines() if "`drc_clean`" in line)
    assert "`drc`" in row


def test_grouped_render_has_group_headings() -> None:
    """The criteria render is split into functional group sub-tables."""
    rendered = render_criteria_reference(_ENDPOINTS)
    for heading in ("#### Build & Elaborate", "#### Implementation & PPA"):
        assert heading in rendered, f"missing group heading: {heading!r}"


def test_every_threshold_param_is_documented() -> None:
    """Every synthesis_ok / fpga_impl_ok param appears in the flavours block.

    Guards against a param being added to the registry but missed by the
    suffix-splitting renderer (which would silently drop it from the docs).
    """
    from booley.criteria.templates import FPGA_IMPL_OK_PARAMS, SYNTHESIS_OK_PARAMS

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


def test_every_cycle_count_threshold_param_is_documented() -> None:
    """The per-test table spells every public Cycle Count parameter exactly."""
    from booley.criteria.thresholds import CYCLE_COUNT_PARAMS

    rendered = render_criteria_params_reference()
    for param in CYCLE_COUNT_PARAMS:
        assert f"`{param}`" in rendered


def test_public_coverage_reference_names_explicit_collection() -> None:
    rendered = render_criteria_reference(_ENDPOINTS)
    row = next(line for line in rendered.splitlines() if "`coverage_{target}`" in line)
    assert "`sim` (`coverage=true`)" in row


def test_specialist_producers_show_named_arguments_without_cli_syntax() -> None:
    rendered = render_criteria_reference(_ENDPOINTS)
    row = next(line for line in rendered.splitlines() if "`review_rtl_bugs`" in line)
    assert '`reviewer` (`category="rtl"`, `focus="bugs"`)' in row
    assert "--" not in row
    assert "MCP" not in rendered
