"""Guard the single-source-of-truth Flow and Specialist reference.

The canonical list is rendered by :mod:`booley.dev_support.flow_specialist_reference` from the
MCP endpoint registry. These tests fail the moment a committed cheatsheet block drifts from the
registry, or a newly added tool is silently dropped from the reference — so the
list can only be updated from one place.
"""

from __future__ import annotations

from pathlib import Path

from tests.shipped_docs_support import rendered_docs_outside_user_docs
from tests.source_checkout_support import source_checkout_files

from booley.dev_support.flow_specialist_reference import (
    _FLOW_KEY_CONTROLS,
    _PURPOSE_SUMMARY_MAX,
    _REVIEW_FOCUS_DESCRIPTIONS,
    EXCLUDED_MCP_TOOLS,
    _main,
    extract_generated,
    render_flow_reference,
    render_specialists_reference,
    splice_generated,
)
from booley.mcp.registry import discover_mcp_tools
from booley.runtime.paths import cheatsheet_path

REPO_ROOT = Path(__file__).resolve().parents[2]

REGEN_HINT = (
    "Regenerate with: python -m booley.dev_support.flow_specialist_reference "
    "src/booley/data/cheatsheet.md"
)


def test_cheatsheet_blocks_match_registry() -> None:
    """The committed cheatsheet blocks are byte-identical to the registry render.

    `booley cheat` splices them live, but the raw file must be correct too.
    """
    text = cheatsheet_path().read_text(encoding="utf-8")
    assert extract_generated(text) == render_flow_reference(), REGEN_HINT
    assert extract_generated(text, name="specialists") == render_specialists_reference(), (
        REGEN_HINT
    )


def test_regenerating_a_stale_cheatsheet_restores_both_blocks(tmp_path: Path) -> None:
    """The documented regeneration command rewrites both blocks in place."""
    committed = cheatsheet_path().read_text(encoding="utf-8")
    stale = tmp_path / "cheatsheet.md"
    stale.write_text(
        splice_generated(splice_generated(committed, "stale"), "stale", name="specialists"),
        encoding="utf-8",
    )
    assert _main([str(stale)]) == 0
    assert stale.read_text(encoding="utf-8") == committed


def test_specialist_reference_names_only_current_common_options() -> None:
    committed = cheatsheet_path().read_text(encoding="utf-8")
    assert "--diagnostic" not in render_specialists_reference()
    assert "--diagnostic" not in committed


def test_every_endpoint_is_rendered_or_explicitly_excluded() -> None:
    """No discovered endpoint may vanish from the reference unnoticed.

    An endpoint is either a Booley Flow/Specialist (rendered) or a
    developer-internal direct MCP tool listed in EXCLUDED_MCP_TOOLS. Adding a new
    direct MCP tool without a decision fails here.
    """
    rendered = render_flow_reference() + render_specialists_reference()
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


def test_flow_table_has_two_columns() -> None:
    """`booley cheat` emits a fixed-width table; a 3rd column overruns it (A3)."""
    rendered = render_flow_reference()
    assert "| Booley Flow | Purpose |" in rendered
    assert "Execution" not in rendered


def test_flows_follow_hardware_flow_order() -> None:
    """The human catalog leads from elaboration through implementation."""
    rows = [
        line.split("`")[1]
        for line in render_flow_reference().splitlines()
        if line.startswith("| `")
    ]
    assert rows == ["sim", "lint", "synth", "fpga", "mutation"]


def test_every_builtin_flow_has_key_controls() -> None:
    flows = {endpoint.name for endpoint in discover_mcp_tools() if endpoint.kind == "flow"}
    assert flows == set(_FLOW_KEY_CONTROLS)
    rendered = render_flow_reference()
    for flow_name, controls in _FLOW_KEY_CONTROLS.items():
        assert f"- `{flow_name}`:" in rendered
        assert controls in rendered


def test_purpose_cells_are_summarized_not_the_agent_description() -> None:
    """Purpose is a one-line summary.

    Tool descriptions are written for the agent picking flags from them, so
    they carry contract prose (asic_synthesize's SDC rules) that turns a table
    cell into a ~900-char wall. The cheatsheet targets humans; the agent still
    reads the full description via the MCP schema.
    """
    rendered = render_flow_reference()
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
    for args in (reviewer.satisfies_args or {}).values():
        assert args.split("--focus ", maxsplit=1)[1] in rendered
    # Mutation testing is a Flow now: no Specialist table, no Specialist section.
    assert "mutation_tester" not in rendered
    assert "| Campaign | Goal arguments | Interactive Mode arguments |" not in rendered


def test_mutation_flow_page_documents_simulation_record_controls() -> None:
    """The Mutation Flow page owns the campaign, seed, threshold, and timeout rules."""
    page = " ".join((REPO_ROOT / "docs" / "user" / "FLOW-MUTATION.md").read_text("utf-8").split())
    assert "`--count <N\\|auto>`" in page
    assert "`--min-detected <N>`" in page
    assert "`--seed <N>`" in page and "(default `0`)" in page
    assert "80 % of `--count`, rounded up" in page
    assert (
        "ten times the test's baseline duration, at least 30 seconds and at most 300 seconds"
        in page
    )
    assert "detected (timeout)" in page
    assert "regen_lock" not in page
    assert "steer" not in page


def test_specialist_examples_use_connected_session_arguments_without_cli_flags() -> None:
    rendered = render_specialists_reference()
    assert "booley specialist <name> [args...]" in rendered
    assert "Goal" not in rendered
    assert "CLI-only controls" in rendered
    for name in ("coverage_analyst", "reviewer"):
        assert f"Ask your Developer Agent to call the `{name}` Specialist" in rendered
    assert "booley specialist <name> --help" in rendered
    assert 'steer=["<context>"]' in rendered
    assert 'record="<exact-coverage.json>"' in rendered


def test_shipped_documentation_never_dispatches_a_specialist_as_a_flow() -> None:
    specialists = {
        endpoint.name for endpoint in discover_mcp_tools() if endpoint.kind == "specialist"
    }
    texts = {
        str(document): document.read_text(encoding="utf-8")
        for document in source_checkout_files(REPO_ROOT, "docs/user", "src/booley/data")
        if document.suffix == ".md"  # Git-ignored rendered docs come from their sources below
    }
    texts.update(rendered_docs_outside_user_docs(REPO_ROOT))
    for document, text in texts.items():
        for name in specialists:
            assert f"booley flow {name}" not in text, document


def test_reviewer_reference_states_kind_and_opposite_scope_contract():
    text = render_specialists_reference()
    assert "(.v, .vh, .sv, .svh)" in text
    assert "opposite-category sources are refused" in text
    reviewer = text[text.index("#### `reviewer`") :]
    assert "Goal" not in reviewer


def test_reviewer_reference_admits_exact_cpp_headers():
    text = render_specialists_reference()
    assert "(.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx)" in text
    assert "only an exact per-file category grant" in text
    assert "refuses C/C++ kinds" not in text
