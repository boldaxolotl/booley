"""Guard the single-source-of-truth Flow and Specialist reference.

The canonical list is rendered by :mod:`booley.dev_support.flow_specialist_reference` from the
MCP endpoint registry. These tests fail the moment a committed doc block drifts from the
registry, or a newly added tool is silently dropped from the reference — so the
list can only be updated from one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.dev_support.flow_specialist_reference import (
    _FLOW_KEY_CONTROLS,
    _PURPOSE_SUMMARY_MAX,
    _REVIEW_FOCUS_DESCRIPTIONS,
    EXCLUDED_MCP_TOOLS,
    extract_generated,
    render_flow_reference,
    render_flow_specialist_reference,
    render_specialists_reference,
)
from booley.mcp.registry import discover_mcp_tools
from booley.runtime.paths import cheatsheet_path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Docs that embed a committed copy of the generated block (base render, no
# project tools). cheatsheet.md is also spliced live by `booley cheat`, but its
# committed copy must still match so the raw file is correct.
_EMBEDDED_DOCS = [
    REPO_ROOT / "docs" / "user" / "USAGE.md",
]

REGEN_HINT = (
    "Regenerate with: python -m booley.dev_support.flow_specialist_reference "
    "docs/user/USAGE.md src/booley/data/cheatsheet.md"
)


@pytest.mark.parametrize("doc", _EMBEDDED_DOCS, ids=lambda p: p.name)
def test_committed_block_matches_registry(doc: Path) -> None:
    """Each committed block is byte-identical to the current registry render."""
    assert doc.exists(), f"missing doc: {doc}"
    committed = extract_generated(doc.read_text(encoding="utf-8"))
    assert committed == render_flow_specialist_reference(), (
        f"{doc.name} tools block is stale. {REGEN_HINT}"
    )


def test_cheatsheet_keeps_tools_and_specialists_in_separate_blocks() -> None:
    text = cheatsheet_path().read_text(encoding="utf-8")
    assert extract_generated(text) == render_flow_reference()
    assert extract_generated(text, name="specialists") == render_specialists_reference()


def test_every_endpoint_is_rendered_or_explicitly_excluded() -> None:
    """No discovered endpoint may vanish from the reference unnoticed.

    An endpoint is either a Booley Flow/Specialist (rendered) or a
    developer-internal direct MCP tool listed in EXCLUDED_MCP_TOOLS. Adding a new
    direct MCP tool without a decision fails here.
    """
    rendered = render_flow_specialist_reference()
    for endpoint in discover_mcp_tools():
        if endpoint.kind in ("flow", "specialist"):
            assert f"`{endpoint.name}`" in rendered, (
                f"{endpoint.name} ({endpoint.kind}) is not in the reference"
            )
        else:
            assert endpoint.name in EXCLUDED_MCP_TOOLS, (
                f"direct MCP tool {endpoint.name!r} is neither rendered nor in "
                f"EXCLUDED_MCP_TOOLS — decide where it belongs in the reference"
            )


def test_docs_render_uses_session_runtime_only_shape() -> None:
    rendered = render_flow_specialist_reference()
    assert "| Booley Flow | Purpose | Sets |" in rendered
    assert "Execution (venues)" not in rendered


def test_terminal_render_omits_the_execution_column() -> None:
    """`booley cheat` emits a fixed-width table; a 4th column overruns it (A3)."""
    rendered = render_flow_reference(execution_column=False)
    assert "| Booley Flow | Purpose | Sets |" in rendered
    assert "Execution" not in rendered
    # Every Booley Flow row still renders when the column is omitted.
    for flow in discover_mcp_tools():
        if flow.kind == "flow":
            assert f"`{flow.name}`" in rendered


def test_flows_follow_hardware_flow_order() -> None:
    """The human catalog leads from elaboration through implementation."""
    rows = [
        line.split("`")[1]
        for line in render_flow_reference().splitlines()
        if line.startswith("| `")
    ]
    assert rows == ["sim", "lint", "synth", "fpga"]


def test_every_builtin_flow_has_key_controls() -> None:
    flows = {endpoint.name for endpoint in discover_mcp_tools() if endpoint.kind == "flow"}
    assert flows == set(_FLOW_KEY_CONTROLS)
    rendered = render_flow_reference(execution_column=False)
    for flow_name, controls in _FLOW_KEY_CONTROLS.items():
        assert f"- `{flow_name}`:" in rendered
        assert controls in rendered


@pytest.mark.parametrize("execution_column", [True, False])
def test_purpose_cells_are_summarized_not_the_agent_description(
    execution_column: bool,
) -> None:
    """Purpose is a one-line summary in every render, docs included.

    Tool descriptions are written for the agent picking flags from them, so
    they carry contract prose (asic_synthesize's SDC rules) that turns a table
    cell into a ~900-char wall. Both renders target humans; the agent still
    reads the full description via the MCP schema.
    """
    rendered = render_flow_reference(execution_column=execution_column)
    assert "A Target with NO SDC is a hard error" not in rendered
    assert "Do NOT use --trace" not in rendered
    for line in rendered.splitlines():
        if not line.startswith("| `"):
            continue
        purpose = line.split("|")[2]
        assert len(purpose) <= _PURPOSE_SUMMARY_MAX + 2, f"unsummarized Purpose cell: {line}"


def test_specialist_reference_lists_every_reviewer_focus_and_mutation_mode() -> None:
    endpoints = {endpoint.name: endpoint for endpoint in discover_mcp_tools()}
    reviewer = endpoints["reviewer"]
    assert set(reviewer.satisfies) == set(_REVIEW_FOCUS_DESCRIPTIONS)

    rendered = render_specialists_reference()
    for criterion, args in (reviewer.satisfies_args or {}).items():
        assert criterion in rendered
        assert args.split("--focus ", maxsplit=1)[1] in rendered
    assert (
        "| Campaign | Ticket Mode (`mandatory` or `optional`) | Standalone CLI options |"
        in rendered
    )
    assert "| Default fixed |" in rendered
    assert "| Explicit fixed |" in rendered
    assert "| Size-scaled |" in rendered
    assert "Mutation goals in a ticket" not in rendered
    assert "Direct goal controls" not in rendered
    assert "Target campaign with `target` + `scope`" in rendered
    assert "`total: N` and `min_detected: K`" in rendered
    assert "`auto: true`" in rendered
    assert "--count N" in rendered
    assert "--count auto" in rendered
    assert "--min-detected K" in rendered
    assert "--regen-lock" in rendered
