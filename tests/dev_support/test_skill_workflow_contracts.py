"""Regression contracts for shipped ticket workflow skills."""

import re

import yaml

from booley.runtime.paths import skills_dir


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
        "Ask: **approve** / **fix here** / **reset** / **archive** / **skip**",
        "Do not hand it back to the Runner for partial rework",
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

    for criterion in (
        "review_rtl_bugs",
        "review_tb_quality",
        "review_rtl_spec",
    ):
        assert criterion in skill
    for criterion in (
        "review_rtl_bugs",
        "review_tb_quality",
        "review_rtl_spec",
        "review_rtl_protocol",
        "review_rtl_security",
        "review_rtl_optimization",
        "review_rtl_code_style",
    ):
        assert f"{criterion}: true" in template
    assert "expands to corrective `_clean`" in contract
    assert "Use explicit `_done` only for" in contract
    assert "Every `_clean` waiver includes a justification" in contract


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
        "The complete ticket is the one post-grill review artifact",
        "Detailed mode skips 2d and 2e",
        "single post-grill review artifact",
        "MANDATORY TICKET APPROVAL",
        "Create this ticket? (yes / edit / cancel)",
        "require no further user confirmation",
        "internal implementation details",
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


def test_ticket_create_uses_complete_project_defaults_only_during_creation():
    skill = _skill_text("booley-ticket-create")
    contract = " ".join(skill.split())

    for required in (
        'resolve_project_dir() / "ticket_defaults.md"',
        "consumed **only here, during creation**",
        "All five blocks must then be present, unique, non-null, and complete",
        "An active file fully replaces the shipped defaults",
        "Validate the **entire active file**",
        "explicitly supply complete per-Ticket `criteria` **and** `on_success`",
        "validation never does",
        '--on-success "$ON_SUCCESS_JSON"',
    ):
        assert required in contract
    assert "merge, add/remove, or inheritance syntax" in contract
    assert "never influence scope, priority, dependencies" in contract


def test_ticket_defaults_template_is_packaged_and_inactive():
    template = _skill_text("booley-ticket-create", "TICKET_DEFAULTS_TEMPLATE.md")
    for heading in ("On success", "Feature", "Bugfix", "Refactor", "Verification"):
        assert template.count(f"## {heading}") == 1

    blocks = re.findall(r"```yaml\n(.*?)```", template, flags=re.DOTALL)
    assert len(blocks) == 5
    assert all(yaml.safe_load(block) is None for block in blocks)
    assert "@ sim_default @ all @ pass -> pass" in template


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
