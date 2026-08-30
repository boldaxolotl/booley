"""Canonical Booley Flow and Specialist references, rendered from the registry.

Single source of truth: :func:`booley.mcp.registry.discover_mcp_tools`. The
``booley cheat`` CLI renders this block live, while ``docs/user/USAGE.md`` embeds a
committed copy between the ``<!-- BEGIN GENERATED: flows -->`` /
``<!-- END GENERATED: flows -->`` markers that a pytest keeps byte-identical to
this renderer. The cheatsheet gives Specialists their own flag-addressable
block. Add, rename, or retire a Flow/Specialist and the lists update everywhere
from this one place.

Regenerate the committed doc blocks with::

    python -m booley.dev_support.flow_specialist_reference \\
        docs/user/USAGE.md src/booley/data/cheatsheet.md
"""

from __future__ import annotations

from pathlib import Path

from booley.mcp.registry import discover_mcp_tools

BEGIN_MARKER = "<!-- BEGIN GENERATED: flows -->"
END_MARKER = "<!-- END GENERATED: flows -->"
SPECIALISTS_BEGIN_MARKER = "<!-- BEGIN GENERATED: specialists -->"
SPECIALISTS_END_MARKER = "<!-- END GENERATED: specialists -->"

# Direct MCP endpoints that are developer-internal plumbing, not a user-facing
# capability, are omitted from the human reference. The staleness test forbids
# silently dropping anything else.
EXCLUDED_MCP_TOOLS = frozenset({"submit_run_report"})


# Backstop on the Purpose cell: a full multi-sentence description produced a
# ~700-char-wide column that overflowed every terminal and truncated the Sets
# column mid-token (QA_REPORT A3). It bounds that wall rather than fitting 80
# columns — the table is wider than a terminal either way. Keep it above the
# longest first sentence in the registry, so the common case reads as a whole
# sentence and the "…" only ever means an endpoint overwrote its opener.
_PURPOSE_SUMMARY_MAX = 120


# Present built-in Booley Flows in the order a reader encounters them in a normal
# hardware flow. Project-defined Booley Flows follow these, sorted by name.
_FLOW_DISPLAY_RANK = {
    name: rank for rank, name in enumerate(("elab", "sim", "lint", "synth", "fpga"))
}

_BASELINE_CONTROL = "`--baseline <ref>` compares metrics against a git revision"


_FLOW_KEY_CONTROLS: dict[str, str] = {
    "elab": ("`--standalone` also proves every RTL module elaborates from its declaring file"),
    "fpga": f"{_BASELINE_CONTROL}; `--no-cache` forces a fresh implementation",
    "lint": "`--scope <file,...>` filters reported findings to selected files",
    "sim": (
        "`--test <name>` selects a test, `--skip <name,...>` excludes tests, "
        "and `--trace` captures waveforms for the simulation run. Focused Cocotb "
        "output summarizes unselected skips; pass `--result-verbosity full` to print "
        "every XML testcase entry (the complete XML and JSON artifacts are always retained)"
    ),
    "synth": (
        f"{_BASELINE_CONTROL}; `--default-clock <ps>` explicitly "
        "supplies a clock only when the Target has no SDC"
    ),
}


# Reviewer focus names and invocations come from the registry. These blurbs are
# the human explanation that registry metadata intentionally does not carry.
# A test requires every reviewer criterion to have one, so a new focus cannot
# silently appear as an unexplained row.
_REVIEW_FOCUS_DESCRIPTIONS: dict[str, str] = {
    "review_rtl_bugs": (
        "Functional bug patterns, synthesis hazards, reset/width/signing mistakes, "
        "and ifdef/config consistency"
    ),
    "review_rtl_protocol": (
        "Bus/protocol rule compliance, handshake behavior, ordering, and "
        "clock-domain crossings (CDC)"
    ),
    "review_rtl_spec": (
        "Spec compliance: the RTL implements what the ticket/spec requires, no more and no less"
    ),
    "review_rtl_code_style": (
        "Comments, naming, readability, maintainability, magic values, and "
        "assertion/cover-point quality"
    ),
    "review_rtl_optimization": (
        "Unused/dead RTL and strict power/performance/area improvements with no "
        "functional or engineering trade-off"
    ),
    "review_rtl_security": (
        "Fault-injection resistance, simple power/timing leakage, secret "
        "exposure, and unsafe failure behavior"
    ),
    "review_tb_quality": (
        "False-pass paths, missing checks and edge cases, coverage gaps, "
        "timing/sampling mistakes, and TB code quality"
    ),
}


def _summarize(text: str) -> str:
    """One-line summary of an endpoint description for a human-sized table.

    Keeps the first sentence, hard-capped. An endpoint's ``description`` is written
    for the *agent* that has to pick flags from it, so it spells out contracts
    (asic_synthesize's SDC rules, simulate's --trace advice) at a length no
    reader wants in a reference table. Callers that render for humans summarize;
    the agent still gets the full text through the MCP schema and `--help`.
    """
    cleaned = " ".join(text.split())
    idx = cleaned.find(". ")
    if idx != -1:
        cleaned = cleaned[:idx]
    if len(cleaned) > _PURPOSE_SUMMARY_MAX:
        cleaned = cleaned[: _PURPOSE_SUMMARY_MAX - 1].rstrip() + "…"
    return cleaned.replace("|", "\\|")


def _format_satisfies(satisfies: tuple[str, ...]) -> str:
    """Render the criterion families an endpoint sets as ``name`` or ``prefix_*``.

    An endpoint that sets one family shows it verbatim; an endpoint that sets several
    (e.g. ``reviewer`` across focuses) collapses to their common ``prefix_*``.
    """
    if not satisfies:
        return "—"
    if len(satisfies) == 1:
        return f"`{satisfies[0]}`"
    prefix = satisfies[0]
    for name in satisfies[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
    return f"`{prefix}*`" if prefix else "—"


def render_flow_reference(
    *,
    project_mcp_tools_dir: Path | None = None,
    execution_column: bool = True,
) -> str:
    """Render the Booley Flows Markdown block from the MCP endpoint registry.

    Purpose cells are always summarized to one line: every consumer of this
    block (the committed docs, ``booley cheat``) renders for a human, and the
    full agent-facing description is a paragraph, not a table cell. See
    :func:`_summarize`.

    Args:
        project_mcp_tools_dir: Optional ``.booley_project/mcp_tools/`` directory; when
            given, project-specific Flows are discovered and rendered too.
        execution_column: Retained for compatibility; ignored because all
            Flows execute inside the Session Runtime.
    """
    endpoints = discover_mcp_tools(project_mcp_tools_dir=project_mcp_tools_dir)
    flows = sorted(
        (endpoint for endpoint in endpoints if endpoint.kind == "flow"),
        key=lambda t: (
            _FLOW_DISPLAY_RANK.get(t.name, len(_FLOW_DISPLAY_RANK)),
            t.name,
        ),
    )
    lines: list[str] = []
    lines.append("Deterministic end-to-end orchestration; no LLM:")
    lines.append("")
    lines.append("| Booley Flow | Purpose | Sets |")
    lines.append("|--------|---------|------|")
    for t in flows:
        lines.append(
            f"| `{t.name}` | {_summarize(t.description)} | {_format_satisfies(t.satisfies)} |"
        )
    controls = [
        f"- `{flow.name}`: {_FLOW_KEY_CONTROLS[flow.name]}"
        for flow in flows
        if flow.name in _FLOW_KEY_CONTROLS
    ]
    if controls:
        lines.extend(
            [
                "",
                "Common controls: `--target <name,...>` selects Target(s); "
                "`--dry-run` prints commands without executing them; "
                "`booley flow <name> --help` shows the full contract.",
                "",
                "Key Flow-specific controls:",
                "",
                *controls,
            ]
        )
    return "\n".join(lines)


def _render_reviewer_reference(satisfies_args: dict[str, str] | None) -> list[str]:
    """Render every registry-declared reviewer focus and its human meaning."""
    commands = satisfies_args or {}
    lines = [
        "#### `reviewer`",
        "",
        "Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and "
        "`MINOR` findings. A terminal `_done` review reports findings without "
        "triggering fixes; `_clean` requires every finding to be verified fixed "
        "or explicitly waived with user-visible justification.",
        "Call `reviewer --scope <file,...> --category <category> --focus <focus>`.",
        "",
        "| Category | Focus | What it checks | Sets |",
        "|----------|-------|----------------|------|",
    ]
    for criterion, description in _REVIEW_FOCUS_DESCRIPTIONS.items():
        invocation = commands.get(criterion)
        if invocation is None:
            continue
        parts = invocation.split()
        category = parts[parts.index("--category") + 1]
        focus = parts[parts.index("--focus") + 1]
        lines.append(f"| `{category}` | `{focus}` | {description} | `{criterion}` |")
    lines.extend(
        [
            "",
            "Controls: `--scope <file,...>` selects files; `--diff-ref <git-ref>` "
            "reviews only the diff; repeatable `--steer` adds review context. "
            "The `spec` focus needs the ticket/spec text: Ticket Mode resolves it "
            "automatically, while Interactive Mode uses `--ticket <path>`.",
        ]
    )
    return lines


def _render_mutation_tester_reference() -> list[str]:
    """Render mutation goals and controls from the specialist's CLI contract."""
    return [
        "#### `mutation_tester`",
        "",
        "Proposal-locked mutation testing. A read-only LLM creator returns exact "
        "source replacements; Booley runs a pristine baseline, then compiles and "
        "tests each replacement in isolation. It does not parse HDL or inject "
        "runtime selectors.",
        "",
        "**Mutation campaign modes:**",
        "",
        "| Campaign | Ticket Mode (`mandatory` or `optional`) | Standalone CLI options |",
        "|----------|-----------------------------------------|------------------------|",
        "| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal options)_ — the same 10-of-10 campaign |",
        "| Explicit fixed | add `total: N` and `min_detected: K` | `--count N` requires all N; add `--min-detected K` to require K |",
        "| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `--count auto`; add `--min-detected K` for an explicit threshold |",
        "",
        "Standalone `--dry-run` prints the source-size breakdown and proposed "
        "auto count without running mutations.",
        "",
        "Targeting and reuse: `--scope <rtl-file,...>` chooses mutation sites; "
        "`--target <sim-target>` chooses the complete runnable Target suite; "
        "`--steer <context>` biases mutation selection. A valid lock "
        "is reused on later runs, so new steering takes effect only with "
        "`--regen-lock`. Standalone calls can supply `--dut-files`, `--dut-top` "
        "as a prompt hint, and `--tb-top` for classic simulator Targets.",
    ]


def render_specialists_reference(*, project_mcp_tools_dir: Path | None = None) -> str:
    """Render Specialists as a separate, task-oriented reference."""
    specialists = sorted(
        (
            t
            for t in discover_mcp_tools(project_mcp_tools_dir=project_mcp_tools_dir)
            if t.kind == "specialist"
        ),
        key=lambda t: t.name,
    )
    lines = [
        "LLM-backed sub-agents running in scoped, isolated workspaces:",
        "",
        "| Specialist | Purpose | Sets | Modifies code |",
        "|------------|---------|------|:-------------:|",
    ]
    for t in specialists:
        modifies = "yes" if t.code_modifying else "—"
        lines.append(
            f"| `{t.name}` | {_summarize(t.description)} "
            f"| {_format_satisfies(t.satisfies)} | {modifies} |"
        )
    reviewer = next((t for t in specialists if t.name == "reviewer"), None)
    mutation_tester = next((t for t in specialists if t.name == "mutation_tester"), None)
    if reviewer is not None:
        lines.extend(["", *_render_reviewer_reference(reviewer.satisfies_args)])
    if mutation_tester is not None:
        lines.extend(["", *_render_mutation_tester_reference()])
    return "\n".join(lines)


def render_flow_specialist_reference(
    *,
    project_mcp_tools_dir: Path | None = None,
    execution_column: bool = True,
) -> str:
    """Render the combined reference used by the longer documentation."""
    flows = render_flow_reference(
        project_mcp_tools_dir=project_mcp_tools_dir,
        execution_column=execution_column,
    )
    specialists = render_specialists_reference(project_mcp_tools_dir=project_mcp_tools_dir)
    return f"**Booley Flows**\n\n{flows}\n\n**Specialists**\n\n{specialists}"


def _generated_markers(name: str) -> tuple[str, str]:
    """Return the marker pair for one generated reference block."""
    if name == "flows":
        return BEGIN_MARKER, END_MARKER
    if name == "specialists":
        return SPECIALISTS_BEGIN_MARKER, SPECIALISTS_END_MARKER
    raise ValueError(f"unknown generated reference block: {name}")


def splice_generated(doc_text: str, body: str, *, name: str = "flows") -> str:
    """Replace one generated reference block in ``doc_text``."""
    begin_marker, end_marker = _generated_markers(name)
    try:
        start = doc_text.index(begin_marker) + len(begin_marker)
        end = doc_text.index(end_marker)
    except ValueError as exc:
        raise ValueError(
            f"{name} markers ({begin_marker} / {end_marker}) not found in document"
        ) from exc
    return doc_text[:start] + "\n" + body + "\n" + doc_text[end:]


def extract_generated(doc_text: str, *, name: str = "flows") -> str:
    """Return one generated block, excluding its markers."""
    begin_marker, end_marker = _generated_markers(name)
    try:
        start = doc_text.index(begin_marker) + len(begin_marker)
        end = doc_text.index(end_marker)
    except ValueError as exc:
        raise ValueError(
            f"{name} markers ({begin_marker} / {end_marker}) not found in document"
        ) from exc
    return doc_text[start:end].strip("\n")


def _main(argv: list[str] | None = None) -> int:
    """Regenerate committed blocks in the given files (or print to stdout)."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Render the canonical Booley Flows & Specialists block.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Markdown files whose generated block should be rewritten in "
        "place. With no paths, the block is printed to stdout.",
    )
    args = parser.parse_args(argv)

    body = render_flow_specialist_reference()
    if not args.paths:
        print(body)
        return 0
    for path in args.paths:
        pth = Path(path)
        text = pth.read_text(encoding="utf-8")
        if SPECIALISTS_BEGIN_MARKER in text:
            text = splice_generated(text, render_flow_reference(), name="flows")
            text = splice_generated(
                text,
                render_specialists_reference(),
                name="specialists",
            )
        else:
            text = splice_generated(text, body)
        pth.write_text(text, encoding="utf-8")
        print(f"updated {pth}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
