"""Tests for redaction.

This is the module whose failures are irreversible: it sits on the path to a
public GitHub issue. So the tests come in two halves — *does it scrub what it
promises* (a miss is a leak), and *does it leave everything else alone* (an
over-eager denylist shreds the report into uselessness, and people then turn
redaction off entirely).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.feedback.redact import (
    GENERIC_IDENTIFIERS,
    apply_plan,
    build_plan,
    diff_summary,
    redact,
    redact_identifiers_enabled,
    residual_risks,
)


@pytest.fixture
def project(tmp_path):
    """A realistic project: git remote, identity, booley.toml, and a .core."""
    root = tmp_path / "rocketwidget"
    (root / ".booley_project").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for args in (
        ["config", "user.name", "Jane Tester"],
        ["config", "user.email", "jane@acme-semi.example"],
        ["remote", "add", "origin", "git@github.com:acme-semi/rocketwidget.git"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / ".booley_project" / "booley.toml").write_text(
        '[project]\nname = "rocketwidget"\n\n[flows.sim]\ndefault_target = "sim_rocketwidget"\n',
        encoding="utf-8",
    )
    (root / "rocketwidget.core").write_text(
        "name: acme:ip:rocketwidget:1.0\ntargets:\n  sim_rocketwidget:\n    toplevel: rocketwidget_top\n",
        encoding="utf-8",
    )
    return root


class TestScrubbing:
    def test_repo_path_is_replaced(self, project):
        out, _ = redact(f"failed in {project}/rtl/foo.sv", project)
        assert str(project) not in out
        assert "<repo>" in out

    def test_git_remote_and_slug_are_replaced(self, project):
        text = "cloned git@github.com:acme-semi/rocketwidget.git — see acme-semi/rocketwidget"
        out, _ = redact(text, project)
        assert "acme-semi" not in out

    def test_committer_identity_is_replaced(self, project):
        out, _ = redact("committed by Jane Tester <jane@acme-semi.example>", project)
        assert "Jane Tester" not in out
        assert "jane@acme-semi.example" not in out

    def test_any_other_email_is_replaced_too(self, project):
        out, _ = redact("log line mentions bob@othercorp.example", project)
        assert "othercorp" not in out

    def test_home_directory_of_any_user_is_replaced(self, project):
        out, _ = redact("/home/someoneelse/work/thing.sv", project)
        assert "someoneelse" not in out

    def test_windows_user_path_is_replaced(self, project):
        out, _ = redact(r"C:\Users\jdoe\proj\thing.sv", project)
        assert "jdoe" not in out

    def test_design_identifiers_from_the_core_are_replaced(self, project):
        out, _ = redact("elaborating rocketwidget_top for sim_rocketwidget", project)
        assert "rocketwidget" not in out
        assert "<module-" in out

    def test_the_same_identifier_maps_to_the_same_placeholder(self, project):
        """Two mentions of one module must still correlate in the report."""
        out, _ = redact("rocketwidget_top failed; rocketwidget_top retried", project)
        assert out.count(out.split()[0]) == 2

    def test_explicit_redact_extra_terms_are_replaced(self, project):
        toml = project / ".booley_project" / "booley.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + '\n[feedback]\nredact_extra = ["Skunkworks"]\n',
            encoding="utf-8",
        )
        out, _ = redact("part of the Skunkworks program", project)
        assert "Skunkworks" not in out

    def test_overridden_stealth_banned_words_are_replaced(self, project):
        toml = project / ".booley_project" / "booley.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + '\n[stealth]\nbanned_words = ["Nightingale"]\n',
            encoding="utf-8",
        )
        out, _ = redact("the Nightingale block", project)
        assert "Nightingale" not in out

    def test_default_stealth_words_are_not_used_as_a_redaction_list(self, project):
        """The shipped banned_words list contains "booley" — scrubbing it from a
        Booley bug report would gut the report to protect nothing."""
        out, _ = redact("booley doctor reported a docker error", project)
        assert "booley" in out
        assert "docker" in out

    def test_framework_name_survives_when_the_repo_itself_is_named_booley(self, tmp_path):
        """The repo directory is also an automatic design-identifier candidate."""
        root = tmp_path / "Booley"
        (root / ".booley_project").mkdir(parents=True)

        out, _ = redact("Booley feedback should preserve the word booley", root)

        assert out == "Booley feedback should preserve the word booley"


class TestNotOverreaching:
    def test_generic_identifiers_survive(self, project):
        text = "the core Target's top module is fine, and the test dir builds"
        out, _ = redact(text, project)
        assert out == text

    def test_relative_repo_path_does_not_rewrite_larger_words(self, tmp_path, monkeypatch):
        """F-24: a short as-given path must not corrupt ordinary prose."""
        monkeypatch.chdir(tmp_path)
        root = Path("work")
        (root / ".booley_project").mkdir(parents=True)

        out, _ = redact("worktrees differ; inspect work/rtl/dut.sv or work", root)

        assert out == "worktrees differ; inspect <repo>/rtl/dut.sv or <repo>"

    def test_a_generically_named_toplevel_is_not_redacted(self, tmp_path):
        """A design whose toplevel is literally `top` must not nuke the word."""
        root = tmp_path / "proj"
        (root / ".booley_project").mkdir(parents=True)
        (root / "d.core").write_text("name: v:l:d:1.0\ntoplevel: top\n", encoding="utf-8")
        out, _ = redact("the top module and the toplevel port", root)
        assert "top module" in out

    def test_short_identifiers_are_not_redacted(self, tmp_path):
        root = tmp_path / "proj"
        (root / ".booley_project").mkdir(parents=True)
        (root / "d.core").write_text("name: v:l:d:1.0\ntoplevel: alu\n", encoding="utf-8")
        plan = build_plan(root)
        assert "alu" not in plan.identifiers

    def test_tool_names_and_versions_survive(self, project):
        text = "verilator 5.024 crashed; yosys 0.67 was fine"
        out, _ = redact(text, project)
        assert out == text

    def test_generic_identifier_list_is_lowercase(self):
        """Membership is tested with .lower(); an uppercase entry would be dead."""
        assert all(word == word.lower() for word in GENERIC_IDENTIFIERS)


class TestIdempotence:
    def test_redacting_twice_changes_nothing(self, project):
        """The report is re-rendered on every setup re-run — placeholders must
        not accumulate."""
        text = f"{project}/rtl/rocketwidget_top.sv by Jane Tester"
        once, _ = redact(text, project)
        twice, hits = redact(once, project)
        assert twice == once
        assert hits == {}

    def test_placeholders_contain_none_of_the_secrets(self, project):
        plan = build_plan(project)
        for secret, placeholder in plan.mapping().items():
            assert secret.lower() not in placeholder.lower()


class TestConfigKnobs:
    def test_identifier_redaction_is_on_by_default(self):
        assert redact_identifiers_enabled({}) is True

    def test_identifier_redaction_can_be_disabled(self, project):
        toml = project / ".booley_project" / "booley.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + "\n[feedback]\nredact_identifiers = false\n",
            encoding="utf-8",
        )
        out, _ = redact("elaborating rocketwidget_top", project)
        assert "rocketwidget_top" in out
        # Paths and identities are scrubbed regardless — the knob is about
        # design names only.
        out2, _ = redact(f"in {project}", project)
        assert str(project) not in out2

    def test_malformed_toml_does_not_disable_redaction(self, project):
        (project / ".booley_project" / "booley.toml").write_text("[[[not toml", encoding="utf-8")
        out, _ = redact(f"path {project} and Jane Tester", project)
        assert str(project) not in out
        assert "Jane Tester" not in out


class TestOrdering:
    def test_longest_path_wins(self, tmp_path):
        """`/home/u/proj` must not be half-eaten by the `/home/u` rule."""
        root = tmp_path / "proj"
        (root / ".booley_project").mkdir(parents=True)
        out, _ = redact(f"{root}/rtl", root)
        assert out == "<repo>/rtl"

    def test_longest_identifier_wins(self, project):
        """`rocketwidget_top` must not be rendered as `<module-N>_top`."""
        out, _ = redact("rocketwidget_top", project)
        assert not out.endswith("_top")


class TestHonesty:
    def test_metrics_are_flagged_as_a_residual_risk(self, project):
        plan = build_plan(project)
        risks = residual_risks("Fmax was 327 MHz at 82.5 kGE", plan)
        assert any("competitively sensitive" in r for r in risks)

    def test_commercial_tools_are_flagged(self, project):
        plan = build_plan(project)
        assert any("license" in r for r in residual_risks("ran under Xcelium", plan))

    def test_the_denylist_caveat_is_always_present(self, project):
        plan = build_plan(project)
        assert any("denylist, not a proof" in r for r in residual_risks("nothing here", plan))

    def test_no_identifier_mapping_is_itself_flagged(self, tmp_path):
        """A repo with no name worth mapping (here: a generic directory name and
        no .core) gets told that design names go out verbatim."""
        root = tmp_path / "ip"
        (root / ".booley_project").mkdir(parents=True)
        plan = build_plan(root)
        assert not plan.identifiers
        assert any("verbatim" in r for r in residual_risks("text", plan))

    def test_diff_summary_reports_no_substitutions_plainly(self):
        assert diff_summary({}) == "no substitutions made"

    def test_diff_summary_counts_each_placeholder(self):
        assert "<repo> (x2)" in diff_summary({"<repo>": 2})


def test_apply_plan_on_an_empty_plan_is_a_no_op(tmp_path):
    root = tmp_path / "bare"
    (root / ".booley_project").mkdir(parents=True)
    plan = build_plan(root)
    plan.literals.clear()
    plan.identifiers.clear()
    plan.patterns.clear()
    assert plan.is_empty()
    assert apply_plan("untouched text", plan) == ("untouched text", {})
