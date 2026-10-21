"""Shipped Project Setup cleanup workflow contracts."""

from booley.harness.booley import _build_parser
from booley.runtime.paths import skills_dir


def _skill(relative: str = "SKILL.md") -> str:
    return (skills_dir() / "booley-setup" / relative).read_text(encoding="utf-8")


def test_setup_advertises_step_seven_and_ledger_contract() -> None:
    skill = _skill()
    template = _skill("SETUP_PLAN_TEMPLATE.md")
    cleanup = " ".join(_skill("steps/7-cleanup.md").split())

    for required in (
        "Steps 0" + "\u2013" + "7",
        "`steps/7-cleanup.md`",
        "a number `0`" + "\u2013" + "`7`",
    ):
        assert required in skill
    for dropped in ("current run's manifest", "inventory-only"):
        assert dropped not in skill
    for required in (
        "Setup artifact retention",
        "Execution ledger",
    ):
        assert required in template
    for required in (
        "booley cleanup prepare",
        "booley cleanup preview",
        "booley cleanup apply",
        "0 removed or already gone, 1 kept, 2 refused",
        "`diagnostic` skips `booley cleanup apply`",
    ):
        assert required in cleanup


def test_setup_skill_drops_manifest_cleanup_contract() -> None:
    dropped = (
        "booley cleanup record",
        "--digest",
        "--cache-disposition",
        "Flow-cache disposition",
        "Cleanup preview digest",
        "Recovery journal",
        "Manifest path",
        "evict-setup-touched",
    )
    for path in sorted((skills_dir() / "booley-setup").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert not [term for term in dropped if term in text], path


def test_cleanup_cli_has_run_root_preview_and_apply() -> None:
    parser = _build_parser()
    preview = parser.parse_args(["cleanup", "preview", "--run-id", "run-1"])
    apply = parser.parse_args(["cleanup", "apply", "--run-id", "run-1"])

    assert (preview.cleanup_command, apply.cleanup_command) == ("preview", "apply")
    assert not hasattr(preview, "retention") and not hasattr(apply, "digest")
