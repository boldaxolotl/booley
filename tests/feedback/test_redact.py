"""Tests for redaction.

This is the module whose failures can become irreversible when a user shares an
exported file. So the tests come in two halves — *does it scrub what it
promises* (a miss is a leak), and *does it leave everything else alone* (an
over-eager denylist shreds the report into uselessness, and people then turn
redaction off entirely).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.feedback.redact import (
    RedactionPlan,
    apply_plan,
    build_plan,
    diff_summary,
    redact,
    residual_risks,
)
from tests.booley_checkouts_support import linked_worktree
from tests.conftest import symlink_or_skip
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S


@pytest.fixture
def project(tmp_path):
    """A realistic project: git remote, identity, booley.toml, and a .core."""
    root = tmp_path / "rocketwidget"
    (root / ".booley").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=SUBPROCESS_TIMEOUT_S)
    for args in (
        ["config", "user.name", "Jane Tester"],
        ["config", "user.email", "jane@acme-semi.example"],
        ["remote", "add", "origin", "git@github.com:acme-semi/rocketwidget.git"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, timeout=SUBPROCESS_TIMEOUT_S)
    (root / ".booley" / "booley.toml").write_text(
        '[project]\nname = "rocketwidget"\n\n[flows.sim]\n', encoding="utf-8"
    )
    (root / "rocketwidget.core").write_text(
        "CAPI=2:\nname: acme:ip:rocketwidget:1.0\ntargets:\n"
        "  sim_rocketwidget:\n    flow: sim\n"
        "    flow_options: {booley: {doctor: [sim]}}\n"
        "    toplevel: rocketwidget_top\n",
        encoding="utf-8",
    )
    return root


def _symlink_booley_dir(project, target, *, link=".booley", link_text=None):
    """Move the project's Booley Directory to *target* and leave a symlink entry at *link*."""
    (project / ".booley").rename(target)
    entry = project / link
    entry.parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(entry, link_text or target)


def _relocate_booley_dir(project):
    """Point ``[project].dir`` at ``tools/booley``."""
    (project / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')


class TestScrubbing:
    @pytest.mark.parametrize(
        ("text", "gone", "kept"),
        [
            ("failed in {project}/rtl/foo.sv", ("{project}",), "<repo>"),
            (
                "cloned git@github.com:acme-semi/rocketwidget.git - see acme-semi/rocketwidget",
                ("acme-semi",),
                "",
            ),
            (
                "committed by Jane Tester <jane@acme-semi.example>",
                ("Jane Tester", "jane@acme-semi.example"),
                "",
            ),
            ("log line mentions bob@othercorp.example", ("othercorp",), ""),
            ("/home/someoneelse/work/thing.sv", ("someoneelse",), ""),
            (r"C:\Users\jdoe\proj\thing.sv", ("jdoe",), ""),
            ("elaborating rocketwidget_top for sim_rocketwidget", ("rocketwidget",), "<module-"),
        ],
        ids=["repo-path", "remote-and-slug", "committer", "email", "home", "windows", "core"],
    )
    def test_secrets_are_replaced(self, project, text, gone, kept):
        out, _ = redact(text.format(project=project), project)
        for secret in gone:
            assert secret.format(project=project) not in out
        if kept:
            assert kept in out

    @pytest.mark.parametrize("spelling", ["entry", "target"])
    def test_symlinked_booley_dir_target_is_project_private(self, project, spelling):
        target = project.parent / "booley-dir-elsewhere"
        _symlink_booley_dir(project, target)
        booley_dir = project / ".booley" if spelling == "entry" else target

        out, _ = apply_plan(f"see {target}/runtime/x.log", build_plan(project, booley_dir))

        assert out == "see <repo>/.booley/runtime/x.log"

    def test_symlinked_booley_dir_target_redacted_in_every_spelling(self, project):
        real = project.parent / "real-data"
        target = real / "prj-booley"
        real.mkdir()
        alias = project.parent / "alias-data"
        symlink_or_skip(alias, real)
        # The link text names the target through another link; resolve() would not.
        _symlink_booley_dir(project, target, link_text=alias / "prj-booley")

        out, _ = apply_plan(f"{alias / 'prj-booley'}/x and {target}/y", build_plan(project))

        assert out == "<repo>/.booley/x and <repo>/.booley/y"

    def test_symlinked_relocated_booley_dir_target_is_redacted(self, project):
        """A symlinked ``tools/booley`` entry hides its target like a symlinked ``.booley``."""
        _relocate_booley_dir(project)
        target = project.parent / "booley-dir-elsewhere"
        _symlink_booley_dir(project, target, link="tools/booley")

        out, _ = apply_plan(f"see {target}/runtime/x.log", build_plan(project))

        assert out == "see <repo>/tools/booley/runtime/x.log"

    def test_symlinked_default_entry_is_hidden_in_a_relocated_project(self, project):
        """Full init still works beside a relocated directory, so its `.booley` link stays private."""
        _relocate_booley_dir(project)
        target = project.parent / "default-entry-elsewhere"
        _symlink_booley_dir(project, target)

        out, _ = apply_plan(f"see {target}/x.log", build_plan(project, project / "tools/booley"))

        assert out == "see <repo>/.booley/x.log"

    def test_another_checkout_with_a_retired_layout_does_not_break_redaction(
        self, tmp_path, monkeypatch
    ):
        """Fix 94eca22f6: one linked worktree's retired ``[project].dir`` must not fail the plan."""
        main, worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime="host")
        (worktree / "booley.toml").write_bytes(b'[project]\ndir = ".booley_project"\n')

        out, _ = redact(f"see {worktree}/x.log in {main}/y.log", main)

        assert out == "see <repo>/x.log in <repo>/y.log"

    def test_redact_default_reads_relocated_booley_toml(self, project):
        """``redact(text, root)`` finds ``booley.toml`` in the relocated directory."""
        _relocate_booley_dir(project)
        relocated = project / "tools" / "booley"
        relocated.mkdir(parents=True)
        (project / ".booley" / "booley.toml").unlink()
        (project / ".booley").rmdir()
        (relocated / "booley.toml").write_bytes(
            b'[project]\nname = "rocketwidget"\n\n[feedback]\nredact_extra = ["zorgblat"]\n'
        )

        out, _ = redact("the zorgblat module failed", project)

        assert "zorgblat" not in out

    def test_core_files_under_relocated_booley_dir_are_skipped(self, project):
        """Copies of ``.core`` files in a relocated Booley Directory add no design names."""
        _relocate_booley_dir(project)
        relocated = project / "tools" / "booley"
        relocated.mkdir(parents=True)
        (relocated / "copy.core").write_bytes(b"CAPI=2:\nname: v:l:copiedsecret:1.0\n")

        names = build_plan(project).identifiers

        assert "copiedsecret" not in names
        assert "rocketwidget" in names

    @pytest.mark.parametrize(
        ("section", "term", "text"),
        [
            (
                '[feedback]\nredact_extra = ["Skunkworks"]',
                "Skunkworks",
                "part of the Skunkworks program",
            ),
            ('[stealth]\nbanned_words = ["Nightingale"]', "Nightingale", "the Nightingale block"),
        ],
        ids=["redact-extra", "stealth-banned-words"],
    )
    def test_explicit_terms_are_replaced(self, project, section, term, text):
        toml = project / ".booley" / "booley.toml"
        toml.write_text(toml.read_text(encoding="utf-8") + f"\n{section}\n", encoding="utf-8")
        out, _ = redact(text, project)
        assert term not in out

    def test_the_same_identifier_maps_to_the_same_placeholder(self, project):
        """Two mentions of one module must still correlate in the report."""
        out, _ = redact("rocketwidget_top failed; rocketwidget_top retried", project)
        assert out.count(out.split()[0]) == 2

    @pytest.mark.parametrize(
        "value", ['"Skunkworks"', '["Skunkworks", 7]'], ids=["bare-string", "non-string-entry"]
    )
    def test_invalid_redact_extra_fails_with_doctors_message(self, project, value):
        """An ignored or coerced term would leak, so a bad knob must stop the plan."""
        toml = project / ".booley" / "booley.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + f"\n[feedback]\nredact_extra = {value}\n",
            encoding="utf-8",
        )
        with pytest.raises(
            ValueError, match=r"\[feedback\]\.redact_extra must be a list of strings"
        ):
            build_plan(project)

    def test_framework_name_survives_when_the_repo_itself_is_named_booley(self, tmp_path):
        """The repo directory is also an automatic design-identifier candidate."""
        root = tmp_path / "Booley"
        (root / ".booley").mkdir(parents=True)

        out, _ = redact("Booley feedback should preserve the word booley", root)

        assert out == "Booley feedback should preserve the word booley"


class TestNotOverreaching:
    @pytest.mark.parametrize(
        "text",
        [
            "the core Target's top module is fine, and the test dir builds",
            "verilator 5.024 crashed; yosys 0.67 was fine",
            "booley doctor reported a docker error",  # shipped banned_words are not a denylist
        ],
        ids=["generic-identifiers", "tool-names", "default-stealth-words"],
    )
    def test_text_survives_unchanged(self, project, text):
        assert redact(text, project)[0] == text

    def test_relative_repo_path_does_not_rewrite_larger_words(self, tmp_path, monkeypatch):
        """F-24: a short as-given path must not corrupt ordinary prose."""
        monkeypatch.chdir(tmp_path)
        root = Path("work")
        (root / ".booley").mkdir(parents=True)

        out, _ = redact("worktrees differ; inspect work/rtl/dut.sv or work", root)

        assert out == "worktrees differ; inspect <repo>/rtl/dut.sv or <repo>"

    @pytest.mark.parametrize("toplevel", ["top", "alu"], ids=["generic", "short"])
    def test_generic_or_short_toplevels_are_not_redacted(self, tmp_path, toplevel):
        """A design whose toplevel is literally `top` or `alu` must not nuke the word."""
        root = tmp_path / "proj"
        (root / ".booley").mkdir(parents=True)
        (root / "d.core").write_text(f"name: v:l:d:1.0\ntoplevel: {toplevel}\n", encoding="utf-8")
        text = f"the {toplevel} module and the toplevel port"
        assert redact(text, root)[0] == text


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
    def test_identifier_redaction_can_be_disabled(self, project):
        toml = project / ".booley" / "booley.toml"
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
        (project / ".booley" / "booley.toml").write_text("[[[not toml", encoding="utf-8")
        out, _ = redact(f"path {project} and Jane Tester", project)
        assert str(project) not in out
        assert "Jane Tester" not in out


class TestOrdering:
    def test_longest_path_wins(self, tmp_path):
        """`/home/u/proj` must not be half-eaten by the `/home/u` rule."""
        root = tmp_path / "proj"
        (root / ".booley").mkdir(parents=True)
        out, _ = redact(f"{root}/rtl", root)
        assert out == "<repo>/rtl"

    def test_longest_identifier_wins(self, project):
        """`rocketwidget_top` must not be rendered as `<module-N>_top`."""
        out, _ = redact("rocketwidget_top", project)
        assert not out.endswith("_top")


class TestHonesty:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Fmax was 327 MHz at 82.5 kGE", "competitively sensitive"),
            ("ran under Xcelium", "license"),
            ("nothing here", "denylist, not a proof"),
        ],
        ids=["metrics", "commercial-tools", "denylist-caveat"],
    )
    def test_residual_risk_lines(self, project, text, expected):
        assert any(expected in risk for risk in residual_risks(text, build_plan(project)))

    def test_no_identifier_mapping_is_itself_flagged(self, tmp_path):
        """A repo with no name worth mapping (here: a generic directory name and
        no .core) gets told that design names go out verbatim."""
        root = tmp_path / "ip"
        (root / ".booley").mkdir(parents=True)
        plan = build_plan(root)
        assert not plan.identifiers
        assert any("verbatim" in r for r in residual_risks("text", plan))

    @pytest.mark.parametrize(
        ("hits", "expected"), [({}, "no substitutions made"), ({"<repo>": 2}, "<repo> (x2)")]
    )
    def test_diff_summary(self, hits, expected):
        assert expected in diff_summary(hits)


def test_apply_plan_on_an_empty_plan_is_a_no_op(tmp_path):
    root = tmp_path / "bare"
    (root / ".booley").mkdir(parents=True)
    plan = build_plan(root)
    plan.literals.clear()
    plan.identifiers.clear()
    plan.patterns.clear()
    assert plan.is_empty()
    assert apply_plan("untouched text", plan) == ("untouched text", {})


def test_feedback_custom_generic_vocabulary_stays_independent(project: Path) -> None:
    text = "generated docker verification agent"
    assert redact(text, project)[0] == text
    config = project / ".booley" / "booley.toml"
    config.write_text(
        config.read_text() + '\n[stealth]\nbanned_words = ["generated", "docker", "agent"]\n'
    )
    output = redact(text, project)[0]
    assert all(term not in output for term in ("generated", "docker", "agent"))


def test_explicit_stealth_tiers_identifier_optout_and_hits(tmp_path):
    from booley.feedback.redact import apply_plan, build_plan

    root = tmp_path / "public"
    directory = root / ".booley"
    directory.mkdir(parents=True)
    (directory / "booley.toml").write_text(
        '[feedback]\nredact_identifiers = false\n[stealth]\nbanned_words = ["agent", "booley"]\nbanned_substrings = ["quokka"]\n'
    )
    plan = build_plan(root)
    text = "booley_config BooleyRunner myquokkafile axi_agent reagent precursor public"
    expected = "<redacted>_config <redacted>Runner my<redacted>file axi_<redacted> reagent precursor public"
    assert apply_plan(text, plan) == (expected, {"<redacted>": 4})
    assert apply_plan(expected, plan) == (expected, {})
    assert plan.mapping()["quokka"] == "<redacted>"


def test_feedback_explicit_terms_duplicate_module_and_no_defaults(tmp_path):
    from booley.feedback.redact import apply_plan, build_plan

    root = tmp_path / "quokka"
    directory = root / ".booley"
    directory.mkdir(parents=True)
    config = directory / "booley.toml"
    config.write_text('[stealth]\nbanned_words = ["quokka"]\nbanned_substrings = ["quokka"]\n')
    plan = build_plan(root)
    assert apply_plan("quokka myquokkafile QuokkaRunner Booley agent", plan) == (
        "<module-1> my<module-1>file <module-1>Runner Booley agent",
        {"<module-1>": 3},
    )
    config.write_text("[feedback]\nredact_identifiers = false\n")
    assert apply_plan("Booley agent quokka", build_plan(root)) == ("Booley agent quokka", {})


def test_feedback_shared_parser_rejects_reserved_substrings(tmp_path):
    import pytest

    from booley.core.boundary import BoundaryError
    from booley.feedback.redact import build_plan

    directory = tmp_path / ".booley"
    directory.mkdir()
    (directory / "booley.toml").write_text('[stealth]\nbanned_substrings = ["mod"]\n')
    # The refused entry is named and the reason is feedback redaction.
    with pytest.raises(BoundaryError, match=r"'mod'.*feedback-redaction placeholder"):
        build_plan(tmp_path)


@pytest.mark.parametrize(
    ("plan", "text", "expected"),
    [
        (
            RedactionPlan(
                identifiers={"quokka": "<module-1>"},
                stealth_terms={"okkafi": "<redacted>", "quokka": "<module-1>"},
                stealth_substrings=("okkafi", "quokka"),
            ),
            "myquokkafile quokka",
            ("my<module-1>le <module-1>", {"<module-1>": 2}),
        ),
        (
            RedactionPlan(identifiers={"quokka": "<module-1>"}),
            "myquokkafile quokka_config QuokkaRunner quokka",
            ("myquokkafile <module-1>_config QuokkaRunner <module-1>", {"<module-1>": 2}),
        ),
        (
            RedactionPlan(identifiers={"alpha-beta": "<module-1>", "beta-gamma": "<module-2>"}),
            "alpha-beta-gamma",
            ("<module-1>-gamma", {"<module-1>": 1}),
        ),
        (
            RedactionPlan(
                identifiers={"alpha-beta": "<module-1>", "beta-gamma": "<module-2>"},
                stealth_terms={"quokka": "<redacted>"},
                stealth_substrings=("quokka",),
            ),
            "alpha-beta-gamma myquokkafile",
            ("<module-1>-gamma my<redacted>file", {"<module-1>": 1, "<redacted>": 1}),
        ),
    ],
    ids=["stealth-and-identifier", "identifier-only", "first-longest", "unprotected-suffix"],
)
def test_overlapping_hand_built_plans(plan, text, expected):
    """Fixes f016991f0 and 7c6df3e98: overlapping terms keep the first longest match."""
    assert apply_plan(text, plan) == expected


def test_design_names_match_at_identifier_component_boundaries(tmp_path):
    """`_` joins identifiers and FuseSoC build directories, so it separates names."""
    root = tmp_path / "ip"
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        '[feedback]\nredact_extra = ["zorgblat"]\n', encoding="utf-8", newline="\n"
    )
    (root / "picorv32.core").write_text(
        "name: booley::picorv32:0\ntoplevel: picorv32_axi\n", encoding="utf-8", newline="\n"
    )
    text = (
        "picorv32_wb picorv32_regs uut.picorv32_core.cpuregs rtl/picorv32_wb/top.v "
        "src/booley__picorv32_0/x.v picorv32_axi zorgblat_cfg picorv32x xpicorv32"
    )
    assert redact(text, root)[0] == (
        "<module-1>_wb <module-1>_regs uut.<module-1>_core.cpuregs rtl/<module-1>_wb/top.v "
        "src/booley__<module-1>_0/x.v <module-2> <redacted>_cfg picorv32x xpicorv32"
    )
