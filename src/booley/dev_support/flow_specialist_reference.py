"""Canonical Booley Flow and Specialist references, rendered from the registry.

Single source of truth: :func:`booley.mcp.registry.discover_mcp_tools`. The
``booley cheat`` CLI renders both blocks live, and ``src/booley/data/cheatsheet.md``
embeds committed copies between the ``<!-- BEGIN GENERATED: flows -->`` and
``<!-- BEGIN GENERATED: specialists -->`` marker pairs that a pytest keeps
byte-identical to this renderer. The user docs link to their own Flow and
Specialist pages instead of embedding a copy. Add, rename, or retire a
Flow/Specialist and the cheatsheet lists update from this one place.

Regenerate the committed blocks with::

    python -m booley.dev_support.flow_specialist_reference src/booley/data/cheatsheet.md
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
EXCLUDED_MCP_TOOLS = frozenset()


# Backstop on the Purpose cell: a full multi-sentence description produced a
# ~700-char-wide column that overflowed every terminal and truncated the next
# column mid-token (QA_REPORT A3). It bounds that wall rather than fitting 80
# columns — the table is wider than a terminal either way. Keep it above the
# longest first sentence in the registry, so the common case reads as a whole
# sentence and the "…" only ever means an endpoint overwrote its opener.
_PURPOSE_SUMMARY_MAX = 120


# Present built-in Booley Flows in the order a reader encounters them in a normal
# hardware flow. Project-defined Booley Flows follow these, sorted by name.
_FLOW_DISPLAY_RANK = {
    name: rank for rank, name in enumerate(("sim", "lint", "synth", "fpga", "mutation"))
}

_BASELINE_CONTROL = "`--baseline <ref>` compares metrics against a git revision"


_FLOW_KEY_CONTROLS: dict[str, str] = {
    "fpga": f"{_BASELINE_CONTROL}; `--ppa-profile compact|balanced|max_frequency` "
    "selects portable optimization intent; `--no-cache` forces a fresh implementation",
    "lint": "`--scope <file,...>` filters reported findings to selected files",
    "mutation": (
        "`--target` names exactly one Simulation Target whose complete suite judges each "
        "Mutant; `--scope <rtl-file,...>` chooses mutation sites (default: the Target's own "
        "RTL, minus testbench, vendored, and generated files); `--count N|auto` sets the "
        "Mutant count (default 10; `auto` scales 3-25 from source size and the time budget) "
        "and `--min-detected K` the pass threshold (default ceil(0.8 x count), of the Mutants "
        "scored when fewer run); `--seed <N>` "
        "(default 0) picks another reproducible Mutant set; `--dry-run` previews the count "
        "without running a simulator. A Mutant that fails to build is dropped and the next "
        "candidate takes its place; a timed-out Mutant counts as `detected (timeout)`; "
        "the default is 10 Mutants with 8 detected"
    ),
    "sim": (
        "`--mode elab-only` compiles, elaborates, and links without running tests; "
        "`--mode elab-only-standalone` adds the stronger module sweep. "
        "Repeat `--test <test-run>` for an exact ordered suite or use `--tests-file <path>`; "
        "a Test Run is `test`, `test+variant`, `test@seed`, `test+variant@seed`, or "
        "`test[+variant]@xN` for N drawn seeds, and `--seed <N>|random` sets or draws the "
        "seed of every selected Test Run; "
        "there is no CLI `--skip`, and configured skips apply only to unfiltered selection. "
        "A Developer Agent's MCP tool call passes the same suite as a `test` array. "
        "Resume one exact durable "
        "Simulation Record with `--resume-from <exact-manifest.json>` (optionally with "
        "`--dry-run`): Cocotb retries the whole batch; a coverage Simulation Record never "
        "resumes (exit 2: the refusal prints the new `booley flow sim --coverage` run that "
        "repeats its Test Runs, seeds and report directory). "
        "`--coverage` / `--cov` collects a native Coverage Record, "
        "and `--trace` captures waveforms for the simulation run. Focused Cocotb "
        "output summarizes unselected skips; the complete XML and JSON results are always retained"
    ),
    "synth": (
        f"{_BASELINE_CONTROL}; physical Targets must own an SDC fileset that creates a clock"
    ),
}


# Reviewer focus rows: the human explanation that registry metadata intentionally
# does not carry, keyed by (category, focus). A test ties the keys to the Reviewer's
# focus sets, so a new focus cannot silently appear as an unexplained row.
_REVIEW_FOCUS_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("rtl", "bugs"): (
        "Functional bug patterns, synthesis hazards, reset/overflow/signing mistakes, "
        "and ifdef consistency across built Targets"
    ),
    ("rtl", "protocol"): (
        "Bus/protocol rule compliance, handshake behavior, ordering, and "
        "clock-domain crossings (CDC)"
    ),
    ("rtl", "spec"): (
        "Spec compliance: the RTL implements what the change request/spec requires, no more and no less"
    ),
    ("rtl", "code_style"): (
        "Comments, naming, readability, maintainability, magic values, and "
        "assertion/cover-point quality"
    ),
    ("rtl", "optimization"): (
        "Functionally dead RTL and strict power/performance/area improvements with no "
        "functional or engineering trade-off"
    ),
    ("rtl", "security"): (
        "Fault-injection resistance, simple power/timing leakage, secret "
        "exposure, and unsafe failure behavior"
    ),
    ("tb", "quality"): (
        "False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, "
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


def render_flow_reference(
    *,
    project_mcp_tools_dir: Path | None = None,
) -> str:
    """Render the Booley Flows Markdown block from the MCP endpoint registry.

    The table is followed by the common and per-Flow control summaries.
    Purpose cells are always summarized to one line: ``booley cheat`` renders
    for a human, and the full agent-facing description is a paragraph, not a
    table cell. See :func:`_summarize`.

    Args:
        project_mcp_tools_dir: Optional ``.booley/mcp_tools/`` directory; when
            given, project-specific Flows are discovered and rendered too.
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
    lines.append("| Booley Flow | Purpose |")
    lines.append("|--------|---------|")
    for t in flows:
        lines.append(f"| `{t.name}` | {_summarize(t.description)} |")
    controls = [
        f"- `{flow.name}`: {_FLOW_KEY_CONTROLS[flow.name]}"
        for flow in flows
        if flow.name in _FLOW_KEY_CONTROLS
    ]
    if controls:
        lines.extend(
            [
                "",
                "Common controls: repeat `--target` or use comma-separated values; "
                "`--target a --target b,c` preserves the order `a`, `b`, `c`, and "
                "duplicate resolved Targets are rejected. A Developer Agent's MCP tool call "
                "takes one comma-separated `target` string instead. "
                "`--dry-run` returns a normalized plan without executing EDA; "
                "`booley flow <name> --help` shows the full contract.",
                "",
                "Key Flow-specific controls:",
                "",
                *controls,
            ]
        )
    return "\n".join(lines)


_REVIEWER_RECHECK = (
    '**Re-check:** after fixing findings, pass `recheck="<evidence file>"` '
    "(CLI: `--recheck <evidence file>`), the `Reviewer evidence:` path of the "
    "earlier review or re-check, instead of running a new review. Run one full "
    "review, then re-check until its findings are fixed; run another full review "
    "only on purpose. Each open finding becomes `fixed`, `still open`, or `can't "
    "tell`, and no new finding is reported. `fixed` needs current code that shows "
    "it. Category, focus, and scope come from the file, so `scope`, `base`, "
    "`category`, `focus`, `spec`, `spec_text`, and `steer` are refused. With "
    "`fail_on`, only `still open` and `can't tell` findings count. To leave a "
    'finding as it is, pass `waive=[["<finding-id>", "<reason>"]]` (CLI: `--waive '
    '<finding-id> "<reason>"`) with `recheck`; the Reviewer accepts or refuses '
    "each proposal, and an accepted `waived` finding never counts for `fail_on`."
)


def _render_reviewer_reference() -> list[str]:
    """Render every registry-declared reviewer focus and its human meaning."""
    lines = [
        "#### `reviewer`",
        "",
        "Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and "
        "`MINOR` findings. It reviews the selected files using your specification or "
        "steering.",
        "",
        "Interactive source review accepts Verilog/SystemVerilog (.v, .vh, .sv, .svh), plus "
        "Python (.py) and explicitly scoped C/C++ testbench sources and headers (.c, .cc, .cpp, "
        ".cxx, .h, .hh, .hpp, .hxx) for category=tb. Sim C/C++ harness inputs count as TB for "
        "isolation and cannot be mutation scope. Declared opposite-category sources are refused; "
        "unregistered scoped sources retain the registration warning and receive only an exact "
        "per-file category grant. Each Reviewer has the opposite category's sources and HDL files "
        "no .core fileset declares hidden; all other project files, such as firmware, are "
        "readable context, and findings outside the scope are filtered.",
        "",
        "The result links saved review evidence, including rejected proposals for "
        "inspection. Rejected proposals do not affect Criteria.",
        "",
        'Ask your Developer Agent to call the `reviewer` Specialist with `category="<category>"` and `focus="<focus>"`.',
        "",
        "| Category | Focus | What it checks |",
        "|----------|-------|----------------|",
    ]
    for (category, focus), description in _REVIEW_FOCUS_DESCRIPTIONS.items():
        lines.append(f"| `{category}` | `{focus}` | {description} |")
    lines.extend(
        [
            "",
            'Also pass `scope="<file,...>"`. Add `spec="<path>"` '
            '(required for the `spec` focus) or `spec_text="<text>"` (CLI: `--spec-text`; '
            'not with `spec`), or `steer=["<context>"]` for context. A completed review exits 0; '
            'pass `fail_on="critical|major|minor"` (CLI: `--fail-on`) to exit 1 when an open '
            "finding is at or above that severity. "
            "`dry_run=true` previews the call. Each new review replaces the previous findings.",
            "",
            '**Changes only:** add `base="<commit>"` (CLI: `--base <commit>`) to review only '
            "the `scope` files changed since the merge-base of that commit and HEAD, "
            "uncommitted changes included. Unchanged `scope` files that "
            "use them are context, the diff and commit messages are given to the Reviewer, and "
            "no change in scope exits 0 without a review. Pass every file of the category as "
            "the `scope` to give the Reviewer the files that use the changed ones. Findings "
            "already present before the change are listed separately as pre-existing and are "
            "never open.",
            "",
            _REVIEWER_RECHECK,
        ]
    )
    return lines


def _render_coverage_analyst_reference() -> list[str]:
    """Render the unified Coverage Record input contract."""
    return [
        "#### `coverage_analyst`",
        "",
        'Ask your Developer Agent to call the `coverage_analyst` Specialist with `record="<exact-coverage.json>"` '
        'and optional `instruction="<question>"`. The Analyst explains retained '
        "native evidence and proposes advisory next steps; its model only reads evidence. "
        "It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. "
        "Screened Waiver Candidates are advisory and need human approval; advisory "
        "reports do not mark strict coverage met. "
        "Verified Target sources are optional; stale sources give report-only analysis.",
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
        "Specialists running in scoped, isolated workspaces:",
        "",
        "Ask your Developer Agent session to invoke a Specialist by name with the arguments below "
        '(written as it passes them, `name="value"`; the CLI spells each as a flag, `--name value`). '
        "Or run `booley specialist <name> [args...]` inside the Sandbox. "
        "`booley specialist` lists visible Specialists; "
        "`booley specialist <name> --help` shows their arguments. "
        "`--max-turns` and `--timeout` are CLI-only controls.",
        "",
        "For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. "
        "Common options are `-C/--project PATH`, `--report-dir`, "
        "and `--target` where supported. `--timeout DURATION` accepts positive "
        "seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). "
        "Model-call minimum budgets apply; seconds-only providers round up. "
        "A CLI run is a Standalone Job: it shows in "
        "the Dashboard and copies its output to `job_logs/standalone/<run_id>/job.log` "
        "in the Project's common Booley Directory (the main checkout's `.booley` from a linked "
        "worktree).",
        "",
        "| Specialist | Purpose | Modifies code |",
        "|------------|---------|:-------------:|",
    ]
    for t in specialists:
        modifies = "yes" if t.code_modifying else "—"
        lines.append(f"| `{t.name}` | {_summarize(t.description)} | {modifies} |")
    reviewer = next((t for t in specialists if t.name == "reviewer"), None)
    coverage_analyst = next((t for t in specialists if t.name == "coverage_analyst"), None)
    if coverage_analyst is not None:
        lines.extend(["", *_render_coverage_analyst_reference()])
    if reviewer is not None:
        lines.extend(["", *_render_reviewer_reference()])
    return "\n".join(lines)


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
    """Regenerate the committed Flow and Specialist blocks in the given files."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Render the canonical Booley Flows and Specialists blocks.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Markdown files whose generated blocks should be rewritten in place.",
    )
    args = parser.parse_args(argv)

    for path in args.paths:
        pth = Path(path)
        text = pth.read_text(encoding="utf-8")
        text = splice_generated(text, render_flow_reference(), name="flows")
        text = splice_generated(text, render_specialists_reference(), name="specialists")
        pth.write_text(text, encoding="utf-8")
        print(f"updated {pth}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
