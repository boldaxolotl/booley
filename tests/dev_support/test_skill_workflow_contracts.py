"""Regression contracts for shipped ticket workflow skills."""

import re

import pytest

from booley.runtime.paths import skills_dir


def _skill_text(name: str, relative: str = "SKILL.md") -> str:
    return (skills_dir() / name / relative).read_text(encoding="utf-8")


def _compact_skill_text(name: str, relative: str = "SKILL.md") -> str:
    return " ".join(_skill_text(name, relative).split())


def test_feedback_routes_private_project_bugs_through_verified_synthetic_reproducer():
    skill = " ".join(_skill_text("booley-feedback").split())
    reproducer = " ".join(_skill_text("booley-feedback", "minimal-reproducer.md").split())

    assert "[minimal-reproducer.md](minimal-reproducer.md)" in skill
    assert "private RTL, testbench, configuration, or logs" in skill
    assert "Private source, original project logs, and scratch reductions stay unattached" in skill
    assert "cannot change a title, exposed-by, or step, or remove attachments" in skill
    for required in (
        "Standalone",
        "Synthetic",
        "Equivalent",
        "Repeatable",
        "Same Booley component, source path, and stable diagnostic fingerprint",
        "counterfactual",
        "below 120 lines and 8,000 characters",
        "never anonymous or guaranteed safe",
    ):
        assert required in reproducer


def test_feedback_exports_offline_for_manual_github_or_email_submission():
    skill = " ".join(_skill_text("booley-feedback").split())

    for required in (
        "runs offline in the Sandbox",
        "use local commands only",
        "booley feedback export F-8 F-9",
        "Read the entire exported file",
        "**Submit on GitHub:**",
        "https://github.com/boldaxolotl/Booley/issues/new",
        "**Email the maintainer:**",
        "mailto:boldaxolotl@proton.me",
        "report ready to send, not submitted",
        "No verified workaround was found",
    ):
        assert required in skill


def test_feedback_requires_an_explicit_request_before_redacted_export():
    skill = _compact_skill_text("booley-feedback")

    assert "Do not create a sanitized report by default" in skill
    assert "export only on explicit request" in skill
    assert "Stop here unless the user explicitly requested a sanitized file" in skill
    assert "Without an explicit export request" in skill


def test_setup_does_not_offer_removed_feedback_submission_workflow():
    main = _compact_skill_text("booley-setup")
    findings = _compact_skill_text("booley-setup", "steps/6-findings.md")
    cleanup = _compact_skill_text("booley-setup", "steps/7-cleanup.md")

    assert "creates a redacted export only when the user explicitly requests one" in main
    assert "asks once whether to send" not in main
    assert "A separate redacted export is created only when the user explicitly" in findings
    assert "only when the user explicitly asks" in findings
    assert "one-time Feedback offer" not in cleanup


def test_setup_grills_one_dependency_frontier_per_round():
    plan = _skill_text("booley-setup", "steps/0-plan.md")
    greenfield = _skill_text("booley-setup", "steps/new-greenfield.md")
    contract = " ".join(f"{plan}\n{greenfield}".split())

    for required in (
        "current **frontier** is every unresolved decision whose prerequisites are already settled",
        "Ask the whole frontier in one round",
        "defer it to a later round",
        "After each response, recompute the frontier",
        "entire initial frontier normally fits in one batched message",
        "ask the user to confirm it",
    ):
        assert required in contract
    assert "one question at a time" not in contract.lower()


def test_setup_preserves_upstream_verdict_sources_when_adapter_is_sufficient():
    plan = " ".join(_skill_text("booley-setup", "steps/0-plan.md").split()).replace("*", "")
    project_config = " ".join(
        _skill_text("booley-setup", "steps/2-project-config.md").split()
    ).replace("*", "")

    assert "Treat vendored and upstream sources as preserved inputs" in plan
    assert "plan the verdict bridge outside them" in plan
    assert "Keep upstream/vendored testbenches unchanged" in project_config
    assert "project-owned adapter, wrapper, or monitor" in project_config
    assert "Modify an upstream source only when the approved plan" in project_config


def test_setup_requires_explicit_memory_dispositions_and_synth_only_surrogates():
    plan = " ".join(_skill_text("booley-setup", "steps/0-plan.md").split())
    project_config = " ".join(_skill_text("booley-setup", "steps/2-project-config.md").split())
    template = _skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md")
    core = _skill_text("booley-setup", "CORE_TEMPLATE.yaml")

    for required in (
        "Memory implementation",
        "exported_boundary",
        "timing_surrogate",
        "An enabled synth Target with any unclassified candidate cannot be approved",
        "RTL elaboration alone never makes this row Green",
    ):
        assert required in plan
    for required in (
        "project-owned replacement seam",
        "never create a universal write-to-read data path",
        "ordinary Verilog/SystemVerilog source",
        "paramtype: vlogdefine",
        "Do not introduce a manifest or custom file type",
        "surrogate contributes zero inferred-memory cells",
        "does not scale with the original memory depth",
    ):
        assert required in project_config
    assert "Memory implementation" in template
    assert "synth_memory" in core
    assert "file_type: systemVerilogSource" in core
    assert "paramtype: vlogdefine" in core
    assert "never attach this fileset or its selection define" in " ".join(core.split())
    combined = " ".join((plan, project_config, template, core))
    assert "booleyMemoryContract" not in combined
    assert "flow_options.memory_contract" not in combined


def test_heal_has_bounded_doctor_repair_and_verification_loop():
    skill = _skill_text("booley-heal")

    for required in (
        "zero `FAIL` findings",
        "zero active `WARN` findings",
        "Doctor's `fix:` hint",
        "from booley.runtime.paths import troubleshooting_path",
        "booley doctor --deep",
        "after 12 remediation passes",
        "final plain `booley doctor`",
        "host: final plain `booley doctor`",
    ):
        assert required in skill
    assert "A clean run is the final deep evidence" in skill
    assert "instead of rerunning the whole deep matrix after each edit" in skill


def test_setup_reserves_deep_doctor_for_one_final_gate():
    skill = _skill_text("booley-setup")
    project_config = _skill_text("booley-setup", "steps/2-project-config.md")
    doctor = _skill_text("booley-setup", "steps/4-doctor.md")
    greenfield = _skill_text("booley-setup", "steps/new-greenfield.md")
    agents = _skill_text("booley-setup", "AGENTS_TEMPLATE.md")

    assert "`booley doctor --deep` both exit 0" in skill
    assert "Reserve the full deep Doctor matrix" in project_config
    assert "booley doctor --deep" not in project_config
    assert "Re-validate with\n`booley doctor --deep`" not in project_config
    assert doctor.count("Run `booley doctor --deep`") == 1
    assert "a failed attempt does not count as the one successful final run" in doctor
    assert "booley doctor --deep" not in greenfield
    assert "does not schedule an additional run" in " ".join(greenfield.split())
    assert "belongs to Project Setup" in " ".join(agents.split())


def test_agents_template_limits_doctor_during_task_work():
    """Issue #885: task agents get Flows plus plain Doctor, never deep or fix-everything."""
    agents = " ".join(_skill_text("booley-setup", "AGENTS_TEMPLATE.md").split())

    for forbidden in (
        "fix every finding",
        "over the final files before handoff",
        "leave no active warnings or errors",
    ):
        assert forbidden not in agents
    for required in (
        "at most, plain `booley doctor`",
        "`booley doctor --deep` belongs to Project Setup, `/booley-heal`, "
        "and Booley version changes",
        "do not run it for task work or handoffs",
        "in the handoff instead of fixing them",
        "Never edit `booley.toml`, `.core` files, `doctor-waivers.toml`, "
        "or Ticket Board directories to silence such a finding",
        "deep verification (`/booley-heal`) is due",
    ):
        assert required in agents


def test_setup_agents_template_advertises_supported_specialists():
    agents = _skill_text("booley-setup", "AGENTS_TEMPLATE.md")

    for specialist in ("`coverage_analyst`", "`reviewer`", "`mutation_tester`"):
        assert specialist in agents


def test_heal_preserves_scope_and_routes_exceptional_findings():
    skill = _skill_text("booley-heal")

    for required in (
        "Preserve all pre-existing changes",
        "submodules discovered from `.gitmodules` as read-only",
        "Do not change RTL or testbench",
        "Do not create a Doctor waiver merely to make the output green",
        "Do not execute an action outside the current Sandbox",
        "invoke\n`/booley-feedback` yourself",
        "Booley never transmits the report",
        "Never describe one of those partial outcomes as healed",
    ):
        assert required in skill


def test_setup_asks_one_git_history_question():
    plan = _skill_text("booley-setup", "steps/0-plan.md")
    project_config = _skill_text("booley-setup", "steps/2-project-config.md")
    greenfield = _skill_text("booley-setup", "steps/new-greenfield.md")
    template = _skill_text("booley-setup", "BOOLEY_TEMPLATE.toml")

    prompt = "Keep Booley out of your git history?"
    compact_plan = " ".join(plan.split())
    assert prompt in compact_plan
    assert prompt in " ".join(greenfield.split())
    assert "Unattended: write `enabled = false`" in compact_plan.replace("*", "")
    assert "Do not omit the block" in project_config
    ignore_prompt = (
        "Should Booley ignore the repository's existing `.core` files and use only the "
        "stealth-authored cores?"
    )
    assert ignore_prompt in compact_plan
    assert "ignore_native_cores = true" in project_config
    assert "[stealth]\n" in template
    assert "enabled = false" in template


def test_setup_dependency_core_refactors_preserve_order_and_target_identity() -> None:
    project_config = _compact_skill_text("booley-setup", "steps/2-project-config.md")
    core_template = _compact_skill_text("booley-setup", "CORE_TEMPLATE.yaml")

    for required in (
        "complete resolved file order",
        "FuseSoC emits dependency-core files before adapter-core files",
        "when that ordering cannot remain identical",
        "declaring core's VLNV and the Target name",
    ):
        assert required in project_config
    assert "compose them through a dependency core" in core_template
    assert "depend: [vendor:library:shared-design:1]" in core_template
    assert "steps/2-project-config.md" in core_template


def test_setup_never_authors_timing_constraints_and_blocks_on_missing_file():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")
    project_config = _compact_skill_text("booley-setup", "steps/2-project-config.md")
    template = _compact_skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md")

    # Row 10: the only sources are the repo's own file or a user-supplied one.
    for required in (
        "**Never author, generate, or guess one.**",
        "**The repo ships it.**",
        "**The user supplies it.**",
        "the Target is **blocked** until the user provides a file",
        "Unattended: leave that Target unconfigured",
        "`synth_mode: logical` needs no SDC",
        "never switch to it silently",
        "is still authoring and is forbidden",
    ):
        assert required in plan
    assert "no upstream SDC; user must supply" in plan
    assert "somebody must author** (an SDC" not in plan
    # Step 2 wires the recorded file verbatim and never patches it.
    for required in (
        "**Timing constraints (SDC/XDC) are never authored here.**",
        "Do not change its contents",
        "Never write a placeholder SDC to unblock Doctor",
    ):
        assert required in project_config
    assert "never agent-authored (a missing file blocks the Target)" in template


def test_setup_plans_one_project_wide_tech_cell_replacement():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")
    template = _compact_skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md")

    assert "one Project-wide **Tech Cell Replacement** mapping" in plan
    assert "per-Target coverage matrix" in plan
    assert _decision_rows(_skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md"))["23"][
        3
    ].startswith("Tech Cell Replacement:")
    assert "continue numbering from 24" in template
    assert "evidence-forced: not applicable" in template
    assert (
        "Synthesis-disabled Projects resolve row 23 as evidence-forced: not applicable "
        "and omit this subsection"
    ) in template
    assert (
        "When synthesis is disabled, resolve this row as `evidence-forced: not applicable` "
        "and omit the replacement subsection"
    ) in plan
    assert "### Tech Cell Replacement" in template
    assert "Caliptra" not in plan
    assert "Nangate" not in plan
    assert "Caliptra" not in template
    assert "Nangate" not in template


def test_setup_discovers_reachable_tech_cell_inputs_by_category():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")

    for category in (
        "documented technology-integration seam",
        "direct library-cell instantiation",
        "behavioral primitive intended for inference or replacement",
        "existing synthesis-time binding or post-inference mapping",
        "other library-dependent cell use requiring review",
    ):
        assert category in plan
    for required in (
        "Flow-supplied physical-library family",
        "actual Liberty input",
        "LEF input",
        "reachable from each enabled synthesis Target",
        "including embedded cores",
        "repository-only evidence",
        "dependency-core provenance",
        "governing define or parameter",
        "discovered-but-unhandled remainder",
        "one authoritative Project-owned source location",
    ):
        assert required in plan


def test_setup_requires_evidenced_tech_cell_decisions():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")

    for required in (
        "Interactive mode asks the user to clarify the choice",
        "Unattended mode selects the mechanism supported by the strongest Project evidence",
        "Stop when ambiguity could change hardware semantics",
        "leave hierarchy coverage incomplete",
        "introduce conflicting definitions",
    ):
        assert required in plan


def test_setup_surfaces_missing_replacements_and_latch_policy():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")
    project_config = _compact_skill_text("booley-setup", "steps/2-project-config.md")

    assert "mark every affected synthesis Target Yellow" in plan
    assert "Do not create a new post-inference mapping as a fallback" in plan
    assert "migration evidence only" in project_config
    assert "expected_latches` only to the evidenced intentional-latch remainder" in project_config
    assert "a passing allowance is not replacement evidence" in project_config


def test_setup_implements_one_authoritative_frontend_definition():
    project_config = _compact_skill_text("booley-setup", "steps/2-project-config.md")

    for required in (
        "Project-wide mapping",
        "single authoritative location",
        "no Target receives a copied or divergent mapping",
        "exactly one compatible definition",
        "duplicate or conflicting module definitions",
        "simulation behavior separate from synthesis-only declarations",
    ):
        assert required in project_config


def test_setup_requires_all_tech_cell_validation_layers():
    project_config = _compact_skill_text("booley-setup", "steps/2-project-config.md")

    for required in (
        "**Semantic:**",
        "**Frontend:**",
        "**Mapped-netlist:**",
        "**Physical-link:**",
        "Record evidence for the Project mapping as a whole and for every enabled synthesis Target",
        "exact stage count and reset semantics",
        "preservation or `dont_touch` intent",
        "applicable timing exceptions",
        "metastability use",
    ):
        assert required in project_config


def test_setup_plan_template_records_tech_cell_evidence_not_cell_names_only():
    template = _compact_skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md")

    for required in (
        "Flow/library and authoritative location",
        "Flow-supplied physical-library family",
        "Liberty input",
        "LEF input (physical mode)",
        "Project inventory",
        "Governing define/parameter",
        "Per-Target coverage matrix",
        "Unhandled discovered findings",
        "Replacement table and semantic decisions",
        "Approved Project-owned inputs",
        "Incomplete/Yellow Targets and open questions",
        "Semantic behavior",
        "Frontend:",
        "Mapped netlist:",
        "Physical link:",
        "CDC/synchronizer",
        "CDC/synchronizer (when applicable)",
        "Flow-supplied LEF/Liberty inputs",
    ):
        assert required in template
    assert "cell-name list alone" in template


def test_setup_tells_owned_main_verilator_targets_to_declare_trace_files():
    step = _compact_skill_text("booley-setup", "steps/2-project-config.md")

    assert "**Trace: nothing to wire.**" not in step
    assert "Icarus needs no wiring" in step
    assert "owns its C++ `main()`" in step
    assert "`[flows.sim].trace_files` to that name relative to `run_cwd`" in step


_SETUP_TERMS = (
    "stealth",
    "Specialist",
    "Target",
    "Flow",
    "Elaboration Check",
    "parity check",
    "Tech Cell Replacement",
    "flow cache",
    "vendored-core quarantine",
    "Sandbox",
    "Doctor",
    "VLNV",
    "hidden-core projection",
    "commit-message scrub",
    ".core",
    "Project Grant",
    "grant",
    "License Profile",
    "EDA tool",
    "Cocotb Target",
)
_REQUIRED_SETUP_TERMS = {
    "stealth",
    "specialist",
    "target",
    "flow",
    "elaboration check",
    "parity check",
    "tech cell replacement",
    "flow cache",
    "vendored-core quarantine",
    "eda tool",
    "cocotb target",
}


def _decision_rows(template: str) -> dict[str, list[str]]:
    section = template.split("## 2. Decision sheet", 1)[1]
    table = section.split("<!--", 1)[0]
    rows = {}
    for line in table.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if re.fullmatch(r"\d+a?", cells[0]):
            assert cells[0] not in rows, f"duplicate decision row {cells[0]}"
            rows[cells[0]] = cells
    return rows


def _glossary_terms() -> set[str]:
    terms = set()
    for line in _skill_text("booley-setup", "GLOSSARY.md").splitlines():
        if line.startswith("## "):
            terms.add(line[3:].strip("`").casefold())
        elif line.startswith("Aliases:"):
            terms.update(alias.strip().strip("`").casefold() for alias in line[8:].split(","))
    return terms


def _assert_terms_defined(text: str, terms: set[str]) -> None:
    for term in _SETUP_TERMS:
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE):
            assert term.casefold() in terms, f"undefined setup term: {term}"


def _setup_row_sections() -> dict[str, str]:
    plan = _skill_text("booley-setup", "steps/0-plan.md")
    part_b = plan.split("## Part B", 1)[1].split("## Part C", 1)[0]
    chunks = re.split(r"^(\d+a?)\. \*\*", part_b, flags=re.MULTILINE)
    return {chunks[i]: " ".join(chunks[i + 1].split()) for i in range(1, len(chunks), 2)}


def test_setup_plan_rows_have_plain_label_and_explanation():
    template = _skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md")
    rows = _decision_rows(template)
    assert set(rows) == {*(str(i) for i in range(1, 24)), "10a"}
    for row in rows.values():
        assert len(row) == 9
        assert all(row[1:4]), f"missing label, explanation, or internal key: {row}"
    for heading in (
        "### Decisions you made",
        "### Defaults accepted",
        "### Settled by your repo or existing config",
    ):
        assert heading in template


def test_setup_plan_plain_text_terms_are_in_glossary():
    terms = _glossary_terms()
    assert terms >= _REQUIRED_SETUP_TERMS
    rows = _decision_rows(_skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md"))
    for row in rows.values():
        _assert_terms_defined(" ".join(row[1:4]), terms)
    # A synthetic row must fail when a visible term loses its glossary entry.
    for term in _SETUP_TERMS:
        with pytest.raises(AssertionError, match="undefined setup term"):
            _assert_terms_defined(f"Choose {term} for this build", terms - {term.casefold()})


def test_setup_grill_mandatory_set():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")
    assert (
        '"Mandatory grill row" means row 4 (unless evidence-forced), merged rows 16 + 20, '
        "and row 19 only when init left a field unset"
    ) in plan
    assert "Keep Booley out of your git history?" in plan
    sections = _setup_row_sections()
    for row in ("17", "18"):
        assert "defaults block" in sections[row]
        for obsolete in ("grill question", "always a grill", "Interactive: ask"):
            assert obsolete not in sections[row]
    for row in ("4", "16", "20"):
        assert "always a grill question" in sections[row].lower()
    review = plan.split("- **`review`**", 1)[1].split("Row 17", 1)[0]
    mandatory = plan.split("- **The mandatory rows", 1)[1].split("- **The defaults block", 1)[0]
    for bullet in (review, mandatory):
        assert not re.search(r"\b(?:17|18)\b", bullet)
    for path in (skills_dir() / "booley-setup").rglob("*.md"):
        text = " ".join(path.read_text(encoding="utf-8").split())
        for obsolete in ("17 (specialists)", "18 (parity)", "Rows 16, 17, and 20"):
            assert obsolete not in text, path


def test_setup_grill_defaults_block():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")
    for required in (
        "one confirm block",
        "Accept these defaults, or name the ones to change",
        "confidence is `low`",
        "no defensible default",
        "unanswered frontier question",
        "same round",
        "otherwise the next round",
        "row 4 or row 16 wait",
        "accepted defaults are `inferred`",
        "high or medium confidence",
    ):
        assert required in plan
    for row in ("17", "18"):
        assert "no star" in _setup_row_sections()[row]


def test_setup_glossary_shipped_and_referenced():
    assert (skills_dir() / "booley-setup" / "GLOSSARY.md").is_file()
    for relative in ("SKILL.md", "steps/0-plan.md", "steps/new-greenfield.md"):
        text = _compact_skill_text("booley-setup", relative)
        assert "GLOSSARY.md" in text
        assert "verbatim" in text


def test_setup_hidden_footprint_enables_stealth():
    sections = _setup_row_sections()
    hidden = sections["16"]
    assert "Row 16 = `hidden`, row 20 = `enabled = true`" in hidden
    assert "including for hidden config-only projects" in hidden
    assert "both are `user-confirmed` from this one answer" in hidden
    assert "only if the user volunteers" in hidden
    assert "Existing hand-set `[stealth]` wins" in hidden
    assert "Unattended: write `enabled = false`" in sections["20"].replace("*", "")
    assert "hidden config alone never enables the scrub" in sections["20"]
    for path in (skills_dir() / "booley-setup").rglob("*"):
        if path.is_file():
            text = " ".join(path.read_text(encoding="utf-8").split())
            assert "Recommend `no` unless row 16" not in text
            assert "hidden config-only project may leave stealth off" not in text


def test_setup_grill_terms_are_in_glossary():
    terms = _glossary_terms()
    _assert_terms_defined(_skill_text("booley-setup", "steps/0-plan.md"), terms)
