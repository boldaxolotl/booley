"""Compose MCP discovery with Criteria reference rendering for maintainers."""

from __future__ import annotations

import argparse
from pathlib import Path

from booley.criteria.endpoint_catalog import CriterionEndpointCatalog
from booley.criteria.reference import (
    has_block,
    render_criteria_params_reference,
    render_criteria_reference,
    splice_generated,
)
from booley.criteria.templates import load_base_criteria
from booley.mcp.registry import criterion_endpoint_relationships, discover_mcp_tools


def _base_endpoint_catalog() -> CriterionEndpointCatalog:
    """Compose the packaged Criterion definitions with discovered built-ins."""
    return CriterionEndpointCatalog.build(
        load_base_criteria(),
        criterion_endpoint_relationships(discover_mcp_tools()),
    )


def _main(argv: list[str] | None = None) -> int:
    """Regenerate every known block present in the given files, or print them."""
    parser = argparse.ArgumentParser(
        description="Render the canonical acceptance-criteria blocks.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Markdown files whose generated blocks should be rewritten in "
        "place. With no paths, all blocks are printed to stdout.",
    )
    args = parser.parse_args(argv)
    catalog = _base_endpoint_catalog()
    renderers = {
        "criteria": lambda: render_criteria_reference(catalog),
        "criteria-params": render_criteria_params_reference,
    }

    if not args.paths:
        for name, render in renderers.items():
            print(f"<!-- {name} -->")
            print(render())
            print()
        return 0

    for path in args.paths:
        destination = Path(path)
        text = destination.read_text(encoding="utf-8")
        for name, render in renderers.items():
            if has_block(text, name):
                text = splice_generated(text, render(), name=name)
        destination.write_text(text, encoding="utf-8")
        print(f"updated {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
