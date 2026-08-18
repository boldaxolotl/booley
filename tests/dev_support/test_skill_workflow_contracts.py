"""Regression contracts for shipped ticket workflow skills."""

from booley.paths import skills_dir


def _skill_text(name: str, relative: str = "SKILL.md") -> str:
    return (skills_dir() / name / relative).read_text(encoding="utf-8")


def test_triage_routes_confirmed_booley_bugs_to_feedback_skill_by_default():
    main = _skill_text("booley-ticket-triage")
    blocked = _skill_text("booley-ticket-triage", "steps/02-blocked.md")
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")

    assert "invoke `/booley-feedback` by default" in main
    assert "invoke\n`/booley-feedback`" in blocked
    assert "invoke `/booley-feedback`" in review
    assert "explicit approval" in main


def test_feedback_routes_private_project_bugs_through_verified_synthetic_reproducer():
    skill = _skill_text("booley-feedback")
    reproducer = _skill_text("booley-feedback", "minimal-reproducer.md")

    assert "`minimal-reproducer.md`" in skill
    assert "private RTL, testbench, configuration, or logs" in skill
    assert "Never attach the private scratch reduction" in skill
    assert "Attachments cannot be removed with `triage`" in skill
    for required in (
        "Standalone",
        "Synthetic",
        "Equivalent",
        "Repeatable",
        "same Booley component and failure path",
        "counterfactual",
        "below 120 lines and 8,000 characters",
        "never anonymous or guaranteed safe",
    ):
        assert required in reproducer


def test_triage_leads_with_explicit_blockers_and_evidence_links():
    blocked = _skill_text("booley-ticket-triage", "steps/02-blocked.md")

    assert "**Blocked by** section" in blocked
    assert "state the board-level block reason" in blocked
    assert "one per numbered item" in blocked
    assert "[blocked.md](/absolute/path/to/blocked.md)" in blocked
    assert "Escalation log: not present" in blocked
    assert "Do not open with the passing checks" in blocked


def test_triage_review_briefing_is_fixed_compact_and_html_linked():
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")
    template = _skill_text("booley-ticket-triage", "review-template.md")

    for required in (
        "booley board review-briefing $SLUG",
        "fast freshness check",
        "Do not run `prepare-review` during\ninteractive triage",
        "Do not routinely reread",
        "every declared criterion",
        "feature-branch commit (oldest first)",
        "changed path (including renames and submodules)",
        "current-run usage summary",
        "on_success.triage_report: false",
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
        "[Run report](/absolute/path/to/REPORT.md)",
        "[HTML explanation](/absolute/runtime/path/to/report.html)",
        "#### Run economics",
    ):
        assert required in template
    assert "run-summary.md" not in template
    assert "usage.md" not in template
    assert "prepare-review $SLUG" not in review
    assert "command:livePreview" not in template


def test_triage_treats_completed_done_reviews_as_met_after_follow_up_fixes():
    review = _skill_text("booley-ticket-triage", "steps/03-review.md")

    for required in (
        "`review_*_done` is copied exactly from persisted criterion state",
        "never infer staleness from later commits",
        "`review_*_clean` remains explicitly\nfreshness-sensitive",
    ):
        assert required in review


def test_ticket_create_defaults_every_review_to_done_mode():
    skill = _skill_text("booley-ticket-create")
    template = _skill_text("booley-ticket-create", "TICKET_TEMPLATE.md")

    for criterion in (
        "review_rtl_bugs_done",
        "review_tb_quality_done",
        "review_rtl_spec_done",
    ):
        assert criterion in skill
    for criterion in (
        "review_rtl_bugs_done",
        "review_tb_quality_done",
        "review_rtl_spec_done",
        "review_rtl_protocol_done",
        "review_rtl_security_done",
        "review_rtl_optimization_done",
        "review_rtl_code_style_done",
    ):
        assert f"{criterion}: true" in template
    assert "`_clean` is\n  opt-in only" in skill


def test_ticket_create_grills_one_dependency_frontier_per_round():
    skill = _skill_text("booley-ticket-create")
    grilling = _skill_text("booley-ticket-create", "grilling.md")
    contract = " ".join(f"{skill}\n{grilling}".split())

    for required in (
        "ask the entire currently unblocked frontier in each round",
        "The **frontier** is every unresolved decision whose prerequisites are already settled",
        "Ask the whole frontier in one round",
        "defer it to a later round",
        "After each response, record the settled decisions and recompute the frontier",
        "ask the user to confirm it",
    ):
        assert required in contract
    assert "one question at a time" not in contract.lower()


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


def test_heal_has_bounded_doctor_repair_and_verification_loop():
    skill = _skill_text("booley-heal")

    for required in (
        "zero `FAIL` findings",
        "zero active `WARN` findings",
        "Doctor's `fix:` hint",
        "from booley.paths import troubleshooting_path",
        "booley doctor --deep",
        "after 12 remediation passes",
        "final plain `booley doctor`",
        "host: final plain `booley doctor`",
    ):
        assert required in skill


def test_heal_preserves_scope_and_routes_exceptional_findings():
    skill = _skill_text("booley-heal")

    for required in (
        "Preserve all pre-existing changes",
        "submodules discovered from `.gitmodules` as read-only",
        "Do not change RTL or testbench",
        "Do not create a Doctor waiver merely to make the output green",
        "Do not execute an action outside the current Session Runtime",
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
