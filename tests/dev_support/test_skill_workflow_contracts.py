"""Regression contracts for shipped ticket workflow skills."""

from booley.runtime.paths import skills_dir


def _skill_text(name: str, relative: str = "SKILL.md") -> str:
    return (skills_dir() / name / relative).read_text(encoding="utf-8")


def _compact_skill_text(name: str, relative: str = "SKILL.md") -> str:
    return " ".join(_skill_text(name, relative).split())


def test_triage_routes_confirmed_booley_bugs_to_feedback_skill_by_default():
    main = _skill_text("booley-ticket-triage")
    blocked = _skill_text("booley-ticket-triage", "steps/02-blocked.md")
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")

    assert "invoke `/booley-feedback` by default" in main
    assert "invoke\n`/booley-feedback`" in blocked
    assert "invoke `/booley-feedback`" in review
    assert "explicit approval" in main


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
        "local commands, not `submit`",
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


def test_triage_leads_with_explicit_blockers_and_evidence_links():
    blocked = _skill_text("booley-ticket-triage", "steps/02-blocked.md")

    assert "**Blocked by** section" in blocked
    assert "state the board-level block reason" in blocked
    assert "one per numbered item" in blocked
    assert "[blocked.md](/absolute/path/to/blocked.md)" in blocked
    assert "Escalation log: not present" in blocked
    assert "Do not open with the passing checks" in blocked


def test_triage_recovers_acceptance_input_changes_through_a_new_generation():
    blocked = _skill_text("booley-ticket-triage", "steps/02-blocked.md")
    summary = _skill_text("booley-ticket-triage", "steps/04-summary.md")

    ordered_steps = (
        'python -m booley.ticket_board return-to-draft "$SLUG"',
        "Correct the authoring filesets",
        "Resolve the moved Ticket's absolute path",
        'python -m booley.ticket_board validate-ticket "<absolute draft Ticket path>" --check-git',
        'python -m booley.ticket_board enqueue "$SLUG"',
    )
    positions = [blocked.index(step) for step in ordered_steps]

    assert positions == sorted(positions)
    assert "acceptance-input-change-required" in blocked
    assert "logs/<slug>/runs/<NNN>/" in blocked
    assert "preserves the old Ticket baseline" in blocked
    assert "fresh Ticket authoring" in blocked
    assert "`outer_worktree` and `project_worktree`" in blocked
    assert "`booley board return-to-draft" not in blocked
    assert "`booley board enqueue" not in blocked
    assert "published and enqueued" in blocked
    assert "published and queued" not in blocked
    assert "| Returned to draft | <n> | ... |" in summary


def test_triage_review_briefing_is_fixed_compact_and_html_linked():
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")
    template = _skill_text("booley-ticket-triage", "review-template.md")

    for required in (
        "booley board show $SLUG",
        "fast freshness check",
        "Do not run `board review` during\ninteractive triage",
        "Do not routinely reread",
        "every declared criterion",
        "feature-branch commit (oldest first)",
        "changed path (including renames and submodules)",
        "current-run usage summary",
        "without `triage_report` in their `on_success` list",
        "deterministic criteria",
    ):
        assert required in review
    for required in (
        "| Category | Criterion | Required? | Status | Metric / evidence |",
        "#### Scope deviations",
        "#### Commit history",
        "`<abbreviated SHA>` — <complete commit subject; one line per commit, oldest first>",
        "#### Changed files",
        "#### Reports",
        "[Developer Agent report (REPORT.md)](/absolute/path/to/REPORT.md)",
        "[Polished HTML report](/absolute/runtime/path/to/report.html)",
        "#### Explanation highlights",
        "#### Review findings and dispositions",
        "#### Run economics",
    ):
        assert required in template
    ordered_sections = (
        "#### Reports",
        "#### Decision summary",
        "#### Findings",
        "#### Explanation highlights",
        "#### Scope deviations",
        "#### Changed files",
        "#### Criteria",
        "#### Review findings and dispositions",
        "#### Commit history",
        "#### Run economics",
    )
    assert [template.index(section) for section in ordered_sections] == sorted(
        template.index(section) for section in ordered_sections
    )
    assert template.index("Developer Agent report") < template.index("Polished HTML report")
    assert "run-summary.md" not in template
    assert "usage.md" not in template
    assert "prepare-review $SLUG" not in review
    assert "command:livePreview" not in template


def test_triage_treats_all_review_modes_as_freshness_sensitive():
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")
    contract = " ".join(review.split())

    for required in (
        "Both `review_*_done` and `review_*_clean` are freshness-sensitive",
        "recorded source fingerprint",
        "accepted waiver",
        "including `MINOR`",
    ):
        assert required in contract


def test_triage_review_distinguishes_direct_fix_from_clean_reset():
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")
    contract = " ".join(review.split())

    for required in (
        "For accepted review, ask: **approve** / **fix here** / **reset** / **archive** / **skip**",
        "For a briefing marked **unaccepted**",
        "Criteria Satisfaction Records are immutable",
        "publishes first acceptance, and completes the Ticket",
        "This is a clean start",
        "Do not selectively retain reviewed work",
        "never resumes through an ordinary move to `queued`",
        '--reason "<correction reason>"',
    ):
        assert required in contract


def test_ticket_create_defaults_every_review_to_corrective_mode():
    skill = _skill_text("booley-ticket-create")
    template = _skill_text("booley-ticket-create", "TICKET_TEMPLATE.md")
    contract = " ".join(skill.split())

    assert "RTL bugs REVIEW" in skill
    assert "TB quality REVIEW" in skill
    assert "rtl: {bugs: clean}" in template
    assert "tb: {quality: clean}" in template
    assert "`REVIEW.done` and `REVIEW.clean` are separate outcomes" in contract


def test_ticket_create_hands_human_off_to_booley_run():
    skill = _skill_text("booley-ticket-create")

    assert "suggest `booley run`" in skill
    assert "/booley-run-and-fix" not in skill


def test_ticket_create_companions_cover_target_plan_decisions():
    guidance = _skill_text("booley-ticket-create", "TICKET_CREATION_TEMPLATE.md")
    grilling = _skill_text("booley-ticket-create", "grilling.md")

    assert "Criteria, Target Plan" in guidance
    for required in (
        "New Target lifecycle",
        "coexist",
        "replace a runnable predecessor",
        "evidence-only",
        "(new)",
        "(temp)",
    ):
        assert required in grilling


def test_ticket_create_stops_at_ticket_target_and_placeholder_authoring():
    skill = _skill_text("booley-ticket-create")
    contract = " ".join(skill.split())

    for required in (
        "Ticket creation authors only the Ticket, Target definitions, their referenced filesets",
        "local parameter declarations, unambiguously owned",
        "empty placeholder files for Scope paths marked `[new]`",
        "existing definitions remain unchanged",
        "The developer who runs the Ticket authors its implementation",
        "A placeholder is a zero-byte file",
        "do not put declarations, modules, packages, assertions, stimulus",
        "approved planned Target definitions, referenced inputs, and owned test tables",
        "create only empty placeholders for `[new]` Scope paths",
        "do not implement any part of the Ticket",
        "stop and report the blocker",
        "creating that code is outside this skill",
    ):
        assert required in contract
    for retired in (
        "author every needed Target/control file",
        "Create or edit all required .core files, constraints",
    ):
        assert retired not in contract


def test_ticket_create_grills_frontiers_then_uses_one_ticket_approval():
    skill = _skill_text("booley-ticket-create")
    grilling = _skill_text("booley-ticket-create", "grilling.md")
    contract = " ".join(f"{skill}\n{grilling}".split())

    for required in (
        "ask the entire currently unblocked frontier in each round",
        "The **frontier** is every unresolved decision whose prerequisites are already settled",
        "Ask the whole frontier in one round",
        "defer it to a later round",
        "After each response, record the settled decisions and recompute the frontier",
        "continue directly to the draft gate",
        "The complete ticket and any new Target definitions form the one post-grill review artifact",
        "Detailed mode skips 2d and 2e",
        "single post-grill review artifact",
        "MANDATORY TICKET APPROVAL",
        "Target Plan",
        "New and Temporal Target entries",
        "complete Target definition",
        "Target Plan: none",
        "Create this ticket and Target Plan? (yes / edit / cancel)",
        "Author them exactly as approved",
        "requires changing an approved Target definition, return to 2f",
        "require no further user confirmation",
        "Basis publication remains an internal implementation detail",
    ):
        assert required in contract
    for retired in (
        "explicit approval to seal",
        "separate seal gate",
        "combined ticket + Target diff",
    ):
        assert retired not in contract
    assert "one question at a time" not in contract.lower()
    assert (
        "summarize the resulting shared understanding and ask the user to confirm it"
        not in contract
    )


def test_ticket_create_applies_free_form_project_guidance_only_during_creation():
    skill = _skill_text("booley-ticket-create")
    contract = " ".join(skill.split())

    for required in (
        "Ticket Creation Guidance is Project-owned, free-form Markdown",
        "consumed **only here, during creation**",
        "Read `ticket_creation.md` when it exists",
        "read `ticket_defaults.md` only when `ticket_creation.md` is absent",
        "Start from the shipped §B/§D inference",
        "Project guidance overrides shipped inference",
        "explicit instructions for the current Ticket override the Project file",
        "Validate the resolved Ticket through §C",
        "has no schema, required headings, completeness check, or static validation pass",
        "Ambiguous, conflicting, or unresolvable applicable guidance",
        "disregard the old scaffold's instructions about YAML activation",
        "An untouched, comment-only legacy scaffold adds no guidance",
        "validation never does",
        '--document-file "$TICKET_PATH"',
    ):
        assert required in contract
    assert "Target annotations, and `on_success`" in contract
    for retired in (
        "All five blocks must then be present",
        "An active file fully replaces",
        "Validate the **entire active file**",
        "merge, add/remove, or inheritance syntax",
    ):
        assert retired not in contract
    assert "`on_success` values" in contract
    assert "remove_targets" not in contract


def test_ticket_create_fixes_target_plan_at_creation_time():
    skill = _skill_text("booley-ticket-create")
    template = _skill_text("booley-ticket-create", "TICKET_TEMPLATE.md")
    contract = " ".join(skill.split())

    for required in (
        "The Target Plan is derived",
        "Use exact registered Target and test selectors",
        "(replaces <existing Target>)",
        "An annotated Target requires `merge`",
    ):
        assert required in contract
    assert "do not author target_plan" in template


def test_ticket_create_reconciles_scope_and_provider_dependencies() -> None:
    skill = _skill_text("booley-ticket-create")
    contract = " ".join(skill.split())

    for required in (
        "ordinary scope-overlap and interface-dependency inference",
        "add that provider to `dependencies` in human mode",
        "reject the request and name every missing provider dependency",
        "After Criteria and the derived Target Plan are fully resolved, rerun §A",
        "mandatory in both lightweight and detailed modes",
        "reconcile the final `dependencies`",
    ):
        assert required in contract


def test_ticket_creation_template_is_packaged_free_form_markdown():
    template = _skill_text("booley-ticket-create", "TICKET_CREATION_TEMPLATE.md")

    assert template.startswith("# Ticket Creation Guidance")
    assert "in any Markdown form" in template
    assert "corrective security review in every feature Ticket" in template
    assert "standard simulation matrix" in template
    assert "area does not regress" in template
    assert "```yaml" not in template


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
    assert "Reuse a successful setup/heal deep result" in agents


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
        "public issue or email submission still requires",
        "Never describe one of those partial outcomes as healed",
    ):
        assert required in skill


def test_setup_makes_stealth_an_explicit_opt_in():
    plan = _skill_text("booley-setup", "steps/0-plan.md")
    project_config = _skill_text("booley-setup", "steps/2-project-config.md")
    greenfield = _skill_text("booley-setup", "steps/new-greenfield.md")
    template = _skill_text("booley-setup", "BOOLEY_TEMPLATE.toml")

    prompt = "Do you want stealth mode: self-contained hidden cores plus the commit-message scrub?"
    compact_plan = " ".join(plan.split())
    assert prompt in compact_plan
    assert prompt in " ".join(greenfield.split())
    assert "Unattended: write `enabled = false`" in compact_plan
    assert "Do not omit the block" in project_config
    ignore_prompt = (
        "Should Booley ignore the repository's existing `.core` files and use only the "
        "stealth-authored cores?"
    )
    assert ignore_prompt in compact_plan
    assert "ignore_native_cores = true" in project_config
    assert "[stealth]\n" in template
    assert "enabled = false" in template


def test_setup_plans_one_project_wide_tech_cell_replacement():
    plan = _compact_skill_text("booley-setup", "steps/0-plan.md")
    template = _compact_skill_text("booley-setup", "SETUP_PLAN_TEMPLATE.md")

    assert "one Project-wide **Tech Cell Replacement** mapping" in plan
    assert "per-Target coverage matrix" in plan
    assert "| 24 | Tech Cell Replacement" in template
    assert "continue numbering from 25" in template
    assert "evidence-forced: not applicable" in template
    assert (
        "Synthesis-disabled Projects resolve row 24 as evidence-forced: not applicable "
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
