"""Canonical Booley Flow and Specialist references, rendered from the registry.

Single source of truth: :func:`booley.mcp.registry.discover_mcp_tools`. The
``booley cheat`` CLI renders this block live, while ``docs/user/CLI-REFERENCE.md`` embeds a
committed copy between the ``<!-- BEGIN GENERATED: flows -->`` /
``<!-- END GENERATED: flows -->`` markers that a pytest keeps byte-identical to
this renderer. The cheatsheet gives Specialists their own flag-addressable
block. Add, rename, or retire a Flow/Specialist and the lists update everywhere
from this one place.

Regenerate the committed doc blocks with::

    python -m booley.dev_support.flow_specialist_reference \\
        docs/user/CLI-REFERENCE.md src/booley/data/cheatsheet.md
"""

from __future__ import annotations

from pathlib import Path

from booley.goals.capabilities import evidence_goal_name
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
# ~700-char-wide column that overflowed every terminal and truncated the Sets
# column mid-token (QA_REPORT A3). It bounds that wall rather than fitting 80
# columns — the table is wider than a terminal either way. Keep it above the
# longest first sentence in the registry, so the common case reads as a whole
# sentence and the "…" only ever means an endpoint overwrote its opener.
_PURPOSE_SUMMARY_MAX = 120


# Present built-in Booley Flows in the order a reader encounters them in a normal
# hardware flow. Project-defined Booley Flows follow these, sorted by name.
_FLOW_DISPLAY_RANK = {name: rank for rank, name in enumerate(("sim", "lint", "synth", "fpga"))}

_BASELINE_CONTROL = "`--baseline <ref>` compares metrics against a git revision"


# Docs render: user docs link to the full per-Flow reference instead of
# repeating the control summaries that `booley cheat` carries.
_FLOW_REFERENCE_POINTER = (
    "Shared Flow options and results are in "
    "[FLOW_REFERENCE.md](FLOW_REFERENCE.md), each Flow's own in "
    "[FLOW-SIM.md](FLOW-SIM.md), [FLOW-LINT.md](FLOW-LINT.md), "
    "[FLOW-SYNTH.md](FLOW-SYNTH.md), and [FLOW-FPGA.md](FLOW-FPGA.md), and report files in "
    "[FLOW_REPORTS.md](../internals/FLOW_REPORTS.md); `booley flow <name> --help` "
    "prints the options too."
)

_FLOW_KEY_CONTROLS: dict[str, str] = {
    "fpga": f"{_BASELINE_CONTROL}; `--ppa-profile compact|balanced|max_frequency` "
    "selects portable optimization intent; `--no-cache` forces a fresh implementation",
    "lint": "`--scope <file,...>` filters reported findings to selected files",
    "sim": (
        "`--mode elab-only` compiles, elaborates, and links without running tests; "
        "`--mode elab-only-standalone` adds the stronger module sweep. "
        "Repeat `--test <test-run>` for an exact ordered suite or use `--tests-file <path>`; "
        "a Test Run is `test`, `test+variant`, `test@seed`, `test+variant@seed`, or "
        "`test[+variant]@xN` for N drawn seeds, and `--seed <N>|random` sets or draws the "
        "seed of every selected Test Run; "
        "there is no CLI `--skip`, and configured skips apply only to unfiltered selection. "
        "MCP passes the same suite as a `test` array. Resume one exact durable "
        "Simulation Campaign with `--resume-from <exact-manifest.json>` (optionally with "
        "`--dry-run`): Cocotb retries the whole batch; a coverage Campaign resumes only "
        "after its collection finished (otherwise exit 2: start a new `--coverage` run). "
        "`--coverage` / `--cov` collects a native Coverage Campaign, "
        "and `--trace` captures waveforms for the simulation run. Focused Cocotb "
        "output summarizes unselected skips; pass `--result-verbosity full` to print "
        "every XML testcase entry (the complete XML and JSON artifacts are always retained)"
    ),
    "synth": (
        f"{_BASELINE_CONTROL}; physical Targets must own an SDC fileset that creates a clock"
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
        "Spec compliance: the RTL implements what the change request/spec requires, no more and no less"
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


def _format_satisfies(satisfies: tuple[str, ...]) -> str:
    """Present only supported Goal names, keeping producer metadata internal."""
    names = dict.fromkeys(name for key in satisfies if (name := evidence_goal_name(key)))
    return ", ".join(f"`{name}`" for name in names) or "—"


def render_flow_reference(
    *,
    project_mcp_tools_dir: Path | None = None,
    execution_column: bool = True,
    key_controls: bool = True,
) -> str:
    """Render the Booley Flows Markdown block from the MCP endpoint registry.

    Purpose cells are always summarized to one line: every consumer of this
    block (the committed docs, ``booley cheat``) renders for a human, and the
    full agent-facing description is a paragraph, not a table cell. See
    :func:`_summarize`.

    Args:
        project_mcp_tools_dir: Optional ``.booley/mcp_tools/`` directory; when
            given, project-specific Flows are discovered and rendered too.
        execution_column: Retained for compatibility; ignored because all
            Flows execute inside the Sandbox.
        key_controls: When true, append the common and per-Flow control
            summaries (used by ``booley cheat``, which is read in the Sandbox).
            When false, link to FLOW_REFERENCE.md and the per-Flow pages
            instead, for the user docs that sit next to them.
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
    if not key_controls:
        lines.extend(["", _FLOW_REFERENCE_POINTER])
        return "\n".join(lines)
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
                "duplicate resolved Targets are rejected. MCP keeps one "
                "comma-separated `target` string. "
                "`--dry-run` returns a normalized plan without executing EDA; "
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
        "`MINOR` findings. In Interactive Mode, review the selected files using your "
        "specification or steering. In Goal Mode, `done` reports findings without "
        "requiring fixes; the review package includes open findings. `clean` requires "
        "every finding to be fixed "
        "or waived with a justification.",
        "",
        "Interactive source review accepts Verilog/SystemVerilog (.v, .vh, .sv, .svh), plus "
        "Python (.py) and explicitly scoped C/C++ testbench sources and headers (.c, .cc, .cpp, "
        ".cxx, .h, .hh, .hpp, .hxx) for category=tb. Goal Mode also accepts other explicitly "
        "scoped support files. Sim C/C++ harness inputs count as TB for isolation and cannot be "
        "mutation scope. Declared opposite-category sources are refused in either mode; "
        "unregistered scoped sources retain the registration warning and receive only an exact "
        "per-file category grant.",
        "",
        "The result links saved review evidence, including rejected proposals for "
        "inspection. Rejected proposals do not affect Criteria.",
        "",
        'Call the `reviewer` Specialist from your Developer Agent session with `category="<category>"` and `focus="<focus>"`.',
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
        lines.append(
            f"| `{category}` | `{focus}` | {description} | `{evidence_goal_name(criterion) or 'Goal unavailable'}` |"
        )
    lines.extend(
        [
            "",
            '**Interactive Mode:** also pass `scope="<file,...>"`. Add `spec="<path>"` '
            '(required for the `spec` focus) or `steer=["<context>"]` for context. '
            "`dry_run=true` previews the call. Each new review replaces the previous findings.",
            "",
            "**Goal Mode:** the Review Goal fixes scope and spec, so `scope`, `spec` and "
            "`steer` are refused. Declare each Review Goal with exactly one `target` or "
            "`paths` (see the Goal catalog).",
            "",
            "**Verify fixes (both modes):** after fixing findings, call `reviewer` with "
            "`verify=true` (CLI: `--verify`). It rechecks only the latest review's open "
            "findings, finds nothing new, and passes when none remain open. To argue a "
            'finding away, add `waive=[["<finding-id>", "<justification>"]]` (CLI: '
            '`--waive <finding-id> "<justification>"`, repeatable); the Reviewer accepts '
            "or rejects each. No other argument is accepted with verify. After repeated "
            "failed verifies, Goal Mode stops and asks you to change or abandon the Goal.",
        ]
    )
    return lines


def _render_mutation_tester_reference() -> list[str]:
    """Render mutation goals and controls from the Specialist's MCP contract."""
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
        "| Campaign | Goal arguments | Interactive Mode arguments |",
        "|----------|-----------------------------------------|------------------------|",
        "| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal arguments)_ — the same 10-of-10 campaign |",
        '| Explicit fixed | add `total: N` and `min_detected: K` | `count="N"` requires all N; add `min_detected=K` to require K |',
        '| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `count="auto"`; add `min_detected=K` for an explicit threshold |',
        "",
        "`dry_run=true` validates Target metadata and prints the source-size breakdown "
        "and proposed auto count without invoking a Specialist or simulator.",
        "",
        'Call the `mutation_tester` Specialist from your Developer Agent session: `scope="<rtl-file,...>"` chooses mutation sites; '
        '`target="<sim-target>"` chooses exactly one complete runnable Target suite; '
        '`steer=["<context>"]` biases mutation selection. A valid lock '
        "is reused on later runs, so new steering takes effect only with "
        "`regen_lock=true`. The Target supplies the testbench top and complete RTL "
        "closure; they are not separate caller inputs.",
    ]


def _render_coverage_analyst_reference() -> list[str]:
    """Render the unified coverage campaign input contract."""
    return [
        "#### `coverage_analyst`",
        "",
        'Call the `coverage_analyst` Specialist from your Developer Agent session with `campaign="<exact-coverage.json>"` '
        'and optional `instruction="<question>"`. The Analyst explains retained '
        "native evidence and proposes advisory next steps; its model only reads evidence. "
        "It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. "
        "In Goal Mode, screened Waiver Candidates support a proposed Goal change, "
        "which needs human approval. Finish requires every Goal met; advisory "
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
        "LLM-powered Specialists running in scoped, isolated workspaces:",
        "",
        "Ask your Developer Agent session to invoke a Specialist by name with the arguments below. "
        "Or run `booley specialist <name> [args...]` inside the Sandbox. "
        "`booley specialist` lists visible Specialists; "
        "`booley specialist <name> --help` shows their arguments. "
        "`--model`, `--max-turns`, and `--timeout` are CLI-only controls.",
        "",
        "For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. "
        "The supported module alternative is `python -m booley.specialists.reviewer` "
        "with the same flags. Common options are `-C/--project PATH`, `--report-dir`, "
        "`--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive "
        "seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). "
        "Model-call minimum budgets apply; seconds-only providers round up. "
        "CLI Flow and Specialist runs never record Goal evidence; use the Developer Agent's "
        "Goal-bound MCP calls with `work_dir`.",
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
    coverage_analyst = next((t for t in specialists if t.name == "coverage_analyst"), None)
    if coverage_analyst is not None:
        lines.extend(["", *_render_coverage_analyst_reference()])
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
        key_controls=False,
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
