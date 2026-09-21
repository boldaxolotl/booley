"""Shipped Project Setup cleanup workflow contracts."""

from booley.harness.booley import _build_parser
from booley.runtime.paths import skills_dir


def _skill(relative: str = "SKILL.md") -> str:
    return (skills_dir() / "booley-setup" / relative).read_text(encoding="utf-8")


def test_setup_advertises_step_seven_and_ledger_contract() -> None:
    skill = _skill()
    template = _skill("SETUP_PLAN_TEMPLATE.md")
    cleanup = _skill("steps/7-cleanup.md")

    for required in (
        "Steps 0" + "\u2013" + "7",
        "`steps/7-cleanup.md`",
        "a number `0`" + "\u2013" + "`7`",
        "current run's manifest",
        "inventory-only",
    ):
        assert required in skill
    for required in (
        "Setup artifact retention",
        "Flow-cache disposition",
        "Execution ledger",
        "Cleanup preview digest",
        "Recovery journal",
    ):
        assert required in template
    for required in (
        "booley cleanup prepare",
        "booley cleanup preview",
        "booley cleanup apply",
        "same-filesystem quarantine",
        "Free-text path-like prose",
        "legacy plan",
    ):
        assert required in cleanup


def test_cleanup_cli_has_digest_guarded_preview_and_apply() -> None:
    parser = _build_parser()
    preview = parser.parse_args(["cleanup", "preview", "--run-id", "run-1"])
    apply = parser.parse_args(["cleanup", "apply", "--run-id", "run-1", "--digest", "abc"])

    assert preview.cleanup_command == "preview"
    assert preview.retention == "minimal"
    assert apply.cleanup_command == "apply"
    assert apply.digest == "abc"
