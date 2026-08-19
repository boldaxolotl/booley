"""Canonical acceptance-criteria reference, rendered from the single source of truth.

Single source of truth: ``data/criteria.toml`` (base) plus an optional project
``.booley_project/criteria.toml``. The ``booley cheat`` CLI renders these blocks
live, while ``docs/USAGE.md`` and ``data/cheatsheet.md`` embed committed copies
between paired ``<!-- BEGIN GENERATED: <name> -->`` / ``<!-- END GENERATED: <name> -->``
markers that a pytest keeps byte-identical to this renderer. Add, rename, or retire
a criterion (or a threshold param) and every reference updates from this one place.

Two generated blocks live here:

* ``criteria`` — the acceptance-criteria table, split into functional groups
  (Build, Review, …) via each criterion's ``group`` field for readability.
* ``criteria-params`` — the threshold "flavours" (absolute caps/floors and
  baseline-relative bounds) that ``synthesis_ok`` / ``fpga_impl_ok`` accept,
  rendered straight from the param registry in :mod:`booley.dev_support.criteria` so
  the docs can never drift from the validator.

The "Set by" column is derived from the MCP-tool registry's ``satisfies`` metadata via
:func:`booley.mcp.registry.build_criterion_endpoint_map` — never hardcoded.

Regenerate the committed doc blocks with::

    python -m booley.dev_support.criteria_reference docs/USAGE.md src/booley/data/cheatsheet.md
"""

from __future__ import annotations

from pathlib import Path

# Human-facing Workflow Region labels + sort order (mirrors developer_prompt._REGION_ORDER).
_REGION_LABEL = {"pre_sim": "pre-sim", "core_loop": "sim loop", "post_sim": "post-sim"}
_REGION_ORDER = {"pre_sim": 0, "core_loop": 1, "post_sim": 2}

# Functional groups for the criteria table: display label + render order. Keyed by
# the ``group`` field in criteria.toml. Any group missing here sorts last under its
# own raw key, so a new group never silently vanishes from the reference.
_GROUP_LABEL: dict[str, str] = {
    "build": "Build & Elaborate",
    "review": "RTL Code Review",
    "review_tb": "Testbench Review",
    "simulation": "Simulation",
    "coverage": "Coverage",
    "verification": "Verification Quality",
    "implementation": "Implementation & PPA",
}
_GROUP_ORDER = {name: i for i, name in enumerate(_GROUP_LABEL)}

# Threshold-param suffixes (longest first so splitting is unambiguous) mapped to the
# flavour they express. Drives the generated criteria-params block.
_PARAM_SUFFIXES: list[tuple[str, str]] = [
    ("_increase_at_most", "increase_at_most"),
    ("_reduce_at_least", "reduce_at_least"),
    ("_max", "max"),
    ("_min", "min"),
]


def _clean(text: str) -> str:
    """Collapse whitespace and escape pipes so text is table-cell safe."""
    return " ".join(text.split()).replace("|", "\\|")


def _display_name(cdef) -> str:
    """Backticked criterion name, with a Target suffix for per-target families."""
    return f"`{cdef.name}_{{target}}`" if cdef.per_target else f"`{cdef.name}`"


def _group_sort_key(group: str) -> tuple[int, str]:
    """Order known groups by _GROUP_ORDER; unknown groups sort last, alphabetically."""
    return (_GROUP_ORDER.get(group, len(_GROUP_ORDER)), group)


def render_criteria_reference(*, project_criteria_path: Path | None = None) -> str:
    """Render the supported acceptance criteria as grouped Markdown tables.

    Criteria are split into functional groups (Build, Review, …) for readability;
    within a group they sort by workflow region then name.

    Args:
        project_criteria_path: Optional ``.booley_project/criteria.toml``; when it
            exists, its project-defined criteria are merged in so the table reflects
            the active project.
    """
    from booley.dev_support.criteria import (
        expand_criteria_defs,
        load_base_criteria,
        load_project_criteria,
        merge_criteria_defs,
    )
    from booley.mcp.registry import build_criterion_endpoint_map, discover_mcp_tools

    base = load_base_criteria()
    project = (
        load_project_criteria(project_criteria_path) if project_criteria_path is not None else []
    )
    merged, _errors = merge_criteria_defs(base, project)

    endpoint_map = build_criterion_endpoint_map(
        expand_criteria_defs(merged, []),
        discover_mcp_tools(),
    )

    # Group -> criteria, each group sorted by (workflow_region, name) for a stable order.
    # Hidden criteria stay usable but are left out of the reference: listing one would
    # advertise a criterion whose producing endpoint is currently de-registered.
    groups: dict[str, list] = {}
    for c in merged:
        if getattr(c, "hidden", False):
            continue
        groups.setdefault(c.group, []).append(c)

    blocks: list[str] = []
    for group in sorted(groups, key=_group_sort_key):
        label = _GROUP_LABEL.get(group, group.replace("_", " ").title())
        rows = sorted(
            groups[group], key=lambda c: (_REGION_ORDER.get(c.workflow_region, 99), c.name)
        )
        lines = [
            f"#### {label}",
            "",
            "| Criterion | Description | Set by | Workflow Region |",
            "|-----------|-------------|--------|-------|",
        ]
        for c in rows:
            endpoint_command = endpoint_map.get(c.name, (None, c.workflow_region))[0]
            set_by = f"`{endpoint_command}`" if endpoint_command else "—"
            region = _REGION_LABEL.get(c.workflow_region, c.workflow_region)
            lines.append(f"| {_display_name(c)} | {_clean(c.description)} | {set_by} | {region} |")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def _param_flavours(params: frozenset[str]) -> dict[str, set[str]]:
    """Group a criterion's threshold params by base metric -> set of flavour suffixes.

    ``{"cell_count_max", "cell_count_reduce_at_least"}`` becomes
    ``{"cell_count": {"max", "reduce_at_least"}}``.
    """
    out: dict[str, set[str]] = {}
    for param in params:
        for suffix, flavour in _PARAM_SUFFIXES:
            if param.endswith(suffix):
                out.setdefault(param[: -len(suffix)], set()).add(flavour)
                break
    return out


def render_criteria_params_reference() -> str:
    """Render the ``synthesis_ok`` / ``fpga_impl_ok`` threshold flavours.

    Straight from the param registry in :mod:`booley.dev_support.criteria`, so the
    documented flavours can never drift from what the validator actually accepts.
    """
    from booley.dev_support.criteria import (
        FPGA_IMPL_OK_MUTEX_PAIRS,
        FPGA_IMPL_OK_PARAMS,
        SYNTHESIS_OK_MUTEX_PAIRS,
        SYNTHESIS_OK_PARAMS,
    )

    flavour_cols = [
        ("max", "≤ cap"),
        ("min", "≥ floor"),
        ("increase_at_most", "≤ +N% vs base"),
        ("reduce_at_least", "≥ -N% vs base"),
    ]

    lines = [
        "Per-target `synthesis_ok` / `fpga_impl_ok` criteria accept optional "
        "threshold **params**. Each takes a `targets:` list, the per-target "
        "scoping key naming which project Targets to check (the key is "
        "`targets`, never `configs`), plus one or more metric params. Four "
        "flavours per metric: two absolute, two relative to the ticket's "
        "`base_sha` baseline:",
        "",
        "| Flavour param suffix | Baseline? | Meaning |",
        "|----------------------|:---------:|---------|",
        "| `_max` | no | metric must stay **≤** the given value |",
        "| `_min` | no | metric must stay **≥** the given value |",
        "| `_increase_at_most` | yes | metric may grow **at most N%** above baseline |",
        "| `_reduce_at_least` | yes | metric must shrink **at least N%** below baseline |",
        "",
        "Syntax (ticket criteria): "
        "`synthesis_ok: {targets: [<target>], cell_count_max: 500, fmax_mhz_min: 400}`.",
        "",
        "In Ticket Mode, synthesis and FPGA implementation recipes are revision-owned "
        "by default; there is no ticket field for declaring whether a recipe may "
        "change. Intake snapshots each existing Target's normalized recipe. A "
        "baseline-relative `synthesis_ok` or `fpga_impl_ok` criterion then "
        "automatically runs `base_sha` with that revision's Target recipe and the "
        "ticket head with the current recipe. Recipe changes are allowed and shown "
        "with the QoR deltas in the Review package. Missing or mismatched baseline "
        "evidence fails the criterion instead of skipping its relative checks.",
        "",
    ]

    for title, params, mutex, note in (
        (
            "`synthesis_ok` (ASIC)",
            SYNTHESIS_OK_PARAMS,
            SYNTHESIS_OK_MUTEX_PAIRS,
            "Absolute area caps pick a unit (`area_um2` / `area_kge`); the "
            "unit-agnostic `area` row carries the baseline-relative bounds only.",
        ),
        ("`fpga_impl_ok` (FPGA)", FPGA_IMPL_OK_PARAMS, FPGA_IMPL_OK_MUTEX_PAIRS, ""),
    ):
        flavours = _param_flavours(params)
        # Column headers stay un-backticked: `booley cheat`'s terminal renderer
        # only strips inline-code from body cells, not headers, and a lone
        # `_max`/`_increase_at_most` token has no matching underscore so GitHub
        # markdown leaves it un-italicised anyway.
        header = "| Metric | " + " | ".join(f"_{k}" for k, _ in flavour_cols) + " |"
        sep = "|--------|" + "|".join([":---:"] * len(flavour_cols)) + "|"
        lines += [f"**{title}**", "", header, sep]
        for metric in sorted(flavours):
            cells = ["✓" if k in flavours[metric] else "—" for k, _ in flavour_cols]
            lines.append(f"| `{metric}` | " + " | ".join(cells) + " |")
        if note:
            lines.append("")
            lines.append(f"> {note}")
        for a, b in mutex:
            lines.append("")
            lines.append(f"> Mutually exclusive: `{a}` ⊕ `{b}`.")
        lines.append("")

    return "\n".join(lines).rstrip("\n")


def _markers(name: str) -> tuple[str, str]:
    """Begin/end HTML-comment markers for a named generated block."""
    return f"<!-- BEGIN GENERATED: {name} -->", f"<!-- END GENERATED: {name} -->"


def splice_generated(doc_text: str, body: str, *, name: str = "criteria") -> str:
    """Replace whatever sits between the named block's markers in ``doc_text``."""
    begin, end = _markers(name)
    try:
        start = doc_text.index(begin) + len(begin)
        stop = doc_text.index(end)
    except ValueError as exc:
        raise ValueError(f"generated markers ({begin} / {end}) not found in document") from exc
    return doc_text[:start] + "\n" + body + "\n" + doc_text[stop:]


def extract_generated(doc_text: str, *, name: str = "criteria") -> str:
    """Return the committed block between the named markers (marker-exclusive)."""
    begin, end = _markers(name)
    try:
        start = doc_text.index(begin) + len(begin)
        stop = doc_text.index(end)
    except ValueError as exc:
        raise ValueError(f"generated markers ({begin} / {end}) not found in document") from exc
    return doc_text[start:stop].strip("\n")


def has_block(doc_text: str, name: str) -> bool:
    """True when ``doc_text`` carries both markers for the named block."""
    begin, end = _markers(name)
    return begin in doc_text and end in doc_text


# Named blocks this module knows how to render, in the order _main splices them.
_RENDERERS = {
    "criteria": render_criteria_reference,
    "criteria-params": render_criteria_params_reference,
}


def _main(argv: list[str] | None = None) -> int:
    """Regenerate every known block present in the given files (or print to stdout).

    Each file only has the blocks it actually embeds rewritten; a file missing a
    block's markers is left untouched for that block (the cheatsheet carries both
    blocks, USAGE.md only the criteria table).
    """
    import argparse

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

    if not args.paths:
        for name, render in _RENDERERS.items():
            print(f"<!-- {name} -->")
            print(render())
            print()
        return 0

    for path in args.paths:
        pth = Path(path)
        text = pth.read_text(encoding="utf-8")
        for name, render in _RENDERERS.items():
            if has_block(text, name):
                text = splice_generated(text, render(), name=name)
        pth.write_text(text, encoding="utf-8")
        print(f"updated {pth}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
